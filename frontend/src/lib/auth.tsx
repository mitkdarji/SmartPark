/** Authentication context: token storage, current user, and role helpers. */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, getToken, setToken } from './api'
import type { User } from './types'

interface AuthState {
  user: User | null
  loading: boolean
  login: (email: string, password: string) => Promise<User>
  register: (payload: Parameters<typeof api.register>[0]) => Promise<User>
  logout: () => void
  isOwner: boolean
  isDriver: boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  // Restore the session on first mount, and drop it if the token has expired.
  useEffect(() => {
    let cancelled = false
    async function restore() {
      if (!getToken()) {
        setLoading(false)
        return
      }
      try {
        const me = await api.me()
        if (!cancelled) setUser(me)
      } catch {
        setToken(null)
        if (!cancelled) setUser(null)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void restore()
    return () => { cancelled = true }
  }, [])

  // Any 401 from anywhere in the app ends the session exactly once.
  useEffect(() => {
    const handler = () => setUser(null)
    window.addEventListener('smartpark:unauthorized', handler)
    return () => window.removeEventListener('smartpark:unauthorized', handler)
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const response = await api.login(email, password)
    setToken(response.access_token)
    setUser(response.user)
    return response.user
  }, [])

  const register = useCallback(async (payload: Parameters<typeof api.register>[0]) => {
    const response = await api.register(payload)
    setToken(response.access_token)
    setUser(response.user)
    return response.user
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo<AuthState>(
    () => ({
      user, loading, login, register, logout,
      isOwner: user?.role === 'owner' || user?.role === 'admin',
      isDriver: user?.role === 'driver',
    }),
    [user, loading, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
