/** Application chrome: brand, role-aware navigation, live status, sign-out. */

import { NavLink, useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuth } from '../lib/auth'
import { api } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { Badge } from './ui'

const DRIVER_NAV = [
  { to: '/', label: 'My parking', end: true },
  { to: '/find', label: 'Find parking' },
  { to: '/wallet', label: 'Wallet' },
  { to: '/history', label: 'History' },
]

const OWNER_NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/layout', label: 'Layout builder' },
  { to: '/gates', label: 'Gate & ANPR' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/operations', label: 'Operations' },
  { to: '/automation', label: 'Automation' },
  { to: '/lab', label: 'Evaluation lab' },
]

export function Shell({ children }: { children: ReactNode }) {
  const { user, logout, isOwner } = useAuth()
  const navigate = useNavigate()
  const health = useAsync(() => api.health(), [])

  const nav = isOwner ? OWNER_NAV : DRIVER_NAV

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-40 border-b border-ink-800 bg-ink-950/85 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] items-center gap-6 px-4 py-3 sm:px-6">
          <button
            onClick={() => navigate('/')}
            className="flex shrink-0 items-center gap-2.5"
          >
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-sm font-extrabold text-white">
              P
            </span>
            <span className="hidden text-sm font-bold tracking-tight text-ink-50 sm:block">
              SmartPark
            </span>
          </button>

          <nav className="flex flex-1 items-center gap-0.5 overflow-x-auto">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                    isActive ? 'bg-ink-800 text-white' : 'text-ink-400 hover:text-ink-100'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="flex shrink-0 items-center gap-3">
            {health.data && (
              <span className="hidden items-center gap-1.5 text-[11px] text-ink-500 lg:flex">
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    health.data.status === 'ok' ? 'bg-brand-500' : 'bg-signal-amber'
                  }`}
                />
                v{health.data.version}
                {!health.data.genai.enabled && <Badge tone="warn">AI key not set</Badge>}
              </span>
            )}
            <div className="hidden text-right sm:block">
              <p className="text-xs font-semibold text-ink-100">{user?.full_name}</p>
              <p className="text-[11px] capitalize text-ink-500">{user?.role}</p>
            </div>
            <button onClick={logout} className="btn-ghost btn-sm">Sign out</button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6">{children}</main>

      <footer className="border-t border-ink-800 px-4 py-4 text-center text-[11px] text-ink-600 sm:px-6">
        SmartPark — intelligent parking allocation &amp; automated billing ·{' '}
        <a href="/docs" className="hover:text-ink-400">API docs</a> ·{' '}
        <a href="/api/v1/city/availability" className="hover:text-ink-400">open data feed</a>
      </footer>
    </div>
  )
}
