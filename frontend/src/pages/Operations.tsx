/** Operations: live sessions, the recognition audit trail, anomalies, briefing. */

import { useState } from 'react'
import { api } from '../lib/api'
import { useAsync, useInterval } from '../hooks/useAsync'
import { useFacility } from '../hooks/useFacility'
import { Badge, Card, Empty, ErrorNote, Segmented, Spinner } from '../components/ui'
import { FacilityPicker } from '../components/FacilityPicker'
import { dateTime, duration, money, prettyPlate, relativeTime } from '../lib/format'

type Tab = 'sessions' | 'recognitions' | 'anomalies'

export function Operations() {
  const { facilities, facilityId, select } = useFacility()
  const [tab, setTab] = useState<Tab>('sessions')
  const [statusFilter, setStatusFilter] = useState('active')
  const [reviewOnly, setReviewOnly] = useState(false)

  const sessions = useAsync(
    async () =>
      facilityId
        ? api.facilitySessions(facilityId, { status: statusFilter || undefined, limit: 100 })
        : null,
    [facilityId, statusFilter],
  )
  const recognitions = useAsync(
    async () =>
      facilityId
        ? api.recognitions(facilityId, { needs_review: reviewOnly || undefined, limit: 80 })
        : [],
    [facilityId, reviewOnly],
  )
  const anomalies = useAsync(
    async () => (facilityId ? api.anomalies(facilityId, false) : []),
    [facilityId],
  )
  const briefing = useAsync(async () => (facilityId ? api.briefing(facilityId, 7) : null), [facilityId])
  const triage = useAsync(async () => (facilityId ? api.triage(facilityId) : null), [facilityId])

  useInterval(() => { sessions.refresh(); anomalies.refresh() }, 30_000)

  const resolve = async (anomalyId: number) => {
    if (!facilityId) return
    await api.resolveAnomaly(facilityId, anomalyId, 'Reviewed from the operations console')
    anomalies.refresh()
    triage.refresh()
  }

  if (facilities.loading && !facilities.data) return <Spinner />
  if (!facilityId) return <Empty title="Create a facility first." />

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Operations</h1>
          <p className="text-sm text-ink-400">
            Who is parked, what the cameras read, and what needs a human.
          </p>
        </div>
        <FacilityPicker facilities={facilities.data ?? []} value={facilityId} onChange={select} />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1fr_360px]">
        <Card
          pad={false}
          title={
            <Segmented
              value={tab}
              onChange={setTab}
              options={[
                { value: 'sessions', label: `Sessions${sessions.data ? ` (${sessions.data.total})` : ''}` },
                { value: 'recognitions', label: 'ANPR log' },
                { value: 'anomalies', label: `Anomalies${anomalies.data ? ` (${anomalies.data.length})` : ''}` },
              ]}
            />
          }
          action={
            tab === 'sessions' ? (
              <select
                className="input w-auto py-1 text-xs"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
              >
                <option value="active">Active</option>
                <option value="completed">Completed</option>
                <option value="denied">Denied</option>
                <option value="">All</option>
              </select>
            ) : tab === 'recognitions' ? (
              <label className="flex items-center gap-2 text-xs text-ink-300">
                <input
                  type="checkbox" checked={reviewOnly}
                  onChange={(event) => setReviewOnly(event.target.checked)}
                  className="h-3.5 w-3.5 rounded border-ink-600 bg-ink-900"
                />
                Needs review only
              </label>
            ) : null
          }
        >
          <div className="max-h-[640px] overflow-auto">
            {tab === 'sessions' && (
              <>
                {sessions.error && <div className="p-5"><ErrorNote message={sessions.error} onRetry={sessions.refresh} /></div>}
                {sessions.loading && !sessions.data && <div className="p-5"><Spinner /></div>}
                {sessions.data?.items.length === 0 && (
                  <div className="p-5"><Empty title="No sessions match this filter." /></div>
                )}
                {sessions.data && sessions.data.items.length > 0 && (
                  <table className="w-full min-w-[720px]">
                    <thead className="sticky top-0 border-b border-ink-800 bg-ink-900">
                      <tr>
                        <th className="th">Vehicle</th><th className="th">Bay</th>
                        <th className="th">Entered</th><th className="th">Duration</th>
                        <th className="th">Strategy</th><th className="th text-right">Amount</th>
                        <th className="th">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sessions.data.items.map((session) => (
                        <tr key={session.id} className="table-row hover:bg-ink-800/30">
                          <td className="td font-mono text-xs tracking-wider">
                            {prettyPlate(session.plate)}
                            {session.entry_confidence < 0.7 && (
                              <Badge tone="warn" className="ml-2">low read</Badge>
                            )}
                          </td>
                          <td className="td">{session.slot_code ?? '—'}</td>
                          <td className="td text-ink-400">{dateTime(session.entry_at)}</td>
                          <td className="td">{duration(session.duration_minutes)}</td>
                          <td className="td text-xs text-ink-400">
                            {session.allocation_strategy ?? '—'}
                          </td>
                          <td className="td tabular text-right">
                            {session.status === 'active'
                              ? money(session.live_amount_minor ?? 0)
                              : money(session.total_minor)}
                          </td>
                          <td className="td">
                            <Badge
                              tone={
                                session.status === 'active' ? 'good'
                                  : session.status === 'denied' ? 'bad'
                                  : session.payment_status === 'failed' ? 'bad' : 'neutral'
                              }
                            >
                              {session.status === 'active' ? 'parked' : session.payment_status}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}

            {tab === 'recognitions' && (
              <>
                {recognitions.loading && !recognitions.data && <div className="p-5"><Spinner /></div>}
                {recognitions.data?.length === 0 && (
                  <div className="p-5"><Empty title="No recognition events recorded." /></div>
                )}
                {recognitions.data && recognitions.data.length > 0 && (
                  <table className="w-full min-w-[700px]">
                    <thead className="sticky top-0 border-b border-ink-800 bg-ink-900">
                      <tr>
                        <th className="th">Plate</th><th className="th">Direction</th>
                        <th className="th text-right">Confidence</th><th className="th">Backend</th>
                        <th className="th text-right">Time</th><th className="th">Decision</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recognitions.data.map((event) => (
                        <tr key={event.id} className="table-row">
                          <td className="td font-mono text-xs tracking-wider">
                            {prettyPlate(event.plate_normalized)}
                            {event.corrected_plate && (
                              <span className="ml-2 text-[11px] text-brand-400">
                                → {prettyPlate(event.corrected_plate)}
                              </span>
                            )}
                          </td>
                          <td className="td text-ink-400">{event.direction}</td>
                          <td className="td tabular text-right">
                            <span
                              className={
                                event.confidence < 0.6 ? 'text-signal-red'
                                  : event.confidence < 0.8 ? 'text-signal-amber' : 'text-brand-400'
                              }
                            >
                              {(event.confidence * 100).toFixed(0)}%
                            </span>
                          </td>
                          <td className="td text-xs text-ink-400">{event.backend}</td>
                          <td className="td tabular text-right text-xs text-ink-500">
                            {event.processing_ms.toFixed(0)} ms
                          </td>
                          <td className="td">
                            {event.needs_review ? (
                              <Badge tone="warn">review</Badge>
                            ) : (
                              <span className="text-xs text-ink-400">{event.decision}</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}

            {tab === 'anomalies' && (
              <div className="divide-y divide-ink-800">
                {anomalies.loading && !anomalies.data && <div className="p-5"><Spinner /></div>}
                {anomalies.data?.length === 0 && (
                  <div className="p-5">
                    <Empty title="Nothing needs attention." hint="No unresolved anomalies at this facility." />
                  </div>
                )}
                {anomalies.data?.map((anomaly) => (
                  <div key={anomaly.id} className="flex items-start gap-3 px-5 py-3.5">
                    <Badge
                      tone={
                        anomaly.severity === 'critical' || anomaly.severity === 'high' ? 'bad'
                          : anomaly.severity === 'medium' ? 'warn' : 'neutral'
                      }
                    >
                      {anomaly.severity}
                    </Badge>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-ink-100">{anomaly.summary}</p>
                      <p className="mt-0.5 text-[11px] text-ink-500">
                        {anomaly.kind.replace(/_/g, ' ')} · {relativeTime(anomaly.created_at)}
                        {anomaly.session_id ? ` · session #${anomaly.session_id}` : ''}
                      </p>
                    </div>
                    <button className="btn-ghost btn-sm" onClick={() => void resolve(anomaly.id)}>
                      Resolve
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>

        <div className="space-y-5">
          <Card
            title="Operations briefing"
            subtitle={briefing.data ? `Generated by ${briefing.data.generated_by}` : undefined}
            action={<button className="btn-ghost btn-sm" onClick={briefing.refresh}>Refresh</button>}
          >
            {briefing.loading && !briefing.data ? (
              <Spinner label="Writing the briefing…" />
            ) : briefing.data ? (
              <div className="space-y-3">
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-200">
                  {briefing.data.body}
                </p>
                {briefing.data.highlights.length > 0 && (
                  <ul className="space-y-1.5 border-t border-ink-800 pt-3">
                    {briefing.data.highlights.map((highlight, index) => (
                      <li key={index} className="flex gap-2 text-xs text-ink-300">
                        <span className="text-brand-500">▸</span>
                        {highlight}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <Empty title="No briefing available." />
            )}
          </Card>

          <Card title="Anomaly triage" subtitle="Grouped, with the first thing to deal with">
            {triage.loading && !triage.data ? (
              <Spinner />
            ) : triage.data ? (
              <div className="space-y-3">
                <p className="text-sm text-ink-200">{triage.data.summary}</p>
                {(triage.data.groups ?? []).map((group: any) => (
                  <div
                    key={group.kind}
                    className="rounded-lg border border-ink-800 px-3 py-2"
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-ink-100">
                        {group.kind.replace(/_/g, ' ')}
                      </span>
                      <Badge tone={group.worst_severity === 'high' ? 'bad' : 'warn'}>
                        {group.count}
                      </Badge>
                    </div>
                    {group.examples?.[0] && (
                      <p className="mt-1 text-[11px] text-ink-500">{group.examples[0]}</p>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <Empty title="Nothing to triage." />
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}
