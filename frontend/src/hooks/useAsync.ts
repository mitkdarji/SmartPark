/** Data-fetching hook with loading, error and manual refresh. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../lib/api'

export interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
  refresh: () => void
  setData: (value: T | null) => void
}

export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: { immediate?: boolean } = {},
): AsyncState<T> {
  const { immediate = true } = options
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(immediate)
  const [nonce, setNonce] = useState(0)

  // Guard against a slow earlier request overwriting a newer result.
  const requestId = useRef(0)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  useEffect(() => {
    if (!immediate && nonce === 0) return
    const id = ++requestId.current
    let cancelled = false
    setLoading(true)

    loaderRef.current()
      .then((result) => {
        if (cancelled || id !== requestId.current) return
        setData(result)
        setError(null)
      })
      .catch((err: unknown) => {
        if (cancelled || id !== requestId.current) return
        setError(err instanceof ApiError ? err.message : 'Something went wrong.')
      })
      .finally(() => {
        if (!cancelled && id === requestId.current) setLoading(false)
      })

    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])
  return { data, error, loading, refresh, setData }
}

/** Re-runs `fn` on an interval, pausing while the tab is hidden. */
export function useInterval(fn: () => void, ms: number | null) {
  const saved = useRef(fn)
  saved.current = fn

  useEffect(() => {
    if (ms === null) return
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') saved.current()
    }, ms)
    return () => window.clearInterval(id)
  }, [ms])
}
