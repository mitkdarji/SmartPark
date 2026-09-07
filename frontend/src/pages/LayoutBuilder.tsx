/**
 * Digital layout builder.
 *
 * Three ways to map a facility, matching the three phases in the project scope:
 * draw bays by hand on the canvas, generate a regular grid, or upload a clean
 * floor plan and let the detector propose bays for review.
 */

import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { useFacility } from '../hooks/useFacility'
import { Badge, Card, Empty, ErrorNote, Modal, Spinner } from '../components/ui'
import { FacilityPicker } from '../components/FacilityPicker'
import { LayoutEditor, toDraft, toDraftGate } from '../components/LayoutEditor'
import type { Aisle, DraftGate, DraftSlot } from '../components/LayoutEditor'
import type { SlotType } from '../lib/types'

export function LayoutBuilder() {
  const { facilities, facilityId, select } = useFacility()
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

  const [draft, setDraft] = useState<DraftSlot[]>([])
  const [draftAisles, setDraftAisles] = useState<Aisle[]>([])
  const [draftGates, setDraftGates] = useState<DraftGate[]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [gridOpen, setGridOpen] = useState(false)
  const [grid, setGrid] = useState({ rows: 4, columns: 12, zone_prefix: 'A', ev_every: 8 })
  const [detected, setDetected] = useState<{ slots: any[]; note: string } | null>(null)

  useEffect(() => {
    setDraft((slots.data ?? []).map(toDraft))
    setDirty(false)
  }, [slots.data])

  // The canvas holds an unsaved draft in React state — the one place in the app
  // where closing the tab really does lose work. Everything else is a committed
  // database transaction the moment it happens.
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const level = useMemo(
    () => levels.data?.find((l) => l.id === levelId) ?? null,
    [levels.data, levelId],
  )

  useEffect(() => {
    setDraftAisles((level?.aisles ?? []) as Aisle[])
  }, [level])

  useEffect(() => {
    // Gates belong to the facility, but only those on this level (or unassigned)
    // are meaningful on this canvas.
    setDraftGates(
      (gates.data ?? [])
        .filter((g) => !g.level_id || g.level_id === levelId)
        .map(toDraftGate),
    )
  }, [gates.data, levelId])


  const update = (next: DraftSlot[]) => {
    setDraft(next)
    setDirty(true)
  }

  const updateAisles = (next: Aisle[]) => {
    setDraftAisles(next)
    setDirty(true)
  }

  const updateGates = (next: DraftGate[]) => {
    setDraftGates(next)
    setDirty(true)
  }

  const save = async () => {
    if (!facilityId || !levelId) return
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const payload = draft.map((slot) => ({
        id: slot.id, code: slot.code, zone: slot.zone,
        x: slot.x, y: slot.y, width: slot.width, height: slot.height,
        rotation: slot.rotation, slot_type: slot.slot_type,
        has_ev_charger: slot.has_ev_charger,
        price_multiplier: slot.price_multiplier, is_active: slot.is_active,
      }))
      await api.saveLayout(facilityId, levelId, payload)

      // Driveways live on the level; the router follows them.
      await api.updateLevel(facilityId, levelId, {
        name: level?.name ?? 'Ground',
        order_index: level?.order_index ?? 0,
        canvas_width: level?.canvas_width ?? 60,
        canvas_height: level?.canvas_height ?? 40,
        aisles: draftAisles,
      })

      // Gates: create, update or delete to match the canvas.
      const existing = (gates.data ?? []).filter(
        (g) => !g.level_id || g.level_id === levelId,
      )
      const keptIds = new Set(draftGates.map((g) => g.id).filter(Boolean))
      for (const gate of existing) {
        if (!keptIds.has(gate.id)) await api.deleteGate(facilityId, gate.id)
      }
      for (const gate of draftGates) {
        if (gate.id) {
          await api.updateGate(facilityId, gate.id, {
            name: gate.name, kind: gate.kind, x: gate.x, y: gate.y,
            is_primary: gate.is_primary, level_id: levelId,
          })
        } else {
          await api.createGate(facilityId, {
            name: gate.name, kind: gate.kind, x: gate.x, y: gate.y,
            is_primary: gate.is_primary, level_id: levelId,
          })
        }
      }

      slots.refresh()
      gates.refresh()
      levels.refresh()
      setNotice(
        `Saved ${payload.length} bays, ${draftAisles.length} driveways and ` +
        `${draftGates.length} gates. Every bay's routed distance from the primary ` +
        `gate was recomputed.`,
      )
      setDirty(false)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The layout could not be saved.')
    } finally {
      setSaving(false)
    }
  }

  const generate = async () => {
    if (!facilityId || !levelId) return
    setSaving(true)
    setError(null)
    try {
      await api.generateGrid(facilityId, { level_id: levelId, ...grid })
      slots.refresh()
      setGridOpen(false)
      setNotice('Grid generated with aisles and routed distances.')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The grid could not be generated.')
    } finally {
      setSaving(false)
    }
  }

  const uploadPlan = async (file: File) => {
    if (!facilityId || !levelId) return
    setSaving(true)
    setError(null)
    try {
      const result = await api.uploadFloorplan(facilityId, levelId, file)
      setDetected({ slots: result.detected_slots ?? [], note: result.note ?? '' })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The floor plan could not be processed.')
    } finally {
      setSaving(false)
    }
  }

  const acceptDetected = () => {
    if (!detected) return
    const existing = new Set(draft.map((s) => s.code))
    const additions: DraftSlot[] = detected.slots
      .filter((proposal) => !existing.has(proposal.code))
      .map((proposal) => ({
        code: proposal.code, zone: proposal.zone,
        x: proposal.x, y: proposal.y,
        width: proposal.width, height: proposal.height,
        rotation: 0, slot_type: proposal.slot_type as SlotType,
        has_ev_charger: false, price_multiplier: 1, is_active: true,
      }))
    update([...draft, ...additions])
    setDetected(null)
    setNotice(`${additions.length} proposed bays added to the canvas. Review, then save.`)
  }

  if (facilities.loading && !facilities.data) return <Spinner />
  if (!facilityId) return <Empty title="Create a facility first." />

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Layout builder</h1>
          <p className="text-sm text-ink-400">
            Map the facility once; allocation, wayfinding and the live map all read from it.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <FacilityPicker facilities={facilities.data ?? []} value={facilityId} onChange={select} />
          {levels.data && levels.data.length > 1 && (
            <select
              className="input w-auto py-1.5 text-sm"
              value={levelId ?? ''}
              onChange={(event) => setLevelId(Number(event.target.value))}
            >
              {levels.data.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          )}
          <button className="btn-ghost btn-sm" onClick={() => setGridOpen(true)}>
            Generate grid
          </button>
          <label className="btn-ghost btn-sm cursor-pointer">
            Upload floor plan
            <input
              type="file" accept="image/*" className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void uploadPlan(file)
              }}
            />
          </label>
        </div>
      </div>

      {error && <ErrorNote message={error} />}
      {notice && (
        <div className="rounded-lg border border-brand-500/40 bg-brand-500/10 px-4 py-2.5 text-sm text-brand-200">
          {notice}
        </div>
      )}

      <Card
        title={
          <span className="flex items-center gap-2">
            {level ? `${level.name} — ${draft.length} bays` : 'Layout'}
            {dirty && <Badge tone="warn">Unsaved changes</Badge>}
          </span>
        }
        subtitle="Coordinates are in metres. Bays, driveways and gates all save together."
        action={
          <div className="flex items-center gap-2">
            {gates.data?.map((gate) => (
              <Badge key={gate.id} tone={gate.kind === 'exit' ? 'warn' : 'good'}>
                {gate.name}
              </Badge>
            ))}
          </div>
        }
      >
        {slots.loading && !slots.data ? (
          <Spinner />
        ) : level ? (
          <LayoutEditor
            level={level}
            slots={draft}
            aisles={draftAisles}
            gates={draftGates}
            onSlotsChange={update}
            onAislesChange={updateAisles}
            onGatesChange={updateGates}
            onSave={save}
            saving={saving}
            dirty={dirty}
          />
        ) : (
          <Empty title="This facility has no levels yet." />
        )}
      </Card>

      <Modal open={gridOpen} title="Generate a regular grid" onClose={() => setGridOpen(false)}>
        <div className="space-y-3">
          <p className="text-xs text-ink-400">
            Lays out back-to-back rows with a drive aisle between each pair, then recomputes every
            bay's routed distance from the entry gate. Existing bay codes are never overwritten.
          </p>
          <div className="grid grid-cols-2 gap-3">
            {([
              ['rows', 'Rows', 1, 40],
              ['columns', 'Bays per row', 1, 60],
            ] as const).map(([key, label, min, max]) => (
              <div key={key}>
                <label className="label">{label}</label>
                <input
                  type="number" min={min} max={max} className="input"
                  value={grid[key]}
                  onChange={(event) =>
                    setGrid((prev) => ({ ...prev, [key]: Number(event.target.value) }))
                  }
                />
              </div>
            ))}
            <div>
              <label className="label">Zone prefix</label>
              <input
                className="input"
                value={grid.zone_prefix}
                onChange={(event) =>
                  setGrid((prev) => ({ ...prev, zone_prefix: event.target.value.toUpperCase() }))
                }
              />
            </div>
            <div>
              <label className="label">EV bay every N</label>
              <input
                type="number" min={0} max={50} className="input"
                value={grid.ev_every}
                onChange={(event) =>
                  setGrid((prev) => ({ ...prev, ev_every: Number(event.target.value) }))
                }
              />
            </div>
          </div>
          <button className="btn-primary w-full" onClick={generate} disabled={saving}>
            {saving ? 'Generating…' : `Generate ${grid.rows * grid.columns} bays`}
          </button>
        </div>
      </Modal>

      <Modal
        open={!!detected}
        title="Bays detected in the floor plan"
        onClose={() => setDetected(null)}
        wide
      >
        {detected && (
          <div className="space-y-3">
            <p className="text-sm text-ink-300">{detected.note}</p>
            {detected.slots.length > 0 ? (
              <>
                <div className="max-h-64 overflow-y-auto rounded-lg border border-ink-800">
                  <table className="w-full">
                    <thead className="sticky top-0 bg-ink-900">
                      <tr>
                        <th className="th">Code</th><th className="th">Zone</th>
                        <th className="th text-right">x</th><th className="th text-right">y</th>
                        <th className="th text-right">w × h</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detected.slots.slice(0, 100).map((proposal) => (
                        <tr key={proposal.code} className="table-row">
                          <td className="td font-mono">{proposal.code}</td>
                          <td className="td">{proposal.zone}</td>
                          <td className="td tabular text-right">{proposal.x}</td>
                          <td className="td tabular text-right">{proposal.y}</td>
                          <td className="td tabular text-right">
                            {proposal.width} × {proposal.height}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="text-xs text-ink-500">
                  These are proposals in the plan's own pixel coordinates. Add them to the canvas,
                  then reposition and rename before saving — nothing is written to the database
                  until you press Save layout.
                </p>
                <div className="flex gap-2">
                  <button className="btn-primary" onClick={acceptDetected}>
                    Add {detected.slots.length} bays to the canvas
                  </button>
                  <button className="btn-ghost" onClick={() => setDetected(null)}>Discard</button>
                </div>
              </>
            ) : (
              <Empty
                title="Nothing detected."
                hint="This detector targets clean line-drawing plans, not photographs or dense CAD exports. Draw the layout by hand instead."
              />
            )}
          </div>
        )}
      </Modal>
    </div>
  )
}
