/** Automation rules: the catalog, the rule list, AI suggestions and the run log. */

import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { useFacility } from '../hooks/useFacility'
import { Badge, Card, Empty, ErrorNote, Modal, Spinner, Toggle } from '../components/ui'
import { FacilityPicker } from '../components/FacilityPicker'
import { relativeTime, titleCase } from '../lib/format'

export function Automation() {
  const { facilities, facilityId, select } = useFacility()
  const catalog = useAsync(() => api.automationCatalog(), [])
  const rules = useAsync(async () => (facilityId ? api.rules(facilityId) : []), [facilityId])
  const runs = useAsync(async () => (facilityId ? api.runs(facilityId, 30) : []), [facilityId])

  const [creating, setCreating] = useState(false)
  const [suggestions, setSuggestions] = useState<any[] | null>(null)
  const [suggestBy, setSuggestBy] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ticked, setTicked] = useState<string | null>(null)

  const [form, setForm] = useState({
    name: '', description: '', trigger: 'schedule:tick',
    action: 'flag_overstays', conditions: '{}', cooldown_seconds: 300,
  })

  const runNow = async () => {
    setBusy(true)
    try {
      const result = await api.tick()
      setTicked(
        `Evaluated ${result.rules_evaluated} scheduled rules; ${result.fired?.length ?? 0} fired.`,
      )
      runs.refresh()
      rules.refresh()
      window.setTimeout(() => setTicked(null), 8000)
    } finally {
      setBusy(false)
    }
  }

  const createRule = async (payload?: Record<string, unknown>, byAi = false) => {
    if (!facilityId) return
    setBusy(true)
    setError(null)
    try {
      const body = payload ?? {
        name: form.name,
        description: form.description,
        trigger: form.trigger,
        conditions: JSON.parse(form.conditions || '{}'),
        actions: [{ action: form.action, params: {} }],
        enabled: true,
        cooldown_seconds: form.cooldown_seconds,
        facility_id: facilityId,
      }
      await api.createRule({ ...body, facility_id: facilityId }, byAi)
      rules.refresh()
      setCreating(false)
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message
          : err instanceof SyntaxError ? 'Conditions must be valid JSON.'
          : 'The rule could not be created.',
      )
    } finally {
      setBusy(false)
    }
  }

  const toggleRule = async (id: number, enabled: boolean) => {
    const rule = rules.data?.find((r) => r.id === id)
    if (!rule) return
    await api.updateRule(id, {
      name: rule.name, description: rule.description, trigger: rule.trigger,
      conditions: rule.conditions, actions: rule.actions, enabled,
      cooldown_seconds: rule.cooldown_seconds, priority: rule.priority,
      facility_id: rule.facility_id,
    })
    rules.refresh()
  }

  const suggest = async () => {
    if (!facilityId) return
    setBusy(true)
    try {
      const result = await api.suggestAutomations(facilityId)
      setSuggestions(result.suggestions ?? [])
      setSuggestBy(result.generated_by ?? '')
    } finally {
      setBusy(false)
    }
  }

  if (facilities.loading && !facilities.data) return <Spinner />
  if (!facilityId) return <Empty title="Create a facility first." />

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Automation</h1>
          <p className="text-sm text-ink-400">
            Declarative when/if/then rules. Stored as data, so a new rule takes effect
            immediately — no deploy.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <FacilityPicker facilities={facilities.data ?? []} value={facilityId} onChange={select} />
          <button className="btn-ghost btn-sm" onClick={() => void suggest()} disabled={busy}>
            Suggest rules
          </button>
          <button className="btn-ghost btn-sm" onClick={() => void runNow()} disabled={busy}>
            Run scheduled pass
          </button>
          <button className="btn-primary btn-sm" onClick={() => setCreating(true)}>
            New rule
          </button>
        </div>
      </div>

      {ticked && (
        <div className="rounded-lg border border-brand-500/40 bg-brand-500/10 px-4 py-2.5 text-sm text-brand-200">
          {ticked}
        </div>
      )}
      {error && <ErrorNote message={error} />}

      <div className="grid gap-5 xl:grid-cols-[1.3fr_1fr]">
        <Card title="Rules" subtitle={`${rules.data?.length ?? 0} configured`} pad={false}>
          {rules.loading && !rules.data && <div className="p-5"><Spinner /></div>}
          {rules.data?.length === 0 && (
            <div className="p-5">
              <Empty title="No rules yet." hint="Add one, or let the copilot suggest rules from this facility's KPIs." />
            </div>
          )}
          <div className="divide-y divide-ink-800">
            {rules.data?.map((rule) => (
              <div key={rule.id} className="px-5 py-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-semibold text-ink-50">{rule.name}</h3>
                      {rule.created_by_ai && <Badge tone="violet">AI drafted</Badge>}
                      {rule.facility_id === null && <Badge tone="info">global</Badge>}
                    </div>
                    <p className="mt-0.5 text-xs text-ink-400">{rule.description}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <Badge tone="neutral">on {rule.trigger}</Badge>
                      {rule.actions.map((action, index) => (
                        <Badge key={index} tone="good">{action.action}</Badge>
                      ))}
                      {Object.keys(rule.conditions ?? {}).length > 0 && (
                        <span className="font-mono text-[10px] text-ink-500">
                          if {JSON.stringify(rule.conditions)}
                        </span>
                      )}
                    </div>
                    <p className="mt-1.5 text-[11px] text-ink-600">
                      Fired {rule.fire_count}× · last {relativeTime(rule.last_fired_at)}
                      {rule.cooldown_seconds ? ` · ${rule.cooldown_seconds}s cooldown` : ''}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-2">
                    <Toggle
                      checked={rule.enabled}
                      onChange={(value) => void toggleRule(rule.id, value)}
                    />
                    <button
                      className="text-[11px] text-ink-500 hover:text-signal-red"
                      onClick={async () => { await api.deleteRule(rule.id); rules.refresh() }}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </Card>

        <Card title="Recent runs" subtitle="What each rule actually did" pad={false}>
          {runs.data?.length === 0 && (
            <div className="p-5"><Empty title="No runs recorded yet." hint="Press “Run scheduled pass” to fire the tick rules now." /></div>
          )}
          <div className="max-h-[520px] divide-y divide-ink-800 overflow-y-auto">
            {runs.data?.map((run) => (
              <div key={run.id} className="px-5 py-3">
                <div className="flex items-center justify-between gap-2">
                  <Badge tone={run.status === 'success' ? 'good' : run.status === 'partial' ? 'warn' : 'bad'}>
                    {run.status}
                  </Badge>
                  <span className="text-[10px] text-ink-600">
                    {relativeTime(run.created_at)} · {run.duration_ms.toFixed(1)} ms
                  </span>
                </div>
                <p className="mt-1 text-xs text-ink-300">
                  {run.trigger_event} →{' '}
                  {run.actions_taken
                    .map((action) =>
                      `${action.action}${
                        action.error ? ` (failed: ${action.error})`
                          : action.result ? ` ${JSON.stringify(action.result).slice(0, 60)}` : ''
                      }`,
                    )
                    .join(', ')}
                </p>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Modal open={creating} title="New automation rule" onClose={() => setCreating(false)}>
        <div className="space-y-3">
          <div>
            <label className="label">Name</label>
            <input
              className="input" value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
            />
          </div>
          <div>
            <label className="label">Description</label>
            <input
              className="input" value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
            />
          </div>
          <div>
            <label className="label">Trigger</label>
            <select
              className="input" value={form.trigger}
              onChange={(event) => setForm({ ...form, trigger: event.target.value })}
            >
              {(catalog.data?.triggers ?? []).map((trigger) => (
                <option key={trigger} value={trigger}>{trigger}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Action</label>
            <select
              className="input" value={form.action}
              onChange={(event) => setForm({ ...form, action: event.target.value })}
            >
              {(catalog.data?.actions ?? []).map((action) => (
                <option key={action} value={action}>{titleCase(action)}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Conditions (JSON)</label>
            <input
              className="input font-mono text-xs" value={form.conditions}
              onChange={(event) => setForm({ ...form, conditions: event.target.value })}
              placeholder='{"occupancy_pct_gte": 70}'
            />
            <p className="mt-1 text-[11px] text-ink-500">{catalog.data?.note}</p>
          </div>
          <div>
            <label className="label">Cooldown (seconds)</label>
            <input
              type="number" min={0} className="input" value={form.cooldown_seconds}
              onChange={(event) =>
                setForm({ ...form, cooldown_seconds: Number(event.target.value) })
              }
            />
          </div>
          <button
            className="btn-primary w-full"
            disabled={busy || form.name.length < 2}
            onClick={() => void createRule()}
          >
            {busy ? 'Creating…' : 'Create rule'}
          </button>
        </div>
      </Modal>

      <Modal
        open={suggestions !== null}
        title="Suggested rules"
        onClose={() => setSuggestions(null)}
        wide
      >
        <div className="space-y-3">
          <p className="text-xs text-ink-400">
            Drafted from this facility's KPIs by {suggestBy || 'the copilot'}. Every suggested
            trigger and action is validated against the registry before it is offered — an
            invented action name is rejected, not executed.
          </p>
          {suggestions?.length === 0 && <Empty title="No suggestions were produced." />}
          {suggestions?.map((suggestion, index) => (
            <div key={index} className="rounded-lg border border-ink-800 p-4">
              <h3 className="text-sm font-semibold text-ink-50">{suggestion.name}</h3>
              <p className="mt-0.5 text-xs text-ink-400">{suggestion.description}</p>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <Badge tone="neutral">on {suggestion.trigger}</Badge>
                {(suggestion.actions ?? []).map((action: any, i: number) => (
                  <Badge key={i} tone="good">{action.action}</Badge>
                ))}
              </div>
              <button
                className="btn-primary btn-sm mt-3"
                onClick={() => void createRule(suggestion, true)}
                disabled={busy}
              >
                Add this rule
              </button>
            </div>
          ))}
        </div>
      </Modal>
    </div>
  )
}
