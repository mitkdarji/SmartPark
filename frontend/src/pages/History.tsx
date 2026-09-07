/** Past parking sessions, with the AI explanation of any bill. */

import { useState } from 'react'
import { api } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { Badge, Card, Empty, ErrorNote, Modal, Spinner } from '../components/ui'
import { dateTime, duration, money, prettyPlate } from '../lib/format'
import type { Session } from '../lib/types'

const PAYMENT_TONE = {
  paid: 'good', waived: 'info', pending: 'warn', failed: 'bad', refunded: 'violet',
} as const

export function History() {
  const sessions = useAsync(() => api.mySessions(50), [])
  const [selected, setSelected] = useState<Session | null>(null)
  const [explanation, setExplanation] = useState<{ text: string; by: string } | null>(null)
  const [explaining, setExplaining] = useState(false)

  const explain = async (session: Session) => {
    setSelected(session)
    setExplanation(null)
    setExplaining(true)
    try {
      const result = await api.explainBill(session.id)
      setExplanation({ text: result.explanation, by: result.generated_by })
    } catch {
      setExplanation({ text: 'The explanation could not be generated.', by: 'error' })
    } finally {
      setExplaining(false)
    }
  }

  return (
    <div className="space-y-5">
      <Card
        title="Parking history"
        subtitle={sessions.data ? `${sessions.data.total} sessions` : undefined}
        pad={false}
      >
        {sessions.error && <div className="p-5"><ErrorNote message={sessions.error} onRetry={sessions.refresh} /></div>}
        {sessions.loading && <div className="p-5"><Spinner /></div>}
        {sessions.data?.items.length === 0 && (
          <Empty title="No parking sessions yet." hint="Your first visit to a SmartPark facility will appear here." />
        )}

        {sessions.data && sessions.data.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px]">
              <thead className="border-b border-ink-800">
                <tr>
                  <th className="th">Facility</th>
                  <th className="th">Vehicle</th>
                  <th className="th">Bay</th>
                  <th className="th">Entered</th>
                  <th className="th">Duration</th>
                  <th className="th text-right">Amount</th>
                  <th className="th">Payment</th>
                  <th className="th" />
                </tr>
              </thead>
              <tbody>
                {sessions.data.items.map((session) => (
                  <tr key={session.id} className="table-row hover:bg-ink-800/30">
                    <td className="td">{session.facility_name ?? `#${session.facility_id}`}</td>
                    <td className="td font-mono text-xs tracking-wider">
                      {prettyPlate(session.plate)}
                    </td>
                    <td className="td">{session.slot_code ?? '—'}</td>
                    <td className="td text-ink-400">{dateTime(session.entry_at)}</td>
                    <td className="td">{duration(session.duration_minutes)}</td>
                    <td className="td tabular text-right font-semibold">
                      {money(session.total_minor, session.currency)}
                    </td>
                    <td className="td">
                      <Badge tone={PAYMENT_TONE[session.payment_status] ?? 'neutral'}>
                        {session.payment_status}
                      </Badge>
                    </td>
                    <td className="td text-right">
                      <button className="btn-ghost btn-sm" onClick={() => void explain(session)}>
                        Explain
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Modal
        open={!!selected}
        title={selected ? `Invoice ${selected.invoice_no ?? selected.id}` : ''}
        onClose={() => { setSelected(null); setExplanation(null) }}
      >
        {selected && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 text-sm">
              {[
                ['Vehicle', prettyPlate(selected.plate)],
                ['Bay', `${selected.slot_code ?? '—'} (zone ${selected.zone ?? '—'})`],
                ['Entered', dateTime(selected.entry_at)],
                ['Left', dateTime(selected.exit_at)],
                ['Billable time', `${selected.billable_minutes} min`],
                ['Walk from gate', `${selected.walk_distance_m.toFixed(0)} m`],
              ].map(([label, value]) => (
                <div key={label}>
                  <p className="text-[11px] uppercase tracking-wider text-ink-500">{label}</p>
                  <p className="text-ink-100">{value}</p>
                </div>
              ))}
            </div>

            <div className="rounded-lg border border-ink-800 p-4">
              {[
                ['Subtotal', selected.subtotal_minor],
                ['Discount', -selected.discount_minor],
                ['Tax', selected.tax_minor],
              ].map(([label, value]) => (
                <div key={label as string} className="flex justify-between py-1 text-sm">
                  <span className="text-ink-400">{label}</span>
                  <span className="tabular">{money(value as number, selected.currency)}</span>
                </div>
              ))}
              <div className="mt-1 flex justify-between border-t border-ink-800 pt-2 text-base font-semibold">
                <span>Total</span>
                <span className="tabular">{money(selected.total_minor, selected.currency)}</span>
              </div>
            </div>

            <div className="rounded-lg border border-ink-800 bg-ink-950/60 p-4">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-ink-500">
                Why this amount
              </p>
              {explaining ? (
                <Spinner label="Generating…" />
              ) : (
                <>
                  <p className="text-sm leading-relaxed text-ink-200">
                    {explanation?.text ?? selected.rate_breakdown?.explanation ?? '—'}
                  </p>
                  {explanation && (
                    <p className="mt-2 text-[11px] text-ink-600">
                      Generated by {explanation.by}
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
