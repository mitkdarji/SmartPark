# API guide

The complete, always-current reference is the generated OpenAPI schema:

- **Swagger UI** — `http://localhost:8000/docs`
- **ReDoc** — `http://localhost:8000/redoc`
- **Schema** — `http://localhost:8000/openapi.json`

This document covers what a schema cannot express: the integration contracts, the
error model, and the flows that span several calls.

---

## Authentication

`POST /api/v1/auth/login` returns a 12-hour HS256 JWT carrying the user id and
role. Send it as `Authorization: Bearer <token>`.

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@smartpark.dev","password":"SmartPark2026!"}' \
  | jq -r .access_token)

curl -s localhost:8000/api/v1/facilities -H "Authorization: Bearer $TOKEN"
```

Role gates are enforced server-side on every route:

| Role | Access |
|---|---|
| `driver` | Own sessions, own wallet, own vehicles, public facility search |
| `owner` | Everything above, plus full control of facilities they own |
| `admin` | Cross-tenant access and reconciliation |

Ownership is checked per object, not per role — an operator asking for another
operator's KPIs gets a `403`, not their data.

---

## Error model

Every error returns the same envelope, so a client needs one handler:

```json
{
  "error": "Vehicle GJ 01 AB 1234 already has an open parking session.",
  "code": "conflict",
  "details": { "plate": "GJ01AB1234", "open_sessions": 1 }
}
```

| `code` | HTTP | Meaning |
|---|---|---|
| `authentication_failed` | 401 | Missing, malformed or expired token |
| `permission_denied` | 403 | Authenticated, but not for this object |
| `vehicle_not_authorised` | 403 | Private facility, plate not on the allow-list |
| `not_found` | 404 | No such facility, session, gate or rule |
| `insufficient_funds` | 402 | `details` carries the exact shortfall |
| `conflict` | 409 | Duplicate session, facility full, occupied bay deletion |
| `plate_recognition_failed` | 422 | No readable plate in the frame |
| `validation_error` | 422 | `details.fields` is the pydantic error list |
| `integration_error` | 502 | A third-party provider failed |

Every response carries `X-Request-ID` and `X-Process-Time`. The request id
appears on every log line for that request, so a client can report a failure by
id and it can be traced end to end.

---

## Gate integration

This is the contract a real barrier controller implements. Two calls.

### Entry

```bash
# With a camera frame
curl -X POST localhost:8000/api/v1/gates/entry \
  -F facility_id=1 -F gate_id=1 -F image=@frame.jpg

# Or with a plate the controller read itself
curl -X POST localhost:8000/api/v1/gates/scan/entry \
  -H 'Content-Type: application/json' \
  -d '{"facility_id":1,"gate_id":1,"plate":"GJ01AB1234"}'
```

```json
{
  "allowed": true,
  "session_id": 42,
  "plate_pretty": "GJ 01 AB 1234",
  "confidence": 0.94,
  "slot": { "id": 13, "code": "GA013", "zone": "GA", "level_id": 1, "type": "standard" },
  "allocation": {
    "strategy": "hybrid",
    "reason": "hybrid score 0.62 (recency+proximity, 18 m)",
    "considered": 99,
    "latency_ms": 3.4,
    "walk_distance_m": 18.1
  },
  "route": {
    "waypoints": [[1,1],[1.5,8],[9.7,8]],
    "distance_m": 18.1,
    "instructions": ["Enter the facility and follow the marked aisle.", "…"]
  },
  "anomalies": [],
  "occupancy_pct": 62.5,
  "processing_ms": 71.4
}
```

**Open the barrier on HTTP 200.** On a non-200 the `code` says what to do:

| Code | Barrier action |
|---|---|
| `vehicle_not_authorised` | Keep closed; the attempt is logged and security is alerted |
| `conflict` (facility full) | Keep closed; show "lot full" |
| `conflict` (duplicate session) | Keep closed; the previous exit was missed — needs an operator |
| `plate_recognition_failed` | Retry the capture, then fall back to manual entry |

### Exit

```bash
curl -X POST localhost:8000/api/v1/gates/exit \
  -F facility_id=1 -F gate_id=2 -F image=@frame.jpg
```

```json
{
  "settled": true,
  "session_id": 42,
  "duration_minutes": 137.0,
  "amount_minor": 12980,
  "payment_status": "paid",
  "invoice_no": "SP-001-000042",
  "quote": {
    "billable_minutes": 135,
    "effective_rate_minor": 4600,
    "multiplier": 1.15,
    "explanation": "2.25 h billable at 46.00 INR/h (1.15x base — evening peak), plus 18% tax.",
    "components": { "occupancy": 1.0, "time": 1.15, "time_label": "evening peak" }
  },
  "wallet_balance_minor": 77020
}
```

**Open the barrier whenever the session closes — including on `payment_status:
"failed"`.** A vehicle trapped behind a barrier over an empty wallet is a worse
outcome than an unpaid invoice; the debt is recorded against the account and the
operator is alerted through the automation engine.

`payment_status` values: `paid`, `waived` (authorised vehicle), `pending` (guest
with no wallet — collect at the gate), `failed` (charge recorded as outstanding).

### Retries are safe

The wallet debit is keyed on the session id. A controller that retries after a
network timeout settles the bill exactly once.

### Testing without hardware

```bash
# Generates a synthetic frame and pushes it through the real pipeline
curl -X POST "localhost:8000/api/v1/gates/1/simulate?direction=entry&difficulty=0.3"
```

The response includes a `simulation` block with the ground-truth plate and
whether the read was correct.

---

## WebSockets

Token in the query string — the WebSocket API cannot set headers.

| Channel | Who | Carries |
|---|---|---|
| `/ws/facility/{id}` | The operator of that facility | Gate events, allocations, releases, occupancy, price changes, anomalies, automation actions |
| `/ws/me` | Any signed-in user | That user's own allocations, exits, wallet movements, notifications |

```js
const ws = new WebSocket(`ws://localhost:8000/ws/facility/1?token=${token}`)
ws.onmessage = (m) => console.log(JSON.parse(m.data))
```

```json
{
  "id": "a3f9c2e1",
  "topic": "slot.allocated",
  "ts": "2026-09-07T10:15:03.221Z",
  "facility_id": 1,
  "user_id": 3,
  "payload": { "slot_code": "GA013", "plate": "GJ01AB1234", "reason": "…" }
}
```

On connect, a facility channel replays the last 20 events so a reconnecting
client is immediately current. Close codes: `4401` unauthenticated, `4403` not
your facility, `4404` no such facility.

---

## Smart-city open data

Unauthenticated, CORS-open, cached 60 s. Counts, coordinates and rates only —
no plates, sessions, users or anything that identifies a person. A test asserts
that.

| Endpoint | Format |
|---|---|
| `GET /api/v1/city/availability` | JSON |
| `GET /api/v1/city/availability.geojson` | GeoJSON `FeatureCollection` |
| `GET /api/v1/city/manifest` | Discovery document, GBFS-style |

```bash
curl -s localhost:8000/api/v1/city/availability | jq '.facilities[0]'
```

---

## AI endpoints

All of these work without an API key; the response says which engine produced it.

| Endpoint | What it does |
|---|---|
| `POST /api/v1/agent/chat` | Tool-calling agent. Role-scoped tools; mutating tools return `pending_confirmation` instead of executing |
| `GET /api/v1/agent/tools` | What this account's agent is allowed to do — the transparency surface |
| `POST /api/v1/voice/converse` | Text in, spoken-ready answer out. `fast_path: true` means the intent router answered without a model call |
| `GET /api/v1/facilities/{id}/ai/briefing` | Operations briefing, grounded on a facts block |
| `GET /api/v1/sessions/{id}/explain` | Plain-language explanation of a bill |
| `GET /api/v1/facilities/{id}/ai/suggest-automations` | Drafts rules; invalid actions are rejected before they are offered |

### The confirmation handshake

A tool that moves money never executes on the model's say-so:

```jsonc
// 1. The agent proposes
{ "reply": "That will add ₹500 to your wallet. Confirm?",
  "pending_confirmation": { "tool": "top_up_wallet", "arguments": { "amount": 500 } } }

// 2. The user approves; the client echoes the tool and its arguments back
POST /api/v1/agent/chat
{ "message": "yes", "confirm_tool": "top_up_wallet", "confirm_arguments": { "amount": 500 } }
```

---

## Evaluation

Pure computation — touches no facility data, safe to call at any time, and fully
reproducible from the request parameters.

```bash
curl -X POST localhost:8000/api/v1/benchmark/allocation \
  -H 'Content-Type: application/json' \
  -d '{"trials":5,"vehicles":800,"ghost_rate":0.1}'

curl "localhost:8000/api/v1/benchmark/anpr?samples=25&difficulty=0.35"
curl "localhost:8000/api/v1/benchmark/allocation/sweep?trials=5"
```

---

## Health

When the frontend bundle is present (the Docker image), the SPA is served at `/`
and the JSON service banner moves to `/api`. In development Vite serves the SPA
and `/` returns the banner.

`GET /health` reports every subsystem separately, so "degraded" is actionable
rather than a single opaque flag:

```json
{
  "status": "ok",
  "database": true,
  "genai": { "enabled": false, "model": "claude-opus-5" },
  "anpr": { "backends": { "segmentation": true, "easyocr": false }, "min_confidence": 0.55 },
  "voice": { "stt_available": false, "intent_router_patterns": 6 },
  "scheduler": true,
  "websocket_clients": 3
}
```
