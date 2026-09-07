/** Small presentational primitives shared across every page. */

import type { ReactNode } from 'react'

export function Card({
  title, subtitle, action, children, className = '', pad = true,
}: {
  title?: ReactNode; subtitle?: ReactNode; action?: ReactNode
  children: ReactNode; className?: string; pad?: boolean
}) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-4 border-b border-ink-800 px-5 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-ink-50">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-400">{subtitle}</p>}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </header>
      )}
      <div className={pad ? 'card-pad' : ''}>{children}</div>
    </section>
  )
}

export function Stat({
  label, value, hint, tone = 'default', icon,
}: {
  label: string; value: ReactNode; hint?: ReactNode
  tone?: 'default' | 'good' | 'warn' | 'bad'; icon?: ReactNode
}) {
  const toneClass = {
    default: 'text-ink-50', good: 'text-brand-400',
    warn: 'text-signal-amber', bad: 'text-signal-red',
  }[tone]
  return (
    <div className="card card-pad">
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-400">{label}</p>
        {icon && <span className="text-ink-500">{icon}</span>}
      </div>
      <p className={`mt-2 text-2xl font-bold tabular ${toneClass}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-400">{hint}</p>}
    </div>
  )
}

export function Badge({
  children, tone = 'neutral', className = '',
}: {
  children: ReactNode
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info' | 'violet'
  className?: string
}) {
  const tones = {
    neutral: 'bg-ink-700/60 text-ink-200',
    good: 'bg-brand-500/20 text-brand-300',
    warn: 'bg-signal-amber/20 text-signal-amber',
    bad: 'bg-signal-red/20 text-signal-red',
    info: 'bg-signal-blue/20 text-signal-blue',
    violet: 'bg-signal-violet/20 text-signal-violet',
  }
  return <span className={`badge ${tones[tone]} ${className}`}>{children}</span>
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2.5 text-sm text-ink-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-ink-600 border-t-brand-400" />
      {label ?? 'Loading…'}
    </div>
  )
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-signal-red/40 bg-signal-red/10 px-4 py-3">
      <span className="mt-0.5 text-signal-red">⚠</span>
      <div className="flex-1">
        <p className="text-sm text-ink-100">{message}</p>
        {onRetry && (
          <button onClick={onRetry} className="btn-ghost btn-sm mt-2">Try again</button>
        )}
      </div>
    </div>
  )
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      <p className="text-sm font-medium text-ink-300">{title}</p>
      {hint && <p className="max-w-sm text-xs text-ink-500">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Field({
  label, hint, children,
}: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <label className="label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] text-ink-500">{hint}</p>}
    </div>
  )
}

export function Toggle({
  checked, onChange, label,
}: { checked: boolean; onChange: (value: boolean) => void; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2.5 text-sm text-ink-200"
    >
      <span
        className={`relative h-5 w-9 rounded-full transition-colors ${
          checked ? 'bg-brand-600' : 'bg-ink-700'
        }`}
      >
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${
            checked ? 'translate-x-4' : 'translate-x-0.5'
          }`}
        />
      </span>
      {label}
    </button>
  )
}

export function Segmented<T extends string>({
  options, value, onChange,
}: { options: { value: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-ink-700 bg-ink-950/60 p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          onClick={() => onChange(option.value)}
          className={`rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
            value === option.value
              ? 'bg-brand-600 text-white'
              : 'text-ink-300 hover:text-white'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Modal({
  open, title, onClose, children, wide = false,
}: {
  open: boolean; title: string; onClose: () => void
  children: ReactNode; wide?: boolean
}) {
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className={`card w-full ${wide ? 'max-w-3xl' : 'max-w-lg'} animate-fade-up`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-ink-800 px-5 py-3.5">
          <h2 className="text-sm font-semibold text-ink-50">{title}</h2>
          <button onClick={onClose} className="text-ink-400 hover:text-white" aria-label="Close">✕</button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto card-pad">{children}</div>
      </div>
    </div>
  )
}

export function ProgressBar({ value, tone = 'brand' }: { value: number; tone?: string }) {
  const clamped = Math.max(0, Math.min(1, value))
  const color =
    clamped > 0.9 ? 'bg-signal-red' : clamped > 0.7 ? 'bg-signal-amber' : `bg-${tone}-500`
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-ink-800">
      <div
        className={`h-full rounded-full transition-all duration-500 ${color}`}
        style={{ width: `${clamped * 100}%` }}
      />
    </div>
  )
}
