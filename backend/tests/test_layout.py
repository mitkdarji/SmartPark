"""Layout construction, wayfinding, and floor-plan slot detection."""

from __future__ import annotations

import cv2
import numpy as np

from app.services.allocation.navigation import AisleGraph, route_to_slot
from app.services.layout import layout_service
from tests.conftest import auth


def test_route_without_aisles_uses_an_l_shaped_path():
    route = route_to_slot((0, 0), (30, 40), aisles=[], slot_code="A001", zone="A")
    assert route.distance_m == 70          # 30 across + 40 down
    assert len(route.waypoints) >= 2
    assert route.instructions


def test_route_follows_the_aisle_graph_when_one_exists():
    aisles = [[[0, 10], [50, 10]], [[50, 10], [50, 40]]]
    route = route_to_slot((0, 0), (50, 40), aisles=aisles, slot_code="B002", zone="B")
    assert route.distance_m > 0
    assert route.instructions[-1].endswith("on your right.")
    assert "B002" in route.instructions[-1]


def test_aisle_graph_reports_when_it_is_empty():
    assert AisleGraph([]).is_empty
    assert not AisleGraph([[[0, 0], [10, 0]]]).is_empty


def test_instructions_include_turns_for_a_dog_leg_route():
    aisles = [[[0, 5], [40, 5]], [[40, 5], [40, 30]]]
    route = route_to_slot((0, 0), (40, 30), aisles=aisles, slot_code="C003", zone="C")
    joined = " ".join(route.instructions).lower()
    assert "turn" in joined or "continue" in joined


def test_floorplan_detection_finds_a_grid_of_bays():
    """A clean synthetic plan — exactly the case this detector claims to handle."""
    image = np.full((700, 1000, 3), 255, dtype=np.uint8)
    for row in range(4):
        for column in range(10):
            x, y = 40 + column * 90, 60 + row * 150
            cv2.rectangle(image, (x, y), (x + 60, y + 110), (0, 0, 0), 2)
    payload = cv2.imencode(".png", image)[1].tobytes()

    result = layout_service.detect_slots_from_image(payload)
    assert len(result["detected_slots"]) >= 20
    first = result["detected_slots"][0]
    assert {"code", "zone", "x", "y", "width", "height"} <= set(first)


def test_floorplan_detection_declines_a_photograph():
    """It must return nothing rather than invent bays from noise."""
    noise = np.random.default_rng(0).integers(0, 255, (400, 600, 3), dtype=np.uint8)
    payload = cv2.imencode(".png", noise)[1].tobytes()
    result = layout_service.detect_slots_from_image(payload)
    assert result["detected_slots"] == []
    assert result["note"]


def test_floorplan_detection_handles_undecodable_input():
    result = layout_service.detect_slots_from_image(b"not an image at all")
    assert result["detected_slots"] == []


async def test_generated_grid_distances_increase_with_depth(client, facility):
    slots = sorted(facility["slots"], key=lambda s: s["code"])
    front = min(s["distance_from_entry"] for s in slots)
    back = max(s["distance_from_entry"] for s in slots)
    assert back > front
    # Metres, not pixels: a 4x6 deck must not be hundreds of metres deep.
    assert back < 200


async def test_adding_a_gate_recomputes_distances(client, owner, facility):
    fid = facility["id"]
    before = {s["code"]: s["distance_from_entry"] for s in facility["slots"]}

    await client.post(
        f"/api/v1/facilities/{fid}/gates",
        headers=auth(owner["access_token"]),
        json={"name": "Far Gate", "kind": "entry", "x": 60.0, "y": 40.0, "is_primary": True},
    )
    after = {
        s["code"]: s["distance_from_entry"]
        for s in (await client.get(f"/api/v1/facilities/{fid}/slots")).json()
    }
    assert after != before, "distances must be recomputed when the primary gate moves"
