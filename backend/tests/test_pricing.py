"""Dynamic pricing and bill computation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.enums import SlotType
from app.models.facility import Facility
from app.services.billing.pricing import MAX_MULTIPLIER, MIN_MULTIPLIER, pricing_engine


def make_facility(**overrides) -> Facility:
    defaults = {
        "id": 1, "owner_id": 1, "name": "Test", "slug": "test", "currency": "INR",
        "base_rate_minor_per_hour": 4000, "free_minutes": 15,
        "billing_increment_minutes": 15, "daily_cap_minor": 40_000,
        "tax_percent": 18.0, "dynamic_pricing_enabled": True,
        "pricing_config": {}, "overstay_after_hours": 12,
    }
    defaults.update(overrides)
    return Facility(**defaults)


NOON = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)   # a Wednesday, off-peak


def test_no_surge_below_the_free_threshold():
    facility = make_facility()
    rate, multiplier, _ = pricing_engine.effective_rate(
        facility, occupancy=0.4, moment=NOON
    )
    assert multiplier == pytest.approx(1.0)
    assert rate == facility.base_rate_minor_per_hour


def test_surge_rises_monotonically_with_occupancy():
    facility = make_facility()
    rates = [
        pricing_engine.effective_rate(facility, occupancy=o, moment=NOON)[0]
        for o in (0.6, 0.7, 0.8, 0.9, 1.0)
    ]
    assert rates == sorted(rates)
    assert rates[-1] > rates[0]


def test_multiplier_is_hard_bounded():
    facility = make_facility(pricing_config={"occupancy_max_multiplier": 99.0})
    _, multiplier, _ = pricing_engine.effective_rate(facility, occupancy=1.0, moment=NOON)
    assert MIN_MULTIPLIER <= multiplier <= MAX_MULTIPLIER


def test_dynamic_pricing_can_be_switched_off():
    facility = make_facility(dynamic_pricing_enabled=False)
    rate, multiplier, components = pricing_engine.effective_rate(
        facility, occupancy=1.0, moment=NOON
    )
    assert rate == facility.base_rate_minor_per_hour
    assert multiplier == 1.0
    assert "disabled" in components["dynamic_pricing"]


def test_free_period_produces_a_zero_bill():
    facility = make_facility()
    quote = pricing_engine.quote(facility, raw_minutes=12, occupancy=0.5, exit_at=NOON)
    assert quote.total_minor == 0
    assert quote.billable_minutes == 0
    assert "free period" in quote.explanation


def test_billing_rounds_up_to_the_increment():
    facility = make_facility(free_minutes=0, billing_increment_minutes=15)
    assert pricing_engine.billable_minutes(facility, 1) == 15
    assert pricing_engine.billable_minutes(facility, 15) == 15
    assert pricing_engine.billable_minutes(facility, 16) == 30


def test_tax_is_applied_to_the_net_amount():
    facility = make_facility(free_minutes=0, tax_percent=18.0)
    quote = pricing_engine.quote(facility, raw_minutes=60, occupancy=0.3, exit_at=NOON)
    net = quote.subtotal_minor - quote.discount_minor
    assert quote.tax_minor == pytest.approx(round(net * 0.18), abs=1)
    assert quote.total_minor == net + quote.tax_minor


def test_daily_cap_limits_a_very_long_stay():
    facility = make_facility(free_minutes=0, daily_cap_minor=20_000)
    quote = pricing_engine.quote(facility, raw_minutes=24 * 60, occupancy=0.9, exit_at=NOON)
    assert quote.subtotal_minor - quote.discount_minor == 20_000
    assert quote.components.get("daily_cap_applied") is True


def test_long_stays_get_a_declining_block_discount():
    facility = make_facility(free_minutes=0, daily_cap_minor=10_000_000)
    short = pricing_engine.quote(facility, raw_minutes=120, occupancy=0.3, exit_at=NOON)
    long = pricing_engine.quote(facility, raw_minutes=600, occupancy=0.3, exit_at=NOON)
    assert short.discount_minor == 0
    assert long.discount_minor > 0
    # The marginal rate must fall, not the total.
    assert long.total_minor > short.total_minor


def test_waived_charges_produce_a_zero_bill_with_a_reason():
    facility = make_facility(free_minutes=0)
    quote = pricing_engine.quote(
        facility, raw_minutes=300, occupancy=0.9, exit_at=NOON, waive=True
    )
    assert quote.total_minor == 0
    assert "waived" in quote.explanation


def test_ev_bays_carry_their_own_multiplier():
    facility = make_facility()
    standard, _, _ = pricing_engine.effective_rate(
        facility, occupancy=0.3, moment=NOON, slot_type=SlotType.STANDARD
    )
    ev, _, _ = pricing_engine.effective_rate(
        facility, occupancy=0.3, moment=NOON, slot_type=SlotType.EV
    )
    assert ev > standard


def test_explanation_names_every_factor_that_moved_the_price():
    facility = make_facility(free_minutes=0)
    quote = pricing_engine.quote(
        facility, raw_minutes=120, occupancy=0.95, exit_at=NOON, slot_type=SlotType.EV
    )
    assert "lot 95% full" in quote.explanation
    assert "ev bay" in quote.explanation


def test_forecast_only_ever_pushes_the_price_up():
    facility = make_facility()
    base, _, _ = pricing_engine.effective_rate(facility, occupancy=0.5, moment=NOON)
    with_surge, _, _ = pricing_engine.effective_rate(
        facility, occupancy=0.5, moment=NOON, forecast_occupancy=0.95
    )
    with_slump, _, _ = pricing_engine.effective_rate(
        facility, occupancy=0.5, moment=NOON, forecast_occupancy=0.1
    )
    assert with_surge > base
    assert with_slump == base
