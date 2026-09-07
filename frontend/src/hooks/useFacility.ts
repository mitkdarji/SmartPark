/** Selected-facility state, persisted so the operator's choice survives reloads. */

import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { useAsync } from './useAsync'
import type { Facility } from '../lib/types'

const KEY = 'smartpark.facility'

export function useFacility() {
  const facilities = useAsync(() => api.myFacilities(), [])
  const [facilityId, setFacilityId] = useState<number | null>(() => {
    const stored = Number(localStorage.getItem(KEY))
    return Number.isFinite(stored) && stored > 0 ? stored : null
  })

  // Fall back to the first facility if the stored one no longer exists.
  useEffect(() => {
    if (!facilities.data?.length) return
    const known = facilities.data.some((f) => f.id === facilityId)
    if (!known) {
      const first = facilities.data[0].id
      setFacilityId(first)
      localStorage.setItem(KEY, String(first))
    }
  }, [facilities.data, facilityId])

  const select = useCallback((id: number) => {
    setFacilityId(id)
    localStorage.setItem(KEY, String(id))
  }, [])

  const facility: Facility | null =
    facilities.data?.find((f) => f.id === facilityId) ?? facilities.data?.[0] ?? null

  return { facilities, facility, facilityId: facility?.id ?? null, select }
}
