/**
 * Create a facility, and optionally lay out its first deck in the same step.
 *
 * Creating a facility on its own leaves an operator on a dashboard with zero
 * bays — technically correct and practically a dead end, because nothing else
 * in the console does anything until bays exist. So the starter grid is part of
 * this flow rather than a separate errand the operator has to discover.
 */

import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { Facility } from '../lib/types'
import { Field, Modal, Toggle } from './ui'

interface Props {
  open: boolean
  onClose: () => void
  onCreated: (facility: Facility) => void
}

const ACCESS_MODES = [
  { value: 'public', label: 'Public', hint: 'Any vehicle is logged and billed' },
  { value: 'private', label: 'Private', hint: 'Only pre-authorised plates may enter' },
] as const

const STRATEGIES = [
  { value: 'hybrid', label: 'Hybrid — recency + proximity (recommended)' },
  { value: 'recency', label: 'Recency — most recently vacated bay' },
  { value: 'nearest', label: 'Nearest — closest bay to the gate' },
  { value: 'balanced', label: 'Balanced — spread load across zones' },
  { value: 'first_fit', label: 'First fit — lowest bay code' },
  { value: 'random', label: 'Random — control condition' },
]

export function NewFacilityModal({ open, onClose, onCreated }: Props) {
  const [form, setForm] = useState({
    name: '',
    address: '',
    city: '',
    access_mode: 'public' as 'public' | 'private',
    allocation_strategy: 'hybrid',
    base_rate: 40,          // rupees per hour, converted to minor units on submit
    free_minutes: 15,
    tax_percent: 18,
    dynamic_pricing_enabled: true,
  })
  const [grid, setGrid] = useState({ enabled: true, rows: 4, columns: 12, ev_every: 8 })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const reset = () => {
    setForm({
      name: '', address: '', city: '', access_mode: 'public',
      allocation_strategy: 'hybrid', base_rate: 40, free_minutes: 15,
      tax_percent: 18, dynamic_pricing_enabled: true,
    })
    setGrid({ enabled: true, rows: 4, columns: 12, ev_every: 8 })
    setError(null)
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const facility = await api.createFacility({
        name: form.name.trim(),
        address: form.address.trim(),
        city: form.city.trim(),
        access_mode: form.access_mode,
        allocation_strategy: form.allocation_strategy,
        base_rate_minor_per_hour: Math.round(form.base_rate * 100),
        free_minutes: form.free_minutes,
        tax_percent: form.tax_percent,
        dynamic_pricing_enabled: form.dynamic_pricing_enabled,
      })

      // The backend seeds one level and a primary gate; fill the level with bays
      // so the new facility can actually take a vehicle straight away.
      if (grid.enabled) {
        const levels = await api.levels(facility.id)
        if (levels.length) {
          await api.generateGrid(facility.id, {
            level_id: levels[0].id,
            rows: grid.rows,
            columns: grid.columns,
            zone_prefix: 'A',
            ev_every: grid.ev_every,
          })
        }
      }

      reset()
      onCreated(facility)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The facility could not be created.')
    } finally {
      setBusy(false)
    }
  }

  const bays = grid.rows * grid.columns

  return (
    <Modal open={open} title="New parking facility" onClose={onClose} wide>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Facility name">
          <input
            className="input" required autoFocus maxLength={120}
            placeholder="Infocity Central Mall"
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Address">
            <input
              className="input" placeholder="Infocity Road"
              value={form.address}
              onChange={(e) => set('address', e.target.value)}
            />
          </Field>
          <Field label="City">
            <input
              className="input" placeholder="Gandhinagar"
              value={form.city}
              onChange={(e) => set('city', e.target.value)}
            />
          </Field>
        </div>

        <Field
          label="Access mode"
          hint={ACCESS_MODES.find((m) => m.value === form.access_mode)?.hint}
        >
          <div className="flex gap-2">
            {ACCESS_MODES.map((mode) => (
              <button
                key={mode.value}
                type="button"
                onClick={() => set('access_mode', mode.value)}
                className={`flex-1 rounded-lg border px-3 py-2 text-sm font-semibold transition-colors ${
                  form.access_mode === mode.value
                    ? 'border-brand-500 bg-brand-600/20 text-white'
                    : 'border-ink-700 text-ink-300 hover:text-white'
                }`}
              >
                {mode.label}
              </button>
            ))}
          </div>
        </Field>

        <Field
          label="Allocation strategy"
          hint="Changeable later. Compare them yourself in the Evaluation lab."
        >
          <select
            className="input"
            value={form.allocation_strategy}
            onChange={(e) => set('allocation_strategy', e.target.value)}
          >
            {STRATEGIES.map((s) => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
        </Field>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Base rate (₹/hour)">
            <input
              type="number" min={1} max={1000} step={1} className="input"
              value={form.base_rate}
              onChange={(e) => set('base_rate', Number(e.target.value))}
            />
          </Field>
          <Field label="Free period (min)">
            <input
              type="number" min={0} max={240} className="input"
              value={form.free_minutes}
              onChange={(e) => set('free_minutes', Number(e.target.value))}
            />
          </Field>
          <Field label="Tax (%)">
            <input
              type="number" min={0} max={50} step={0.5} className="input"
              value={form.tax_percent}
              onChange={(e) => set('tax_percent', Number(e.target.value))}
            />
          </Field>
        </div>

        <div className="rounded-lg border border-ink-800 p-3">
          <Toggle
            checked={form.dynamic_pricing_enabled}
            onChange={(v) => set('dynamic_pricing_enabled', v)}
            label="Occupancy-based dynamic pricing"
          />
          <p className="mt-1.5 text-[11px] text-ink-500">
            The rate stays flat until the lot is 60% full, then rises to a capped ceiling.
            Every bill shows the full breakdown.
          </p>
        </div>

        <div className="rounded-lg border border-ink-800 p-3">
          <Toggle
            checked={grid.enabled}
            onChange={(v) => setGrid((p) => ({ ...p, enabled: v }))}
            label="Lay out a starter deck now"
          />
          {grid.enabled ? (
            <>
              <div className="mt-3 grid grid-cols-3 gap-3">
                <Field label="Rows">
                  <input
                    type="number" min={1} max={40} className="input"
                    value={grid.rows}
                    onChange={(e) => setGrid((p) => ({ ...p, rows: Number(e.target.value) }))}
                  />
                </Field>
                <Field label="Bays per row">
                  <input
                    type="number" min={1} max={60} className="input"
                    value={grid.columns}
                    onChange={(e) => setGrid((p) => ({ ...p, columns: Number(e.target.value) }))}
                  />
                </Field>
                <Field label="EV bay every">
                  <input
                    type="number" min={0} max={50} className="input"
                    value={grid.ev_every}
                    onChange={(e) => setGrid((p) => ({ ...p, ev_every: Number(e.target.value) }))}
                  />
                </Field>
              </div>
              <p className="mt-1.5 text-[11px] text-ink-500">
                Creates {bays} bays in back-to-back rows with drive aisles between them, and
                computes each bay's routed distance from the entry gate. You can redraw any of
                it in the Layout builder.
              </p>
            </>
          ) : (
            <p className="mt-1.5 text-[11px] text-ink-500">
              The facility will have no bays until you draw them in the Layout builder.
            </p>
          )}
        </div>

        {error && (
          <p className="rounded-md border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-xs text-signal-red">
            {error}
          </p>
        )}

        <div className="flex gap-2 pt-1">
          <button type="submit" className="btn-primary flex-1" disabled={busy || !form.name.trim()}>
            {busy
              ? 'Creating…'
              : grid.enabled
                ? `Create facility with ${bays} bays`
                : 'Create facility'}
          </button>
          <button type="button" className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  )
}
