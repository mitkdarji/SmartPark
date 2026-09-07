/**
 * Gate and ANPR console.
 *
 * Three ways to exercise the same pipeline: a generated synthetic frame, an
 * uploaded photo, and a typed plate. Whichever you use, the response shows the
 * full inference trace — every backend's proposal, how consensus was reached,
 * what the allocator decided and why.
 */

import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { useFacility } from '../hooks/useFacility'
import { Badge, Card, Empty, ErrorNote, Segmented, Spinner, Stat } from '../components/ui'
import { FacilityPicker } from '../components/FacilityPicker'
import { duration, money, prettyPlate } from '../lib/format'
import type { GateResult, RecognitionResult } from '../lib/types'

type Mode = 'synthetic' | 'upload' | 'plate'

export function GateConsole() {
  const { facilities, facilityId, select } = useFacility()
  const gates = useAsync(async () => (facilityId ? api.gates(facilityId) : []), [facilityId])
  const anprStatus = useAsync(() => api.anprStatus(), [])

  const [mode, setMode] = useState<Mode>('synthetic')
  const [direction, setDirection] = useState<'entry' | 'exit'>('entry')
  const [difficulty, setDifficulty] = useState(0.3)
  const [plate, setPlate] = useState('')
  const [gateId, setGateId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<GateResult | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [recognition, setRecognition] = useState<RecognitionResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = async (file?: File) => {
    if (!facilityId) return
    setBusy(true)
    setError(null)
    setResult(null)
    setRecognition(null)
    try {
      if (mode === 'synthetic') {
        // Show the operator the exact frame the pipeline is about to read.
        const sample = await api.anprSample({
          plate: direction === 'exit' ? plate || undefined : plate || undefined,
          difficulty,
        })
        setPreview(`data:${sample.media_type};base64,${sample.image_base64}`)
        setResult(
          await api.simulateGate(facilityId, {
            direction, difficulty, plate: plate || undefined,
            gate_id: gateId ?? undefined,
          }),
        )
      } else if (mode === 'upload' && file) {
        setPreview(URL.createObjectURL(file))
        setResult(await api.gateImage(facilityId, direction, file, gateId ?? undefined))
      } else if (mode === 'plate') {
        setPreview(null)
        const payload = { facility_id: facilityId, plate, gate_id: gateId ?? undefined }
        setResult(
          direction === 'entry' ? await api.scanEntry(payload) : await api.scanExit(payload),
        )
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The gate operation failed.')
    } finally {
      setBusy(false)
    }
  }

  const recogniseOnly = async (file: File) => {
    setBusy(true)
    setError(null)
    setResult(null)
    setPreview(URL.createObjectURL(file))
    try {
      setRecognition(await api.anprRecognize(file))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Recognition failed.')
    } finally {
      setBusy(false)
    }
  }

  const trace = result?.recognition ?? recognition

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Gate &amp; ANPR console</h1>
          <p className="text-sm text-ink-400">
            Drive the real recognition, allocation and billing pipeline without any hardware.
          </p>
        </div>
        <FacilityPicker facilities={facilities.data ?? []} value={facilityId} onChange={select} />
      </div>

      {anprStatus.data && (
        <Card title="Recognition stack" subtitle="Which OCR backends are loadable right now">
          <div className="flex flex-wrap items-center gap-4">
            {Object.entries(anprStatus.data.backends as Record<string, boolean>).map(
              ([name, available]) => (
                <span key={name} className="flex items-center gap-2 text-sm">
                  <span
                    className={`h-2 w-2 rounded-full ${available ? 'bg-brand-500' : 'bg-ink-600'}`}
                  />
                  <span className={available ? 'text-ink-100' : 'text-ink-500'}>{name}</span>
                </span>
              ),
            )}
            <span className="text-xs text-ink-500">
              Accept above {(anprStatus.data.min_confidence * 100).toFixed(0)}% ·
              escalate to the vision model below {(anprStatus.data.escalation_threshold * 100).toFixed(0)}%
              {anprStatus.data.vision_escalation_enabled ? '' : ' (needs ANTHROPIC_API_KEY)'}
            </span>
          </div>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-[380px_1fr]">
        <Card title="Trigger a scan">
          <div className="space-y-4">
            <Segmented
              value={mode}
              onChange={(value) => { setMode(value); setResult(null); setRecognition(null) }}
              options={[
                { value: 'synthetic', label: 'Synthetic frame' },
                { value: 'upload', label: 'Upload photo' },
                { value: 'plate', label: 'Type plate' },
              ]}
            />

            <div>
              <label className="label">Direction</label>
              <Segmented
                value={direction}
                onChange={setDirection}
                options={[{ value: 'entry', label: 'Entry' }, { value: 'exit', label: 'Exit' }]}
              />
            </div>

            {gates.data && gates.data.length > 1 && (
              <div>
                <label className="label">Gate</label>
                <select
                  className="input"
                  value={gateId ?? ''}
                  onChange={(event) => setGateId(event.target.value ? Number(event.target.value) : null)}
                >
                  <option value="">Primary gate</option>
                  {gates.data.map((gate) => (
                    <option key={gate.id} value={gate.id}>{gate.name} ({gate.kind})</option>
                  ))}
                </select>
              </div>
            )}

            {mode === 'synthetic' && (
              <div>
                <label className="label">
                  Image difficulty — {(difficulty * 100).toFixed(0)}%
                </label>
                <input
                  type="range" min={0} max={1} step={0.05} value={difficulty}
                  onChange={(event) => setDifficulty(Number(event.target.value))}
                  className="w-full accent-brand-500"
                />
                <p className="mt-1 text-[11px] text-ink-500">
                  Scales blur, noise, skew, glare and plate size — 0 is a studio-clean crop,
                  1 is a hard night-time read.
                </p>
              </div>
            )}

            {(mode === 'synthetic' || mode === 'plate') && (
              <div>
                <label className="label">
                  Plate {mode === 'synthetic' && <span className="normal-case text-ink-500">(optional — random if blank)</span>}
                </label>
                <input
                  className="input uppercase font-mono tracking-wider"
                  placeholder="GJ01AB1234"
                  value={plate}
                  onChange={(event) => setPlate(event.target.value.toUpperCase())}
                />
              </div>
            )}

            {mode === 'upload' ? (
              <div className="space-y-2">
                <label className="btn-ghost w-full cursor-pointer">
                  Choose an image…
                  <input
                    type="file" accept="image/*" className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      if (file) void run(file)
                    }}
                  />
                </label>
                <label className="btn-ghost btn-sm w-full cursor-pointer">
                  Recognise only (no gate action)
                  <input
                    type="file" accept="image/*" className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      if (file) void recogniseOnly(file)
                    }}
                  />
                </label>
              </div>
            ) : (
              <button
                className="btn-primary w-full"
                disabled={busy || !facilityId || (mode === 'plate' && plate.length < 4)}
                onClick={() => void run()}
              >
                {busy ? 'Processing…' : `Run ${direction} scan`}
              </button>
            )}

            {error && <ErrorNote message={error} />}

            {preview && (
              <div>
                <p className="label">Camera frame</p>
                <img
                  src={preview}
                  alt="Gate camera frame"
                  className="w-full rounded-lg border border-ink-800"
                />
              </div>
            )}
          </div>
        </Card>

        <div className="space-y-5">
          {busy && <Card><Spinner label="Running the pipeline…" /></Card>}

          {result && (
            <>
              <Card
                title={result.allowed !== undefined ? 'Entry result' : 'Exit result'}
                action={
                  result.simulation ? (
                    <Badge tone={result.simulation.read_correct ? 'good' : 'bad'}>
                      {result.simulation.read_correct ? 'Read correct' : 'Misread'}
                    </Badge>
                  ) : null
                }
              >
                <div className="grid gap-4 sm:grid-cols-3">
                  <Stat
                    label="Plate"
                    value={<span className="font-mono">{prettyPlate(result.plate)}</span>}
                    hint={
                      result.simulation
                        ? `Ground truth ${prettyPlate(result.simulation.ground_truth_plate)}`
                        : `${(result.confidence * 100).toFixed(0)}% confidence`
                    }
                    tone={result.confidence > 0.75 ? 'good' : 'warn'}
                  />
                  {result.slot ? (
                    <Stat
                      label="Assigned bay"
                      value={result.slot.code}
                      hint={`Zone ${result.slot.zone} · ${result.allocation?.walk_distance_m.toFixed(0)} m walk`}
                      tone="good"
                    />
                  ) : (
                    <Stat
                      label="Amount"
                      value={money(result.amount_minor ?? 0)}
                      hint={`${duration(result.duration_minutes)} · ${result.payment_status}`}
                      tone={result.payment_status === 'paid' ? 'good' : 'warn'}
                    />
                  )}
                  <Stat
                    label="Pipeline time"
                    value={`${result.processing_ms.toFixed(0)} ms`}
                    hint={
                      result.allocation
                        ? `Allocation ${result.allocation.latency_ms.toFixed(2)} ms over ${result.allocation.considered} candidates`
                        : result.invoice_no ?? undefined
                    }
                  />
                </div>

                {result.allocation && (
                  <p className="mt-4 rounded-lg border border-ink-800 bg-ink-950/50 px-4 py-3 text-sm text-ink-200">
                    <span className="font-semibold text-ink-50">
                      {result.allocation.strategy}
                    </span>{' '}
                    — {result.allocation.reason}
                    {result.allocation.contention_retries > 0 && (
                      <span className="text-ink-400">
                        {' '}· {result.allocation.contention_retries} bay(s) claimed by another
                        vehicle first
                      </span>
                    )}
                  </p>
                )}

                {result.quote && (
                  <div className="mt-4 rounded-lg border border-ink-800 bg-ink-950/50 p-4">
                    <p className="text-sm text-ink-200">{result.quote.explanation}</p>
                    <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                      {[
                        ['Billable', `${result.quote.billable_minutes} min`],
                        ['Subtotal', money(result.quote.subtotal_minor)],
                        ['Tax', money(result.quote.tax_minor)],
                        ['Total', money(result.quote.total_minor)],
                      ].map(([label, value]) => (
                        <div key={label}>
                          <p className="text-ink-500">{label}</p>
                          <p className="tabular font-semibold text-ink-100">{value}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {result.route?.instructions?.length ? (
                  <ol className="mt-4 space-y-1.5">
                    {result.route.instructions.map((step, index) => (
                      <li key={index} className="text-sm text-ink-300">
                        <span className="mr-2 text-ink-500">{index + 1}.</span>{step}
                      </li>
                    ))}
                  </ol>
                ) : null}

                {result.anomalies.length > 0 && (
                  <div className="mt-4 space-y-2">
                    {result.anomalies.map((anomaly, index) => (
                      <div
                        key={index}
                        className="rounded-lg border border-signal-amber/40 bg-signal-amber/10 px-3 py-2"
                      >
                        <Badge tone={anomaly.severity === 'high' ? 'bad' : 'warn'}>
                          {anomaly.kind.replace(/_/g, ' ')}
                        </Badge>
                        <p className="mt-1 text-xs text-ink-200">{anomaly.summary}</p>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            </>
          )}

          {trace && (
            <Card
              title="Recognition trace"
              subtitle={`${trace.detections} region(s) detected · ${trace.reads} OCR read(s) · ${trace.backend}${trace.escalated ? ' · escalated to the vision model' : ''}`}
            >
              {trace.candidates.length === 0 ? (
                <Empty title="No candidates were produced." hint={trace.error ?? undefined} />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px]">
                    <thead className="border-b border-ink-800">
                      <tr>
                        <th className="th">Candidate</th>
                        <th className="th">Format</th>
                        <th className="th text-right">Score</th>
                        <th className="th text-right">Confidence</th>
                        <th className="th">Votes</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trace.candidates.map((candidate, index) => (
                        <tr key={candidate.plate} className="table-row">
                          <td className="td font-mono">
                            {candidate.plate}
                            {index === 0 && <Badge tone="good" className="ml-2">chosen</Badge>}
                            {candidate.matched_known && (
                              <Badge tone="info" className="ml-2">context match</Badge>
                            )}
                          </td>
                          <td className="td text-ink-400">{candidate.format.replace('in_', '')}</td>
                          <td className="td tabular text-right">{candidate.score.toFixed(3)}</td>
                          <td className="td tabular text-right">
                            {(candidate.confidence * 100).toFixed(0)}%
                          </td>
                          <td className="td text-[11px] text-ink-500">
                            {candidate.votes.slice(0, 3).join(' · ')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          )}

          {!result && !recognition && !busy && (
            <Card>
              <Empty
                title="No scan run yet."
                hint="Generate a synthetic gate frame, upload a photo, or type a plate to drive the full pipeline."
              />
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
