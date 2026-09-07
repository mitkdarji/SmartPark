/** Display helpers. Money is always integer minor units until the last moment. */

export function money(minor: number | null | undefined, currency = 'INR'): string {
  if (minor === null || minor === undefined) return '—'
  const symbol = ({ INR: '₹', USD: '$', EUR: '€' } as Record<string, string>)[currency] ?? `${currency} `
  return `${symbol}${(minor / 100).toLocaleString('en-IN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`
}

export function compactMoney(minor: number, currency = 'INR'): string {
  const symbol = ({ INR: '₹', USD: '$', EUR: '€' } as Record<string, string>)[currency] ?? ''
  const value = minor / 100
  if (value >= 1e7) return `${symbol}${(value / 1e7).toFixed(2)}Cr`
  if (value >= 1e5) return `${symbol}${(value / 1e5).toFixed(2)}L`
  if (value >= 1e3) return `${symbol}${(value / 1e3).toFixed(1)}k`
  return `${symbol}${value.toFixed(0)}`
}

export function duration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return '—'
  if (minutes < 1) return '<1 min'
  if (minutes < 60) return `${Math.round(minutes)} min`
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return m ? `${h}h ${m}m` : `${h}h`
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  const seconds = (Date.now() - then) / 1000
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

export const pct = (value: number, digits = 0) => `${(value * 100).toFixed(digits)}%`

export function prettyPlate(plate: string): string {
  const clean = plate.replace(/[^A-Z0-9]/gi, '').toUpperCase()
  const standard = /^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$/.exec(clean)
  return standard ? standard.slice(1).join(' ') : clean
}

export function titleCase(value: string): string {
  return value.replace(/[_.]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export const SLOT_TYPE_LABEL: Record<string, string> = {
  standard: 'Standard', compact: 'Compact', large: 'Large',
  ev: 'EV charging', accessible: 'Accessible', two_wheeler: 'Two-wheeler',
}

export const STATUS_COLOR: Record<string, string> = {
  empty: '#1fa571', occupied: '#e5484d', reserved: '#f0a92a',
  blocked: '#65748d', maintenance: '#8b5cf6',
}

export const SEVERITY_CLASS: Record<string, string> = {
  low: 'bg-ink-700/60 text-ink-200',
  medium: 'bg-signal-amber/20 text-signal-amber',
  high: 'bg-signal-red/20 text-signal-red',
  critical: 'bg-signal-red text-white',
}
