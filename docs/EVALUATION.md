# Evaluation

Everything here is reproducible:

```bash
cd backend
.venv/bin/python -m scripts.benchmark --all --trials 6 --vehicles 800 --samples 25
```

The harness imports the same `choose_slot` and the same `ANPRPipeline` the
production request path calls. There is no separate reference implementation, so
these numbers describe shipped code rather than a model of it.

---

## 1. Allocation policies

### 1.1 What is being compared

| Policy | Rule |
|---|---|
| `nearest` | The empty bay closest to the entry gate. **Baseline.** |
| `recency` | The most-recently-vacated bay — an LRU cache in physical form. **The policy under study.** |
| `first_fit` | Lowest bay code. What a paper-ticket attendant does. |
| `random` | Uniform random. The control condition. |
| `balanced` | Least-loaded zone, then nearest within it. |
| `hybrid` | Recency and proximity scored together, plus zone balance and type fit. **Production default.** |

### 1.2 Workload

Non-homogeneous Poisson arrivals shaped by a mall demand curve (morning ramp,
lunch peak, evening peak). Lognormal dwell, median ≈95 min with a heavy tail.
Realistic vehicle mix. 120 bays across 5 zones, 800 vehicles over 14 hours,
averaged over 6 seeds. Peak occupancy reaches 100%, so the lot is genuinely
contended — a half-empty lot would make every policy look identical.

### 1.3 Metrics, and why more than one

No single number settles this, so the harness reports six.

| Metric | What it captures |
|---|---|
| `walk_m` | Mean entry→bay distance. The driver's primary experience. |
| `effective_walk_m` | Distance actually driven, including re-search detours. |
| `p90_walk_m` | The tail. A good mean with a terrible 90th percentile is a bad policy. |
| `conflict_rate` | Share of arrivals parking within 18 m of a bay another car entered in the last 90 s — the proxy for queueing behind someone manoeuvring. |
| `reuse_gap_min` | Minutes between a bay being vacated and refilled. The direct measure of the working-set effect. |
| `slot_gini` | Inequality of per-bay usage. Even wear is a real operational concern. |

### 1.4 Results — perfect occupancy information

| Strategy | Walk (m) | p90 (m) | Conflict | Reuse gap | Gini | Decision | Composite |
|---|---:|---:|---:|---:|---:|---:|---:|
| **hybrid** | 83.8 | 149.4 | 24.8% | 5.0 min | 0.180 | 12 µs | **65.5** |
| **recency** | 84.4 | 150.4 | 23.0% | 5.0 min | 0.178 | 3 µs | 64.8 |
| first_fit | 83.6 | 149.9 | 27.2% | 4.9 min | 0.182 | 2 µs | 61.2 |
| nearest | **83.3** | **148.8** | 28.5% | 5.0 min | 0.184 | 5 µs | 60.0 |
| random | 89.0 | 153.5 | 18.6% | 11.7 min | 0.164 | 2 µs | 38.3 |
| balanced | 89.1 | 153.5 | **17.1%** | 5.7 min | **0.172** | 3 µs | 34.1 |

**`nearest` winning on distance is not a finding.** It is distance-optimal by
construction when the occupancy map is correct. The harness reproducing that is
a soundness check on the simulator.

The finding is the **trade curve**:

- `recency` costs **+1.3% distance** and buys **−19% aisle conflicts**.
- `hybrid` costs **+0.6% distance** and buys **−13% conflicts**.
- `balanced` buys the fewest conflicts (−40%) but costs **+7% distance**.

Under the stated weighting — effective walk 60%, conflict 25%, wear 15% — hybrid
and recency come out on top. Those weights are a **value judgement, not a derived
truth**. Weight distance at 90% and `nearest` wins; weight congestion at 60% and
`balanced` does. Every underlying metric is published so the weighting can be
argued with, which is the point.

### 1.5 An observation worth more than the ranking

`nearest` and `recency` have almost the same reuse gap (5.0 min). That is not a
coincidence and it explains why recency's gains are modest:

> **In a busy lot, `nearest` is already implicitly LRU.** When the front zone is
> saturated, the closest free bay is usually one that *just* freed — because if
> it had been free for a while, an earlier arrival would have taken it.

The recency policy makes explicit what the nearest policy does incidentally. That
is why the honest headline is "comparable distance, materially fewer conflicts"
rather than a large win.

### 1.6 Results — degraded occupancy information

The realistic case: some vehicles park without being logged (a visitor tailgates
the barrier, a bay sensor fails, someone straddles two bays). The system believes
those bays are free; a driver sent to one finds it taken, is re-allocated, and
pays a detour.

At a 10% ghost rate:

| Strategy | Walk (m) | Effective walk (m) | Detour | Misallocations | Composite |
|---|---:|---:|---:|---:|---:|
| first_fit | 83.2 | 88.7 | +5.5 | 8.0% | **65.9** |
| hybrid | 83.5 | 89.3 | +5.9 | **7.4%** | 65.4 |
| recency | 84.7 | 90.3 | +5.6 | 8.4% | 60.7 |
| nearest | **82.4** | 88.5 | +6.1 | 8.5% | 60.0 |
| balanced | 89.1 | 94.2 | +5.0 | 8.2% | 47.2 |
| random | 89.9 | 95.8 | +6.0 | 8.6% | 37.7 |

Composite score as the map gets less trustworthy (6 seeds):

| Ghost rate | hybrid | recency | first_fit | nearest | random | balanced |
|---|---:|---:|---:|---:|---:|---:|
| 0% | **66.9** | 65.2 | 61.9 | 60.0 | 40.4 | 29.0 |
| 4% | **68.0** | 63.5 | 54.6 | 62.0 | 35.4 | 61.0 |
| 8% | 65.8 | **67.2** | 63.7 | 56.3 | 36.7 | 57.0 |
| 12% | **69.1** | 62.8 | 60.0 | 54.6 | 37.4 | 47.7 |
| 18% | 65.1 | **67.4** | 60.6 | 61.1 | 36.1 | 46.8 |

`hybrid` and `recency` lead at every staleness level. `nearest` degrades fastest,
which is what you would expect from a policy with no notion of confidence.

### 1.7 A hypothesis that did not survive

The recency policy was designed on an explicit analogy: an LRU cache reuses its
hottest line because recently-touched entries are least likely to have gone
stale. Applied here, the prediction was that recency should **avoid ghost bays**
— a bay confirmed vacated ninety seconds ago has had far less time to acquire an
unlogged occupant than one that has sat "free" for three hours.

**The data does not support it.** Misallocation rates cluster between 7.4% and
8.6% across *every* policy, and `recency` (8.4%) is no better than `nearest`
(8.5%). Unlogged vehicles land on free bays roughly uniformly, and the exposure
advantage the analogy predicts is swamped by that.

Where hybrid does come out ahead under staleness (7.4%), the mechanism is not
freshness — it is that scoring proximity alongside recency keeps the candidate
set in the busy front zones, which turn over fast enough that few ghosts
accumulate there.

This is reported rather than buried because a policy's *stated* mechanism failing
while the policy still performs well is exactly the kind of result that changes
what you build next. The honest conclusion is:

> Recency-aware allocation is worth shipping for its **congestion** properties,
> not for the stale-information robustness that motivated it.

### 1.8 Threats to validity

- **Simulated demand.** The arrival curve and dwell distribution are modelled,
  not observed. Absolute numbers would shift on real traffic; the *ordering*
  under a fixed weighting is the transferable part.
- **Conflict is a proxy.** 18 m within 90 s stands in for aisle queueing. It is
  not measured vehicle delay, and a real facility with instrumented aisles would
  measure that directly.
- **Ghosts are uniform.** Real unlogged parking clusters near entrances. A
  spatially-biased ghost model would likely *hurt* the front-loading policies
  (`nearest`, `hybrid`) more than the numbers here suggest.
- **One lot geometry.** Five zones in a rectangle. A long single-aisle lot or a
  spiral ramp deck would change the distance spread and could change the ranking.

---

## 2. Plate recognition

### 2.1 Pipeline

```
decode → detect regions (blackhat → Sobel → morphology → contour scoring)
       → perspective deskew
       → 5 preprocessing variants  ×  N OCR backends
       → weighted vote + format prior + context prior
       → escalate to Claude vision if consensus is weak
       → decide (accept / flag for review)
```

The ensemble weights each backend by measured reliability, adds a bonus for
strings matching the Indian plate grammar, and — at the exit gate — a bonus for
matching a plate currently parked inside. That last prior matters: the set of
vehicles on site is small and known, so a noisy exit read snaps onto the right
session instead of creating a phantom one.

### 2.2 Accuracy

25 labelled frames per level, CPU only, built-in segmentation backend:

| Difficulty | Exact | Within 1 char | Mean confidence | Latency |
|---|---:|---:|---:|---:|
| 0.00 | 92% | 92% | 0.97 | 36 ms |
| 0.15 | 96% | 96% | 0.99 | 35 ms |
| 0.30 | 96% | 96% | 0.96 | 35 ms |
| 0.45 | 76% | 76% | 0.74 | 35 ms |
| 0.60 | 96% | 100% | 0.95 | 38 ms |
| 0.75 | 92% | 96% | 0.93 | 39 ms |

The dip at 0.45 is where synthetic glare switches on; the confidence score tracks
it down to 0.74, so the pipeline knows it is struggling and flags those reads for
review rather than opening a barrier on a guess. That the *confidence is honest*
matters more than the accuracy number.

### 2.3 What this number is not

**It is not an accuracy claim.** The frames are rendered by SmartPark's own
generator, and the built-in segmentation reader matches glyphs against templates
drawn from the *same font family*. The table above measures the pipeline —
detection, deskew, the preprocessing fan-out, the voting ensemble — under
controlled degradation. It does not measure recognition.

Rendering the same plates in Arial instead, and changing nothing else:

| Font | Exact match |
|---|---:|
| Hershey (the generator's font) | **5 / 5** |
| Arial (unseen by the reader) | **0 / 5** |

Four of the five Arial plates produced no read at all; the fifth returned
`TN09PQ3821` for `TN09PQ3321`. That is the correct result for a template
matcher given a font it has never seen, and it is why the built-in reader is
discounted to 0.85 confidence and sits last in the ensemble's trust weighting.

It exists for one reason: so the platform has **no hard dependency on a heavy
runtime** and stays fully demonstrable offline. It is not fit for a gate camera.

Real-world reading is the job of `easyocr` — a CRNN trained on photographs — or
the Claude vision escalation. Both are implemented, both plug into the same
ensemble, and neither is installed by default. **Nothing in this repository has
been tested against a photograph of a real number plate.**

An honest evaluation needs a labelled dataset of Indian plates shot at
gate-camera angles. That is the obvious next piece of work, and the
`RecognitionEvent` table already stores operator corrections precisely so that
dataset accumulates from live use.

## 3. Demand forecasting

Gradient-boosted regression on lagged occupancy (t−1, t−2, t−3, t−24), cyclical
time encodings, and weather.

| | |
|---|---|
| Training rows | 480 hourly observations |
| Validation | Chronological holdout — the last 20%, never a random split |
| **Holdout MAE** | **4.3 percentage points** |
| Persistence baseline MAE | 10.1 percentage points |
| R² | 0.968 |

**Two things keep this honest.** A random split would leak the future into
training through the lag features and produce a flattering, meaningless score, so
the holdout is strictly the tail of the series. And the model is always compared
against "predict the same as an hour ago" — the API returns both numbers, and a
model that fails to beat its baseline says so in its own response.

**Caveat:** the 480 observations are back-filled synthetic history generated from
a smooth demand curve. R² of 0.968 reflects how learnable that generator is, not
how predictable a real car park is. Expect materially worse on live data; the
architecture (chronological holdout, baseline comparison, honest fallback below
120 observations) is what transfers.

Below 120 usable observations the model is not trained at all — a seasonal-naive
baseline serves instead, and every response says which produced the number.

---

## 4. Test coverage

125 tests, ~4 s. Concentrated where being wrong is expensive:

| Area | What is actually asserted |
|---|---|
| Plate handling | Positional OCR repair (`MHI2AB1S34` → `MH12AB1534`), confusion-aware similarity, fuzzy match refusing a distant plate |
| Allocation | Each policy's rule, EV/accessible eligibility, hybrid *refusing* a far-but-fresher bay, gate-aware distance |
| Simulator | Vehicle conservation, `nearest` reproducing distance-optimality, staleness producing detours |
| Pricing | Monotonic surge, hard multiplier bounds, free period, increment rounding, daily cap, declining block tariff |
| Wallet | Idempotent retry billing once, held funds unspendable, **a stale read across two sessions cannot overdraw**, reconciliation catching injected drift |
| ANPR | Ensemble consensus, positional repair, the context prior rescuing a noisy read, clean failure on undecodable input |
| Layout | Routed distances, aisle graph pathfinding, floor-plan detection **declining a photograph** rather than inventing bays |
| API | Full entry→exit lifecycle, capacity exhaustion, private-mode allow-list, cross-tenant access denial, occupied-bay deletion refused, city feed carrying no personal data |

The wallet concurrency test deserves a note: an `asyncio.gather` over one session
does not test concurrency, it tests interleaving. The real test opens two
sessions on a file-backed database, lets one commit, and asserts the other's
stale read is rejected by the predicate inside the `UPDATE` — which is the
lost-update scenario the guard actually exists to prevent.
