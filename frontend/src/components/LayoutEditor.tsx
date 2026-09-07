/**
 * Canvas layout builder.
 *
 * The owner draws their facility: drag to create a bay, click to select, drag to
 * move, handle to resize, keyboard to nudge and delete. Coordinates are metres
 * throughout — the editor only ever converts to pixels for display.
 *
 * Editing is local until "Save layout", which sends the whole level in one
 * request. That is what "save my drawing" means to the person drawing it, and it
 * lets the server reject a layout that would orphan a parked vehicle.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Level, Slot, SlotType } from '../lib/types'
import { SLOT_TYPE_LABEL, STATUS_COLOR } from '../lib/format'
import { Badge } from './ui'

export interface DraftSlot {
  id?: number
  code: string
  zone: string
  x: number
  y: number
  width: number
  height: number
  rotation: number
  slot_type: SlotType
  has_ev_charger: boolean
  price_multiplier: number
  is_active: boolean
  status?: string
}

const DEFAULT_W = 2.5
const DEFAULT_H = 5.0
const GRID = 0.5           // snap step, in metres
const MIN_DIM = 1.2

export function toDraft(slot: Slot): DraftSlot {
  return {
    id: slot.id, code: slot.code, zone: slot.zone, x: slot.x, y: slot.y,
    width: slot.width, height: slot.height, rotation: slot.rotation,
    slot_type: slot.slot_type, has_ev_charger: slot.has_ev_charger,
    price_multiplier: slot.price_multiplier, is_active: slot.is_active,
    status: slot.status,
  }
}

const snap = (value: number) => Math.round(value / GRID) * GRID

export function LayoutEditor({
  level, slots, onChange, onSave, saving, dirty,
}: {
  level: Level
  slots: DraftSlot[]
  onChange: (slots: DraftSlot[]) => void
  onSave: () => void
  saving: boolean
  dirty: boolean
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [drag, setDrag] = useState<
    | { kind: 'create'; from: { x: number; y: number }; to: { x: number; y: number } }
    | { kind: 'move'; index: number; offset: { x: number; y: number } }
    | { kind: 'resize'; index: number }
    | null
  >(null)
  const [zonePrefix, setZonePrefix] = useState('A')

  const extent = useMemo(() => {
    const xs = [0, level.canvas_width, ...slots.flatMap((s) => [s.x, s.x + s.width])]
    const ys = [0, level.canvas_height, ...slots.flatMap((s) => [s.y, s.y + s.height])]
    return {
      width: Math.max(...xs) + 4,
      height: Math.max(...ys) + 4,
    }
  }, [level, slots])

  /** Pointer position in metres. */
  const toMetres = useCallback((event: React.MouseEvent): { x: number; y: number } => {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const rect = svg.getBoundingClientRect()
    const scale = extent.width / rect.width
    return {
      x: (event.clientX - rect.left) * scale,
      y: (event.clientY - rect.top) * (extent.height / rect.height),
    }
  }, [extent])

  const nextCode = useCallback(() => {
    const used = new Set(slots.map((s) => s.code))
    for (let n = 1; n < 10000; n += 1) {
      const candidate = `${zonePrefix}${String(n).padStart(3, '0')}`
      if (!used.has(candidate)) return candidate
    }
    return `${zonePrefix}${Date.now() % 1000}`
  }, [slots, zonePrefix])

  const onMouseDown = (event: React.MouseEvent) => {
    if (event.button !== 0) return
    const point = toMetres(event)
    const hitIndex = [...slots]
      .map((slot, index) => ({ slot, index }))
      .reverse()
      .find(({ slot }) =>
        point.x >= slot.x && point.x <= slot.x + slot.width &&
        point.y >= slot.y && point.y <= slot.y + slot.height,
      )?.index

    if (hitIndex !== undefined) {
      const slot = slots[hitIndex]
      setSelected(hitIndex)
      // Bottom-right corner is the resize handle.
      const nearCorner =
        point.x > slot.x + slot.width - 0.9 && point.y > slot.y + slot.height - 0.9
      setDrag(
        nearCorner
          ? { kind: 'resize', index: hitIndex }
          : { kind: 'move', index: hitIndex, offset: { x: point.x - slot.x, y: point.y - slot.y } },
      )
      return
    }

    setSelected(null)
    setDrag({ kind: 'create', from: point, to: point })
  }

  const onMouseMove = (event: React.MouseEvent) => {
    if (!drag) return
    const point = toMetres(event)

    if (drag.kind === 'create') {
      setDrag({ ...drag, to: point })
      return
    }
    if (drag.kind === 'move') {
      const next = [...slots]
      next[drag.index] = {
        ...next[drag.index],
        x: Math.max(0, snap(point.x - drag.offset.x)),
        y: Math.max(0, snap(point.y - drag.offset.y)),
      }
      onChange(next)
      return
    }
    const next = [...slots]
    const slot = next[drag.index]
    next[drag.index] = {
      ...slot,
      width: Math.max(MIN_DIM, snap(point.x - slot.x)),
      height: Math.max(MIN_DIM, snap(point.y - slot.y)),
    }
    onChange(next)
  }

  const onMouseUp = () => {
    if (drag?.kind === 'create') {
      const { from, to } = drag
      const width = Math.abs(to.x - from.x)
      const height = Math.abs(to.y - from.y)
      // A click without a drag places a default-sized bay; a drag sizes it.
      const bay: DraftSlot = {
        code: nextCode(),
        zone: zonePrefix,
        x: Math.max(0, snap(width < 1 ? from.x : Math.min(from.x, to.x))),
        y: Math.max(0, snap(height < 1 ? from.y : Math.min(from.y, to.y))),
        width: width < 1 ? DEFAULT_W : Math.max(MIN_DIM, snap(width)),
        height: height < 1 ? DEFAULT_H : Math.max(MIN_DIM, snap(height)),
        rotation: 0,
        slot_type: 'standard',
        has_ev_charger: false,
        price_multiplier: 1,
        is_active: true,
      }
      onChange([...slots, bay])
      setSelected(slots.length)
    }
    setDrag(null)
  }

  const updateSelected = (patch: Partial<DraftSlot>) => {
    if (selected === null) return
    const next = [...slots]
    next[selected] = { ...next[selected], ...patch }
    onChange(next)
  }

  const removeSelected = useCallback(() => {
    if (selected === null) return
    if (slots[selected]?.status === 'occupied') return   // server would reject it anyway
    onChange(slots.filter((_, index) => index !== selected))
    setSelected(null)
  }, [selected, slots, onChange])

  // Keyboard: nudge with arrows, delete with Delete/Backspace.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (selected === null) return
      const target = event.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return

      if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault()
        removeSelected()
        return
      }
      const step = event.shiftKey ? GRID * 4 : GRID
      const moves: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0], ArrowRight: [step, 0],
        ArrowUp: [0, -step], ArrowDown: [0, step],
      }
      const move = moves[event.key]
      if (!move) return
      event.preventDefault()
      const next = [...slots]
      next[selected] = {
        ...next[selected],
        x: Math.max(0, next[selected].x + move[0]),
        y: Math.max(0, next[selected].y + move[1]),
      }
      onChange(next)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [selected, slots, onChange, removeSelected])

  const current = selected !== null ? slots[selected] : null
  const duplicates = useMemo(() => {
    const seen = new Map<string, number>()
    slots.forEach((s) => seen.set(s.code, (seen.get(s.code) ?? 0) + 1))
    return [...seen.entries()].filter(([, count]) => count > 1).map(([code]) => code)
  }, [slots])

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_290px]">
      <div>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-ink-400">Zone prefix</span>
          <input
            value={zonePrefix}
            onChange={(event) => setZonePrefix(event.target.value.toUpperCase().slice(0, 2) || 'A')}
            className="input w-16 py-1 text-center text-xs"
          />
          <span className="text-xs text-ink-500">
            Drag on empty space to draw a bay · click to select · corner handle to resize ·
            arrows to nudge · Delete to remove
          </span>
        </div>

        <svg
          ref={svgRef}
          viewBox={`0 0 ${extent.width} ${extent.height}`}
          width="100%"
          height={480}
          className="cursor-crosshair rounded-lg border border-ink-800 bg-ink-950/70 select-none"
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={onMouseUp}
          onMouseLeave={onMouseUp}
        >
          <defs>
            <pattern id="grid" width={GRID * 4} height={GRID * 4} patternUnits="userSpaceOnUse">
              <path
                d={`M ${GRID * 4} 0 L 0 0 0 ${GRID * 4}`}
                fill="none" stroke="rgba(148,163,184,0.10)" strokeWidth={0.05}
              />
            </pattern>
          </defs>
          <rect width={extent.width} height={extent.height} fill="url(#grid)" />

          {(level.aisles ?? []).map((line, index) => (
            <polyline
              key={`aisle-${index}`}
              points={line.map(([x, y]) => `${x},${y}`).join(' ')}
              fill="none" stroke="rgba(148,163,184,0.2)" strokeWidth={0.6}
              strokeLinecap="round"
            />
          ))}

          {slots.map((slot, index) => {
            const isSelected = index === selected
            const occupied = slot.status === 'occupied'
            return (
              <g key={`${slot.code}-${index}`}>
                <rect
                  x={slot.x} y={slot.y} width={slot.width} height={slot.height}
                  rx={0.25}
                  fill={occupied ? STATUS_COLOR.occupied : slot.has_ev_charger ? '#199e70' : '#3987e5'}
                  fillOpacity={isSelected ? 0.55 : 0.26}
                  stroke={isSelected ? '#ffffff' : occupied ? STATUS_COLOR.occupied : '#3987e5'}
                  strokeWidth={isSelected ? 0.18 : 0.08}
                />
                <text
                  x={slot.x + slot.width / 2} y={slot.y + slot.height / 2}
                  textAnchor="middle" dominantBaseline="central"
                  fontSize={Math.min(slot.width * 0.28, 0.9)}
                  fill="#eceef2" pointerEvents="none"
                >
                  {slot.code}
                </text>
                {isSelected && (
                  <rect
                    x={slot.x + slot.width - 0.55} y={slot.y + slot.height - 0.55}
                    width={0.55} height={0.55} fill="#ffffff"
                    className="cursor-nwse-resize"
                  />
                )}
              </g>
            )
          })}

          {drag?.kind === 'create' && (
            <rect
              x={Math.min(drag.from.x, drag.to.x)}
              y={Math.min(drag.from.y, drag.to.y)}
              width={Math.abs(drag.to.x - drag.from.x)}
              height={Math.abs(drag.to.y - drag.from.y)}
              fill="#3987e5" fillOpacity={0.2}
              stroke="#3987e5" strokeWidth={0.1} strokeDasharray="0.3 0.2"
            />
          )}
        </svg>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3 text-xs text-ink-400">
            <span>{slots.length} bays</span>
            <span>·</span>
            <span>{slots.filter((s) => s.has_ev_charger).length} EV</span>
            {duplicates.length > 0 && (
              <Badge tone="bad">Duplicate codes: {duplicates.join(', ')}</Badge>
            )}
          </div>
          <button
            className="btn-primary"
            disabled={saving || !dirty || duplicates.length > 0}
            onClick={onSave}
          >
            {saving ? 'Saving…' : dirty ? 'Save layout' : 'Saved'}
          </button>
        </div>
      </div>

      <aside className="card card-pad h-fit">
        <h3 className="mb-3 text-sm font-semibold text-ink-50">
          {current ? `Bay ${current.code}` : 'No bay selected'}
        </h3>
        {!current ? (
          <p className="text-xs text-ink-500">
            Select a bay to edit its code, zone, type and pricing — or drag on the canvas to
            draw a new one.
          </p>
        ) : (
          <div className="space-y-3">
            <div>
              <label className="label">Code</label>
              <input
                className="input"
                value={current.code}
                onChange={(event) => updateSelected({ code: event.target.value.toUpperCase() })}
              />
            </div>
            <div>
              <label className="label">Zone</label>
              <input
                className="input"
                value={current.zone}
                onChange={(event) => updateSelected({ zone: event.target.value.toUpperCase() })}
              />
            </div>
            <div>
              <label className="label">Type</label>
              <select
                className="input"
                value={current.slot_type}
                onChange={(event) => updateSelected({ slot_type: event.target.value as SlotType })}
              >
                {Object.entries(SLOT_TYPE_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="label">Width (m)</label>
                <input
                  type="number" step="0.1" className="input" value={current.width}
                  onChange={(e) => updateSelected({ width: Math.max(MIN_DIM, Number(e.target.value)) })}
                />
              </div>
              <div>
                <label className="label">Length (m)</label>
                <input
                  type="number" step="0.1" className="input" value={current.height}
                  onChange={(e) => updateSelected({ height: Math.max(MIN_DIM, Number(e.target.value)) })}
                />
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm text-ink-200">
              <input
                type="checkbox" checked={current.has_ev_charger}
                onChange={(event) => updateSelected({ has_ev_charger: event.target.checked })}
                className="h-4 w-4 rounded border-ink-600 bg-ink-900"
              />
              EV charger fitted
            </label>
            <div>
              <label className="label">Price multiplier</label>
              <input
                type="number" step="0.05" min="0.1" max="5" className="input"
                value={current.price_multiplier}
                onChange={(e) => updateSelected({ price_multiplier: Number(e.target.value) })}
              />
            </div>
            {current.status === 'occupied' ? (
              <p className="rounded-md border border-signal-amber/40 bg-signal-amber/10 px-3 py-2 text-[11px] text-signal-amber">
                A vehicle is parked here. It cannot be deleted until the vehicle leaves.
              </p>
            ) : (
              <button className="btn-danger btn-sm w-full" onClick={removeSelected}>
                Delete bay
              </button>
            )}
          </div>
        )}
      </aside>
    </div>
  )
}
