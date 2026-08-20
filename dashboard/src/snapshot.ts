/** Frozen GET /snapshot field names. Never rename. Cards on `/` bind to these. */

/** Assignment lifecycle names. Stored API values remain unchanged. */
export const STAGE_LABELS = [
  "placed",
  "confirmed",
  "being prepared",
  "ready",
  "out for delivery",
  "delivered",
] as const;

export type StageLabel = (typeof STAGE_LABELS)[number];

/** Short presentation descriptions without simulator-specific equipment. */
export const STAGE_SEAMS: Partial<Record<StageLabel, string>> = {
  confirmed: "accepted — queued for preparation",
  "being prepared": "preparation underway",
  ready: "waiting for courier assignment or pickup",
  "out for delivery": "picked up — courier en route",
};

export type TerminalRates = {
  delivered: number;
  cancelled: number;
  failed: number;
};

export type E2eLatency = {
  p50: number | null;
  p95: number | null;
};

export type Conservation = {
  accepted: number;
  delivered: number;
  cancelled: number;
  failed: number;
  in_flight: number;
  parked: number;
  residual: number;
};

export type TraceEvent = {
  id: string;
  from_state: string | null;
  to_state: string;
  actor: string;
  cause: string;
  timestamp: string;
  applied: boolean;
};

export type TraceAttempt = {
  id: string;
  work_item_id: string;
  work_type: string;
  started_at: string;
  ended_at: string | null;
  lease_owner: string;
  outcome: string | null;
  idempotency_key: string;
};

export type OrderTrace = {
  order_id: string;
  order_events: TraceEvent[];
  attempts: TraceAttempt[];
};

export type OrderSummary = {
  id: string;
  state: string;
  accepted_at: string;
  items: string[];
};

export type AcceptReject = {
  accepted: number;
  rejected: number;
};

export type OldestOpen = {
  age_s: number | null;
  stage: string | null;
};

export type Http429s = {
  door: number;
  kitchen: number;
  courier: number;
};

export type StretchingEtas = {
  count: number;
  max_stretch_s: number | null;
};

export type ParkedRow = {
  id: string;
  order_id: string;
  work_type: string;
  owner: string | null;
  reason: string | null;
  next_action: string | null;
};

export type LeasedRow = {
  id: string;
  order_id: string;
  work_type: string;
  owner: string | null;
  lease_until: string;
};

export type SimHttpLane = {
  requests_per_min: number;
  latency_p50_s: number | null;
  latency_p95_s: number | null;
  timeout: number;
  http_5xx: number;
  http_429: number;
};

export type SimHttp = {
  restaurant: SimHttpLane;
  courier: SimHttpLane;
};

export type SlotUse = {
  used: number;
  cap: number;
  per_worker_cap: number;
};

export type OutboundSlots = {
  worker_replicas: number;
  restaurant: SlotUse;
  courier: SlotUse;
  task: SlotUse;
};

export type NoProgress = {
  threshold_s: number;
  count: number;
};

export type Snapshot = {
  cohort_id: string;
  stages: Record<string, number>;
  terminal_rates_per_min: TerminalRates;
  e2e_latency_s: E2eLatency;
  conservation: Conservation;
  duplicate_attempts: number;
  duplicate_effects: number | null;
  startup_scan: number;
  invalid_transitions: number;
  state_vs_last_order_events_mismatches: number;
  currently_leased: number;
  currently_leased_items: LeasedRow[];
  orders: OrderSummary[];
  trace: OrderTrace | null;
  accept_reject: AcceptReject;
  backlog: Record<string, number>;
  retry_rate: number;
  oldest_open: OldestOpen;
  oldest_unparked?: OldestOpen;
  http_429s: Http429s;
  stretching_etas: StretchingEtas;
  parked_list: ParkedRow[];
  sim_http: SimHttp;
  outbound_slots: OutboundSlots;
  no_progress_beyond_threshold: NoProgress;
  orphaned_tickets: number;
};

export const POLL_MS = 1000;

function apiPath(path: string): string {
  const base = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
  return `${base}${path}`;
}

export type LoadgenStatus = {
  cohort_id: string;
  h: number | null;
  /** "fallback" until a calibrate run measures this host. */
  h_source?: string;
  calibrated?: boolean;
  rate_rps: number;
  offered?: number;
  placed: number;
  rejected_429: number;
  other_http?: number;
  transport_unknown?: number;
  running: boolean;
};

export type SimFaultStatus = {
  mode: string;
  mix: string;
  blackout_remaining_s: number;
  confirm_unavailable: string[];
};

export type SimFaults = {
  restaurant: SimFaultStatus;
  courier: SimFaultStatus;
};

export async function fetchLoadgenStatus(signal?: AbortSignal): Promise<LoadgenStatus> {
  const response = await fetch("/loadgen/status", { signal });
  if (!response.ok) {
    throw new Error(`GET /loadgen/status ${response.status}`);
  }
  return (await response.json()) as LoadgenStatus;
}

export async function fetchSimFaults(signal?: AbortSignal): Promise<SimFaults> {
  const [restaurant, courier] = await Promise.all([
    fetch("/rsim/admin/faults", { signal }),
    fetch("/csim/admin/faults", { signal }),
  ]);
  if (!restaurant.ok) {
    throw new Error(`GET /rsim/admin/faults ${restaurant.status}`);
  }
  if (!courier.ok) {
    throw new Error(`GET /csim/admin/faults ${courier.status}`);
  }
  return {
    restaurant: (await restaurant.json()) as SimFaultStatus,
    courier: (await courier.json()) as SimFaultStatus,
  };
}

/** Restaurant menu counters. Same ids as the sim; not a GET /snapshot field. */
export const MENU_ITEMS = ["chips", "taco", "burrito"] as const;

export type MenuItemId = (typeof MENU_ITEMS)[number];

export type MenuStock = Record<MenuItemId, number>;

export async function fetchMenuStock(signal?: AbortSignal): Promise<MenuStock> {
  const response = await fetch("/rsim/admin/stock", { signal });
  if (!response.ok) {
    throw new Error(`GET /rsim/admin/stock ${response.status}`);
  }
  return (await response.json()) as MenuStock;
}

export function stockLine(stock: MenuStock): string {
  return MENU_ITEMS.map((item) => `${item} ${stock[item]}`).join(" · ");
}

export function snapshotUrl(opts?: { cohortId?: string; orderId?: string }): string {
  const path = apiPath("/snapshot");
  const params = new URLSearchParams();
  if (opts?.cohortId) {
    params.set("cohort_id", opts.cohortId);
  }
  if (opts?.orderId) {
    params.set("order_id", opts.orderId);
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

export type RedriveResponse = {
  id: string;
  order_id: string;
  work_type: string;
  status: string;
  attempt_count: number;
  next_attempt_at: string | null;
  idempotency_key: string;
};

export async function redriveWorkItem(workItemId: string): Promise<RedriveResponse> {
  const path = `/work-items/${encodeURIComponent(workItemId)}/redrive`;
  const response = await fetch(apiPath(path), { method: "POST" });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`POST ${path} ${response.status}: ${body.slice(0, 200)}`);
  }
  return (await response.json()) as RedriveResponse;
}

export async function fetchSnapshot(opts?: {
  cohortId?: string;
  orderId?: string;
  signal?: AbortSignal;
}): Promise<Snapshot> {
  const response = await fetch(snapshotUrl(opts), { signal: opts?.signal });
  if (!response.ok) {
    throw new Error(`GET /snapshot ${response.status}`);
  }
  return (await response.json()) as Snapshot;
}
