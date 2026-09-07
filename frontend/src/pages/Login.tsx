/** Sign-in and registration, with one-click demo accounts. */

import { useState } from 'react'
import { ApiError } from '../lib/api'
import { useAuth } from '../lib/auth'

const DEMO_PASSWORD = 'SmartPark2026!'
const DEMO_ACCOUNTS = [
  { email: 'owner@smartpark.dev', label: 'Facility operator', hint: 'Full operator console' },
  { email: 'mit.darji@smartpark.dev', label: 'Driver', hint: 'Wallet, live session, voice assistant' },
]

export function Login() {
  const { login, register } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [form, setForm] = useState({
    email: '', password: '', full_name: '', plate: '', role: 'driver',
  })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const set = (key: string, value: string) => setForm((prev) => ({ ...prev, [key]: value }))

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') {
        await login(form.email.trim(), form.password)
      } else {
        await register({
          email: form.email.trim(),
          password: form.password,
          full_name: form.full_name.trim(),
          role: form.role,
          plate: form.plate.trim() || undefined,
        })
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  const useDemo = async (email: string) => {
    setBusy(true)
    setError(null)
    try {
      await login(email, DEMO_PASSWORD)
    } catch {
      setError('Demo accounts are not seeded. Run: python -m scripts.seed --reset --train')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-ink-950 px-4 py-10">
      <div className="grid w-full max-w-4xl gap-8 lg:grid-cols-2">
        <div className="flex flex-col justify-center">
          <div className="mb-4 flex items-center gap-3">
            <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-brand-600 text-lg font-extrabold text-white">
              P
            </span>
            <h1 className="text-2xl font-bold tracking-tight text-white">SmartPark</h1>
          </div>
          <p className="text-sm leading-relaxed text-ink-300">
            A camera reads the number plate at the gate. An allocation policy picks the bay
            and the driver is guided straight to it. On the way out the same read closes the
            session, prices the stay against live occupancy, and settles it from a wallet —
            with no ticket, no attendant and no cash.
          </p>
          <ul className="mt-5 space-y-2 text-sm text-ink-400">
            {[
              'ANPR: OpenCV detection with a voting OCR ensemble',
              'Recency-aware slot allocation, benchmarked against four baselines',
              'Occupancy-based dynamic pricing with an auditable breakdown',
              'Demand forecasting, anomaly detection and a rules engine',
              'A voice assistant and an operations copilot that call real tools',
            ].map((line) => (
              <li key={line} className="flex items-start gap-2">
                <span className="mt-1 text-brand-500">▸</span>
                {line}
              </li>
            ))}
          </ul>
        </div>

        <div className="card card-pad">
          <div className="mb-4 flex gap-1 rounded-lg bg-ink-950/70 p-1">
            {(['login', 'register'] as const).map((value) => (
              <button
                key={value}
                onClick={() => { setMode(value); setError(null) }}
                className={`flex-1 rounded-md py-1.5 text-sm font-semibold transition-colors ${
                  mode === value ? 'bg-brand-600 text-white' : 'text-ink-400 hover:text-white'
                }`}
              >
                {value === 'login' ? 'Sign in' : 'Create account'}
              </button>
            ))}
          </div>

          <form onSubmit={submit} className="space-y-3">
            {mode === 'register' && (
              <div>
                <label className="label">Full name</label>
                <input
                  className="input" required value={form.full_name}
                  onChange={(event) => set('full_name', event.target.value)}
                />
              </div>
            )}
            <div>
              <label className="label">Email</label>
              <input
                className="input" type="email" required autoComplete="email"
                value={form.email} onChange={(event) => set('email', event.target.value)}
              />
            </div>
            <div>
              <label className="label">Password</label>
              <input
                className="input" type="password" required minLength={8}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                value={form.password} onChange={(event) => set('password', event.target.value)}
              />
            </div>
            {mode === 'register' && (
              <>
                <div>
                  <label className="label">I am a</label>
                  <select
                    className="input" value={form.role}
                    onChange={(event) => set('role', event.target.value)}
                  >
                    <option value="driver">Driver</option>
                    <option value="owner">Parking facility operator</option>
                  </select>
                </div>
                {form.role === 'driver' && (
                  <div>
                    <label className="label">Vehicle number (optional)</label>
                    <input
                      className="input uppercase" placeholder="GJ 01 AB 1234"
                      value={form.plate} onChange={(event) => set('plate', event.target.value)}
                    />
                  </div>
                )}
              </>
            )}

            {error && (
              <p className="rounded-md border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-xs text-signal-red">
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}
            </button>
          </form>

          <div className="mt-5 border-t border-ink-800 pt-4">
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-500">
              Demo accounts
            </p>
            <div className="space-y-1.5">
              {DEMO_ACCOUNTS.map((account) => (
                <button
                  key={account.email}
                  onClick={() => void useDemo(account.email)}
                  disabled={busy}
                  className="flex w-full items-center justify-between rounded-lg border border-ink-800 px-3 py-2 text-left hover:border-brand-600"
                >
                  <span>
                    <span className="block text-xs font-semibold text-ink-100">{account.label}</span>
                    <span className="block text-[11px] text-ink-500">{account.hint}</span>
                  </span>
                  <span className="text-xs text-brand-400">Open →</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
