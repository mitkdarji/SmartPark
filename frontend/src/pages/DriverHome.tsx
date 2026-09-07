/** Driver home: the live session, the route to the bay, and the assistant. */

import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { useAsync, useInterval } from '../hooks/useAsync'
import { subscribe } from '../lib/ws'
import { Badge, Card, ErrorNote, Empty, Spinner, Stat } from '../components/ui'
import { ParkingMap } from '../components/ParkingMap'
import { VoiceAssistant } from '../components/VoiceAssistant'
import { dateTime, duration, money, prettyPlate } from '../lib/format'
import type { LiveEvent, Route } from '../lib/types'

export function DriverHome() {
  const session = useAsync(() => api.activeSession(), [])
  const wallet = useAsync(() => api.wallet(), [])
  const vehicles = useAsync(() => api.vehicles(), [])
  const [flash, setFlash] = useState<string | null>(null)

  const facilityId = session.data?.facility_id ?? null
  const levelId = session.data?.navigation_path && 'level_id' in session.data.navigation_path
    ? (session.data.navigation_path as Route).level_id
    : null

  const levels = useAsync(
    async () => (facilityId ? api.levels(facilityId) : []),
    [facilityId],
  )
  const slots = useAsync(
    async () => (facilityId ? api.slots(facilityId, levelId ?? undefined) : []),
    [facilityId, levelId],
  )
  const gates = useAsync(
    async () => (facilityId ? api.gates(facilityId) : []),
    [facilityId],
  )

  // The running cost changes with the clock, so refresh it on a timer.
  useInterval(() => session.refresh(), session.data ? 30_000 : null)

  // A gate event about this driver should update the page immediately.
  useEffect(() => {
    const subscription = subscribe('me', (event: LiveEvent) => {
      if (['slot.allocated', 'vehicle.exited', 'wallet.debited'].includes(event.topic)) {
        session.refresh()
        wallet.refresh()
        setFlash(
          event.topic === 'slot.allocated'
            ? `Bay ${event.payload.slot_code} assigned — follow the route below.`
            : event.topic === 'vehicle.exited'
              ? `Session closed. ${money(event.payload.amount_minor)} charged.`
              : `${money(event.payload.amount_minor)} debited from your wallet.`,
        )
        window.setTimeout(() => setFlash(null), 8000)
      }
    })
    return () => subscription.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const route = useMemo(() => {
    const path = session.data?.navigation_path
    if (!path || !('waypoints' in path)) return null
    return path as Route
  }, [session.data])

  const level = useMemo(
    () => levels.data?.find((l) => l.id === (levelId ?? levels.data?.[0]?.id)) ?? levels.data?.[0] ?? null,
    [levels.data, levelId],
  )

  const mapSlots = useMemo(
    () => (slots.data ?? []).filter((s) => !level || s.level_id === level.id),
    [slots.data, level],
  )

  if (session.loading && !session.data) return <Spinner label="Loading your parking…" />
  if (session.error) {
    // Distinguish "nothing to show" from "we could not find out" — rendering an
    // error as an empty state quietly tells the driver something untrue.
    return <ErrorNote message={session.error} onRetry={session.refresh} />
  }

  return (
    <div className="space-y-5">
      {flash && (
        <div className="animate-fade-up rounded-lg border border-brand-500/40 bg-brand-500/10 px-4 py-3 text-sm text-brand-200">
          {flash}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Wallet balance"
          value={wallet.data ? money(wallet.data.balance_minor, wallet.data.currency) : '—'}
          hint={
            wallet.data?.auto_reload_enabled
              ? `Auto top-up at ${money(wallet.data.auto_reload_threshold_minor)}`
              : 'Auto top-up is off'
          }
          tone={wallet.data && wallet.data.balance_minor < 10_000 ? 'warn' : 'good'}
        />
        <Stat
          label="Status"
          value={session.data ? 'Parked' : 'Not parked'}
          hint={session.data ? session.data.facility_name ?? '' : 'No active session'}
          tone={session.data ? 'good' : 'default'}
        />
        <Stat
          label="Parked for"
          value={session.data ? duration(session.data.duration_minutes) : '—'}
          hint={session.data ? `Since ${dateTime(session.data.entry_at)}` : undefined}
        />
        <Stat
          label="Charge so far"
          value={session.data ? money(session.data.live_amount_minor ?? 0) : '—'}
          hint={session.data ? 'Settled automatically on exit' : undefined}
          tone="default"
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-[1.55fr_1fr]">
        <div className="space-y-5">
          {session.data ? (
            <Card
              title={`Your bay — ${session.data.slot_code ?? '—'}`}
              subtitle={`${session.data.facility_name ?? ''} · zone ${session.data.zone ?? '—'} · ${prettyPlate(session.data.plate)}`}
              action={<Badge tone="good">Active</Badge>}
              pad={false}
            >
              <div className="px-5 pb-5 pt-4">
                {slots.loading ? (
                  <Spinner />
                ) : (
                  <ParkingMap
                    level={level}
                    slots={mapSlots}
                    gates={gates.data ?? []}
                    route={route}
                    highlightSlotId={session.data.slot_id}
                    height={340}
                  />
                )}

                {route?.instructions?.length ? (
                  <div className="mt-4 rounded-lg border border-ink-800 bg-ink-950/50 p-4">
                    <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
                      Directions · {route.distance_m.toFixed(0)} m
                    </p>
                    <ol className="space-y-1.5">
                      {route.instructions.map((step, index) => (
                        <li key={index} className="flex gap-2.5 text-sm text-ink-200">
                          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-ink-800 text-[11px] font-semibold text-ink-300">
                            {index + 1}
                          </span>
                          {step}
                        </li>
                      ))}
                    </ol>
                  </div>
                ) : null}
              </div>
            </Card>
          ) : (
            <Card title="No active parking session">
              <Empty
                title="You are not parked anywhere right now."
                hint="When a camera reads your plate at a SmartPark gate, your bay and route appear here automatically."
                action={
                  <a href="/find" className="btn-primary btn-sm">Find parking nearby</a>
                }
              />
            </Card>
          )}

          <Card title="My vehicles" subtitle="Plates linked to automatic billing">
            {vehicles.error && <ErrorNote message={vehicles.error} onRetry={vehicles.refresh} />}
            {vehicles.data?.length ? (
              <div className="space-y-2">
                {vehicles.data.map((vehicle) => (
                  <div
                    key={vehicle.id}
                    className="flex items-center justify-between rounded-lg border border-ink-800 px-3 py-2.5"
                  >
                    <div>
                      <p className="font-mono text-sm font-semibold tracking-wider text-ink-50">
                        {prettyPlate(vehicle.plate)}
                      </p>
                      <p className="text-xs text-ink-500">
                        {[vehicle.make, vehicle.model, vehicle.color].filter(Boolean).join(' · ') ||
                          vehicle.vehicle_type}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {vehicle.is_ev && <Badge tone="good">EV</Badge>}
                      {vehicle.is_accessible_permit && <Badge tone="info">Accessible</Badge>}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <Empty title="No vehicles registered yet." />
            )}
          </Card>
        </div>

        <Card
          title="SmartPark assistant"
          subtitle="Voice or text — it reads your real session data"
          className="lg:sticky lg:top-20 lg:h-fit"
        >
          <VoiceAssistant facilityId={facilityId} />
        </Card>
      </div>
    </div>
  )
}
