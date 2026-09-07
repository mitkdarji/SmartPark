/** Geometry helpers for the layout editor. All coordinates are metres. */

export type Point = { x: number; y: number }

export const GRID = 0.5          // snap step
export const MIN_DIM = 1.2       // smallest a bay may be dragged
export const DEFAULT_W = 2.5
export const DEFAULT_H = 5.0

/**
 * The resize handle, in metres.
 *
 * This was the bug that made bays hard to select: a fixed 0.9 m corner on a
 * 2.5 m bay meant 36% of every bay started a resize instead of a move. The
 * handle is now proportional, capped, and only live on the *selected* bay — so
 * clicking anywhere on an unselected bay always selects it.
 */
export const handleSize = (w: number, h: number) =>
  Math.max(0.35, Math.min(0.7, Math.min(w, h) * 0.22))

export const GATE_RADIUS = 1.1   // click target for a gate marker
export const AISLE_TOLERANCE = 0.9

export const snap = (v: number) => Math.round(v / GRID) * GRID

export function dist(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

/** Perpendicular distance from `p` to the segment a-b. */
export function distToSegment(p: Point, a: Point, b: Point): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const lenSq = dx * dx + dy * dy
  if (lenSq < 1e-9) return dist(p, a)
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq))
  return dist(p, { x: a.x + t * dx, y: a.y + t * dy })
}

/** Closest distance from `p` to any segment of a polyline. */
export function distToPolyline(p: Point, line: number[][]): number {
  let best = Infinity
  for (let i = 0; i < line.length - 1; i += 1) {
    const d = distToSegment(
      p,
      { x: line[i][0], y: line[i][1] },
      { x: line[i + 1][0], y: line[i + 1][1] },
    )
    if (d < best) best = d
  }
  return best
}

export function inRect(
  p: Point,
  r: { x: number; y: number; width: number; height: number },
): boolean {
  return p.x >= r.x && p.x <= r.x + r.width && p.y >= r.y && p.y <= r.y + r.height
}
