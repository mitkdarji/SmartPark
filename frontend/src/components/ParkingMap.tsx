/**
 * Live parking map.
 *
 * Renders one level's bays, gates and (optionally) a driver's route. Slot
 * geometry is stored in metres, so the map fits the level's extent to the
 * available pixels and everything else follows from that one scale factor.
 *
 * Two colour modes, deliberately distinct jobs:
 *   status — categorical state (empty / occupied / reserved / blocked)
 *   heat   — sequential magnitude (how often each bay gets used)
 */

import { useMemo, useState } from 'react'
import { heatColor } from './charts/palette'
import type { Gate, Level, Route, Slot, SlotHeat } from '../lib/types'
import { SLOT_TYPE_LABEL, STATUS_COLOR } from '../lib/format'

type Bay = Slot | SlotHeat

interface Props {
  level: Level | null
  slots: Bay[]
  gates?: Gate[]
  route?: Route | null
  highlightSlotId?: number | null
  mode?: 'status' | 'heat'
  height?: number
  onSlotClick?: (slot: Bay) => void
  showLegend?: boolean
}

const PAD = 18

function slotId(slot: Bay): number {
  return 'id' in slot ? (slot as Slot).id : (slot as SlotHeat).slot_id
}

export function ParkingMap({
  level, slots, gates = [], route = null, highlightSlotId = null,
  mode = 'status', height = 420, onSlotClick, showLegend = true,
}: Props) {
  const [hover, setHover] = useState<Bay | null>(null)

  const geometry = useMemo(() => {
    if (!slots.length) return null
    // Fit to the actual content, not the declared canvas — a level whose bays
    // occupy a corner should still fill the viewport.
    const xs = slots.flatMap((s) => [s.x, s.x + s.width])
    const ys = slots.flatMap((s) => [s.y, s.y + s.height])
    for (const gate of gates) { xs.push(gate.x); ys.push(gate.y) }
    for (const line of level?.aisles ?? []) {
      for (const [x, y] of line) { xs.push(x); ys.push(y) }
    }
    for (const [x, y] of route?.waypoints ?? []) { xs.push(x); ys.push(y) }

    const minX = Math.min(...xs) - 2
    const maxX = Math.max(...xs) + 2
    const minY = Math.min(...ys) - 2
    const maxY = Math.max(...ys) + 2
    return {
      minX, minY,
      width: Math.max(1, maxX - minX),
      height: Math.max(1, maxY - minY),
    }
  }, [slots, gates, level, route])

  if (!geometry) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center rounded-lg border border-dashed border-ink-700 text-sm text-ink-500"
      >
        No bays on this level yet.
      </div>
    )
  }

  const viewBox = `${geometry.minX - PAD / 6} ${geometry.minY - PAD / 6} ${geometry.width + PAD / 3} ${geometry.height + PAD / 3}`
  const strokeUnit = geometry.width / 400   // keep stroke weight visually constant

  const fillFor = (slot: Bay): string => {
    if (mode === 'heat') return heatColor('heat' in slot ? (slot as SlotHeat).heat : 0)
    return STATUS_COLOR[slot.status] ?? '#65748d'
  }

  const routePath = (route?.waypoints ?? [])
    .map(([x, y], index) => `${index ? 'L' : 'M'}${x},${y}`)
    .join(' ')

  return (
    <div className="relative">
      <svg
        viewBox={viewBox}
        width="100%"
        height={height}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={`Parking layout with ${slots.length} bays`}
        className="rounded-lg bg-ink-950/60"
      >
        {/* Drive aisles sit under everything else. */}
        {(level?.aisles ?? []).map((line, index) => (
          <polyline
            key={`aisle-${index}`}
            points={line.map(([x, y]) => `${x},${y}`).join(' ')}
            fill="none"
            stroke="rgba(148,163,184,0.18)"
            strokeWidth={strokeUnit * 6}
            strokeLinecap="round"
          />
        ))}

        {slots.map((slot) => {
          const id = slotId(slot)
          const isHighlighted = highlightSlotId === id
          return (
            <g key={id}>
              <rect
                x={slot.x} y={slot.y} width={slot.width} height={slot.height}
                rx={strokeUnit * 1.5}
                fill={fillFor(slot)}
                fillOpacity={mode === 'heat' ? 0.85 : slot.status === 'empty' ? 0.28 : 0.75}
                stroke={isHighlighted ? '#ffffff' : fillFor(slot)}
                strokeWidth={isHighlighted ? strokeUnit * 3 : strokeUnit}
                className={onSlotClick ? 'cursor-pointer' : ''}
                onMouseEnter={() => setHover(slot)}
                onMouseLeave={() => setHover(null)}
                onClick={() => onSlotClick?.(slot)}
              />
              {/* An EV bay carries a mark, not just a hue — colour alone is not identity. */}
              {'has_ev_charger' in slot && (slot as Slot).has_ev_charger && (
                <text
                  x={slot.x + slot.width / 2} y={slot.y + slot.height / 2}
                  textAnchor="middle" dominantBaseline="central"
                  fontSize={slot.height * 0.4} fill="#ffffff" opacity={0.85}
                  pointerEvents="none"
                >⚡</text>
              )}
              {isHighlighted && (
                <rect
                  x={slot.x} y={slot.y} width={slot.width} height={slot.height}
                  rx={strokeUnit * 1.5} fill="none" stroke="#ffffff"
                  strokeWidth={strokeUnit * 2} className="animate-pulse"
                  pointerEvents="none"
                />
              )}
            </g>
          )
        })}

        {route && routePath && (
          <>
            <path
              d={routePath} fill="none" stroke="#3987e5"
              strokeWidth={strokeUnit * 4} strokeLinecap="round" strokeLinejoin="round"
              opacity={0.35}
            />
            <path
              d={routePath} fill="none" stroke="#3987e5"
              strokeWidth={strokeUnit * 2.2} strokeLinecap="round" strokeLinejoin="round"
              strokeDasharray="6 4"
            >
              <animate
                attributeName="stroke-dashoffset"
                from="40" to="0" dur="1.6s" repeatCount="indefinite"
              />
            </path>
          </>
        )}

        {gates.map((gate) => (
          <g key={gate.id}>
            <circle
              cx={gate.x} cy={gate.y} r={strokeUnit * 5}
              fill={gate.kind === 'exit' ? '#d95926' : '#199e70'}
              stroke="#0b0e14" strokeWidth={strokeUnit}
            />
            <text
              x={gate.x} y={gate.y - strokeUnit * 8}
              textAnchor="middle" fontSize={strokeUnit * 9}
              fill="#b0b9c8"
            >
              {gate.name}
            </text>
          </g>
        ))}
      </svg>

      {hover && (
        <div className="pointer-events-none absolute left-3 top-3 rounded-lg border border-ink-700 bg-ink-950/95 px-3 py-2 text-xs shadow-xl">
          <p className="font-semibold text-ink-50">
            {hover.code} <span className="font-normal text-ink-400">· zone {hover.zone}</span>
          </p>
          <p className="mt-0.5 text-ink-300">
            {SLOT_TYPE_LABEL[hover.slot_type] ?? hover.slot_type} · {hover.status}
          </p>
          <p className="text-ink-400">
            {hover.distance_from_entry.toFixed(0)} m from the entry
            {'total_uses' in hover ? ` · used ${hover.total_uses}×` : ''}
          </p>
        </div>
      )}

      {showLegend && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-ink-300">
          {mode === 'status' ? (
            <>
              {(['empty', 'occupied', 'reserved', 'blocked'] as const).map((status) => (
                <span key={status} className="flex items-center gap-1.5">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-sm"
                    style={{ background: STATUS_COLOR[status] }}
                  />
                  {status}
                </span>
              ))}
              <span className="flex items-center gap-1.5 text-ink-400">⚡ EV charging</span>
            </>
          ) : (
            <span className="flex items-center gap-2">
              Least used
              <span className="flex overflow-hidden rounded-sm">
                {[0.05, 0.25, 0.45, 0.65, 0.85, 0.98].map((value) => (
                  <span key={value} className="h-2.5 w-6" style={{ background: heatColor(value) }} />
                ))}
              </span>
              Most used
            </span>
          )}
        </div>
      )}
    </div>
  )
}
