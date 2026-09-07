/**
 * Canvas layout builder.
 *
 * Four tools, because a car park is four kinds of thing:
 *   Select  click / drag / resize anything already drawn
 *   Bay     drag out a parking bay
 *   Aisle   click a path of points to draw a driveway, Enter or double-click to finish
 *   Gate    click to drop an entry or exit point
 *
 * Coordinates are metres throughout; the editor converts to pixels only for
 * display. Editing is local until Save, which persists bays, aisles and gates
 * together — that is what "save my drawing" means to the person drawing it.
 *
 * Aisles are not decoration: the wayfinder routes along them with Dijkstra, and
 * every bay's stored distance from the entry gate is measured along that graph.
 * Moving a gate or redrawing an aisle re-ranks which bay the allocator picks.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Gate, Level, Slot, SlotType } from '../lib/types'
import { SLOT_TYPE_LABEL, STATUS_COLOR } from '../lib/format'
import { Badge } from './ui'
import {
  AISLE_TOLERANCE, DEFAULT_H, DEFAULT_W, GATE_RADIUS, GRID, MIN_DIM,
  distToPolyline, handleSize, inRect, snap,
} from './layout/geometry'
import type { Point } from './layout/geometry'

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

export interface DraftGate {
  id?: number
  name: string
  kind: 'entry' | 'exit' | 'bidirectional'
  x: number
  y: number
  is_primary: boolean
  is_active: boolean
}

export type Aisle = number[][]

export type Tool = 'select' | 'bay' | 'aisle' | 'gate'

type Selection =
  | { kind: 'slot'; index: number }
  | { kind: 'gate'; index: number }
  | { kind: 'aisle'; index: number }
  | null

export function toDraft(slot: Slot): DraftSlot {
  return {
    id: slot.id, code: slot.code, zone: slot.zone, x: slot.x, y: slot.y,
    width: slot.width, height: slot.height, rotation: slot.rotation,
    slot_type: slot.slot_type, has_ev_charger: slot.has_ev_charger,
    price_multiplier: slot.price_multiplier, is_active: slot.is_active,
    status: slot.status,
  }
}

export function toDraftGate(gate: Gate): DraftGate {
  return {
    id: gate.id, name: gate.name, kind: gate.kind,
    x: gate.x, y: gate.y, is_primary: gate.is_primary, is_active: gate.is_active,
  }
}

const GATE_COLOR = { entry: '#199e70', exit: '#d95926', bidirectional: '#3987e5' }

const TOOLS: { id: Tool; label: string; key: string; hint: string }[] = [
  { id: 'select', label: 'Select', key: 'V', hint: 'Click to select · drag to move · corner handle to resize · arrows to nudge · Delete to remove' },
  { id: 'bay', label: 'Bay', key: 'B', hint: 'Drag to draw a bay, or click to drop a standard 2.5 x 5 m one' },
  { id: 'aisle', label: 'Driveway', key: 'A', hint: 'Click each corner of the driving lane · Enter or double-click to finish · Esc to cancel' },
  { id: 'gate', label: 'Gate', key: 'G', hint: 'Click to place an entry or exit point. The primary gate is where every bay distance is measured from' },
]

interface Props {
  level: Level
  slots: DraftSlot[]
  aisles: Aisle[]
  gates: DraftGate[]
  onSlotsChange: (slots: DraftSlot[]) => void
  onAislesChange: (aisles: Aisle[]) => void
  onGatesChange: (gates: DraftGate[]) => void
  onSave: () => void
  saving: boolean
  dirty: boolean
}

export function LayoutEditor({
  level, slots, aisles, gates,
  onSlotsChange, onAislesChange, onGatesChange, onSave, saving, dirty,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [tool, setTool] = useState<Tool>('select')
  const [selected, setSelected] = useState<Selection>(null)
  const [zonePrefix, setZonePrefix] = useState('A')
  const [pending, setPending] = useState<Point[]>([])       // aisle being drawn
  const [cursor, setCursor] = useState<Point | null>(null)
  const [drag, setDrag] = useState<
    | { kind: 'create'; from: Point; to: Point }
    | { kind: 'move-slot'; index: number; offset: Point }
    | { kind: 'resize-slot'; index: number }
    | { kind: 'move-gate'; index: number }
    | null
  >(null)

  const extent = useMemo(() => {
    const xs = [0, level.canvas_width, ...slots.flatMap((s) => [s.x, s.x + s.width])]
    const ys = [0, level.canvas_height, ...slots.flatMap((s) => [s.y, s.y + s.height])]
    for (const g of gates) { xs.push(g.x + 3); ys.push(g.y + 3) }
    for (const line of aisles) for (const [x, y] of line) { xs.push(x + 2); ys.push(y + 2) }
    return { width: Math.max(...xs) + 4, height: Math.max(...ys) + 4 }
  }, [level, slots, gates, aisles])

  const toMetres = useCallback((event: React.MouseEvent): Point => {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const rect = svg.getBoundingClientRect()
    return {
      x: (event.clientX - rect.left) * (extent.width / rect.width),
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

  // ── Hit testing ─────────────────────────────────────────────
  /**
   * Bays are returned smallest-first and clicking the same spot again cycles
   * through the stack, so a bay hidden under a larger one is still reachable.
   */
  const hitSlots = useCallback((p: Point): number[] => {
    return slots
      .map((slot, index) => ({ slot, index }))
      .filter(({ slot }) => inRect(p, slot))
      .sort((a, b) =>
        a.slot.width * a.slot.height - b.slot.width * b.slot.height)
      .map(({ index }) => index)
  }, [slots])

  const hitGate = useCallback((p: Point): number => {
    for (let i = gates.length - 1; i >= 0; i -= 1) {
      if (Math.hypot(gates[i].x - p.x, gates[i].y - p.y) <= GATE_RADIUS) return i
    }
    return -1
  }, [gates])

  const hitAisle = useCallback((p: Point): number => {
    for (let i = aisles.length - 1; i >= 0; i -= 1) {
      if (distToPolyline(p, aisles[i]) <= AISLE_TOLERANCE) return i
    }
    return -1
  }, [aisles])

  // ── Pointer handling ────────────────────────────────────────
  const onMouseDown = (event: React.MouseEvent) => {
    if (event.button !== 0) return
    const point = toMetres(event)

    if (tool === 'aisle') {
      setPending((prev) => [...prev, { x: snap(point.x), y: snap(point.y) }])
      return
    }

    if (tool === 'gate') {
      const gate: DraftGate = {
        name: `Gate ${gates.length + 1}`,
        kind: gates.length === 0 ? 'entry' : 'exit',
        x: snap(point.x), y: snap(point.y),
        is_primary: gates.length === 0,
        is_active: true,
      }
      onGatesChange([...gates, gate])
      setSelected({ kind: 'gate', index: gates.length })
      setTool('select')
      return
    }

    if (tool === 'bay') {
      setSelected(null)
      setDrag({ kind: 'create', from: point, to: point })
      return
    }

    // ── select ──
    // 1. the selected bay's resize handle
    if (selected?.kind === 'slot') {
      const slot = slots[selected.index]
      if (slot) {
        const hs = handleSize(slot.width, slot.height)
        if (point.x > slot.x + slot.width - hs && point.y > slot.y + slot.height - hs) {
          setDrag({ kind: 'resize-slot', index: selected.index })
          return
        }
      }
    }

    // 2. gates sit above bays — they are small and easy to miss otherwise
    const gateIndex = hitGate(point)
    if (gateIndex >= 0) {
      setSelected({ kind: 'gate', index: gateIndex })
      setDrag({ kind: 'move-gate', index: gateIndex })
      return
    }

    // 3. bays, cycling through any stack under the cursor
    const stack = hitSlots(point)
    if (stack.length) {
      const current = selected?.kind === 'slot' ? stack.indexOf(selected.index) : -1
      const index = stack[(current + 1) % stack.length]
      setSelected({ kind: 'slot', index })
      const slot = slots[index]
      setDrag({
        kind: 'move-slot', index,
        offset: { x: point.x - slot.x, y: point.y - slot.y },
      })
      return
    }

    // 4. driveways
    const aisleIndex = hitAisle(point)
    if (aisleIndex >= 0) {
      setSelected({ kind: 'aisle', index: aisleIndex })
      return
    }

    setSelected(null)
  }

  const onMouseMove = (event: React.MouseEvent) => {
    const point = toMetres(event)
    if (tool === 'aisle' && pending.length) setCursor(point)
    if (!drag) return

    if (drag.kind === 'create') {
      setDrag({ ...drag, to: point })
      return
    }
    if (drag.kind === 'move-slot') {
      const next = [...slots]
      next[drag.index] = {
        ...next[drag.index],
        x: Math.max(0, snap(point.x - drag.offset.x)),
        y: Math.max(0, snap(point.y - drag.offset.y)),
      }
      onSlotsChange(next)
      return
    }
    if (drag.kind === 'resize-slot') {
      const next = [...slots]
      const slot = next[drag.index]
      next[drag.index] = {
        ...slot,
        width: Math.max(MIN_DIM, snap(point.x - slot.x)),
        height: Math.max(MIN_DIM, snap(point.y - slot.y)),
      }
      onSlotsChange(next)
      return
    }
    if (drag.kind === 'move-gate') {
      const next = [...gates]
      next[drag.index] = {
        ...next[drag.index],
        x: Math.max(0, snap(point.x)), y: Math.max(0, snap(point.y)),
      }
      onGatesChange(next)
    }
  }

  const onMouseUp = () => {
    if (drag?.kind === 'create') {
      const { from, to } = drag
      const w = Math.abs(to.x - from.x)
      const h = Math.abs(to.y - from.y)
      const bay: DraftSlot = {
        code: nextCode(),
        zone: zonePrefix,
        x: Math.max(0, snap(w < 1 ? from.x : Math.min(from.x, to.x))),
        y: Math.max(0, snap(h < 1 ? from.y : Math.min(from.y, to.y))),
        width: w < 1 ? DEFAULT_W : Math.max(MIN_DIM, snap(w)),
        height: h < 1 ? DEFAULT_H : Math.max(MIN_DIM, snap(h)),
        rotation: 0, slot_type: 'standard', has_ev_charger: false,
        price_multiplier: 1, is_active: true,
      }
      onSlotsChange([...slots, bay])
      setSelected({ kind: 'slot', index: slots.length })
      setTool('select')
    }
    setDrag(null)
  }

  const finishAisle = useCallback(() => {
    if (pending.length >= 2) {
      onAislesChange([...aisles, pending.map((p) => [p.x, p.y])])
    }
    setPending([])
    setCursor(null)
    if (pending.length >= 2) setTool('select')
  }, [pending, aisles, onAislesChange])

  const removeSelected = useCallback(() => {
    if (!selected) return
    if (selected.kind === 'slot') {
      if (slots[selected.index]?.status === 'occupied') return
      onSlotsChange(slots.filter((_, i) => i !== selected.index))
    } else if (selected.kind === 'gate') {
      if (gates.length <= 1) return       // a facility needs a way in
      onGatesChange(gates.filter((_, i) => i !== selected.index))
    } else {
      onAislesChange(aisles.filter((_, i) => i !== selected.index))
    }
    setSelected(null)
  }, [selected, slots, gates, aisles, onSlotsChange, onGatesChange, onAislesChange])

  // ── Keyboard ────────────────────────────────────────────────
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return

      if (event.key === 'Escape') {
        setPending([]); setCursor(null); setSelected(null)
        return
      }
      if (event.key === 'Enter' && tool === 'aisle') {
        event.preventDefault(); finishAisle(); return
      }
      const shortcut = TOOLS.find((t) => t.key.toLowerCase() === event.key.toLowerCase())
      if (shortcut && !event.metaKey && !event.ctrlKey) {
        setTool(shortcut.id); setPending([]); return
      }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault(); removeSelected(); return
      }

      if (selected?.kind !== 'slot' && selected?.kind !== 'gate') return
      const step = event.shiftKey ? GRID * 4 : GRID
      const moves: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0], ArrowRight: [step, 0],
        ArrowUp: [0, -step], ArrowDown: [0, step],
      }
      const move = moves[event.key]
      if (!move) return
      event.preventDefault()
      if (selected.kind === 'slot') {
        const next = [...slots]
        next[selected.index] = {
          ...next[selected.index],
          x: Math.max(0, next[selected.index].x + move[0]),
          y: Math.max(0, next[selected.index].y + move[1]),
        }
        onSlotsChange(next)
      } else {
        const next = [...gates]
        next[selected.index] = {
          ...next[selected.index],
          x: Math.max(0, next[selected.index].x + move[0]),
          y: Math.max(0, next[selected.index].y + move[1]),
        }
        onGatesChange(next)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [tool, selected, slots, gates, onSlotsChange, onGatesChange, removeSelected, finishAisle])

  const duplicates = useMemo(() => {
    const seen = new Map<string, number>()
    slots.forEach((s) => seen.set(s.code, (seen.get(s.code) ?? 0) + 1))
    return [...seen.entries()].filter(([, n]) => n > 1).map(([code]) => code)
  }, [slots])

  const noPrimary = gates.length > 0 && !gates.some((g) => g.is_primary)
  const activeTool = TOOLS.find((t) => t.id === tool)!

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
      <div>
        {/* ── Toolbar ── */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border border-ink-700 bg-ink-950/60 p-0.5">
            {TOOLS.map((t) => (
              <button
                key={t.id}
                onClick={() => { setTool(t.id); setPending([]); setSelected(null) }}
                title={`${t.label} (${t.key})`}
                className={`rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
                  tool === t.id ? 'bg-brand-600 text-white' : 'text-ink-300 hover:text-white'
                }`}
              >
                {t.label}
                <span className="ml-1.5 opacity-50">{t.key}</span>
              </button>
            ))}
          </div>
          {tool === 'bay' && (
            <>
              <span className="text-xs text-ink-400">Zone</span>
              <input
                value={zonePrefix}
                onChange={(e) => setZonePrefix(e.target.value.toUpperCase().slice(0, 2) || 'A')}
                className="input w-14 py-1 text-center text-xs"
              />
            </>
          )}
          {tool === 'aisle' && pending.length > 0 && (
            <button className="btn-primary btn-sm" onClick={finishAisle}>
              Finish driveway ({pending.length} points)
            </button>
          )}
        </div>
        <p className="mb-2 text-xs text-ink-500">{activeTool.hint}</p>

        {/* ── Canvas ── */}
        <svg
          ref={svgRef}
          viewBox={`0 0 ${extent.width} ${extent.height}`}
          width="100%"
          height={480}
          className={`select-none rounded-lg border border-ink-800 bg-ink-950/70 ${
            tool === 'select' ? 'cursor-default' : 'cursor-crosshair'
          }`}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={onMouseUp}
          onMouseLeave={onMouseUp}
          onDoubleClick={() => tool === 'aisle' && finishAisle()}
        >
          <defs>
            <pattern id="grid" width={GRID * 4} height={GRID * 4} patternUnits="userSpaceOnUse">
              <path d={`M ${GRID * 4} 0 L 0 0 0 ${GRID * 4}`} fill="none"
                    stroke="rgba(148,163,184,0.10)" strokeWidth={0.05} />
            </pattern>
          </defs>
          <rect width={extent.width} height={extent.height} fill="url(#grid)" />

          {/* driveways */}
          {aisles.map((line, index) => {
            const isSel = selected?.kind === 'aisle' && selected.index === index
            return (
              <g key={`aisle-${index}`}>
                <polyline
                  points={line.map(([x, y]) => `${x},${y}`).join(' ')}
                  fill="none"
                  stroke={isSel ? '#ffffff' : 'rgba(148,163,184,0.30)'}
                  strokeWidth={isSel ? 1.0 : 0.75}
                  strokeLinecap="round" strokeLinejoin="round"
                />
                {isSel && line.map(([x, y], i) => (
                  <circle key={i} cx={x} cy={y} r={0.3} fill="#ffffff" />
                ))}
              </g>
            )
          })}

          {/* driveway being drawn */}
          {pending.length > 0 && (
            <>
              <polyline
                points={[...pending, ...(cursor ? [cursor] : [])]
                  .map((p) => `${p.x},${p.y}`).join(' ')}
                fill="none" stroke="#3987e5" strokeWidth={0.7}
                strokeDasharray="0.6 0.4" strokeLinecap="round"
              />
              {pending.map((p, i) => (
                <circle key={i} cx={p.x} cy={p.y} r={0.32} fill="#3987e5" />
              ))}
            </>
          )}

          {/* bays */}
          {slots.map((slot, index) => {
            const isSel = selected?.kind === 'slot' && selected.index === index
            const occupied = slot.status === 'occupied'
            const hs = handleSize(slot.width, slot.height)
            return (
              <g key={`${slot.code}-${index}`}>
                <rect
                  x={slot.x} y={slot.y} width={slot.width} height={slot.height} rx={0.25}
                  fill={occupied ? STATUS_COLOR.occupied : slot.has_ev_charger ? '#199e70' : '#3987e5'}
                  fillOpacity={isSel ? 0.55 : 0.26}
                  stroke={isSel ? '#ffffff' : occupied ? STATUS_COLOR.occupied : '#3987e5'}
                  strokeWidth={isSel ? 0.18 : 0.08}
                />
                <text
                  x={slot.x + slot.width / 2} y={slot.y + slot.height / 2}
                  textAnchor="middle" dominantBaseline="central"
                  fontSize={Math.min(slot.width * 0.28, 0.9)}
                  fill="#eceef2" pointerEvents="none"
                >{slot.code}</text>
                {isSel && (
                  <rect
                    x={slot.x + slot.width - hs} y={slot.y + slot.height - hs}
                    width={hs} height={hs} fill="#ffffff"
                    className="cursor-nwse-resize"
                  />
                )}
              </g>
            )
          })}

          {/* gates */}
          {gates.map((gate, index) => {
            const isSel = selected?.kind === 'gate' && selected.index === index
            return (
              <g key={`gate-${index}`} className="cursor-pointer">
                <circle
                  cx={gate.x} cy={gate.y} r={GATE_RADIUS}
                  fill={GATE_COLOR[gate.kind]} fillOpacity={0.25}
                  stroke={isSel ? '#ffffff' : GATE_COLOR[gate.kind]}
                  strokeWidth={isSel ? 0.22 : 0.12}
                />
                <circle cx={gate.x} cy={gate.y} r={0.42} fill={GATE_COLOR[gate.kind]} />
                <text
                  x={gate.x} y={gate.y - GATE_RADIUS - 0.5}
                  textAnchor="middle" fontSize={0.85} fill="#b0b9c8"
                  pointerEvents="none"
                >
                  {gate.name}{gate.is_primary ? ' ★' : ''}
                </text>
              </g>
            )
          })}

          {/* bay being drawn */}
          {drag?.kind === 'create' && (
            <rect
              x={Math.min(drag.from.x, drag.to.x)} y={Math.min(drag.from.y, drag.to.y)}
              width={Math.abs(drag.to.x - drag.from.x)}
              height={Math.abs(drag.to.y - drag.from.y)}
              fill="#3987e5" fillOpacity={0.2}
              stroke="#3987e5" strokeWidth={0.1} strokeDasharray="0.3 0.2"
            />
          )}
        </svg>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-400">
            <span>{slots.length} bays</span>
            <span>·</span>
            <span>{slots.filter((s) => s.has_ev_charger).length} EV</span>
            <span>·</span>
            <span>{aisles.length} driveways</span>
            <span>·</span>
            <span>{gates.length} gates</span>
            {duplicates.length > 0 && (
              <Badge tone="bad">Duplicate codes: {duplicates.join(', ')}</Badge>
            )}
            {noPrimary && <Badge tone="warn">No primary gate</Badge>}
            {gates.length === 0 && <Badge tone="warn">No gate — vehicles cannot enter</Badge>}
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

      <Inspector
        selected={selected}
        slots={slots} gates={gates} aisles={aisles}
        onSlotsChange={onSlotsChange} onGatesChange={onGatesChange}
        onRemove={removeSelected}
      />
    </div>
  )
}

// ── Inspector ─────────────────────────────────────────────────

function Inspector({
  selected, slots, gates, aisles, onSlotsChange, onGatesChange, onRemove,
}: {
  selected: Selection
  slots: DraftSlot[]
  gates: DraftGate[]
  aisles: Aisle[]
  onSlotsChange: (s: DraftSlot[]) => void
  onGatesChange: (g: DraftGate[]) => void
  onRemove: () => void
}) {
  if (!selected) {
    return (
      <aside className="card card-pad h-fit">
        <h3 className="mb-2 text-sm font-semibold text-ink-50">Nothing selected</h3>
        <p className="text-xs leading-relaxed text-ink-500">
          Pick a tool above, or click something on the canvas to edit it.
        </p>
        <div className="mt-3 space-y-1.5 border-t border-ink-800 pt-3 text-[11px] text-ink-500">
          <p><b className="text-ink-300">Overlapping bays?</b> Click the same spot again to cycle through the stack.</p>
          <p><b className="text-ink-300">Driveways</b> are what the router follows — every bay's distance from the gate is measured along them.</p>
          <p><b className="text-ink-300">The primary gate</b> (★) is the origin those distances are measured from.</p>
        </div>
      </aside>
    )
  }

  if (selected.kind === 'slot') {
    const slot = slots[selected.index]
    if (!slot) return null
    const update = (patch: Partial<DraftSlot>) => {
      const next = [...slots]
      next[selected.index] = { ...next[selected.index], ...patch }
      onSlotsChange(next)
    }
    return (
      <aside className="card card-pad h-fit space-y-3">
        <h3 className="text-sm font-semibold text-ink-50">Bay {slot.code}</h3>
        <div>
          <label className="label">Code</label>
          <input className="input" value={slot.code}
                 onChange={(e) => update({ code: e.target.value.toUpperCase() })} />
        </div>
        <div>
          <label className="label">Zone</label>
          <input className="input" value={slot.zone}
                 onChange={(e) => update({ zone: e.target.value.toUpperCase() })} />
        </div>
        <div>
          <label className="label">Type</label>
          <select className="input" value={slot.slot_type}
                  onChange={(e) => update({ slot_type: e.target.value as SlotType })}>
            {Object.entries(SLOT_TYPE_LABEL).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="label">Width (m)</label>
            <input type="number" step="0.1" className="input" value={slot.width}
                   onChange={(e) => update({ width: Math.max(MIN_DIM, Number(e.target.value)) })} />
          </div>
          <div>
            <label className="label">Length (m)</label>
            <input type="number" step="0.1" className="input" value={slot.height}
                   onChange={(e) => update({ height: Math.max(MIN_DIM, Number(e.target.value)) })} />
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-200">
          <input type="checkbox" checked={slot.has_ev_charger}
                 onChange={(e) => update({ has_ev_charger: e.target.checked })}
                 className="h-4 w-4 rounded border-ink-600 bg-ink-900" />
          EV charger fitted
        </label>
        <div>
          <label className="label">Price multiplier</label>
          <input type="number" step="0.05" min="0.1" max="5" className="input"
                 value={slot.price_multiplier}
                 onChange={(e) => update({ price_multiplier: Number(e.target.value) })} />
        </div>
        {slot.status === 'occupied' ? (
          <p className="rounded-md border border-signal-amber/40 bg-signal-amber/10 px-3 py-2 text-[11px] text-signal-amber">
            A vehicle is parked here. It cannot be deleted until the vehicle leaves.
          </p>
        ) : (
          <button className="btn-danger btn-sm w-full" onClick={onRemove}>Delete bay</button>
        )}
      </aside>
    )
  }

  if (selected.kind === 'gate') {
    const gate = gates[selected.index]
    if (!gate) return null
    const update = (patch: Partial<DraftGate>) => {
      let next = [...gates]
      next[selected.index] = { ...next[selected.index], ...patch }
      // Exactly one primary: it is the origin for every stored bay distance.
      if (patch.is_primary) {
        next = next.map((g, i) => (i === selected.index ? g : { ...g, is_primary: false }))
      }
      onGatesChange(next)
    }
    return (
      <aside className="card card-pad h-fit space-y-3">
        <h3 className="text-sm font-semibold text-ink-50">Gate — {gate.name}</h3>
        <div>
          <label className="label">Name</label>
          <input className="input" value={gate.name}
                 onChange={(e) => update({ name: e.target.value })} />
        </div>
        <div>
          <label className="label">Direction</label>
          <select className="input" value={gate.kind}
                  onChange={(e) => update({ kind: e.target.value as DraftGate['kind'] })}>
            <option value="entry">Entry only</option>
            <option value="exit">Exit only</option>
            <option value="bidirectional">Both ways</option>
          </select>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="label">X (m)</label>
            <input type="number" step="0.5" className="input" value={gate.x}
                   onChange={(e) => update({ x: Number(e.target.value) })} />
          </div>
          <div>
            <label className="label">Y (m)</label>
            <input type="number" step="0.5" className="input" value={gate.y}
                   onChange={(e) => update({ y: Number(e.target.value) })} />
          </div>
        </div>
        <label className="flex items-start gap-2 text-sm text-ink-200">
          <input type="checkbox" checked={gate.is_primary}
                 onChange={(e) => update({ is_primary: e.target.checked })}
                 className="mt-0.5 h-4 w-4 rounded border-ink-600 bg-ink-900" />
          <span>
            Primary gate
            <span className="mt-0.5 block text-[11px] text-ink-500">
              Every bay's stored distance is measured from here, so moving it re-ranks
              which bay the allocator picks.
            </span>
          </span>
        </label>
        {gates.length > 1 ? (
          <button className="btn-danger btn-sm w-full" onClick={onRemove}>Delete gate</button>
        ) : (
          <p className="rounded-md border border-signal-amber/40 bg-signal-amber/10 px-3 py-2 text-[11px] text-signal-amber">
            This is the only gate. A facility needs at least one, or vehicles have no
            way in.
          </p>
        )}
      </aside>
    )
  }

  const line = aisles[selected.index]
  const length = line
    ? line.slice(1).reduce((sum, [x, y], i) =>
        sum + Math.hypot(x - line[i][0], y - line[i][1]), 0)
    : 0
  return (
    <aside className="card card-pad h-fit space-y-3">
      <h3 className="text-sm font-semibold text-ink-50">Driveway {selected.index + 1}</h3>
      <div className="space-y-1 text-sm text-ink-300">
        <div className="flex justify-between">
          <span className="text-ink-500">Points</span><span>{line?.length ?? 0}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-ink-500">Length</span><span>{length.toFixed(1)} m</span>
        </div>
      </div>
      <p className="text-[11px] leading-relaxed text-ink-500">
        Driveways form the graph the wayfinder routes along. Every bay's distance from
        the entry gate is measured through them, so redrawing one changes which bay the
        allocator considers closest.
      </p>
      <button className="btn-danger btn-sm w-full" onClick={onRemove}>Delete driveway</button>
    </aside>
  )
}
