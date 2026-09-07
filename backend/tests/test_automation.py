"""Automation rule engine: condition language and action registry."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.events import Event, Topic
from app.services.automation.engine import ACTION_REGISTRY, TRIGGERS, evaluate_conditions

NOW = datetime(2026, 6, 3, 14, 30, tzinfo=UTC)   # Wednesday, 14:30


def event(**payload) -> Event:
    return Event(topic=Topic.OCCUPANCY_CHANGED, payload=payload, facility_id=1)


def test_empty_conditions_always_match():
    assert evaluate_conditions({}, event(), NOW)


def test_gte_and_lte_comparators():
    e = event(occupancy_pct=75)
    assert evaluate_conditions({"occupancy_pct_gte": 70}, e, NOW)
    assert not evaluate_conditions({"occupancy_pct_gte": 80}, e, NOW)
    assert evaluate_conditions({"occupancy_pct_lte": 80}, e, NOW)
    assert not evaluate_conditions({"occupancy_pct_lte": 70}, e, NOW)


def test_equality_comparator():
    e = event(status="failed")
    assert evaluate_conditions({"status_eq": "failed"}, e, NOW)
    assert not evaluate_conditions({"status_eq": "paid"}, e, NOW)


def test_clock_conditions():
    assert evaluate_conditions({"hour": 14}, None, NOW)
    assert not evaluate_conditions({"hour": 3}, None, NOW)
    assert evaluate_conditions({"weekday": 2}, None, NOW)
    assert not evaluate_conditions({"weekday": 6}, None, NOW)


def test_all_conditions_must_hold():
    e = event(occupancy_pct=75)
    assert evaluate_conditions({"occupancy_pct_gte": 70, "hour": 14}, e, NOW)
    assert not evaluate_conditions({"occupancy_pct_gte": 70, "hour": 9}, e, NOW)


def test_action_parameters_are_not_treated_as_gates():
    """`overstay_hours` configures the action; it must not block the rule."""
    assert evaluate_conditions({"overstay_hours": 12}, event(), NOW)


def test_every_registered_action_is_callable():
    assert ACTION_REGISTRY
    for name, handler in ACTION_REGISTRY.items():
        assert callable(handler), name


def test_seeded_triggers_are_all_known_topics():
    for trigger in TRIGGERS:
        assert trigger == "schedule:tick" or "." in trigger
