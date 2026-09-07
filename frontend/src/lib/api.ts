/**
 * Typed API client.
 *
 * One place owns the base URL, the bearer token, and error shaping — so every
 * caller gets a real `ApiError` with the backend's own message rather than a
 * bare "fetch failed", and a 401 anywhere clears the session exactly once.
 */

import type {
  AgentReply, Anomaly, AuthResponse, AutomationRule, AutomationRun, BenchmarkReport,
  Briefing, Facility, Forecast, Gate, GateResult, Health, KPIs, Level, OccupancyPoint,
  RecognitionResult, Session, Slot, SlotHeat, StrategyRow, User, Vehicle, VoiceReply,
  Wallet, WalletTxn,
} from './types'

const BASE = '/api/v1'
const TOKEN_KEY = 'smartpark.token'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string = 'error',
    readonly details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* private browsing — the session simply won't persist across reloads */
  }
}

type Options = {
  method?: string
  body?: unknown
  form?: FormData
  auth?: boolean
  query?: Record<string, string | number | boolean | undefined | null>
}

function buildUrl(path: string, query?: Options['query']): string {
  const url = path.startsWith('http') ? path : `${BASE}${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

export async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { method = 'GET', body, form, auth = true, query } = options
  const headers: Record<string, string> = {}

  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
    })
  } catch {
    throw new ApiError('Could not reach the SmartPark server.', 0, 'network_error')
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { error: text }
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      setToken(null)
      window.dispatchEvent(new CustomEvent('smartpark:unauthorized'))
    }
    throw new ApiError(
      payload?.error ?? payload?.detail ?? `Request failed (${response.status})`,
      response.status,
      payload?.code ?? 'error',
      payload?.details ?? {},
    )
  }
  return payload as T
}

const get = <T>(path: string, query?: Options['query']) => request<T>(path, { query })
const post = <T>(path: string, body?: unknown, query?: Options['query']) =>
  request<T>(path, { method: 'POST', body, query })

export const api = {
  // ── Meta ────────────────────────────────────────────────
  health: () => request<Health>('/health', { auth: false }).catch(() =>
    request<Health>('../health' as string, { auth: false }),
  ),

  // ── Auth ────────────────────────────────────────────────
  login: (email: string, password: string) =>
    request<AuthResponse>('/auth/login', { method: 'POST', body: { email, password }, auth: false }),
  register: (payload: {
    email: string; password: string; full_name: string
    role?: string; phone?: string; plate?: string
  }) => request<AuthResponse>('/auth/register', { method: 'POST', body: payload, auth: false }),
  me: () => get<User>('/auth/me'),

  // ── Wallet ──────────────────────────────────────────────
  wallet: () => get<Wallet>('/wallet'),
  topUp: (amount_minor: number) => post<Wallet>('/wallet/topup', { amount_minor }),
  transactions: (limit = 50) => get<WalletTxn[]>('/wallet/transactions', { limit }),
  setAutoReload: (payload: { enabled: boolean; threshold_minor: number; amount_minor: number }) =>
    request<Wallet>('/wallet/auto-reload', { method: 'PUT', body: payload }),
  reconcile: () => get<Record<string, any>>('/wallet/reconcile'),

  // ── Vehicles ────────────────────────────────────────────
  vehicles: () => get<Vehicle[]>('/vehicles'),
  addVehicle: (payload: Partial<Vehicle> & { plate: string }) => post<Vehicle>('/vehicles', payload),
  deleteVehicle: (id: number) => request<void>(`/vehicles/${id}`, { method: 'DELETE' }),

  // ── Facilities ──────────────────────────────────────────
  myFacilities: () => get<Facility[]>('/facilities'),
  searchFacilities: (query: { city?: string; q?: string }) =>
    get<Facility[]>('/facilities/search', query),
  facility: (id: number) => get<Facility>(`/facilities/${id}`),
  createFacility: (payload: Record<string, unknown>) => post<Facility>('/facilities', payload),
  updateFacility: (id: number, payload: Record<string, unknown>) =>
    request<Facility>(`/facilities/${id}`, { method: 'PATCH', body: payload }),

  levels: (id: number) => get<Level[]>(`/facilities/${id}/levels`),
  updateLevel: (id: number, levelId: number, payload: Record<string, unknown>) =>
    request<Level>(`/facilities/${id}/levels/${levelId}`, { method: 'PATCH', body: payload }),
  slots: (id: number, levelId?: number) =>
    get<Slot[]>(`/facilities/${id}/slots`, { level_id: levelId }),
  saveLayout: (id: number, levelId: number, slots: unknown[]) =>
    request<Slot[]>(`/facilities/${id}/slots`, {
      method: 'PUT', body: { level_id: levelId, slots },
    }),
  generateGrid: (id: number, query: Record<string, string | number>) =>
    post<Slot[]>(`/facilities/${id}/slots/generate`, undefined, query),
  setSlotStatus: (id: number, slotId: number, status: string) =>
    request<Slot>(`/facilities/${id}/slots/${slotId}/status`, {
      method: 'PATCH', body: { status },
    }),
  gates: (id: number) => get<Gate[]>(`/facilities/${id}/gates`),
  createGate: (id: number, payload: Record<string, unknown>) =>
    post<Gate>(`/facilities/${id}/gates`, payload),
  authorized: (id: number) => get<any[]>(`/facilities/${id}/authorized`),
  addAuthorized: (id: number, payload: Record<string, unknown>) =>
    post<any>(`/facilities/${id}/authorized`, payload),
  removeAuthorized: (id: number, recordId: number) =>
    request<void>(`/facilities/${id}/authorized/${recordId}`, { method: 'DELETE' }),
  uploadFloorplan: (id: number, levelId: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ floorplan_path: string; detected_slots: any[]; note?: string }>(
      `/facilities/${id}/floorplan?level_id=${levelId}&detect=true`,
      { method: 'POST', form },
    )
  },

  // ── Gates ───────────────────────────────────────────────
  scanEntry: (payload: { facility_id: number; plate: string; gate_id?: number; vehicle_type?: string; needs_charger?: boolean }) =>
    post<GateResult>('/gates/scan/entry', payload),
  scanExit: (payload: { facility_id: number; plate: string; gate_id?: number }) =>
    post<GateResult>('/gates/scan/exit', payload),
  simulateGate: (facilityId: number, query: { direction: string; difficulty?: number; plate?: string; gate_id?: number }) =>
    post<GateResult>(`/gates/${facilityId}/simulate`, undefined, query),
  gateImage: (facilityId: number, direction: 'entry' | 'exit', file: File, gateId?: number) => {
    const form = new FormData()
    form.append('facility_id', String(facilityId))
    form.append('image', file)
    if (gateId) form.append('gate_id', String(gateId))
    return request<GateResult>(`/gates/${direction}`, { method: 'POST', form })
  },

  // ── Sessions ────────────────────────────────────────────
  mySessions: (limit = 20) =>
    get<{ items: Session[]; total: number }>('/sessions/me', { limit }),
  activeSession: () => get<Session | null>('/sessions/active'),
  session: (id: number) => get<Session>(`/sessions/${id}`),
  explainBill: (id: number) => get<{ explanation: string; generated_by: string }>(`/sessions/${id}/explain`),
  facilitySessions: (id: number, query: Record<string, any> = {}) =>
    get<{ items: Session[]; total: number }>(`/facilities/${id}/sessions`, query),
  recognitions: (id: number, query: Record<string, any> = {}) =>
    get<any[]>(`/facilities/${id}/recognitions`, query),
  anomalies: (id: number, resolved = false) =>
    get<Anomaly[]>(`/facilities/${id}/anomalies`, { resolved }),
  resolveAnomaly: (id: number, anomalyId: number, note = '') =>
    post<Anomaly>(`/facilities/${id}/anomalies/${anomalyId}/resolve`, undefined, { note }),

  // ── Analytics ───────────────────────────────────────────
  kpis: (id: number, days = 7) => get<KPIs>(`/facilities/${id}/analytics/kpis`, { days }),
  occupancy: (id: number, hours = 168) =>
    get<{ points: OccupancyPoint[] }>(`/facilities/${id}/analytics/occupancy`, { hours }),
  forecast: (id: number, hours = 6) =>
    get<Forecast>(`/facilities/${id}/analytics/forecast`, { hours }),
  slotHeat: (id: number) => get<{ slots: SlotHeat[] }>(`/facilities/${id}/analytics/slots`),
  allocationPerformance: (id: number, days = 30) =>
    get<{ strategies: StrategyRow[]; note: string }>(`/facilities/${id}/analytics/allocation`, { days }),
  sustainability: (id: number, days = 30) =>
    get<Record<string, any>>(`/facilities/${id}/analytics/sustainability`, { days }),
  trainModels: (id: number) => post<Record<string, any>>(`/facilities/${id}/analytics/train`),

  // ── Evaluation ──────────────────────────────────────────
  benchmarkAllocation: (payload: Record<string, unknown>) =>
    post<BenchmarkReport>('/benchmark/allocation', payload),
  benchmarkAnpr: (samples = 25, difficulty = 0.35) =>
    get<Record<string, any>>('/benchmark/anpr', { samples, difficulty }),

  // ── ANPR ────────────────────────────────────────────────
  anprStatus: () => get<Record<string, any>>('/anpr/status'),
  anprSample: (query: { plate?: string; difficulty?: number; seed?: number }) =>
    get<{ ground_truth_plate: string; pretty: string; image_base64: string; media_type: string }>(
      '/anpr/sample.json', query,
    ),
  anprRecognize: (file: File, knownPlates: string[] = []) => {
    const form = new FormData()
    form.append('image', file)
    return request<RecognitionResult>(
      `/anpr/recognize?known_plates=${encodeURIComponent(knownPlates.join(','))}`,
      { method: 'POST', form },
    )
  },
  validatePlate: (plate: string) => get<Record<string, any>>('/anpr/validate', { plate }),

  // ── AI ──────────────────────────────────────────────────
  agentChat: (payload: {
    message: string; conversation_id?: number | null; facility_id?: number | null
    channel?: string; confirm_tool?: string; confirm_arguments?: Record<string, unknown>
  }) => post<AgentReply>('/agent/chat', payload),
  agentTools: () => get<{ role: string; generative_ai_enabled: boolean; tools: any[] }>('/agent/tools'),
  voiceConverse: (payload: { text: string; conversation_id?: number | null; facility_id?: number | null }) =>
    post<VoiceReply>('/voice/converse', payload),
  voiceStatus: () => get<Record<string, any>>('/voice/status'),
  briefing: (id: number, days = 7) => get<Briefing>(`/facilities/${id}/ai/briefing`, { days }),
  triage: (id: number) => get<Record<string, any>>(`/facilities/${id}/ai/triage`),
  suggestAutomations: (id: number) => get<Record<string, any>>(`/facilities/${id}/ai/suggest-automations`),

  // ── Automation ──────────────────────────────────────────
  automationCatalog: () => get<{ triggers: string[]; actions: string[]; note: string }>('/automation/catalog'),
  rules: (facilityId?: number) => get<AutomationRule[]>('/automation/rules', { facility_id: facilityId }),
  createRule: (payload: Record<string, unknown>, createdByAi = false) =>
    post<AutomationRule>('/automation/rules', payload, { created_by_ai: createdByAi }),
  updateRule: (id: number, payload: Record<string, unknown>) =>
    request<AutomationRule>(`/automation/rules/${id}`, { method: 'PATCH', body: payload }),
  deleteRule: (id: number) => request<void>(`/automation/rules/${id}`, { method: 'DELETE' }),
  runs: (facilityId?: number, limit = 30) =>
    get<AutomationRun[]>('/automation/runs', { facility_id: facilityId, limit }),
  tick: () => post<Record<string, any>>('/automation/tick'),

  // ── Smart city ──────────────────────────────────────────
  cityFeed: () => request<Record<string, any>>('/city/availability', { auth: false }),
}
