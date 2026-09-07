/**
 * Evaluation lab.
 *
 * Runs the same allocation policies and the same ANPR pipeline the platform
 * uses in production, against controlled synthetic workloads. Everything here
 * is reproducible from the parameters on screen.
 */

import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { Badge, Card, Empty, ErrorNote, Spinner, Stat } from '../components/ui'
import { BarChart } from '../components/charts/BarChart'
import { LineChart } from '../components/charts/LineChart'
import { titleCase } from '../lib/format'
import type { BenchmarkReport } from '../lib/types'

const METRIC_HELP: Record<string, string> = {
  walk_m: 'Mean entry-to-bay distance, ignoring detours.',
  effective_walk_m: 'Distance actually driven, including re-search after a misallocation.',
  misallocation_rate: 'Share of arrivals sent to a bay that turned out to be occupied.',
  conflict_rate: 'Share parking within 18 m of a bay another car entered in the last 90 s.',
  reuse_gap_min: 'Mean minutes between a bay being vacated and refilled — the LRU effect.',
  slot_gini: 'Inequality of per-bay usage. 0 is perfectly even wear.',
  composite_score: 'Weighted: 60% effective walk, 25% aisle conflict, 15% wear.',
}

export function EvaluationLab() {
  const [params, setParams] = useState({
    trials: 5, vehicles: 800, zones: 5, slots_per_zone: 24, hours: 14, ghost_rate: 0,
  })
  const [report, setReport] = useState<BenchmarkReport | null>(null)
  const [comparison, setComparison] = useState<BenchmarkReport | null>(null)
  const [anpr, setAnpr] = useState<Record<string, any> | null>(null)
  const [anprSweep, setAnprSweep] = useState<{ difficulty: number; exact: number; near: number }[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const runAllocation = async () => {
    setBusy('allocation')
    setError(null)
    try {
      // Run both information regimes so the contrast is visible in one action.
      const [clean, degraded] = await Promise.all([
        api.benchmarkAllocation({ ...params, ghost_rate: 0 }),
        api.benchmarkAllocation({ ...params, ghost_rate: 0.1 }),
      ])
      setReport(clean)
      setComparison(degraded)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The benchmark failed.')
    } finally {
      setBusy(null)
    }
  }

  const runAnpr = async () => {
    setBusy('anpr')
    setError(null)
    try {
      const difficulties = [0, 0.15, 0.3, 0.45, 0.6, 0.75]
      const results = []
      for (const difficulty of difficulties) {
        const result = await api.benchmarkAnpr(20, difficulty)
        results.push({
          difficulty,
          exact: result.exact_match_rate * 100,
          near: result.near_match_rate * 100,
        })
        if (difficulty === 0.3) setAnpr(result)
      }
      setAnprSweep(results)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The ANPR benchmark failed.')
    } finally {
      setBusy(null)
    }
  }

  const active = report

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-white">Evaluation lab</h1>
        <p className="text-sm text-ink-400">
          The allocation policies and the recognition pipeline, measured against controlled
          workloads. Same code as production — these are not re-implementations.
        </p>
      </div>

      {error && <ErrorNote message={error} />}

      <Card
        title="Allocation strategy benchmark"
        subtitle="Discrete-event simulation of one trading day, averaged over several seeds"
        action={
          <button className="btn-primary btn-sm" onClick={runAllocation} disabled={busy !== null}>
            {busy === 'allocation' ? 'Running…' : 'Run benchmark'}
          </button>
        }
      >
        <div className="mb-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {([
            ['trials', 'Seeds', 1, 20],
            ['vehicles', 'Vehicles/day', 50, 5000],
            ['zones', 'Zones', 1, 12],
            ['slots_per_zone', 'Bays per zone', 4, 200],
            ['hours', 'Hours', 1, 24],
          ] as const).map(([key, label, min, max]) => (
            <div key={key}>
              <label className="label">{label}</label>
              <input
                type="number" min={min} max={max} className="input py-1.5"
                value={params[key]}
                onChange={(event) =>
                  setParams((prev) => ({ ...prev, [key]: Number(event.target.value) }))
                }
              />
            </div>
          ))}
        </div>

        {busy === 'allocation' && <Spinner label="Simulating both information regimes…" />}

        {active && (
          <div className="space-y-6">
            <div className="grid gap-4 sm:grid-cols-3">
              <Stat
                label="Winner (perfect information)"
                value={titleCase(active.winner)}
                hint={`${active.lot.capacity} bays · ${active.workload.vehicles} vehicles · ${active.workload.trials} seeds`}
                tone="good"
              />
              <Stat
                label="Winner (10% stale map)"
                value={comparison ? titleCase(comparison.winner) : '—'}
                hint="Some vehicles park unlogged, so the occupancy map is wrong"
                tone="good"
              />
              <Stat
                label="Peak occupancy"
                value={`${(active.results[0] as any).peak_occupancy_pct?.toFixed(0) ?? '—'}%`}
                hint="The lot is genuinely contended, not half empty"
              />
            </div>

            <div className="grid gap-5 lg:grid-cols-2">
              <div>
                <h3 className="mb-2 text-sm font-semibold text-ink-100">
                  Mean walking distance — lower is better
                </h3>
                <BarChart
                  bars={active.results
                    .slice()
                    .sort((a, b) => a.walk_m - b.walk_m)
                    .map((row) => ({
                      label: titleCase(row.strategy),
                      value: row.walk_m,
                      highlight: row.strategy === 'recency' || row.strategy === 'hybrid',
                      hint: `p90 ${row.p90_walk_m.toFixed(1)} m · ${
                        row.walk_m_vs_nearest_pct !== undefined
                          ? `${row.walk_m_vs_nearest_pct > 0 ? '+' : ''}${row.walk_m_vs_nearest_pct.toFixed(1)}% vs nearest`
                          : ''
                      }`,
                    }))}
                  format={(value) => `${value.toFixed(1)} m`}
                  ariaLabel="Mean walking distance by strategy"
                />
              </div>

              <div>
                <h3 className="mb-2 text-sm font-semibold text-ink-100">
                  Aisle conflict rate — lower is better
                </h3>
                <BarChart
                  bars={active.results
                    .slice()
                    .sort((a, b) => (a.conflict_rate ?? 0) - (b.conflict_rate ?? 0))
                    .map((row) => ({
                      label: titleCase(row.strategy),
                      value: (row.conflict_rate ?? 0) * 100,
                      highlight: row.strategy === 'recency' || row.strategy === 'hybrid',
                      hint: METRIC_HELP.conflict_rate,
                    }))}
                  format={(value) => `${value.toFixed(1)}%`}
                  ariaLabel="Aisle conflict rate by strategy"
                />
              </div>
            </div>

            <div>
              <h3 className="mb-2 text-sm font-semibold text-ink-100">Full results</h3>
              <div className="overflow-x-auto rounded-lg border border-ink-800">
                <table className="w-full min-w-[900px]">
                  <thead className="border-b border-ink-800 bg-ink-900/60">
                    <tr>
                      <th className="th">Strategy</th>
                      <th className="th text-right">Walk (m)</th>
                      <th className="th text-right">p90 (m)</th>
                      <th className="th text-right">Conflict</th>
                      <th className="th text-right">Reuse gap</th>
                      <th className="th text-right">Wear (Gini)</th>
                      <th className="th text-right">Decision</th>
                      <th className="th text-right">Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {active.results.map((row) => (
                      <tr
                        key={row.strategy}
                        className={`table-row ${
                          row.strategy === active.winner ? 'bg-brand-500/5' : ''
                        }`}
                      >
                        <td className="td font-medium">
                          {titleCase(row.strategy)}
                          {row.strategy === active.winner && (
                            <Badge tone="good" className="ml-2">best</Badge>
                          )}
                        </td>
                        <td className="td tabular text-right">{row.walk_m.toFixed(1)}</td>
                        <td className="td tabular text-right">{row.p90_walk_m.toFixed(1)}</td>
                        <td className="td tabular text-right">
                          {((row.conflict_rate ?? 0) * 100).toFixed(1)}%
                        </td>
                        <td className="td tabular text-right">
                          {(row.reuse_gap_min ?? 0).toFixed(1)} min
                        </td>
                        <td className="td tabular text-right">{(row.slot_gini ?? 0).toFixed(3)}</td>
                        <td className="td tabular text-right text-ink-400">
                          {(row.latency_us ?? 0).toFixed(0)} µs
                        </td>
                        <td className="td tabular text-right font-semibold">
                          {(row.composite_score ?? 0).toFixed(1)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {comparison && (
              <div>
                <h3 className="mb-1 text-sm font-semibold text-ink-100">
                  Under a 10% stale occupancy map
                </h3>
                <p className="mb-3 text-xs leading-relaxed text-ink-400">
                  Some vehicles park without being logged, so the system believes those bays are
                  free. A driver sent to one finds it taken, has to be re-allocated, and pays a
                  detour. Read the misallocation column carefully:{' '}
                  <b className="text-ink-200">
                    the rates are close across every policy
                  </b>
                  , because unlogged vehicles land on free bays roughly uniformly. Recency does{' '}
                  <i>not</i> dodge stale bays the way an LRU cache dodges stale entries — the
                  hypothesis the policy was designed on is not supported here, and that is worth
                  reporting rather than burying. What does help is picking a bay that is both
                  close and fresh, which is why hybrid carries the lowest detour cost.
                </p>
                <div className="overflow-x-auto rounded-lg border border-ink-800">
                  <table className="w-full min-w-[700px]">
                    <thead className="border-b border-ink-800 bg-ink-900/60">
                      <tr>
                        <th className="th">Strategy</th>
                        <th className="th text-right">Walk (m)</th>
                        <th className="th text-right">Effective walk (m)</th>
                        <th className="th text-right">Misallocations</th>
                        <th className="th text-right">Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {comparison.results.map((row) => (
                        <tr key={row.strategy} className="table-row">
                          <td className="td font-medium">{titleCase(row.strategy)}</td>
                          <td className="td tabular text-right">{row.walk_m.toFixed(1)}</td>
                          <td className="td tabular text-right">
                            {(row.effective_walk_m ?? 0).toFixed(1)}
                            <span className="ml-1 text-[11px] text-signal-amber">
                              +{((row.effective_walk_m ?? 0) - row.walk_m).toFixed(1)}
                            </span>
                          </td>
                          <td className="td tabular text-right">
                            {((row.misallocation_rate ?? 0) * 100).toFixed(1)}%
                          </td>
                          <td className="td tabular text-right font-semibold">
                            {(row.composite_score ?? 0).toFixed(1)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div className="rounded-lg border border-ink-800 bg-ink-950/50 p-4 text-xs leading-relaxed text-ink-400">
              <p className="mb-1.5 font-semibold text-ink-200">Reading these numbers</p>
              <p>
                <b className="text-ink-200">Nearest is distance-optimal by construction</b> under
                perfect information — that is a theorem, and the harness reproducing it is a
                check that the simulation is sound, not a finding. The interesting question is
                what a policy gives up, and what it buys. Recency stays within a couple of percent
                of optimal on distance while cutting aisle conflicts materially, because reusing
                the bay that just freed keeps the occupied set compact. Hybrid scores recency and
                proximity together, so it keeps that behaviour without ever sending a driver far
                across the deck for a marginally fresher bay.
              </p>
              <p className="mt-2">
                The composite weights ({Object.entries(active.weights ?? {})
                  .map(([key, value]) => `${key.replace(/_/g, ' ')} ${(Number(value) * 100).toFixed(0)}%`)
                  .join(', ')}) are a stated value judgement, not a derived truth. Change them and
                the ranking can change; every underlying metric is in the table above so you can.
              </p>
            </div>
          </div>
        )}

        {!active && busy !== 'allocation' && (
          <Empty
            title="No benchmark run yet."
            hint="Runs six policies over both information regimes. A few seconds at the default settings."
          />
        )}
      </Card>

      <Card
        title="ANPR accuracy sweep"
        subtitle="Recognition rate as a function of image degradation"
        action={
          <button className="btn-primary btn-sm" onClick={runAnpr} disabled={busy !== null}>
            {busy === 'anpr' ? 'Measuring…' : 'Run sweep'}
          </button>
        }
      >
        {busy === 'anpr' && <Spinner label="Generating and reading labelled frames…" />}

        {anprSweep.length > 0 && (
          <div className="space-y-4">
            <LineChart
              height={230}
              yMin={0}
              yMax={100}
              yFormat={(value) => `${value.toFixed(0)}%`}
              xFormat={(value) => `${(value * 100).toFixed(0)}%`}
              series={[
                {
                  name: 'Exact match',
                  points: anprSweep.map((row) => ({ x: row.difficulty, y: row.exact })),
                },
                {
                  name: 'Within one character',
                  points: anprSweep.map((row) => ({ x: row.difficulty, y: row.near })),
                  color: '#d95926',
                  dashed: true,
                },
              ]}
              ariaLabel="ANPR accuracy against image difficulty"
            />
            <p className="text-xs text-ink-400">
              The x-axis is the degradation level applied to the generated frame — blur, noise,
              perspective skew, glare and plate size, all scaled together.
            </p>

            {anpr && (
              <>
                <div className="grid gap-4 sm:grid-cols-4">
                  <Stat
                    label="Exact match"
                    value={`${(anpr.exact_match_rate * 100).toFixed(0)}%`}
                    hint="At 30% difficulty"
                    tone="good"
                  />
                  <Stat
                    label="Mean confidence"
                    value={`${(anpr.mean_confidence * 100).toFixed(0)}%`}
                  />
                  <Stat
                    label="Latency"
                    value={`${anpr.mean_latency_ms.toFixed(0)} ms`}
                    hint="Detect → OCR ensemble → decide"
                  />
                  <Stat
                    label="Active backends"
                    value={(anpr.backends?.active ?? []).length}
                    hint={(anpr.backends?.active ?? []).join(', ')}
                  />
                </div>
                <div className="rounded-lg border border-signal-amber/40 bg-signal-amber/10 p-4 text-xs leading-relaxed text-ink-200">
                  <p className="mb-1 font-semibold text-signal-amber">
                    What this number is and is not
                  </p>
                  {anpr.note}
                </div>
              </>
            )}
          </div>
        )}

        {anprSweep.length === 0 && busy !== 'anpr' && (
          <Empty
            title="No sweep run yet."
            hint="Generates labelled synthetic frames at six degradation levels and reads each one."
          />
        )}
      </Card>
    </div>
  )
}
