"""Allocation policies and the offline evaluation harness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import AllocationStrategy, SlotType, VehicleType
from app.services.allocation.simulator import build_lot, compare, simulate
from app.services.allocation.strategies import (
    AllocationRequest,
    SlotView,
    choose_slot,
    effective_distance,
    eligible_slots,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_slot(slot_id: int, *, distance: float, vacated_minutes_ago: float | None = None,
              slot_type: str = SlotType.STANDARD, zone: str = "A", charger: bool = False) -> SlotView:
    return SlotView(
        id=slot_id, code=f"A{slot_id:03d}", zone=zone, level_id=1,
        x=distance, y=0.0, slot_type=slot_type, distance_from_entry=distance,
        has_ev_charger=charger,
        last_vacated_at=(
            NOW - timedelta(minutes=vacated_minutes_ago)
            if vacated_minutes_ago is not None else None
        ),
    )


def request(**kwargs) -> AllocationRequest:
    return AllocationRequest(now=NOW, **kwargs)


def test_nearest_picks_the_closest_bay():
    slots = [make_slot(1, distance=80), make_slot(2, distance=12), make_slot(3, distance=45)]
    decision = choose_slot(slots, request(), AllocationStrategy.NEAREST)
    assert decision.slot.id == 2


def test_recency_picks_the_most_recently_vacated_bay():
    slots = [
        make_slot(1, distance=10, vacated_minutes_ago=120),
        make_slot(2, distance=95, vacated_minutes_ago=2),     # far but freshest
        make_slot(3, distance=30, vacated_minutes_ago=45),
    ]
    decision = choose_slot(slots, request(), AllocationStrategy.RECENCY)
    assert decision.slot.id == 2
    assert "recently vacated" in decision.reason


def test_recency_falls_back_to_nearest_on_a_cold_lot():
    slots = [make_slot(1, distance=80), make_slot(2, distance=12)]
    decision = choose_slot(slots, request(), AllocationStrategy.RECENCY)
    assert decision.slot.id == 2
    assert "cold start" in decision.reason


def test_hybrid_refuses_a_far_bay_that_is_only_marginally_fresher():
    """The whole point of hybrid: recency must not override a much shorter walk."""
    slots = [
        make_slot(1, distance=10, vacated_minutes_ago=8),
        make_slot(2, distance=140, vacated_minutes_ago=1),
    ]
    decision = choose_slot(slots, request(), AllocationStrategy.HYBRID)
    assert decision.slot.id == 1
    # Pure recency would have taken the distant one — that is the contrast.
    assert choose_slot(slots, request(), AllocationStrategy.RECENCY).slot.id == 2


def test_ev_vehicle_is_routed_to_a_charging_bay():
    slots = [
        make_slot(1, distance=5),
        make_slot(2, distance=90, slot_type=SlotType.EV, charger=True),
    ]
    decision = choose_slot(slots, request(vehicle_type=VehicleType.EV, is_ev=True), AllocationStrategy.NEAREST)
    assert decision.slot.id == 2


def test_charger_request_fails_rather_than_substituting():
    slots = [make_slot(1, distance=5), make_slot(2, distance=9)]
    decision = choose_slot(
        slots, request(vehicle_type=VehicleType.EV, is_ev=True, needs_charger=True),
        AllocationStrategy.NEAREST,
    )
    assert decision.slot is None


def test_accessible_bays_are_reserved_for_permit_holders():
    slots = [
        make_slot(1, distance=5, slot_type=SlotType.ACCESSIBLE),
        make_slot(2, distance=60),
    ]
    general = eligible_slots(slots, request())
    assert [s.id for s in general] == [2]

    permitted = eligible_slots(slots, request(accessible=True))
    assert [s.id for s in permitted] == [1]


def test_effective_distance_accounts_for_the_entry_gate_used():
    slot = make_slot(1, distance=50)
    slot.x, slot.y = 10.0, 0.0
    primary, other = (0.0, 0.0), (30.0, 0.0)

    assert effective_distance(slot, request(entry_point=primary, primary_point=primary)) == 50
    # Entering from a gate 20 m nearer the bay should reduce the effective distance.
    via_other = effective_distance(slot, request(entry_point=other, primary_point=primary))
    assert via_other == pytest.approx(50 + 20 - 10)


def test_all_strategies_return_a_bay_when_one_is_free():
    slots = [make_slot(i, distance=10 * i, vacated_minutes_ago=i) for i in range(1, 6)]
    for strategy in AllocationStrategy:
        decision = choose_slot(slots, request(), strategy)
        assert decision.slot is not None, strategy


def test_empty_pool_yields_no_decision():
    assert choose_slot([], request(), AllocationStrategy.HYBRID).slot is None


def test_simulator_conserves_vehicles():
    result = simulate(AllocationStrategy.HYBRID, vehicles=200, seed=1, hours=8)
    assert result.parked + result.rejected == result.arrivals
    assert result.walk_m > 0


def test_nearest_is_distance_optimal_under_perfect_information():
    """A theorem, not a hope — the harness must reproduce it or it is broken."""
    lot = build_lot(zones=4, slots_per_zone=15)
    results = {
        s: simulate(s, slots=lot, vehicles=400, seed=5, hours=10).walk_m
        for s in (
            AllocationStrategy.NEAREST, AllocationStrategy.RANDOM, AllocationStrategy.BALANCED
        )
    }
    assert results[AllocationStrategy.NEAREST] < results[AllocationStrategy.RANDOM]
    assert results[AllocationStrategy.NEAREST] < results[AllocationStrategy.BALANCED]


def test_stale_occupancy_map_causes_detours():
    clean = simulate(AllocationStrategy.HYBRID, vehicles=400, seed=3, ghost_rate=0.0)
    degraded = simulate(AllocationStrategy.HYBRID, vehicles=400, seed=3, ghost_rate=0.15)
    assert clean.misallocation_rate == 0.0
    assert degraded.misallocation_rate > 0.0
    assert degraded.effective_walk_m > degraded.walk_m


def test_compare_ranks_every_strategy():
    report = compare(trials=2, vehicles=250, zones=3, slots_per_zone=10, hours=8)
    assert len(report["results"]) == 6
    scores = [r["composite_score"] for r in report["results"]]
    assert scores == sorted(scores, reverse=True)
    assert report["winner"] == report["results"][0]["strategy"]
