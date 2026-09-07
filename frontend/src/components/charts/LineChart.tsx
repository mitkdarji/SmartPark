/**
 * Multi-series line / area chart with a crosshair tooltip.
 *
 * One y-axis, always — two measures of different scale get two charts, never a
 * second axis. A dashed series renders the forecast, and an optional band
 * carries its uncertainty so a prediction never looks like a measurement.
 */

import { useMemo, useState } from 'react'
import { INK, seriesColor } from './palette'

export interface Series {
  name: string
  points: { x: number; y: number }[]
  color?: string
  dashed?: boolean
  area?: boolean
  /** Symmetric uncertainty around y, drawn as a translucent band. */
  band?: { x: number; lo: number; hi: number }[]
}

interface Props {
  series: Series[]
  height?: number
  yFormat?: (value: number) => string
  xFormat?: (value: number) => string
  yMax?: number
  yMin?: number
  showLegend?: boolean
  ariaLabel?: string
}

const PAD = { top: 14, right: 16, bottom: 26, left: 46 }

export function LineChart({
  series, height = 220, yFormat = (v) => v.toFixed(0), xFormat = (v) => String(v),
  yMax, yMin, showLegend = true, ariaLabel,
}: Props) {
  const [width, setWidth] = useState(720)
  const [hover, setHover] = useState<{ x: number; index: number } | null>(null)

  const visible = series.filter((s) => s.points.length > 0)

  const scales = useMemo(() => {
    const xs = visible.flatMap((s) => s.points.map((p) => p.x))
    const ys = visible.flatMap((s) => s.points.map((p) => p.y))
    const bandYs = visible.flatMap((s) => (s.band ?? []).flatMap((b) => [b.lo, b.hi]))
    const allY = [...ys, ...bandYs]

    const x0 = xs.length ? Math.min(...xs) : 0
    const x1 = xs.length ? Math.max(...xs) : 1
    const y0 = yMin ?? (allY.length ? Math.min(...allY, 0) : 0)
    const y1 = yMax ?? (allY.length ? Math.max(...allY) * 1.08 : 1)

    const plotW = Math.max(1, width - PAD.left - PAD.right)
    const plotH = Math.max(1, height - PAD.top - PAD.bottom)

    return {
      x0, x1, y0, y1, plotW, plotH,
      sx: (x: number) => PAD.left + ((x - x0) / Math.max(1e-9, x1 - x0)) * plotW,
      sy: (y: number) => PAD.top + plotH - ((y - y0) / Math.max(1e-9, y1 - y0)) * plotH,
    }
  }, [visible, width, height, yMax, yMin])

  // Track the rendered width so the chart is fluid without a resize library.
  const measure = (node: SVGSVGElement | null) => {
    if (!node) return
    const observed = node.getBoundingClientRect().width
    if (observed && Math.abs(observed - width) > 1) setWidth(observed)
  }

  const ticks = useMemo(() => {
    const count = 4
    return Array.from({ length: count + 1 }, (_, i) => scales.y0 + ((scales.y1 - scales.y0) * i) / count)
  }, [scales])

  const longest = visible.reduce(
    (best, s) => (s.points.length > best.points.length ? s : best),
    visible[0] ?? { points: [] as Series['points'], name: '' },
  )

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!longest.points.length) return
    const rect = event.currentTarget.getBoundingClientRect()
    const px = event.clientX - rect.left
    let nearest = 0
    let best = Infinity
    longest.points.forEach((point, index) => {
      const distance = Math.abs(scales.sx(point.x) - px)
      if (distance < best) { best = distance; nearest = index }
    })
    setHover({ x: scales.sx(longest.points[nearest].x), index: nearest })
  }

  if (!visible.length) {
    return (
      <div style={{ height }} className="flex items-center justify-center text-sm text-ink-500">
        No data for this period.
      </div>
    )
  }

  return (
    <figure className="w-full">
      <svg
        ref={measure}
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={ariaLabel ?? visible.map((s) => s.name).join(', ')}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        className="overflow-visible"
      >
        {/* Recessive grid — reference lines, not content. */}
        {ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={PAD.left} x2={width - PAD.right}
              y1={scales.sy(tick)} y2={scales.sy(tick)}
              stroke={INK.grid} strokeWidth={1}
            />
            <text
              x={PAD.left - 8} y={scales.sy(tick)} dy="0.32em"
              textAnchor="end" fontSize={10} fill={INK.muted} className="tabular"
            >
              {yFormat(tick)}
            </text>
          </g>
        ))}

        {/* Uncertainty bands sit behind every line. */}
        {visible.map((s, index) => {
          if (!s.band?.length) return null
          const color = s.color ?? seriesColor(index)
          const top = s.band.map((b) => `${scales.sx(b.x)},${scales.sy(b.hi)}`).join(' ')
          const bottom = [...s.band].reverse().map((b) => `${scales.sx(b.x)},${scales.sy(b.lo)}`).join(' ')
          return (
            <polygon
              key={`band-${s.name}`}
              points={`${top} ${bottom}`}
              fill={color}
              opacity={0.14}
            />
          )
        })}

        {visible.map((s, index) => {
          const color = s.color ?? seriesColor(index)
          const path = s.points.map((p, i) => `${i ? 'L' : 'M'}${scales.sx(p.x)},${scales.sy(p.y)}`).join(' ')
          return (
            <g key={s.name}>
              {s.area && (
                <path
                  d={`${path} L${scales.sx(s.points[s.points.length - 1].x)},${scales.sy(scales.y0)} L${scales.sx(s.points[0].x)},${scales.sy(scales.y0)} Z`}
                  fill={color}
                  opacity={0.13}
                />
              )}
              <path
                d={path}
                fill="none"
                stroke={color}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeDasharray={s.dashed ? '5 4' : undefined}
              />
            </g>
          )
        })}

        {/* Crosshair + markers. A 2px surface ring keeps overlapping dots legible. */}
        {hover && (
          <g pointerEvents="none">
            <line
              x1={hover.x} x2={hover.x} y1={PAD.top} y2={height - PAD.bottom}
              stroke={INK.axis} strokeWidth={1} strokeDasharray="3 3"
            />
            {visible.map((s, index) => {
              const point = s.points[Math.min(hover.index, s.points.length - 1)]
              if (!point) return null
              return (
                <circle
                  key={`dot-${s.name}`}
                  cx={scales.sx(point.x)} cy={scales.sy(point.y)} r={4.5}
                  fill={s.color ?? seriesColor(index)}
                  stroke={INK.surface} strokeWidth={2}
                />
              )
            })}
          </g>
        )}

        <line
          x1={PAD.left} x2={width - PAD.right}
          y1={height - PAD.bottom} y2={height - PAD.bottom}
          stroke={INK.axis} strokeWidth={1}
        />
        {longest.points.length > 1 &&
          [0, Math.floor(longest.points.length / 2), longest.points.length - 1].map((i) => (
            <text
              key={`x-${i}`}
              x={scales.sx(longest.points[i].x)} y={height - 8}
              textAnchor={i === 0 ? 'start' : i === longest.points.length - 1 ? 'end' : 'middle'}
              fontSize={10} fill={INK.muted}
            >
              {xFormat(longest.points[i].x)}
            </text>
          ))}
      </svg>

      {hover && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-ink-800 bg-ink-950/80 px-3 py-2 text-xs">
          <span className="font-semibold text-ink-200">
            {xFormat(longest.points[Math.min(hover.index, longest.points.length - 1)]?.x ?? 0)}
          </span>
          {visible.map((s, index) => {
            const point = s.points[Math.min(hover.index, s.points.length - 1)]
            if (!point) return null
            return (
              <span key={`tt-${s.name}`} className="flex items-center gap-1.5 text-ink-300">
                <span
                  className="inline-block h-2 w-2 rounded-full"
                  style={{ background: s.color ?? seriesColor(index) }}
                />
                {s.name}
                <b className="tabular text-ink-50">{yFormat(point.y)}</b>
              </span>
            )
          })}
        </div>
      )}

      {showLegend && visible.length > 1 && (
        <figcaption className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1">
          {visible.map((s, index) => (
            <span key={`lg-${s.name}`} className="flex items-center gap-1.5 text-xs text-ink-300">
              <svg width="14" height="8" aria-hidden>
                <line
                  x1="0" y1="4" x2="14" y2="4"
                  stroke={s.color ?? seriesColor(index)} strokeWidth={2}
                  strokeDasharray={s.dashed ? '4 3' : undefined}
                />
              </svg>
              {s.name}
            </span>
          ))}
        </figcaption>
      )}
    </figure>
  )
}
