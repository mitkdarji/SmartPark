"""Discrete-event simulator for comparing allocation policies.

Runs the *same* strategy functions the production engine uses against a
synthetic day of traffic, so the reported numbers describe shipped code.

Two regimes are simulated, because they give genuinely different answers:

  Perfect information (`ghost_rate = 0`)
      The occupancy map is exactly right. `nearest` is then distance-optimal by
      construction and nothing can beat it on walking distance — that is a
      theorem, not an experimental result, and the harness confirms it.

  Degraded information (`ghost_rate > 0`)
      The realistic case. Vehicles park without being logged: a visitor tail-
      gates through the barrier, a bay sensor fails, someone parks across two
      bays. The system believes those bays are free. A driver sent to one finds
      it taken, must re-search, and is re-allocated — paying a detour.

      This is where the recency policy earns its place, and for exactly the
      reason LRU works in a cache: a bay confirmed vacated ninety seconds ago is
      far less likely to have gone stale than one that has sat "free" for three
      hours. `nearest` is indifferent to that; `recency` optimises for it.

Metrics
    walk_m              mean entry→slot distance, ignoring detours
    effective_walk_m    mean distance actually driven, including re-search
                        detours after a misallocation — the number a driver feels
    misallocation_rate  share of arrivals sent to an already-occupied bay
    p90_walk_m          tail experience, not just the average
    conflict_rate       share of arrivals parking within 18 m of a bay another
                        car entered in the last 90 s — proxy for aisle queueing
    reuse_gap_min       mean minutes between a bay being vacated and refilled;
                        the direct measure of the working-set (LRU) effect
    slot_gini           inequality of per-slot usage (0 = perfectly even wear)
    rejection_rate      arrivals turned away because the lot was full
    latency_us          policy decision time
"""

from __future__ import annotations

import heapq
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.models.enums import AllocationStrategy, SlotType, VehicleType
from app.services.allocation.strategies import AllocationRequest, SlotView, choose_slot

VEHICLE_MIX = [
    (VehicleType.HATCHBACK, 0.38),
    (VehicleType.SEDAN, 0.26),
    (VehicleType.SUV, 0.18),
    (VehicleType.TWO_WHEELER, 0.12),
    (VehicleType.EV, 0.06),
]

# Relative arrival intensity by hour of day for a shopping-mall car park.
DEMAND_CURVE = [
    0.05, 0.03, 0.02, 0.02, 0.03, 0.06, 0.12, 0.22, 0.38, 0.55, 0.72, 0.88,
    1.00, 0.94, 0.82, 0.78, 0.85, 0.95, 1.00, 0.92, 0.74, 0.48, 0.26, 0.12,
]

CONFLICT_WINDOW_S = 90.0
CONFLICT_RADIUS_M = 18.0   # roughly three bay widths — where cars block each other
MAX_RESEARCH_ATTEMPTS = 4  # how many times a driver retries before giving up

# Weights for the composite score. These are a stated value judgement, not a
# derived truth: the distance a driver actually covers dominates, aisle
# contention matters next, even wear is a minor operational nicety.
COMPOSITE_WEIGHTS = {"effective_walk_m": 0.60, "conflict_rate": 0.25, "slot_gini": 0.15}


@dataclass(slots=True)
class SimSlot:
    view: SlotView
    uses: int = 0
    # True when a vehicle occupies the bay without the system knowing.
    ghost_until: float | None = None


@dataclass(slots=True)
class SimResult:
    strategy: str
    ghost_rate: float
    arrivals: int
    parked: int
    rejected: int
    walk_m: float
    effective_walk_m: float
    p90_walk_m: float
    misallocation_rate: float
    conflict_rate: float
    reuse_gap_min: float
    slot_gini: float
    rejection_rate: float
    latency_us: float
    peak_occupancy_pct: float
    distances: list[float] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "ghost_rate": self.ghost_rate,
            "arrivals": self.arrivals,
            "parked": self.parked,
            "rejected": self.rejected,
            "walk_m": round(self.walk_m, 2),
            "effective_walk_m": round(self.effective_walk_m, 2),
            "p90_walk_m": round(self.p90_walk_m, 2),
            "misallocation_rate": round(self.misallocation_rate, 4),
            "conflict_rate": round(self.conflict_rate, 4),
            "reuse_gap_min": round(self.reuse_gap_min, 2),
            "slot_gini": round(self.slot_gini, 4),
            "rejection_rate": round(self.rejection_rate, 4),
            "latency_us": round(self.latency_us, 1),
            "peak_occupancy_pct": round(self.peak_occupancy_pct, 2),
        }


def build_lot(
    *,
    zones: int = 5,
    slots_per_zone: int = 24,
    zone_depth_m: float = 32.0,
    ev_share: float = 0.06,
    accessible_share: float = 0.04,
    seed: int = 7,
) -> list[SlotView]:
    """A rectangular multi-zone lot; zone A is nearest the entry gate."""
    rng = random.Random(seed)
    slots: list[SlotView] = []
    for z in range(zones):
        zone_name = chr(ord("A") + z)
        base_distance = 12.0 + z * zone_depth_m
        for i in range(slots_per_zone):
            roll = rng.random()
            if roll < ev_share:
                slot_type, charger = SlotType.EV, True
            elif roll < ev_share + accessible_share:
                slot_type, charger = SlotType.ACCESSIBLE, False
            elif roll < ev_share + accessible_share + 0.18:
                slot_type, charger = SlotType.COMPACT, False
            elif roll < ev_share + accessible_share + 0.30:
                slot_type, charger = SlotType.LARGE, False
            else:
                slot_type, charger = SlotType.STANDARD, False

            lateral = (i % 12) * 2.6
            slots.append(
                SlotView(
                    id=len(slots) + 1,
                    code=f"{zone_name}{i + 1:03d}",
                    zone=zone_name,
                    level_id=1 + z // 3,
                    x=lateral,
                    y=base_distance,
                    slot_type=slot_type,
                    distance_from_entry=base_distance + lateral,
                    has_ev_charger=charger,
                    last_vacated_at=None,
                )
            )
    return slots


def _gini(values: list[int]) -> float:
    if not values or all(v == 0 for v in values):
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    cumulative = sum((i + 1) * v for i, v in enumerate(ordered))
    total = sum(ordered)
    return (2 * cumulative) / (n * total) - (n + 1) / n


def _sample_vehicle(rng: random.Random) -> tuple[str, bool]:
    roll, cumulative = rng.random(), 0.0
    for vtype, share in VEHICLE_MIX:
        cumulative += share
        if roll <= cumulative:
            return vtype, vtype == VehicleType.EV
    return VehicleType.HATCHBACK, False


def simulate(
    strategy: str,
    *,
    slots: list[SlotView] | None = None,
    vehicles: int = 800,
    hours: float = 14.0,
    seed: int = 42,
    mean_dwell_min: float = 95.0,
    ghost_rate: float = 0.0,
) -> SimResult:
    """One simulated day under one policy.

    `ghost_rate` is the share of arrivals that park *unlogged*, creating bays
    the occupancy map wrongly believes are free.
    """
    rng = random.Random(seed)
    # strategy_random draws from the module RNG; seed it so trials reproduce.
    random.seed(seed)

    base_slots = slots or build_lot()
    lot = [
        SimSlot(
            view=SlotView(
                id=s.id, code=s.code, zone=s.zone, level_id=s.level_id, x=s.x, y=s.y,
                slot_type=s.slot_type, distance_from_entry=s.distance_from_entry,
                has_ev_charger=s.has_ev_charger, last_vacated_at=None,
            )
        )
        for s in base_slots
    ]
    by_id = {s.view.id: s for s in lot}
    capacity = len(lot)
    origin = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)

    # Arrival instants, thinned from the demand curve.
    weights = [DEMAND_CURVE[int((6 + h) % 24)] for h in range(int(hours) + 1)]
    total_weight = sum(weights) or 1.0
    arrival_times: list[float] = []
    for hour_index, weight in enumerate(weights):
        for _ in range(int(round(vehicles * weight / total_weight))):
            arrival_times.append((hour_index + rng.random()) * 3600.0)
    arrival_times.sort()

    departures: list[tuple[float, int]] = []       # (time, slot_id), known to the system
    ghost_departures: list[tuple[float, int]] = [] # (time, slot_id), invisible to it
    occupied: set[int] = set()                     # what the system believes
    recent_parks: list[tuple[float, float, float]] = []

    distances: list[float] = []
    effective_distances: list[float] = []
    latencies: list[float] = []
    reuse_gaps: list[float] = []
    conflicts = misallocations = rejected = 0
    peak_occupied = 0
    entry_point = (0.0, 0.0)

    def expire(now: float) -> None:
        while departures and departures[0][0] <= now:
            departed_at, slot_id = heapq.heappop(departures)
            occupied.discard(slot_id)
            by_id[slot_id].view.last_vacated_at = origin + timedelta(seconds=departed_at)
        while ghost_departures and ghost_departures[0][0] <= now:
            _, slot_id = heapq.heappop(ghost_departures)
            by_id[slot_id].ghost_until = None

    for now in arrival_times:
        expire(now)

        # An unlogged vehicle may take a bay the system still believes is free.
        if ghost_rate > 0 and rng.random() < ghost_rate:
            phantom_pool = [
                s for s in lot if s.view.id not in occupied and s.ghost_until is None
            ]
            if phantom_pool:
                phantom = rng.choice(phantom_pool)
                dwell = min(rng.lognormvariate(math.log(mean_dwell_min), 0.75) * 60.0, 14 * 3600)
                phantom.ghost_until = now + dwell
                heapq.heappush(ghost_departures, (now + dwell, phantom.view.id))

        vehicle_type, is_ev = _sample_vehicle(rng)

        zone_totals: dict[str, int] = {}
        zone_used: dict[str, int] = {}
        for s in lot:
            zone_totals[s.view.zone] = zone_totals.get(s.view.zone, 0) + 1
            if s.view.id in occupied:
                zone_used[s.view.zone] = zone_used.get(s.view.zone, 0) + 1
        zone_load = {z: zone_used.get(z, 0) / n for z, n in zone_totals.items()}

        request = AllocationRequest(
            vehicle_type=vehicle_type,
            is_ev=is_ev,
            needs_charger=is_ev and rng.random() < 0.6,
            accessible=rng.random() < 0.03,
            entry_point=entry_point,
            now=origin + timedelta(seconds=now),
            zone_load=zone_load,
        )

        # Drive to the assigned bay; if it turns out to be taken, re-search.
        blocked: set[int] = set()
        detour = 0.0
        last_position = entry_point
        chosen: SimSlot | None = None

        for _ in range(MAX_RESEARCH_ATTEMPTS):
            free = [
                s.view for s in lot if s.view.id not in occupied and s.view.id not in blocked
            ]
            if not free:
                break

            t0 = time.perf_counter()
            decision = choose_slot(free, request, strategy)
            latencies.append((time.perf_counter() - t0) * 1e6)
            if decision.slot is None:
                break

            candidate = by_id[decision.slot.id]
            leg = math.hypot(
                candidate.view.x - last_position[0], candidate.view.y - last_position[1]
            )

            if candidate.ghost_until is not None and candidate.ghost_until > now:
                # Bay is occupied by an unlogged vehicle. The driver reports it,
                # the system corrects its map, and re-allocates.
                misallocations += 1
                detour += leg
                last_position = (candidate.view.x, candidate.view.y)
                blocked.add(candidate.view.id)
                occupied.add(candidate.view.id)
                heapq.heappush(departures, (candidate.ghost_until, candidate.view.id))
                continue

            chosen = candidate
            break

        if chosen is None:
            rejected += 1
            continue

        if chosen.view.last_vacated_at is not None:
            reuse_gaps.append((request.now - chosen.view.last_vacated_at).total_seconds() / 60.0)

        recent_parks = [p for p in recent_parks if now - p[0] < CONFLICT_WINDOW_S]
        cx, cy = chosen.view.x, chosen.view.y
        if any(math.hypot(cx - px, cy - py) <= CONFLICT_RADIUS_M for _, px, py in recent_parks):
            conflicts += 1
        recent_parks.append((now, cx, cy))

        dwell = min(rng.lognormvariate(math.log(mean_dwell_min), 0.75) * 60.0, 14 * 3600)
        occupied.add(chosen.view.id)
        chosen.uses += 1
        heapq.heappush(departures, (now + dwell, chosen.view.id))

        distances.append(chosen.view.distance_from_entry)
        effective_distances.append(chosen.view.distance_from_entry + detour)
        peak_occupied = max(peak_occupied, len(occupied))

    parked = len(distances)
    ordered = sorted(distances)
    return SimResult(
        strategy=strategy,
        ghost_rate=ghost_rate,
        arrivals=len(arrival_times),
        parked=parked,
        rejected=rejected,
        walk_m=statistics.fmean(distances) if distances else 0.0,
        effective_walk_m=statistics.fmean(effective_distances) if effective_distances else 0.0,
        p90_walk_m=ordered[int(0.9 * (len(ordered) - 1))] if ordered else 0.0,
        misallocation_rate=misallocations / max(1, len(arrival_times)),
        conflict_rate=conflicts / parked if parked else 0.0,
        reuse_gap_min=statistics.fmean(reuse_gaps) if reuse_gaps else 0.0,
        slot_gini=_gini([s.uses for s in lot]),
        rejection_rate=rejected / max(1, len(arrival_times)),
        latency_us=statistics.fmean(latencies) if latencies else 0.0,
        peak_occupancy_pct=100.0 * peak_occupied / capacity if capacity else 0.0,
        distances=distances,
    )


METRIC_KEYS = [
    "walk_m", "effective_walk_m", "p90_walk_m", "misallocation_rate",
    "conflict_rate", "reuse_gap_min", "slot_gini", "rejection_rate",
    "latency_us", "peak_occupancy_pct",
]


def compare(
    strategies: list[str] | None = None,
    *,
    trials: int = 5,
    vehicles: int = 800,
    zones: int = 5,
    slots_per_zone: int = 24,
    hours: float = 14.0,
    ghost_rate: float = 0.0,
) -> dict:
    """Average each policy over several seeds, then rank them."""
    strategies = strategies or [
        AllocationStrategy.NEAREST, AllocationStrategy.RECENCY,
        AllocationStrategy.FIRST_FIT, AllocationStrategy.RANDOM,
        AllocationStrategy.BALANCED, AllocationStrategy.HYBRID,
    ]
    lot = build_lot(zones=zones, slots_per_zone=slots_per_zone)

    aggregated: list[dict] = []
    for strategy in strategies:
        runs = [
            simulate(
                strategy, slots=lot, vehicles=vehicles, hours=hours,
                seed=1000 + t, ghost_rate=ghost_rate,
            )
            for t in range(trials)
        ]
        row: dict = {"strategy": strategy, "trials": trials, "ghost_rate": ghost_rate}
        for key in METRIC_KEYS:
            values = [getattr(r, key) for r in runs]
            row[key] = round(statistics.fmean(values), 4)
            row[f"{key}_sd"] = round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0
        row["parked"] = int(statistics.fmean([r.parked for r in runs]))
        aggregated.append(row)

    baseline = next((r for r in aggregated if r["strategy"] == AllocationStrategy.NEAREST), None)
    if baseline:
        for row in aggregated:
            for key in ("walk_m", "effective_walk_m", "conflict_rate", "misallocation_rate"):
                base = baseline[key]
                row[f"{key}_vs_nearest_pct"] = (
                    round(100.0 * (row[key] - base) / base, 2) if base else 0.0
                )

    # Min-max normalise each metric across the observed range before weighting,
    # so a metric with a narrow spread is not silently drowned out by one with a
    # wide spread. Lower is better for every metric in the composite.
    for key in COMPOSITE_WEIGHTS:
        values = [r[key] for r in aggregated]
        lo, hi = min(values), max(values)
        span = hi - lo
        for row in aggregated:
            row[f"_norm_{key}"] = 0.0 if span < 1e-9 else (row[key] - lo) / span

    for row in aggregated:
        penalty = sum(w * row[f"_norm_{k}"] for k, w in COMPOSITE_WEIGHTS.items())
        row["composite_score"] = round(100 * (1 - penalty), 2)
        for key in COMPOSITE_WEIGHTS:
            row.pop(f"_norm_{key}", None)

    aggregated.sort(key=lambda r: r["composite_score"], reverse=True)
    return {
        "lot": {"zones": zones, "slots_per_zone": slots_per_zone, "capacity": len(lot)},
        "workload": {
            "vehicles": vehicles, "hours": hours, "trials": trials, "ghost_rate": ghost_rate,
        },
        "weights": COMPOSITE_WEIGHTS,
        "results": aggregated,
        "winner": aggregated[0]["strategy"] if aggregated else None,
    }


def sweep_ghost_rates(
    rates: list[float] | None = None, *, trials: int = 3, vehicles: int = 800
) -> dict:
    """How each policy degrades as the occupancy map becomes less trustworthy."""
    rates = rates or [0.0, 0.04, 0.08, 0.12, 0.18]
    return {
        "rates": rates,
        "runs": {
            str(rate): compare(trials=trials, vehicles=vehicles, ghost_rate=rate)["results"]
            for rate in rates
        },
    }
