/**
 * Horizontal bar chart with per-bar hover tooltips.
 *
 * Horizontal because the category labels are strategy names — readable without
 * rotating text. Bars are anchored to the baseline with 4px rounded data-ends
 * and separated by a 2px surface gap.
 */

import { useState } from 'react'
import { INK, seriesColor } from './palette'

export interface Bar {
  label: string
  value: number
  color?: string
  hint?: string
  highlight?: boolean
}

export function BarChart({
  bars, format = (v) => v.toFixed(1), height = 26, gap = 10, ariaLabel,
  colorByIndex = false,
}: {
  bars: Bar[]
  format?: (value: number) => string
  height?: number
  gap?: number
  ariaLabel?: string
  colorByIndex?: boolean
}) {
  const [hover, setHover] = useState<number | null>(null)
  if (!bars.length) {
    return <p className="py-6 text-center text-sm text-ink-500">No data yet.</p>
  }

  const max = Math.max(...bars.map((b) => Math.abs(b.value)), 1e-9)

  return (
    <div className="w-full" role="img" aria-label={ariaLabel}>
      {bars.map((bar, index) => {
        const ratio = Math.abs(bar.value) / max
        const color =
          bar.color ?? (colorByIndex ? seriesColor(index) : bar.highlight ? seriesColor(2) : seriesColor(0))
        return (
          <div
            key={bar.label}
            className="group relative"
            style={{ marginBottom: index === bars.length - 1 ? 0 : gap }}
            onMouseEnter={() => setHover(index)}
            onMouseLeave={() => setHover(null)}
          >
            <div className="mb-1 flex items-baseline justify-between gap-3">
              <span
                className={`truncate text-xs ${bar.highlight ? 'font-semibold text-ink-50' : 'text-ink-300'}`}
              >
                {bar.label}
              </span>
              {/* Selective direct labels: the value, not a number on every tick. */}
              <span className="tabular shrink-0 text-xs font-semibold text-ink-100">
                {format(bar.value)}
              </span>
            </div>
            <div
              className="w-full overflow-hidden rounded"
              style={{ height, background: 'rgba(148,163,184,0.09)' }}
            >
              <div
                className="h-full rounded transition-all duration-500"
                style={{
                  width: `${Math.max(ratio * 100, 1.5)}%`,
                  background: color,
                  opacity: hover === null || hover === index ? 1 : 0.55,
                }}
              />
            </div>
            {bar.hint && hover === index && (
              <div className="absolute right-0 top-full z-10 mt-1 rounded-md border border-ink-700 bg-ink-950 px-2.5 py-1.5 text-[11px] text-ink-200 shadow-lg">
                {bar.hint}
              </div>
            )}
          </div>
        )
      })}
      <p className="mt-3 text-[11px] text-ink-500" style={{ color: INK.muted }}>
        Bars are scaled against the largest value shown.
      </p>
    </div>
  )
}

/** Grouped bars: one row per category, one segment per series. */
export function GroupedBars({
  categories, seriesNames, values, format = (v) => v.toFixed(1), ariaLabel,
}: {
  categories: string[]
  seriesNames: string[]
  /** values[categoryIndex][seriesIndex] */
  values: number[][]
  format?: (value: number) => string
  ariaLabel?: string
}) {
  const [hover, setHover] = useState<{ row: number; col: number } | null>(null)
  const max = Math.max(...values.flat().map(Math.abs), 1e-9)

  return (
    <div className="w-full" role="img" aria-label={ariaLabel}>
      {categories.map((category, row) => (
        <div key={category} className="mb-4 last:mb-0">
          <p className="mb-1.5 text-xs font-medium text-ink-200">{category}</p>
          <div className="flex flex-col gap-[2px]">
            {seriesNames.map((name, col) => {
              const value = values[row]?.[col] ?? 0
              return (
                <div
                  key={name}
                  className="flex items-center gap-2"
                  onMouseEnter={() => setHover({ row, col })}
                  onMouseLeave={() => setHover(null)}
                >
                  <div
                    className="h-3.5 flex-1 overflow-hidden rounded"
                    style={{ background: 'rgba(148,163,184,0.09)' }}
                  >
                    <div
                      className="h-full rounded transition-all duration-500"
                      style={{
                        width: `${Math.max((Math.abs(value) / max) * 100, 1.5)}%`,
                        background: seriesColor(col),
                        opacity:
                          hover === null || (hover.row === row && hover.col === col) ? 1 : 0.5,
                      }}
                    />
                  </div>
                  <span className="tabular w-20 shrink-0 text-right text-[11px] text-ink-300">
                    {format(value)}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      ))}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {seriesNames.map((name, index) => (
          <span key={name} className="flex items-center gap-1.5 text-xs text-ink-300">
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ background: seriesColor(index) }}
            />
            {name}
          </span>
        ))}
      </div>
    </div>
  )
}

/** Compact inline trend, no axes — for stat tiles. */
export function Sparkline({
  values, width = 110, height = 30, color = seriesColor(0),
}: { values: number[]; width?: number; height?: number; color?: string }) {
  if (values.length < 2) return null
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = Math.max(1e-9, max - min)
  const path = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width
      const y = height - ((value - min) / span) * (height - 4) - 2
      return `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <svg width={width} height={height} aria-hidden className="overflow-visible">
      <path d={path} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" />
    </svg>
  )
}
