"""Slot-allocation policies, written as pure functions.

Every strategy takes the same `SlotView` list and returns a `Decision`. Keeping
them free of ORM objects means the identical code runs against the live
database and inside the offline simulator, so the benchmark measures the policy
that actually ships — not a re-implementation of it.

The policy under study is `recency`: assign the most-recently-vacated slot,
the physical analogue of an LRU cache reusing its hottest line. The baselines
it is measured against are `nearest`, `first_fit` and `random`; `hybrid`
combines recency with proximity and is the production default.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models.enums import AllocationStrategy, SlotType, VehicleType

# Which physical slot types a vehicle may legally occupy, best fit first.
TYPE_COMPATIBILITY: dict[str, tuple[str, ...]] = {
    VehicleType.TWO_WHEELER: (SlotType.TWO_WHEELER, SlotType.COMPACT, SlotType.STANDARD),
    VehicleType.HATCHBACK: (SlotType.COMPACT, SlotType.STANDARD, SlotType.LARGE),
    VehicleType.SEDAN: (SlotType.STANDARD, SlotType.LARGE),
    VehicleType.SUV: (SlotType.LARGE, SlotType.STANDARD),
    VehicleType.TRUCK: (SlotType.LARGE,),
    VehicleType.EV: (SlotType.EV, SlotType.STANDARD, SlotType.LARGE),
}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class SlotView:
    """The minimum a policy needs to know about a slot."""

    id: int
    code: str
    zone: str
    level_id: int
    x: float
    y: float
    slot_type: str
    distance_from_entry: float
    has_ev_charger: bool = False
    last_vacated_at: datetime | None = None
    total_uses: int = 0
    price_multiplier: float = 1.0

    def distance_to(self, point: tuple[float, float]) -> float:
        """Straight-line distance within this slot's own level."""
        return math.hypot(self.x - point[0], self.y - point[1])


@dataclass(slots=True)
class AllocationRequest:
    vehicle_type: str = VehicleType.HATCHBACK
    is_ev: bool = False
    needs_charger: bool = False
    accessible: bool = False
    entry_point: tuple[float, float] = (0.0, 0.0)
    preferred_zone: str | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Live occupancy per zone, used by the balanced and hybrid policies.
    zone_load: dict[str, float] = field(default_factory=dict)
    # Position of the facility's primary gate, against which every slot's
    # `distance_from_entry` was precomputed.
    primary_point: tuple[float, float] | None = None


@dataclass(slots=True)
class Decision:
    slot: SlotView | None
    reason: str
    considered: int
    scores: dict[int, float] = field(default_factory=dict)


def effective_distance(slot: SlotView, request: AllocationRequest) -> float:
    """How far this bay really is from the gate the vehicle just entered.

    `distance_from_entry` is precomputed per facility: the routed distance along
    the aisle graph from the *primary* gate, plus a penalty per deck above
    ground. It is the authoritative measure, because slot coordinates are stored
    per level and therefore repeat across floors — a bare Euclidean distance
    would rank a bay two decks up as though it were beside the entrance.

    When the vehicle entered through a different gate, the straight-line
    component is swapped for that gate's: the routed baseline is kept, and only
    the part that actually depends on which door was used is re-measured.
    """
    base = slot.distance_from_entry
    if request.primary_point is None or request.entry_point == request.primary_point:
        return base
    return max(
        0.0,
        base
        + slot.distance_to(request.entry_point)
        - slot.distance_to(request.primary_point),
    )


# ── Eligibility ───────────────────────────────────────────────

def eligible_slots(slots: Sequence[SlotView], request: AllocationRequest) -> list[SlotView]:
    """Filter to slots this vehicle may physically and legally occupy."""
    if request.accessible:
        accessible = [s for s in slots if s.slot_type == SlotType.ACCESSIBLE]
        if accessible:
            return accessible

    if request.needs_charger or request.is_ev:
        charging = [s for s in slots if s.has_ev_charger or s.slot_type == SlotType.EV]
        if charging:
            return charging
        if request.needs_charger:
            # Explicitly asked to charge and nothing is free: no substitute.
            return []

    allowed = TYPE_COMPATIBILITY.get(request.vehicle_type, (SlotType.STANDARD, SlotType.LARGE))
    # Never hand a general vehicle an accessible bay while others exist.
    pool = [s for s in slots if s.slot_type in allowed and s.slot_type != SlotType.ACCESSIBLE]
    if pool:
        return pool
    fallback = [s for s in slots if s.slot_type != SlotType.ACCESSIBLE]
    return fallback or list(slots)


# ── Policies ──────────────────────────────────────────────────

def strategy_nearest(slots: list[SlotView], request: AllocationRequest) -> Decision:
    """Baseline: the empty slot closest to the entry gate.

    Distance-optimal by construction, which is exactly why it is the yardstick.
    Its weakness is that it concentrates every arrival into the same front
    aisles, so queueing and cross-traffic build up there.
    """
    if not slots:
        return Decision(None, "no eligible slot", 0)
    best = min(slots, key=lambda s: (effective_distance(s, request), s.code))
    return Decision(
        best,
        f"nearest to entry ({effective_distance(best, request):.0f} m)",
        len(slots),
    )


def strategy_recency(slots: list[SlotView], request: AllocationRequest) -> Decision:
    """Proposed: the most-recently-vacated slot — an LRU cache in physical form.

    Reusing the slot that just freed keeps the *active* set of occupied bays
    compact and self-renewing: the lot behaves like a working set instead of
    spreading arrivals over the whole floor. Slots never used are ranked last,
    ordered by distance, so a cold lot still fills sensibly from the front.
    """
    if not slots:
        return Decision(None, "no eligible slot", 0)

    vacated = [s for s in slots if s.last_vacated_at is not None]
    if vacated:
        best = max(
            vacated,
            key=lambda s: (s.last_vacated_at or _EPOCH, -effective_distance(s, request)),
        )
        age_s = (request.now - (best.last_vacated_at or _EPOCH)).total_seconds()
        return Decision(best, f"most recently vacated ({age_s / 60:.1f} min ago)", len(slots))

    best = min(slots, key=lambda s: (effective_distance(s, request), s.code))
    return Decision(best, "cold start — nearest to entry", len(slots))


def strategy_random(slots: list[SlotView], request: AllocationRequest) -> Decision:
    """Baseline: uniform random. The control condition for the evaluation."""
    if not slots:
        return Decision(None, "no eligible slot", 0)
    best = random.choice(slots)
    return Decision(best, "uniform random", len(slots))


def strategy_first_fit(slots: list[SlotView], request: AllocationRequest) -> Decision:
    """Baseline: lowest slot code — how a paper-ticket attendant assigns bays."""
    if not slots:
        return Decision(None, "no eligible slot", 0)
    best = min(slots, key=lambda s: s.code)
    return Decision(best, f"first free by code ({best.code})", len(slots))


def strategy_balanced(slots: list[SlotView], request: AllocationRequest) -> Decision:
    """Baseline: fill the least-loaded zone, then nearest within it.

    Minimises aisle congestion, at the cost of longer walks.
    """
    if not slots:
        return Decision(None, "no eligible slot", 0)
    if not request.zone_load:
        return strategy_nearest(slots, request)
    coldest = min({s.zone for s in slots}, key=lambda z: request.zone_load.get(z, 0.0))
    pool = [s for s in slots if s.zone == coldest] or slots
    best = min(pool, key=lambda s: (effective_distance(s, request), s.code))
    return Decision(
        best, f"least-loaded zone {coldest} ({request.zone_load.get(coldest, 0):.0%} full)", len(slots)
    )


# Weights for `hybrid`. Recency leads; proximity keeps walks short; zone balance
# and type fit break ties. Tuned on the simulator — see docs/EVALUATION.md.
HYBRID_WEIGHTS = {"recency": 0.42, "proximity": 0.36, "balance": 0.14, "fit": 0.08}
RECENCY_HALF_LIFE_S = 900.0  # a slot freed 15 min ago scores half of one freed now


def strategy_hybrid(slots: list[SlotView], request: AllocationRequest) -> Decision:
    """Production default: recency-led, distance-aware, congestion-aware.

    Pure recency can send a driver across the lot to a bay that happened to free
    up a second ago. Scoring recency *and* proximity together keeps the
    working-set behaviour while refusing decisions that are much worse than the
    nearest alternative.
    """
    if not slots:
        return Decision(None, "no eligible slot", 0)

    distances = [effective_distance(s, request) for s in slots]
    d_min, d_max = min(distances), max(distances)
    d_range = max(d_max - d_min, 1e-6)

    scores: dict[int, float] = {}
    best_slot, best_score = None, -1.0
    for slot, distance in zip(slots, distances, strict=True):
        # Recency: exponential decay on how long ago the slot was freed.
        if slot.last_vacated_at is None:
            recency = 0.15
        else:
            age = max(0.0, (request.now - slot.last_vacated_at).total_seconds())
            recency = math.exp(-age / RECENCY_HALF_LIFE_S)

        proximity = 1.0 - (distance - d_min) / d_range
        balance = 1.0 - request.zone_load.get(slot.zone, 0.0)

        fit = 1.0
        allowed = TYPE_COMPATIBILITY.get(request.vehicle_type, ())
        if allowed and slot.slot_type in allowed:
            # Reward the tightest legal fit so large bays stay free for large cars.
            fit = 1.0 - (allowed.index(slot.slot_type) * 0.25)
        if request.preferred_zone and slot.zone == request.preferred_zone:
            fit = min(1.0, fit + 0.2)

        score = (
            HYBRID_WEIGHTS["recency"] * recency
            + HYBRID_WEIGHTS["proximity"] * proximity
            + HYBRID_WEIGHTS["balance"] * balance
            + HYBRID_WEIGHTS["fit"] * fit
        )
        scores[slot.id] = round(score, 4)
        if score > best_score:
            best_slot, best_score = slot, score

    assert best_slot is not None
    return Decision(
        best_slot,
        f"hybrid score {best_score:.3f} "
        f"(recency+proximity, {effective_distance(best_slot, request):.0f} m)",
        len(slots),
        scores,
    )


STRATEGIES: dict[str, Callable[[list[SlotView], AllocationRequest], Decision]] = {
    AllocationStrategy.NEAREST: strategy_nearest,
    AllocationStrategy.RECENCY: strategy_recency,
    AllocationStrategy.RANDOM: strategy_random,
    AllocationStrategy.FIRST_FIT: strategy_first_fit,
    AllocationStrategy.BALANCED: strategy_balanced,
    AllocationStrategy.HYBRID: strategy_hybrid,
}


def choose_slot(
    slots: Sequence[SlotView],
    request: AllocationRequest,
    strategy: str = AllocationStrategy.HYBRID,
) -> Decision:
    """Filter for eligibility, then apply the named policy."""
    pool = eligible_slots(slots, request)
    if not pool:
        return Decision(None, "no slot matches the vehicle's requirements", 0)
    fn = STRATEGIES.get(strategy, strategy_hybrid)
    return fn(pool, request)
