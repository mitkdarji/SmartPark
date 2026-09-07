"""End-to-end API tests: the complete parking lifecycle over HTTP."""

from __future__ import annotations

from tests.conftest import auth, register


async def test_health_reports_every_subsystem(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    for key in ("database", "genai", "anpr", "voice", "scheduler"):
        assert key in body


async def test_registration_creates_a_funded_wallet(client):
    account = await register(client, "new@test.dev", plate="GJ99XX1111")
    wallet = await client.get(
        "/api/v1/auth/me/wallet", headers=auth(account["access_token"])
    )
    assert wallet.status_code == 200
    assert wallet.json()["balance_minor"] > 0


async def test_duplicate_email_is_rejected(client, driver):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "driver@test.dev", "password": "TestPassword123!",
            "full_name": "Someone Else", "role": "driver",
        },
    )
    assert response.status_code == 409


async def test_login_failure_does_not_reveal_whether_the_email_exists(client, driver):
    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"email": "driver@test.dev", "password": "WrongPassword1!"},
    )
    unknown_email = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@test.dev", "password": "WrongPassword1!"},
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["error"] == unknown_email.json()["error"]


async def test_protected_route_requires_a_token(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    ).status_code == 401


async def test_driver_cannot_create_a_facility(client, driver):
    response = await client.post(
        "/api/v1/facilities",
        headers=auth(driver["access_token"]),
        json={"name": "Sneaky Lot"},
    )
    assert response.status_code == 403


async def test_grid_generation_builds_a_usable_layout(client, facility):
    assert len(facility["slots"]) == 24
    codes = [s["code"] for s in facility["slots"]]
    assert len(set(codes)) == len(codes)
    # Distances must be computed, finite and in a plausible metre range.
    distances = [s["distance_from_entry"] for s in facility["slots"]]
    assert all(0 < d < 500 for d in distances), distances[:5]
    assert any(s["has_ev_charger"] for s in facility["slots"])


async def test_full_entry_to_exit_lifecycle(client, owner, driver, facility):
    fid = facility["id"]

    entry = await client.post(
        "/api/v1/gates/scan/entry",
        json={"facility_id": fid, "plate": "GJ01AB1234"},
    )
    assert entry.status_code == 200, entry.text
    body = entry.json()
    assert body["allowed"] is True
    assert body["slot"]["code"]
    assert body["route"]["instructions"]
    session_id = body["session_id"]

    # The bay is now occupied and the driver has an active session.
    active = await client.get("/api/v1/sessions/active", headers=auth(driver["access_token"]))
    assert active.json()["id"] == session_id
    assert active.json()["live_amount_minor"] is not None

    # The same plate cannot enter twice.
    again = await client.post(
        "/api/v1/gates/scan/entry", json={"facility_id": fid, "plate": "GJ01AB1234"}
    )
    assert again.status_code == 409

    exit_response = await client.post(
        "/api/v1/gates/scan/exit", json={"facility_id": fid, "plate": "GJ01AB1234"}
    )
    assert exit_response.status_code == 200, exit_response.text
    settled = exit_response.json()
    assert settled["session_id"] == session_id
    assert settled["invoice_no"]
    # Inside the free period, so nothing to pay — but the session must close.
    assert settled["payment_status"] in ("paid", "waived")

    closed = await client.get(
        f"/api/v1/sessions/{session_id}", headers=auth(driver["access_token"])
    )
    assert closed.json()["status"] == "completed"


async def test_exit_without_an_entry_is_rejected(client, facility):
    response = await client.post(
        "/api/v1/gates/scan/exit",
        json={"facility_id": facility["id"], "plate": "KA05ZZ0000"},
    )
    assert response.status_code == 404


async def test_slot_is_released_and_timestamped_on_exit(client, owner, facility):
    fid = facility["id"]
    entry = (
        await client.post(
            "/api/v1/gates/scan/entry", json={"facility_id": fid, "plate": "MH12QQ4321"}
        )
    ).json()
    slot_id = entry["slot"]["id"]

    slots = (
        await client.get(f"/api/v1/facilities/{fid}/slots")
    ).json()
    occupied = next(s for s in slots if s["id"] == slot_id)
    assert occupied["status"] == "occupied"

    await client.post("/api/v1/gates/scan/exit", json={"facility_id": fid, "plate": "MH12QQ4321"})

    slots = (await client.get(f"/api/v1/facilities/{fid}/slots")).json()
    freed = next(s for s in slots if s["id"] == slot_id)
    assert freed["status"] == "empty"
    # `last_vacated_at` is what the recency allocator sorts on — it must be set.
    assert freed["last_vacated_at"] is not None


async def test_facility_fills_up_and_then_refuses_entry(client, facility):
    fid = facility["id"]
    capacity = len(facility["slots"])

    accepted = 0
    for i in range(capacity + 3):
        response = await client.post(
            "/api/v1/gates/scan/entry",
            json={"facility_id": fid, "plate": f"GJ{i % 90 + 1:02d}ZZ{i:04d}"},
        )
        if response.status_code == 200:
            accepted += 1
        else:
            assert response.status_code == 409
            assert "full" in response.json()["error"].lower()
            break

    assert accepted <= capacity
    summary = (await client.get(f"/api/v1/facilities/{fid}")).json()
    assert summary["free"] == 0


async def test_private_facility_enforces_its_allow_list(client, owner):
    created = (
        await client.post(
            "/api/v1/facilities",
            headers=auth(owner["access_token"]),
            json={"name": "Restricted Yard", "access_mode": "private"},
        )
    ).json()
    fid = created["id"]
    levels = (await client.get(f"/api/v1/facilities/{fid}/levels")).json()
    await client.post(
        f"/api/v1/facilities/{fid}/slots/generate?level_id={levels[0]['id']}&rows=2&columns=4",
        headers=auth(owner["access_token"]),
    )

    denied = await client.post(
        "/api/v1/gates/scan/entry", json={"facility_id": fid, "plate": "DL01ZZ9999"}
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "vehicle_not_authorised"

    await client.post(
        f"/api/v1/facilities/{fid}/authorized",
        headers=auth(owner["access_token"]),
        json={"plate": "DL01ZZ9999", "label": "Contractor", "waive_charges": True},
    )
    allowed = await client.post(
        "/api/v1/gates/scan/entry", json={"facility_id": fid, "plate": "DL01ZZ9999"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["allowed"] is True


async def test_owner_cannot_read_another_operators_facility(client, owner, facility):
    other = await register(client, "other-owner@test.dev", role="owner")
    response = await client.get(
        f"/api/v1/facilities/{facility['id']}/analytics/kpis",
        headers=auth(other["access_token"]),
    )
    assert response.status_code == 403


async def test_driver_cannot_read_another_drivers_session(client, driver, facility):
    entry = (
        await client.post(
            "/api/v1/gates/scan/entry",
            json={"facility_id": facility["id"], "plate": "GJ01AB1234"},
        )
    ).json()
    intruder = await register(client, "intruder@test.dev")
    response = await client.get(
        f"/api/v1/sessions/{entry['session_id']}", headers=auth(intruder["access_token"])
    )
    assert response.status_code == 403


async def test_layout_save_refuses_to_delete_an_occupied_bay(client, owner, facility):
    fid = facility["id"]
    entry = (
        await client.post(
            "/api/v1/gates/scan/entry", json={"facility_id": fid, "plate": "TN09AA1111"}
        )
    ).json()
    occupied_code = entry["slot"]["code"]

    remaining = [
        {k: s[k] for k in ("id", "code", "zone", "x", "y", "width", "height", "slot_type")}
        for s in facility["slots"]
        if s["code"] != occupied_code
    ]
    response = await client.put(
        f"/api/v1/facilities/{fid}/slots",
        headers=auth(owner["access_token"]),
        json={"level_id": facility["level_id"], "slots": remaining},
    )
    assert response.status_code == 409
    assert occupied_code in response.json()["error"]


async def test_duplicate_slot_codes_are_rejected(client, owner, facility):
    slots = [
        {"code": "A001", "zone": "A", "x": 0, "y": 0},
        {"code": "A001", "zone": "A", "x": 5, "y": 0},
    ]
    response = await client.put(
        f"/api/v1/facilities/{facility['id']}/slots",
        headers=auth(owner["access_token"]),
        json={"level_id": facility["level_id"], "slots": slots},
    )
    assert response.status_code == 409
    assert "duplicate" in response.json()["error"].lower()


async def test_vehicle_registration_rejects_a_plate_owned_by_someone_else(client, driver):
    other = await register(client, "second@test.dev")
    response = await client.post(
        "/api/v1/vehicles",
        headers=auth(other["access_token"]),
        json={"plate": "GJ01AB1234"},
    )
    assert response.status_code == 409


async def test_city_feed_is_public_and_carries_no_personal_data(client, facility):
    response = await client.get("/api/v1/city/availability")
    assert response.status_code == 200
    body = response.json()
    assert body["facility_count"] >= 1
    assert body["license"]

    serialized = response.text.lower()
    for leak in ("plate", "email", "session", "password"):
        assert leak not in serialized, f"city feed leaked '{leak}'"


async def test_city_geojson_is_well_formed(client, facility):
    body = (await client.get("/api/v1/city/availability.geojson")).json()
    assert body["type"] == "FeatureCollection"
    assert isinstance(body["features"], list)


async def test_openapi_schema_builds(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"]
    assert len(schema["paths"]) > 40
