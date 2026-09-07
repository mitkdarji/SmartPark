/** Facility discovery with live availability and the current rate. */

import { useState } from 'react'
import { api } from '../lib/api'
import { useAsync, useInterval } from '../hooks/useAsync'
import { Badge, Card, Empty, ErrorNote, ProgressBar, Spinner } from '../components/ui'
import { money } from '../lib/format'

export function FindParking() {
  const [city, setCity] = useState('')
  const [query, setQuery] = useState('')
  const facilities = useAsync(() => api.searchFacilities({ city, q: query }), [city, query])

  useInterval(() => facilities.refresh(), 30_000)

  return (
    <div className="space-y-5">
      <Card title="Find parking" subtitle="Live availability across every SmartPark facility">
        <div className="flex flex-wrap gap-3">
          <input
            className="input max-w-xs"
            placeholder="City"
            value={city}
            onChange={(event) => setCity(event.target.value)}
          />
          <input
            className="input max-w-sm"
            placeholder="Name or address"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="btn-ghost" onClick={facilities.refresh}>Refresh</button>
        </div>
      </Card>

      {facilities.error && <ErrorNote message={facilities.error} onRetry={facilities.refresh} />}
      {facilities.loading && !facilities.data && <Spinner />}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {facilities.data?.map((facility) => {
          const occupancy = (facility.occupancy_pct ?? 0) / 100
          const free = facility.free ?? 0
          return (
            <Card key={facility.id} title={facility.name} subtitle={facility.address || facility.city}>
              <div className="space-y-3">
                <div className="flex items-baseline justify-between">
                  <span className="text-2xl font-bold tabular text-ink-50">
                    {free}
                    <span className="ml-1 text-sm font-normal text-ink-500">
                      / {facility.capacity ?? 0} free
                    </span>
                  </span>
                  <Badge tone={facility.access_mode === 'private' ? 'violet' : 'good'}>
                    {facility.access_mode}
                  </Badge>
                </div>

                <ProgressBar value={occupancy} />
                <p className="text-xs text-ink-400">
                  {(facility.occupancy_pct ?? 0).toFixed(0)}% occupied
                </p>

                <div className="flex items-center justify-between border-t border-ink-800 pt-3">
                  <div>
                    <p className="text-[11px] uppercase tracking-wider text-ink-500">Current rate</p>
                    <p className="text-lg font-semibold tabular text-ink-50">
                      {money(facility.effective_rate_minor ?? 0, facility.currency)}
                      <span className="text-xs font-normal text-ink-500">/hour</span>
                    </p>
                  </div>
                  {facility.dynamic_pricing_enabled &&
                    (facility.effective_rate_minor ?? 0) > facility.base_rate_minor_per_hour && (
                      <Badge tone="warn">Demand pricing</Badge>
                    )}
                </div>

                {facility.amenities.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {facility.amenities.slice(0, 4).map((amenity) => (
                      <span
                        key={amenity}
                        className="rounded-full bg-ink-800/70 px-2 py-0.5 text-[10px] text-ink-300"
                      >
                        {amenity.replace(/_/g, ' ')}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </Card>
          )
        })}
      </div>

      {facilities.data?.length === 0 && (
        <Empty title="No facilities matched that search." hint="Try clearing the filters." />
      )}
    </div>
  )
}
