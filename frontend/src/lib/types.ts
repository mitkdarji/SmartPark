/** Shapes mirroring the backend's Pydantic schemas. */

export type Role = 'driver' | 'owner' | 'admin'

export interface User {
  id: number
  email: string
  full_name: string
  phone: string | null
  role: Role
  locale: string
  is_active: boolean
  created_at: string
}

export interface AuthResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export interface Wallet {
  id: number
  balance_minor: number
  held_minor: number
  currency: string
  auto_reload_enabled: boolean
  auto_reload_threshold_minor: number
  auto_reload_amount_minor: number
}

export interface WalletTxn {
  id: number
  txn_type: 'credit' | 'debit' | 'hold' | 'release' | 'refund'
  amount_minor: number
  balance_after_minor: number
  currency: string
  description: string
  reference: string | null
  session_id: number | null
  created_at: string
}

export interface Vehicle {
  id: number
  plate: string
  plate_normalized: string
  make: string | null
  model: string | null
  color: string | null
  vehicle_type: string
  is_ev: boolean
  is_accessible_permit: boolean
}

export interface Facility {
  id: number
  owner_id: number
  name: string
  slug: string
  description: string
  address: string
  city: string
  latitude: number | null
  longitude: number | null
  access_mode: 'public' | 'private'
  allocation_strategy: string
  currency: string
  base_rate_minor_per_hour: number
  free_minutes: number
  billing_increment_minutes: number
  daily_cap_minor: number
  tax_percent: number
  dynamic_pricing_enabled: boolean
  overstay_after_hours: number
  amenities: string[]
  pricing_config: Record<string, unknown>
  is_active: boolean
  publish_to_city_feed: boolean
  created_at: string
  // Present on the summary variant.
  capacity?: number
  occupied?: number
  free?: number
  occupancy_pct?: number
  effective_rate_minor?: number
  active_sessions?: number
}

export interface Level {
  id: number
  facility_id: number
  name: string
  order_index: number
  canvas_width: number
  canvas_height: number
  floorplan_path: string | null
  aisles: number[][][]
  meta: Record<string, unknown>
}

export type SlotStatus = 'empty' | 'occupied' | 'reserved' | 'blocked' | 'maintenance'
export type SlotType =
  | 'standard' | 'compact' | 'large' | 'ev' | 'accessible' | 'two_wheeler'

export interface Slot {
  id: number
  facility_id: number
  level_id: number
  code: string
  zone: string
  x: number
  y: number
  width: number
  height: number
  rotation: number
  slot_type: SlotType
  status: SlotStatus
  is_active: boolean
  has_ev_charger: boolean
  price_multiplier: number
  distance_from_entry: number
  last_vacated_at: string | null
  total_uses: number
}

export interface Gate {
  id: number
  facility_id: number
  level_id: number | null
  name: string
  kind: 'entry' | 'exit' | 'bidirectional'
  x: number
  y: number
  camera_id: string | null
  is_primary: boolean
  is_active: boolean
  last_seen_at: string | null
}

export interface Route {
  waypoints: [number, number][]
  distance_m: number
  instructions: string[]
  level_id: number | null
}

export interface Session {
  id: number
  facility_id: number
  slot_id: number | null
  user_id: number | null
  plate: string
  plate_normalized: string
  entry_at: string
  exit_at: string | null
  status: 'active' | 'completed' | 'denied' | 'abandoned'
  allocation_strategy: string | null
  allocation_reason: string
  walk_distance_m: number
  entry_confidence: number
  exit_confidence: number
  billable_minutes: number
  subtotal_minor: number
  discount_minor: number
  tax_minor: number
  total_minor: number
  currency: string
  payment_status: 'pending' | 'paid' | 'failed' | 'refunded' | 'waived'
  invoice_no: string | null
  rate_breakdown: Record<string, any>
  navigation_path: Route | Record<string, never>
  notes: string
  facility_name?: string | null
  slot_code?: string | null
  zone?: string | null
  duration_minutes?: number
  live_amount_minor?: number | null
}

export interface GateResult {
  allowed?: boolean
  settled?: boolean
  session_id: number | null
  plate: string
  plate_pretty: string
  confidence: number
  reason: string
  slot?: { id: number; code: string; zone: string; level_id: number; type: string; x: number; y: number } | null
  route?: Route | null
  allocation?: {
    strategy: string; reason: string; considered: number
    latency_ms: number; walk_distance_m: number; contention_retries: number
  } | null
  recognition?: RecognitionResult | null
  anomalies: AnomalyFinding[]
  estimated_rate_minor?: number
  occupancy_pct?: number
  duration_minutes?: number
  amount_minor?: number
  payment_status?: string
  invoice_no?: string | null
  quote?: Record<string, any> | null
  wallet_balance_minor?: number | null
  processing_ms: number
  simulation?: { ground_truth_plate: string; difficulty: number; read_correct: boolean }
}

export interface RecognitionResult {
  plate: string
  plate_pretty: string
  confidence: number
  accepted: boolean
  needs_review: boolean
  backend: string
  candidates: {
    plate: string; score: number; confidence: number
    format: string; votes: string[]; matched_known: string | null
  }[]
  bbox: number[]
  processing_ms: number
  detections: number
  reads: number
  escalated: boolean
  error: string | null
}

export interface AnomalyFinding {
  kind: string
  severity: 'low' | 'medium' | 'high' | 'critical'
  score: number
  summary: string
  detail: Record<string, unknown>
  session_id: number | null
}

export interface Anomaly extends AnomalyFinding {
  id: number
  facility_id: number | null
  resolved: boolean
  resolved_at: string | null
  created_at: string
}

export interface KPIs {
  facility_id: number
  period_days: number
  sessions: number
  revenue_minor: number
  avg_stay_minutes: number
  avg_walk_distance_m: number
  avg_ticket_minor: number
  utilisation_pct: number
  turnover_per_slot: number
  payment_success_rate: number
  wallet_sessions: number
  guest_sessions: number
  guest_uncollected: number
  anpr_accuracy_pct: number
  anpr_review_rate: number
  open_anomalies: number
  currency: string
}

export interface ForecastPoint {
  ts: string
  hour: number
  occupancy_pct: number
  occupied_estimate: number | null
  confidence: number
}

export interface Forecast {
  facility_id: number
  horizon_hours: number
  model: string
  mae: number | null
  r2: number | null
  trained_rows: number
  note: string
  points: ForecastPoint[]
}

export interface OccupancyPoint {
  ts: string
  occupancy_pct: number
  capacity: number
  occupied: number
  arrivals: number
  departures: number
  avg_dwell_minutes: number
  revenue_minor: number
  effective_rate_minor: number
}

export interface SlotHeat {
  slot_id: number
  code: string
  zone: string
  level_id: number
  x: number
  y: number
  width: number
  height: number
  status: SlotStatus
  slot_type: SlotType
  total_uses: number
  occupied_minutes: number
  heat: number
  distance_from_entry: number
  last_vacated_at: string | null
}

export interface StrategyRow {
  strategy: string
  sessions?: number
  parked?: number
  trials?: number
  walk_m: number
  effective_walk_m?: number
  p90_walk_m: number
  misallocation_rate?: number
  conflict_rate?: number
  reuse_gap_min?: number
  slot_gini?: number
  rejection_rate?: number
  latency_us?: number
  composite_score?: number
  mean_walk_m?: number
  mean_latency_ms?: number
  sufficient_sample?: boolean
  walk_m_vs_nearest_pct?: number
  conflict_rate_vs_nearest_pct?: number
}

export interface BenchmarkReport {
  lot: { zones: number; slots_per_zone: number; capacity: number }
  workload: { vehicles: number; hours: number; trials: number; ghost_rate: number }
  weights: Record<string, number>
  results: StrategyRow[]
  winner: string
}

export interface AutomationRule {
  id: number
  facility_id: number | null
  name: string
  description: string
  trigger: string
  conditions: Record<string, unknown>
  actions: { action: string; params: Record<string, unknown> }[]
  enabled: boolean
  cooldown_seconds: number
  priority: number
  created_by_ai: boolean
  last_fired_at: string | null
  fire_count: number
  created_at: string
}

export interface AutomationRun {
  id: number
  rule_id: number
  facility_id: number | null
  status: string
  trigger_event: string
  actions_taken: { action: string; result?: unknown; error?: string }[]
  detail: string
  duration_ms: number
  created_at: string
}

export interface AgentReply {
  reply: string
  conversation_id: number | null
  tool_calls: { tool: string; arguments: Record<string, unknown>; result: unknown; latency_ms: number }[]
  pending_confirmation: { tool: string; arguments: Record<string, unknown>; description: string } | null
  iterations: number
  latency_ms: number
  usage: { input_tokens?: number; output_tokens?: number }
  degraded: boolean
  error: string | null
  speakable: string | null
}

export interface VoiceReply {
  transcript: string
  reply: string
  speakable: string
  intent: string | null
  intent_confidence: number
  fast_path: boolean
  conversation_id: number | null
  latency_ms: number
  degraded: boolean
}

export interface Briefing {
  title: string
  body: string
  highlights: string[]
  generated_by: string
  facts: Record<string, any>
  generated_at: string
}

export interface LiveEvent {
  id: string
  topic: string
  ts: string
  facility_id: number | null
  user_id: number | null
  payload: Record<string, any>
}

export interface Health {
  status: string
  version: string
  environment: string
  database: boolean
  genai: { enabled: boolean; model: string }
  anpr: { backends: Record<string, boolean>; active: string[]; min_confidence: number; vision_escalation_enabled: boolean }
  voice: { stt_backend: string; stt_available: boolean; tts_backend: string; intent_router_patterns: number }
  scheduler: boolean
  websocket_clients: number
  timestamp: string
}
