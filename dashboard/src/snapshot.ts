/** Frozen GET /snapshot field names. Never rename. Cards on `/` bind to these. */

export const STAGE_LABELS = [
  "placed",
  "confirmed",
  "being prepared",
  "ready",
  "out for delivery",
  "delivered",
] as const;

export type StageLabel = (typeof STAGE_LABELS)[number];

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
};

export type OrderTrace = {
  order_id: string;
  order_events: TraceEvent[];
  attempts: TraceAttempt[];
};

export type Snapshot = {
  cohort_id: string;
  stages: Record<string, number>;
  terminal_rates_per_min: TerminalRates;
  e2e_latency_s: E2eLatency;
  conservation: Conservation;
  duplicate_attempts: number;
  duplicate_effects: number;
  startup_scan: number;
  invalid_transitions: number;
  state_vs_last_order_events_mismatches: number;
  currently_leased: number;
  trace: OrderTrace | null;
};

export const POLL_MS = 1000;

export function snapshotUrl(opts?: { cohortId?: string; orderId?: string }): string {
  const base = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
  const path = `${base}/snapshot`;
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
