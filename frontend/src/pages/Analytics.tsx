/** Analytics: KPIs, demand forecasting, bay utilisation and measured allocation. */

import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { useAsync } from '../hooks/useAsync'
import { useFacility } from '../hooks/useFacility'
import { Badge, Card, Empty, ErrorNote, Segmented, Spinner, Stat } from '../components/ui'
import { FacilityPicker } from '../components/FacilityPicker'
import { ParkingMap } from '../components/ParkingMap'
import { LineChart } from '../components/charts/LineChart'
import { BarChart } from '../components/charts/BarChart'
import { clockTime, compactMoney, duration, money, titleCase } from '../lib/format'

export function Analytics() {
  const { facilities, facilityId, select } = useFacility()
  const [days, setDays] = useState<'7' | '14' | '30'>('7')

  const kpis = useAsync(
    async () => (facilityId ? api.kpis(facilityId, Number(days)) : null),
    [facilityId, days],
  )
  const occupancy = useAsync(
    async () => (facilityId ? api.occupancy(facilityId, Number(days) * 24) : null),
    [facilityId, days],
  )
  const forecast = useAsync(async () => (facilityId ? api.forecast(facilityId, 12) : null), [facilityId])
  const heat = useAsync(async () => (facilityId ? api.slotHeat(facilityId) : null), [facilityId])
  const allocation = useAsync(
    async () => (facilityId ? api.allocationPerformance(facilityId, Number(days)) : null),
    [facilityId, days],
  )
  const sustainability = useAsync(
    async () => (facilityId ? api.sustainability(facilityId, Number(days)) : null),
    [facilityId, days],
  )
  const levels = useAsync(async () => (facilityId ? api.levels(facilityId) : []), [facilityId])
  const [levelId, setLevelId] = useState<number | null>(null)
  useEffect(() => {
    if (levels.data?.length && !levels.data.some((l) => l.id === levelId)) {
      setLevelId(levels.data[0].id)
    }
  }, [levels.data, levelId])

  const [training, setTraining] = useState(false)
  const retrain = async () => {
    if (!facilityId) return
    setTraining(true)
    try {
      await api.trainModels(facilityId)
      forecast.refresh()
    } finally {
      setTraining(false)
    }
  }

  const points = occupancy.data?.points ?? []
  const occupancySeries = points.map((point, index) => ({
    x: index, y: point.occupancy_pct * 100, ts: point.ts,
  }))
  const revenueSeries = points.map((point, index) => ({
    x: index, y: point.revenue_minor / 100, ts: point.ts,
  }))
  const arrivalSeries = points.map((point, index) => ({ x: index, y: point.arrivals }))
  const departureSeries = points.map((point, index) => ({ x: index, y: point.departures }))

  const forecastPoints = forecast.data?.points ?? []

  if (facilities.loading && !facilities.data) return <Spinner />
  if (!facilityId) return <Empty title="Create a facility first." />

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Analytics</h1>
          <p className="text-sm text-ink-400">Measured performance, and what the models predict next.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <FacilityPicker facilities={facilities.data ?? []} value={facilityId} onChange={select} />
          <Segmented
            value={days}
            onChange={setDays}
            options={[
              { value: '7', label: '7 days' },
              { value: '14', label: '14 days' },
              { value: '30', label: '30 days' },
            ]}
          />
        </div>
      </div>

      {kpis.error && <ErrorNote message={kpis.error} onRetry={kpis.refresh} />}

      {kpis.data && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="Revenue"
            value={compactMoney(kpis.data.revenue_minor, kpis.data.currency)}
            hint={`${kpis.data.sessions} sessions · ${money(kpis.data.avg_ticket_minor)} average`}
          />
          <Stat
            label="Mean utilisation"
            value={`${kpis.data.utilisation_pct.toFixed(1)}%`}
            hint={`${kpis.data.turnover_per_slot.toFixed(2)} uses per bay`}
          />
          <Stat
            label="Average stay"
            value={duration(kpis.data.avg_stay_minutes)}
            hint={`${kpis.data.avg_walk_distance_m.toFixed(0)} m average walk`}
          />
          <Stat
            label="ANPR confidence"
            value={`${kpis.data.anpr_accuracy_pct.toFixed(1)}%`}
            hint={`${(kpis.data.anpr_review_rate * 100).toFixed(1)}% flagged for review`}
            tone={kpis.data.anpr_review_rate > 0.15 ? 'warn' : 'good'}
          />
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-2">
        <Card title="Occupancy over time" subtitle={`Hourly snapshots across ${days} days`}>
          {occupancy.loading && !occupancy.data ? (
            <Spinner />
          ) : (
            <LineChart
              height={230}
              yMin={0}
              yMax={100}
              yFormat={(value) => `${value.toFixed(0)}%`}
              xFormat={(value) => {
                const point = occupancySeries[Math.round(value)]
                return point ? clockTime(point.ts).slice(0, 5) : ''
              }}
              series={[{ name: 'Occupancy', points: occupancySeries, area: true }]}
              showLegend={false}
              ariaLabel="Occupancy percentage over time"
            />
          )}
        </Card>

        <Card
          title="Demand forecast — next 12 hours"
          subtitle={forecast.data?.note}
          action={
            <button className="btn-ghost btn-sm" onClick={retrain} disabled={training}>
              {training ? 'Training…' : 'Retrain'}
            </button>
          }
        >
          {forecast.loading && !forecast.data ? (
            <Spinner />
          ) : (
            <>
              <LineChart
                height={200}
                yMin={0}
                yMax={100}
                yFormat={(value) => `${value.toFixed(0)}%`}
                xFormat={(value) => {
                  const point = forecastPoints[Math.round(value)]
                  return point ? `${String(point.hour).padStart(2, '0')}:00` : ''
                }}
                series={[
                  {
                    name: 'Predicted occupancy',
                    points: forecastPoints.map((point, index) => ({
                      x: index, y: point.occupancy_pct * 100,
                    })),
                    color: '#d95926',
                    dashed: true,
                    band: forecastPoints.map((point, index) => {
                      const spread = (1 - point.confidence) * 45
                      return {
                        x: index,
                        lo: Math.max(0, point.occupancy_pct * 100 - spread),
                        hi: Math.min(100, point.occupancy_pct * 100 + spread),
                      }
                    }),
                  },
                ]}
                showLegend={false}
                ariaLabel="Forecast occupancy with an uncertainty band"
              />
              {forecast.data && (
                <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-ink-800 pt-3 text-xs">
                  <Badge tone={forecast.data.model === 'seasonal_naive' ? 'warn' : 'good'}>
                    {forecast.data.model.replace(/_/g, ' ')}
                  </Badge>
                  {forecast.data.mae !== null && (
                    <span className="text-ink-400">
                      Holdout MAE{' '}
                      <b className="tabular text-ink-100">
                        {(forecast.data.mae * 100).toFixed(1)} pp
                      </b>{' '}
                      on {forecast.data.trained_rows} observations
                    </span>
                  )}
                  {forecast.data.r2 !== null && (
                    <span className="text-ink-400">
                      R² <b className="tabular text-ink-100">{forecast.data.r2.toFixed(3)}</b>
                    </span>
                  )}
                  <span className="text-ink-500">The band widens with the horizon.</span>
                </div>
              )}
            </>
          )}
        </Card>

        <Card title="Arrivals and departures" subtitle="Hourly flow through the gates">
          <LineChart
            height={200}
            yFormat={(value) => value.toFixed(0)}
            xFormat={(value) => {
              const point = points[Math.round(value)]
              return point ? clockTime(point.ts).slice(0, 5) : ''
            }}
            series={[
              { name: 'Arrivals', points: arrivalSeries },
              { name: 'Departures', points: departureSeries, color: '#d95926' },
            ]}
            ariaLabel="Arrivals and departures per hour"
          />
        </Card>

        <Card title="Revenue per hour" subtitle="Settled charges, in rupees">
          <LineChart
            height={200}
            yFormat={(value) => `₹${value.toFixed(0)}`}
            xFormat={(value) => {
              const point = revenueSeries[Math.round(value)]
              return point ? clockTime(point.ts).slice(0, 5) : ''
            }}
            series={[{ name: 'Revenue', points: revenueSeries, area: true, color: '#199e70' }]}
            showLegend={false}
            ariaLabel="Revenue per hour"
          />
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.4fr_1fr]">
        <Card
          title="Bay utilisation heat map"
          subtitle="Which bays actually get used — the direct read-out of the allocation policy"
          action={
            levels.data && levels.data.length > 1 ? (
              <select
                className="input w-auto py-1 text-xs"
                value={levelId ?? ''}
                onChange={(event) => setLevelId(Number(event.target.value))}
              >
                {levels.data.map((level) => (
                  <option key={level.id} value={level.id}>{level.name}</option>
                ))}
              </select>
            ) : null
          }
        >
          {heat.loading && !heat.data ? (
            <Spinner />
          ) : (
            <ParkingMap
              level={levels.data?.find((l) => l.id === levelId) ?? null}
              slots={(heat.data?.slots ?? []).filter((slot) => slot.level_id === levelId)}
              mode="heat"
              height={340}
            />
          )}
        </Card>

        <div className="space-y-5">
          <Card
            title="Allocation strategies — measured"
            subtitle={allocation.data?.note}
          >
            {allocation.loading && !allocation.data ? (
              <Spinner />
            ) : allocation.data?.strategies.length ? (
              <BarChart
                bars={allocation.data.strategies.map((row) => ({
                  label: `${titleCase(row.strategy)} · ${row.sessions} sessions`,
                  value: row.mean_walk_m ?? 0,
                  highlight: row.strategy === 'hybrid' || row.strategy === 'recency',
                  hint: row.sufficient_sample
                    ? `p90 ${row.p90_walk_m?.toFixed(0)} m · decision ${row.mean_latency_ms?.toFixed(2)} ms`
                    : 'Fewer than 20 sessions — not yet meaningful',
                }))}
                format={(value) => `${value.toFixed(1)} m`}
                ariaLabel="Mean walking distance by allocation strategy"
              />
            ) : (
              <Empty title="No allocations recorded in this window." />
            )}
          </Card>

          {sustainability.data && (
            <Card
              title="Sustainability"
              subtitle="Modelled from stated assumptions — an estimate, not a measurement"
            >
              <div className="space-y-2.5">
                {[
                  ['Guided arrivals', sustainability.data.guided_arrivals],
                  ['Search minutes avoided', sustainability.data.estimated_search_minutes_avoided],
                  ['CO₂ avoided', `${sustainability.data.estimated_co2_avoided_kg} kg`],
                  ['Fuel avoided', `${sustainability.data.estimated_fuel_avoided_litres} L`],
                  ['Paper tickets not printed', sustainability.data.paperless_tickets_issued],
                ].map(([label, value]) => (
                  <div key={label as string} className="flex items-baseline justify-between">
                    <span className="text-sm text-ink-400">{label}</span>
                    <span className="tabular text-sm font-semibold text-ink-50">{value}</span>
                  </div>
                ))}
                <p className="border-t border-ink-800 pt-2.5 text-[11px] leading-relaxed text-ink-500">
                  {sustainability.data.assumptions?.basis}
                </p>
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
