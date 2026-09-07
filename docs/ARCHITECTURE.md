# Architecture

## Request flow

The two paths that matter are entry and exit. Everything else in the system
reads what these two write.

```
POST /gates/entry  (camera frame or plate)
  │
  ├─ ANPRPipeline.recognize
  │     detect regions → deskew → 5 preprocessings × N backends
  │     → weighted vote + format prior → escalate if weak → decide
  │     └─ persists a RecognitionEvent (the audit trail) either way
  │
  ├─ resolve vehicle + owner        (an unknown plate is still billable as a guest)
  ├─ private mode? check the allow-list      → deny, log, raise
  ├─ duplicate open session?                 → conflict
  │
  ├─ AllocationEngine.allocate
  │     eligibility filter (type, EV, accessible)
  │     → policy: nearest │ recency │ random │ first_fit │ balanced │ hybrid
  │     → atomic claim: UPDATE … WHERE status='empty'  (retry on a lost race)
  │     → route: Dijkstra over the level's aisle graph → turn-by-turn text
  │
  ├─ create ParkingSession, snapshot the rate
  └─ emit VEHICLE_ENTERED, SLOT_ALLOCATED, OCCUPANCY_CHANGED
        └─► WebSocket fan-out · automation rules · analytics
```

```
POST /gates/exit
  │
  ├─ ANPRPipeline.recognize  ← primed with the plates currently parked inside
  ├─ find the active session
  ├─ PricingEngine.quote     occupancy × time × slot type × forecast, all bounded
  ├─ WalletService.debit     idempotency key = session id
  │     └─ on failure: session still closes, bay still frees, debt recorded
  ├─ AllocationEngine.release → stamps last_vacated_at (the recency input)
  ├─ anomaly checks (rules + IsolationForest)
  └─ emit VEHICLE_EXITED, SLOT_RELEASED, WALLET_DEBITED
```

## Why it is shaped this way

### The event bus is the spine

Every meaningful action publishes to an in-process async bus. The WebSocket
broadcaster, the automation rules engine and the analytics recorder are all
*subscribers* — the request path does not know they exist. Adding a fourth
consumer means adding a subscriber, not editing `ParkingService`.

A misbehaving subscriber can never fail the originating request: `publish`
gathers with `return_exceptions=True` and logs failures. Swapping the bus for
Redis Streams in a multi-node deployment means reimplementing `publish` and
`subscribe` only.

### Policies are pure functions

`app/services/allocation/strategies.py` contains no ORM types. It operates on a
`SlotView` dataclass, which is why the identical code runs against the live
database *and* inside the offline simulator. The benchmark therefore measures the
policy that actually ships, not a re-implementation that might have drifted.

### Concurrency is guarded in the database, never in Python

Two cars can reach two gates in the same millisecond. Two places take this
seriously:

```python
# Slot claim — succeeds only if the bay is still empty.
UPDATE slots SET status='occupied' WHERE id=? AND status='empty'
# rowcount == 0 → someone else won; drop that bay and re-run the policy.

# Wallet debit — the balance guard is evaluated inside the database.
UPDATE wallets SET balance = balance - ? WHERE id=? AND (balance - held) >= ?
```

The read that informed the decision is never trusted. This is what makes
double-allocation and overdraft structurally impossible rather than unlikely.

### Money is integers, and the ledger is the truth

Balances are `int` minor units (paise) — never floats. The stored balance is
derived state; `wallet_transactions` is authoritative, and `reconcile()`
recomputes one from the other and reports drift. The automation engine runs it
nightly, so a bug is found by the system rather than by a customer.

Every mutation takes an idempotency key. A gate controller that retries after a
network timeout settles the bill once.

### AI degrades instead of failing

`ClaudeClient.enabled` is false without an API key, and every caller has a
deterministic fallback: templates for briefings, a keyword router for the agent,
the local segmentation reader for OCR. The response always states which path
produced it. This is what makes it defensible to put a language model in a
request path at all.

### The agent cannot exceed its own scope

Agent tools are role-filtered *and* row-filtered in the handler, not in the
prompt — an agent cannot widen its own access by being talked into it. Tools that
move money or change pricing are marked `mutating` and return a confirmation
request instead of executing; only an explicit approval token from the user
unlocks them.

### Automation rules are data

Triggers, conditions and actions are rows. Conditions use a small fixed
comparator set (`_gte`, `_lte`, `_eq`, `hour`, `weekday`) with no `eval` — the
language is deliberately one in which nothing dangerous is expressible, because
the AI copilot drafts rules into it. Action names are validated against a
registry, so the worst a hallucinated action can do is get rejected.

## Module map

```
backend/app/
├── core/           config · logging · security · errors · event bus
├── db/             async engine, session, UTCDateTime type decorator
├── models/         user · facility · parking · automation · enums
├── schemas/        pydantic request/response contracts
├── api/
│   ├── deps.py     auth, role gates, ownership checks
│   └── v1/routers/ auth vehicles wallet facilities gates sessions
│                   analytics anpr ai automation city realtime
├── services/
│   ├── anpr/       plate_utils detector preprocess ocr_backends
│   │               ensemble pipeline synthetic
│   ├── allocation/ strategies engine navigation simulator
│   ├── billing/    pricing wallet
│   ├── ml/         features forecaster dwell anomaly
│   ├── genai/      client copilot
│   ├── agents/     tools runtime
│   ├── voice/      pipeline (STT · intent router · TTS)
│   ├── automation/ engine (triggers · conditions · action registry)
│   ├── integrations/ payments notifications weather
│   ├── realtime/   websocket manager
│   ├── parking.py  the lifecycle orchestrator
│   ├── analytics.py KPIs · forecasting · city feed · sustainability
│   └── layout.py   grid generation · distance recompute · plan detection
└── workers/        APScheduler jobs
```

## Data model

| Table | Role |
|---|---|
| `users` `wallets` `wallet_transactions` | Identity and the append-only money ledger |
| `vehicles` | Plate → owner. `plate_normalized` is the only key ANPR matches on |
| `facilities` `levels` `slots` `gates` | Facility topology. `slots.last_vacated_at` is what the recency policy sorts on |
| `authorized_vehicles` | Allow-list for private mode, with reserved bays and charge waivers |
| `parking_sessions` | One visit, entry to settled bill, plus allocation telemetry |
| `recognition_events` | Every ANPR inference, kept whether or not it produced a session |
| `anomalies` | Rule and model findings, with severity and resolution |
| `occupancy_snapshots` `price_snapshots` | Time series for the forecaster and for billing disputes |
| `automation_rules` `automation_runs` | Rules as data, with an execution log |
| `agent_conversations` `agent_messages` | Full agent transcripts including tool calls |

Two indexes carry the hot paths: `ix_slot_alloc (facility, status, type)` for
allocation and `ix_slot_recency (facility, status, last_vacated_at)` for the
recency sort.

### A note on `UTCDateTime`

SQLite has no native timestamp type and returns naive datetimes even from a
`timezone=True` column. Mixing those with the aware values the application
produces raises *"can't subtract offset-naive and offset-aware datetimes"* — and
it surfaces in the worst place, inside the allocator's recency comparison. One
`TypeDecorator` normalises on the way in and out, so no call site needs to know.

## Scaling path

Nothing here needs rewriting to grow; each step is a substitution:

| Step | Change |
|---|---|
| Postgres | `DATABASE_URL` only — asyncpg is a drop-in for aiosqlite |
| Multiple API nodes | Event bus → Redis Streams; scheduler moves to a dedicated worker with a database lock |
| Real gate cameras | Gate controllers already POST to `/gates/entry`; add device API keys (the `api_keys` table exists) |
| Higher OCR accuracy | Install `easyocr`, or set `ANTHROPIC_API_KEY` for vision escalation — no code change |
| Object storage | `settings.upload_dir` becomes an S3 prefix |
