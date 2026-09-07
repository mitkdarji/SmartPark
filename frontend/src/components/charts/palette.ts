/**
 * Chart colour roles.
 *
 * These are the dark-mode steps of the reference categorical palette, validated
 * against this app's card surface (#141821) with the data-viz validator:
 * lightness band, chroma floor, adjacent-pair CVD separation (worst ΔE 8.4),
 * normal-vision floor (worst ΔE 19.3) and 3:1 contrast all pass for six slots.
 *
 * Slots are assigned in fixed order and never cycled. A seventh series folds
 * into "Other" rather than inventing a hue.
 */

export const SERIES = [
  '#3987e5', // 1 blue
  '#d95926', // 2 orange
  '#199e70', // 3 aqua
  '#c98500', // 4 yellow
  '#d55181', // 5 magenta
  '#008300', // 6 green
] as const

/** Sequential ramp for magnitude (heat maps). One hue, light → dark. */
export const SEQUENTIAL = [
  '#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b',
] as const

/** Reserved for state. Never reused as a series colour. */
export const STATUS = {
  good: '#199e70',
  warning: '#c98500',
  serious: '#d95926',
  critical: '#e66767',
} as const

export const INK = {
  surface: '#141821',
  grid: 'rgba(148,163,184,0.13)',
  axis: 'rgba(148,163,184,0.35)',
  primary: '#eceef2',
  secondary: '#b0b9c8',
  muted: '#65748d',
} as const

export function seriesColor(index: number): string {
  return SERIES[index % SERIES.length]
}

/** Map a 0–1 magnitude onto the sequential ramp. */
export function heatColor(value: number): string {
  const clamped = Math.max(0, Math.min(1, value))
  const index = Math.min(SEQUENTIAL.length - 1, Math.floor(clamped * SEQUENTIAL.length))
  return SEQUENTIAL[index]
}
