/** Wallet: balance, top-up, ledger, auto-reload, and the reconciliation check. */

import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { Badge, Card, Empty, ErrorNote, Spinner, Stat, Toggle } from '../components/ui'
import { dateTime, money } from '../lib/format'

const QUICK_AMOUNTS = [10_000, 25_000, 50_000, 100_000]

export function WalletPage() {
  const wallet = useAsync(() => api.wallet(), [])
  const transactions = useAsync(() => api.transactions(50), [])
  const [amount, setAmount] = useState(50_000)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reconciliation, setReconciliation] = useState<Record<string, any> | null>(null)

  const topUp = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.topUp(amount)
      wallet.refresh()
      transactions.refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'The top-up failed.')
    } finally {
      setBusy(false)
    }
  }

  const toggleAutoReload = async (enabled: boolean) => {
    if (!wallet.data) return
    await api.setAutoReload({
      enabled,
      threshold_minor: wallet.data.auto_reload_threshold_minor,
      amount_minor: wallet.data.auto_reload_amount_minor,
    })
    wallet.refresh()
  }

  const runReconciliation = async () => {
    setReconciliation(await api.reconcile())
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <Stat
          label="Balance"
          value={wallet.data ? money(wallet.data.balance_minor, wallet.data.currency) : '—'}
          hint="Charged automatically when you exit"
          tone="good"
        />
        <Stat
          label="Available"
          value={
            wallet.data
              ? money(wallet.data.balance_minor - wallet.data.held_minor, wallet.data.currency)
              : '—'
          }
          hint={wallet.data?.held_minor ? `${money(wallet.data.held_minor)} on hold` : 'Nothing held'}
        />
        <Stat
          label="Auto top-up"
          value={wallet.data?.auto_reload_enabled ? 'On' : 'Off'}
          hint={
            wallet.data
              ? `${money(wallet.data.auto_reload_amount_minor)} when below ${money(wallet.data.auto_reload_threshold_minor)}`
              : undefined
          }
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-[1fr_1.4fr]">
        <div className="space-y-5">
          <Card title="Add money">
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                {QUICK_AMOUNTS.map((value) => (
                  <button
                    key={value}
                    onClick={() => setAmount(value)}
                    className={`rounded-lg border px-3 py-2 text-sm font-semibold transition-colors ${
                      amount === value
                        ? 'border-brand-500 bg-brand-600/20 text-white'
                        : 'border-ink-700 text-ink-300 hover:text-white'
                    }`}
                  >
                    {money(value)}
                  </button>
                ))}
              </div>
              <div>
                <label className="label">Other amount (₹)</label>
                <input
                  type="number" min={1} max={50000} className="input"
                  value={amount / 100}
                  onChange={(event) => setAmount(Math.round(Number(event.target.value) * 100))}
                />
              </div>
              {error && (
                <p className="rounded-md border border-signal-red/40 bg-signal-red/10 px-3 py-2 text-xs text-signal-red">
                  {error}
                </p>
              )}
              <button className="btn-primary w-full" onClick={topUp} disabled={busy || amount <= 0}>
                {busy ? 'Processing…' : `Add ${money(amount)}`}
              </button>
              <p className="text-[11px] text-ink-500">
                Payments route through the configured gateway adapter. The default is a mock
                provider, so no real money moves in this demo.
              </p>
            </div>
          </Card>

          <Card title="Auto top-up">
            <div className="space-y-3">
              <Toggle
                checked={wallet.data?.auto_reload_enabled ?? false}
                onChange={(value) => void toggleAutoReload(value)}
                label="Top up automatically when the balance runs low"
              />
              <p className="text-xs text-ink-400">
                Keeps an exit from failing because the wallet ran empty. The charge is attempted
                before the debit, and a declined card never blocks the barrier.
              </p>
            </div>
          </Card>

          <Card
            title="Ledger integrity"
            subtitle="Recompute the balance from the transaction history"
            action={<button className="btn-ghost btn-sm" onClick={runReconciliation}>Check</button>}
          >
            {reconciliation ? (
              <div className="space-y-1.5 text-sm">
                <div className="flex justify-between">
                  <span className="text-ink-400">Stored balance</span>
                  <span className="tabular">{money(reconciliation.stored_balance_minor)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-ink-400">Derived from ledger</span>
                  <span className="tabular">{money(reconciliation.derived_balance_minor)}</span>
                </div>
                <div className="flex items-center justify-between border-t border-ink-800 pt-2">
                  <span className="text-ink-400">Drift</span>
                  {reconciliation.balanced ? (
                    <Badge tone="good">Balanced</Badge>
                  ) : (
                    <Badge tone="bad">{money(reconciliation.drift_minor)}</Badge>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-xs text-ink-500">
                The balance is derived state; the transaction rows are the truth. This check
                proves the two still agree.
              </p>
            )}
          </Card>
        </div>

        <Card title="Transactions" subtitle="Every credit and debit, newest first" pad={false}>
          {transactions.error && (
            <div className="p-5"><ErrorNote message={transactions.error} onRetry={transactions.refresh} /></div>
          )}
          {transactions.loading && <div className="p-5"><Spinner /></div>}
          {transactions.data?.length === 0 && (
            <Empty title="No transactions yet." hint="Charges appear here the moment you exit a facility." />
          )}
          <div className="max-h-[560px] overflow-y-auto">
            {transactions.data?.map((txn) => {
              const isDebit = txn.txn_type === 'debit' || txn.txn_type === 'hold'
              return (
                <div key={txn.id} className="table-row flex items-center gap-3 px-5 py-3">
                  <span
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-sm ${
                      isDebit ? 'bg-signal-red/15 text-signal-red' : 'bg-brand-500/15 text-brand-400'
                    }`}
                  >
                    {isDebit ? '↓' : '↑'}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-ink-100">{txn.description}</p>
                    <p className="text-[11px] text-ink-500">
                      {dateTime(txn.created_at)} · {txn.reference}
                    </p>
                  </div>
                  <div className="text-right">
                    <p
                      className={`tabular text-sm font-semibold ${
                        isDebit ? 'text-signal-red' : 'text-brand-400'
                      }`}
                    >
                      {isDebit ? '−' : '+'}{money(txn.amount_minor, txn.currency)}
                    </p>
                    <p className="tabular text-[11px] text-ink-500">
                      {money(txn.balance_after_minor, txn.currency)}
                    </p>
                  </div>
                </div>
              )
            })}
          </div>
        </Card>
      </div>
    </div>
  )
}
