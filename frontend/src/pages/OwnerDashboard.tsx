/** Operator dashboard: live occupancy, KPIs, the event stream and the copilot. */

import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { useAsync, useInterval } from '../hooks/useAsync'
import { useFacility } from '../hooks/useFacility'
import { subscribe } from '../lib/ws'
import { Badge, Card, Empty, ErrorNote, Spinner, Stat } from '../components/ui'
import { FacilityPicker } from '../components/FacilityPicker'
import { ParkingMap } from '../components/ParkingMap'
import { VoiceAssistant } from '../components/VoiceAssistant'
import { LineChart } from '../components/charts/LineChart'
import { clockTime, compactMoney, duration, money, relativeTime, titleCase } from '../lib/format'
import type { LiveEvent } from '../lib/types'

const TOPIC_TONE: Record<string, 'good' | 'bad' | 'warn' | 'info' | 'violet' | 'neutral'> = {
  'vehicle.entered': 'good',
  'vehicle.exited': 'info',
  'vehicle.entry_denied': 'bad',
  'slot.allocated': 'good',
  'slot.released': 'neutral',
  'facility.occupancy_changed': 'neutral',
  'facility.price_changed': 'warn',
  'security.anomaly_detected': 'bad',
  'session.overstay': 'warn',
  'automation.action': 'violet',
}

export function OwnerDashboard() {
  const { facilities, facility, facilityId, select } = useFacility()
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [connection, setConnection] = useState<'connecting' | 'open' | 'closed'>('connecting')

  const levels = useAsync(async () => (facilityId ? api.levels(facilityId) : []), [facilityId])
  const [levelId, setLevelId] = useState<number | null>(null)
  useEffect(() => {
    if (levels.data?.length && !levels.data.some((l) => l.id === levelId)) {
      setLevelId(levels.data[0].id)
    }
  }, [levels.data, levelId])

  const slots = useAsync(
    async () => (facilityId && levelId ? api.slots(facilityId, levelId) : []),
    [facilityId, levelId],
  )
  const gates = useAsync(async () => (facilityId ? api.gates(facilityId) : []), [facilityId])
  const kpis = useAsync(async () => (facilityId ? api.kpis(facilityId, 7) : null), [facilityId])
  const occupancy = useAsync(
    async () => (facilityId ? api.occupancy(facilityId, 72) : null),
    [facilityId],
  )
  const forecast = useAsync(async () => (facilityId ? api.forecast(facilityId, 8) : null), [facilityId])

  useInterval(() => { facilities.refresh(); slots.refresh() }, 45_000)

  // Live feed. Reconnects on its own; the badge shows the real state.
  useEffect(() => {
    if (!facilityId) return
    const subscription = subscribe(
      `facility/${facilityId}`,
      (event) => {
        setEvents((prev) => [event, ...prev].slice(0, 60))
        if (['slot.allocated', 'slot.released', 'slot.state_changed'].includes(event.topic)) {
          slots.refresh()
          facilities.refresh()
        }
      },
      setConnection,
    )
    return () => subscription.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [facilityId])

  if (facilities.loading && !facilities.data) return <Spinner label="Loading your facilities…" />
  if (facilities.error) return <ErrorNote message={facilities.error} onRetry={facilities.refresh} />
  if (!facility) {
    return (
      <Empty
        title="No facilities yet."
        hint="Create a facility to start mapping bays and taking vehicles."
      />
    )
  }

  const occupancySeries = (occupancy.data?.points ?? []).map((point, index) => ({
    x: index,
    y: point.occupancy_pct * 100,
    ts: point.ts,
  }))
  const forecastPoints = forecast.data?.points ?? []
  const forecastSeries = forecastPoints.map((point, index) => ({
    x: occupancySeries.length - 1 + index + 1,
    y: point.occupancy_pct * 100,
  }))
  const forecastBand = forecastPoints.map((point, index) => {
    // Confidence shrinks with the horizon; show that as a widening band.
    const spread = (1 - point.confidence) * 45
    return {
      x: occupancySeries.length - 1 + index + 1,
      lo: Math.max(0, point.occupancy_pct * 100 - spread),
      hi: Math.min(100, point.occupancy_pct * 100 + spread),
    }
  })

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">{facility.name}</h1>
          <p className="text-sm text-ink-400">
            {facility.address || facility.city} · {facility.access_mode} ·{' '}
            {titleCase(facility.allocation_strategy)} allocation
          </p>
        </div>
        <div className="flex items-center gap-3">
          <FacilityPicker facilities={facilities.data ?? []} value={facilityId} onChange={select} />
          <Badge tone={connection === 'open' ? 'good' : connection === 'connecting' ? 'warn' : 'bad'}>
            {connection === 'open' ? 'Live' : connection}
          </Badge>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Occupancy"
          value={`${(facility.occupancy_pct ?? 0).toFixed(0)}%`}
          hint={`${facility.occupied ?? 0} of ${facility.capacity ?? 0} bays in use`}
          tone={(facility.occupancy_pct ?? 0) > 90 ? 'warn' : 'good'}
        />
        <Stat
          label="Current rate"
          value={money(facility.effective_rate_minor ?? 0, facility.currency)}
          hint={
            (facility.effective_rate_minor ?? 0) > facility.base_rate_minor_per_hour
              ? `Demand pricing · base ${money(facility.base_rate_minor_per_hour)}`
              : 'At base rate'
          }
          tone={
            (facility.effective_rate_minor ?? 0) > facility.base_rate_minor_per_hour
              ? 'warn' : 'default'
          }
        />
        <Stat
          label="Revenue · 7 days"
          value={kpis.data ? compactMoney(kpis.data.revenue_minor, kpis.data.currency) : '—'}
          hint={kpis.data ? `${kpis.data.sessions} sessions · ${money(kpis.data.avg_ticket_minor)} average` : undefined}
        />
        <Stat
          label="Open anomalies"
          value={kpis.data?.open_anomalies ?? '—'}
          hint={kpis.data ? `ANPR review rate ${(kpis.data.anpr_review_rate * 100).toFixed(1)}%` : undefined}
          tone={(kpis.data?.open_anomalies ?? 0) > 20 ? 'bad' : (kpis.data?.open_anomalies ?? 0) > 0 ? 'warn' : 'good'}
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.6fr_1fr]">
        <div className="space-y-5">
          <Card
            title="Live occupancy map"
            subtitle={`${facility.free ?? 0} bays free right now`}
            action={
              levels.data && levels.data.length > 1 ? (
                <select
                  className="input w-auto py-1 text-xs"
                  value={levelId ?? ''}
                  onChange={(event) => setLevelId(Number(event.target.value))}
                >
                  {levels.data.map((level) => (
                    <option key={level.id} value={level.id}>{level.name}</option>
                  ))}
                </select>
              ) : null
            }
          >
            {slots.loading && !slots.data ? (
              <Spinner />
            ) : (
              <ParkingMap
                level={levels.data?.find((l) => l.id === levelId) ?? null}
                slots={slots.data ?? []}
                gates={(gates.data ?? []).filter((g) => !g.level_id || g.level_id === levelId)}
                height={400}
              />
            )}
          </Card>

          <Card
            title="Occupancy — last 72 hours and the next 8"
            subtitle={
              forecast.data
                ? `Forecast from the ${forecast.data.model.replace(/_/g, ' ')} model. ${forecast.data.note}`
                : undefined
            }
          >
            <LineChart
              height={240}
              yMin={0}
              yMax={100}
              yFormat={(value) => `${value.toFixed(0)}%`}
              xFormat={(value) => {
                const point = occupancySeries[Math.round(value)]
                return point ? clockTime(point.ts).slice(0, 5) : 'forecast'
              }}
              series={[
                { name: 'Measured', points: occupancySeries, area: true },
                {
                  name: 'Forecast',
                  points: forecastSeries,
                  dashed: true,
                  band: forecastBand,
                  color: '#d95926',
                },
              ]}
              ariaLabel="Occupancy history and forecast"
            />
          </Card>
        </div>

        <div className="space-y-5">
          <Card
            title="Live event stream"
            subtitle="Every gate, allocation and automation action as it happens"
            pad={false}
          >
            <div className="max-h-[380px] overflow-y-auto">
              {events.length === 0 ? (
                <div className="p-5">
                  <Empty
                    title="Waiting for activity…"
                    hint="Run a scan from the Gate & ANPR page to see events appear here instantly."
                  />
                </div>
              ) : (
                events.map((event) => (
                  <div key={event.id} className="table-row px-4 py-2.5">
                    <div className="flex items-start justify-between gap-2">
                      <Badge tone={TOPIC_TONE[event.topic] ?? 'neutral'}>
                        {event.topic.split('.')[1] ?? event.topic}
                      </Badge>
                      <span className="shrink-0 text-[10px] text-ink-600">
                        {relativeTime(event.ts)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-ink-300">{summarise(event)}</p>
                  </div>
                ))
              )}
            </div>
          </Card>

          <Card title="Operations copilot" subtitle="Grounded in this facility's live data">
            <VoiceAssistant facilityId={facilityId} isOwner compact />
          </Card>
        </div>
      </div>

      {kpis.data && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Average stay" value={duration(kpis.data.avg_stay_minutes)} hint="Last 7 days" />
          <Stat
            label="Average walk"
            value={`${kpis.data.avg_walk_distance_m.toFixed(0)} m`}
            hint="Gate to assigned bay"
          />
          <Stat
            label="Turnover"
            value={`${kpis.data.turnover_per_slot.toFixed(1)}×`}
            hint="Uses per bay over the period"
          />
          <Stat
            label="Wallet settlement"
            value={`${(kpis.data.payment_success_rate * 100).toFixed(1)}%`}
            hint={`${kpis.data.guest_sessions} guest sessions collected at the gate`}
            tone={kpis.data.payment_success_rate > 0.97 ? 'good' : 'warn'}
          />
        </div>
      )}
    </div>
  )
}

function summarise(event: LiveEvent): string {
  const p = event.payload
  switch (event.topic) {
    case 'vehicle.entered':
      return `${p.plate} entered → bay ${p.slot_code} (${(p.confidence * 100).toFixed(0)}% read confidence)`
    case 'vehicle.exited':
      return `${p.plate} left after ${duration(p.duration_minutes)} · ${money(p.amount_minor)} ${p.payment_status}`
    case 'vehicle.entry_denied':
      return `${p.plate} refused — ${p.reason}`
    case 'slot.allocated':
      return `Bay ${p.slot_code} assigned to ${p.plate} — ${p.reason}`
    case 'slot.released':
      return `Bay ${p.slot_code} freed${p.reason ? ` (${p.reason})` : ''}`
    case 'facility.occupancy_changed':
      return `${p.occupied}/${p.capacity} occupied (${p.occupancy_pct}%) · rate ${money(p.effective_rate_minor)}`
    case 'facility.price_changed':
      return `Rate now ${money(p.effective_rate_minor)} (${p.multiplier}× base)`
    case 'security.anomaly_detected':
      return p.summary
    case 'session.overstay':
      return `${p.plate} has been parked ${p.hours} h`
    case 'automation.action':
      return `Rule "${p.rule}" fired → ${(p.actions ?? []).join(', ')}`
    default:
      return JSON.stringify(p).slice(0, 130)
  }
}
