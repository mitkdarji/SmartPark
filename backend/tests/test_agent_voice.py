"""AI agent tools, the confirmation gate, and the voice intent router."""

from __future__ import annotations

import pytest

from app.models.enums import UserRole
from app.services.agents.tools import TOOLS, TOOLS_BY_NAME, tools_for_role
from app.services.voice.pipeline import classify_intent, speakable
from tests.conftest import auth

# ── Tool surface ──────────────────────────────────────────────

def test_every_tool_declares_a_valid_json_schema():
    for tool in TOOLS:
        declaration = tool.declaration()
        assert declaration["name"] and declaration["description"]
        schema = declaration["input_schema"]
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties", {}), dict)
        for required in schema.get("required", []):
            assert required in schema["properties"], f"{tool.name}: {required}"


def test_tools_are_scoped_by_role():
    driver_tools = {t.name for t in tools_for_role(UserRole.DRIVER)}
    owner_tools = {t.name for t in tools_for_role(UserRole.OWNER)}

    assert "find_my_vehicle" in driver_tools
    assert "get_revenue_report" not in driver_tools
    assert "update_facility_pricing" not in driver_tools

    assert "get_revenue_report" in owner_tools
    assert "get_wallet_balance" not in owner_tools


def test_money_moving_tools_are_marked_as_requiring_confirmation():
    """A model must not be able to spend money on its own say-so."""
    assert TOOLS_BY_NAME["top_up_wallet"].mutating
    assert TOOLS_BY_NAME["update_facility_pricing"].mutating
    assert not TOOLS_BY_NAME["find_my_vehicle"].mutating
    assert not TOOLS_BY_NAME["get_revenue_report"].mutating


def test_admin_sees_the_whole_surface():
    assert len(tools_for_role(UserRole.ADMIN)) == len(TOOLS)


# ── Voice ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("where is my car", "find_my_vehicle"),
        ("Where's my vehicle?", "find_my_vehicle"),
        ("where did i park", "find_my_vehicle"),
        ("how much do i owe", "get_current_charges"),
        ("what's my bill", "get_current_charges"),
        ("wallet balance please", "get_wallet_balance"),
        ("give me directions", "get_directions_to_my_slot"),
        ("navigate to my slot", "get_directions_to_my_slot"),
        ("find parking near me", "find_parking_nearby"),
    ],
)
def test_intent_router_matches_common_utterances(utterance, expected):
    intent = classify_intent(utterance)
    assert intent is not None, utterance
    assert intent.name == expected
    assert intent.confidence > 0.8


@pytest.mark.parametrize(
    "utterance",
    ["what is the meaning of life", "book me a flight to Delhi", "", "   "],
)
def test_intent_router_defers_when_unsure(utterance):
    """Better to hand off to the agent than to answer the wrong question fast."""
    assert classify_intent(utterance) is None


def test_speakable_spells_out_slot_codes():
    spoken = speakable("Your car is in slot B014.")
    assert "B 0 1 4" in spoken
    assert "B014" not in spoken


def test_speakable_strips_markdown_and_currency_codes():
    spoken = speakable("**Total**: ₹185.00 (INR)")
    assert "*" not in spoken
    assert "rupees" in spoken


# ── Agent HTTP surface ────────────────────────────────────────

async def test_agent_answers_from_the_fallback_without_an_api_key(client, driver, facility):
    await client.post(
        "/api/v1/gates/scan/entry",
        json={"facility_id": facility["id"], "plate": "GJ01AB1234"},
    )
    response = await client.post(
        "/api/v1/agent/chat",
        headers=auth(driver["access_token"]),
        json={"message": "where is my car?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True          # no key configured in tests
    assert "slot" in body["reply"].lower()
    assert body["speakable"]


async def test_agent_tool_catalog_is_role_scoped_over_http(client, driver, owner):
    driver_tools = (
        await client.get("/api/v1/agent/tools", headers=auth(driver["access_token"]))
    ).json()
    owner_tools = (
        await client.get("/api/v1/agent/tools", headers=auth(owner["access_token"]))
    ).json()

    driver_names = {t["name"] for t in driver_tools["tools"]}
    owner_names = {t["name"] for t in owner_tools["tools"]}
    assert "get_revenue_report" in owner_names
    assert "get_revenue_report" not in driver_names
    assert any(t["requires_confirmation"] for t in driver_tools["tools"])


async def test_voice_fast_path_avoids_the_model(client, driver, facility):
    await client.post(
        "/api/v1/gates/scan/entry",
        json={"facility_id": facility["id"], "plate": "GJ01AB1234"},
    )
    response = await client.post(
        "/api/v1/voice/converse",
        headers=auth(driver["access_token"]),
        json={"text": "where is my car"},
    )
    body = response.json()
    assert body["fast_path"] is True
    assert body["intent"] == "find_my_vehicle"
    assert body["speakable"]


async def test_voice_status_reports_available_backends(client):
    body = (await client.get("/api/v1/voice/status")).json()
    assert "stt_backend" in body
    assert body["intent_router_patterns"] > 0


# ─────────────────────────────────────────────────────────────
# The offline fallback must be role-aware.
#
# An operator once asked "How is the facility running right now?" and was told
# about their wallet balance and directions to their slot — the driver fallback,
# served to someone who has neither. Worse, the Operations copilot *suggests*
# four operator questions, and none of them were answerable without an API key,
# even though every one is a plain database query underneath.
# ─────────────────────────────────────────────────────────────

OWNER_SUGGESTED = [
    "How is the facility running right now?",
    "Revenue for the last 7 days",
    "What will occupancy be in 3 hours?",
    "Any open anomalies?",
]

DRIVER_WORDS = ("wallet balance", "your vehicle is", "directions to your slot")


async def test_operator_fallback_never_answers_with_driver_text(client, owner, facility):
    """Every question the operator UI suggests must get an operator answer."""
    for question in OWNER_SUGGESTED:
        response = await client.post(
            "/api/v1/agent/chat",
            headers=auth(owner["access_token"]),
            json={"message": question, "facility_id": facility["id"]},
        )
        assert response.status_code == 200, question
        reply = response.json()["reply"].lower()

        assert "your wallet balance" not in reply, f"driver text served to an owner: {question}"
        assert "directions to your slot" not in reply, f"driver text served to an owner: {question}"
        # It must actually answer, not just decline.
        assert "can only answer a few set questions" not in reply, (
            f"the UI suggests {question!r} but the fallback cannot answer it"
        )


async def test_operator_fallback_routes_each_question_to_the_right_tool(client, owner, facility):
    """Spot-check that the four questions reach four different answers."""
    replies = {}
    for question in OWNER_SUGGESTED:
        r = await client.post(
            "/api/v1/agent/chat",
            headers=auth(owner["access_token"]),
            json={"message": question, "facility_id": facility["id"]},
        )
        replies[question] = r.json()["reply"].lower()

    assert "full" in replies[OWNER_SUGGESTED[0]] or "bays" in replies[OWNER_SUGGESTED[0]]
    assert "sessions" in replies[OWNER_SUGGESTED[1]] or "took" in replies[OWNER_SUGGESTED[1]]
    # The forecast question contains the word "occupancy", so it must not be
    # swallowed by the live-status branch.
    assert any(w in replies[OWNER_SUGGESTED[2]] for w in ("projected", "forecast", "peak", "history"))
    assert "anomal" in replies[OWNER_SUGGESTED[3]]


async def test_driver_fallback_is_unchanged(client, driver, facility):
    """The driver path must keep working exactly as before."""
    await client.post(
        "/api/v1/gates/scan/entry",
        json={"facility_id": facility["id"], "plate": "GJ01AB1234"},
    )
    response = await client.post(
        "/api/v1/agent/chat",
        headers=auth(driver["access_token"]),
        json={"message": "where is my car?"},
    )
    reply = response.json()["reply"].lower()
    assert "slot" in reply and "zone" in reply
