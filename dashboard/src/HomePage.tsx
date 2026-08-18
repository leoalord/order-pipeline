import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";

import {
  PresenterRail,
  scenarioLabel,
  type ScenarioId,
} from "./ControlPage";
import {
  fetchLoadgenStatus,
  fetchSimFaults,
  fetchSnapshot,
  POLL_MS,
  redriveWorkItem,
  STAGE_LABELS,
  STAGE_SEAMS,
  type LoadgenStatus,
  type OrderSummary,
  type SimHttpLane,
  type SimFaultStatus,
  type SimFaults,
  type Snapshot,
  type StageLabel,
} from "./snapshot";

type DetailPanel =
  | { kind: "order"; orderId: string }
  | { kind: "zone"; zone: "restaurant" | "delivery" }
  | { kind: "correctness" }
  | { kind: "system"; system: "worker" };

type HealthTone = "healthy" | "pressure" | "fault";

function simFaultActive(status: SimFaultStatus | undefined): boolean {
  return Boolean(
    status &&
      (status.mode !== "off" ||
        status.blackout_remaining_s > 0 ||
        status.confirm_unavailable.length > 0),
  );
}

function simFaultLabel(status: SimFaultStatus | undefined, normal: string): string {
  if (!status) return normal;
  if (status.blackout_remaining_s > 0) {
    return `⚠ Blackout · ${Math.ceil(status.blackout_remaining_s)}s`;
  }
  const targeted = status.confirm_unavailable.length;
  if (targeted > 0) {
    return `⚠ ${targeted} targeted confirm${targeted === 1 ? "" : "s"}`;
  }
  return status.mode === "off" ? normal : "⚠ Fault active";
}

const ORDER_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const API_STATE_BY_STAGE: Record<StageLabel, string> = {
  placed: "placed",
  confirmed: "confirmed",
  "being prepared": "being_prepared",
  ready: "ready",
  "out for delivery": "out_for_delivery",
  delivered: "delivered",
};

const STAGE_DESCRIPTIONS: Record<StageLabel, string> = {
  placed: "Order received",
  confirmed: "Restaurant accepted",
  "being prepared": "Preparation underway",
  ready: "Ready for pickup",
  "out for delivery": "Courier en route",
  delivered: "Completed",
};

const SCENARIO_COPY: Record<
  ScenarioId,
  { title: string; body: string; tone: "neutral" | "success" | "warning" | "danger" }
> = {
  ready: {
    title: "System ready",
    body: "Open Presenter controls to start the walkthrough.",
    tone: "neutral",
  },
  normal: {
    title: "Normal flow",
    body: "Follow the focused ticket across the handoff. Counts reconcile as work completes.",
    tone: "success",
  },
  rush: {
    title: "Rush pressure",
    body: "Watch stage stacks, busy responses, and the oldest order rise before the drain.",
    tone: "warning",
  },
  outage: {
    title: "Restaurant outage",
    body: "Targeted confirmations retry with the same keys while ordinary orders keep flowing.",
    tone: "danger",
  },
  worker_crash: {
    title: "Worker crash",
    body: "A visible lease identifies the owner; a survivor resumes with the same idempotency key.",
    tone: "danger",
  },
  courier_failure: {
    title: "Courier failure",
    body: "Dispatch parks without changing the lifecycle. Recover, then Redrive the same work item.",
    tone: "danger",
  },
};

function fmt(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(digits);
}

function displayCode(id: string): string {
  const hex = id.replaceAll("-", "").slice(-8);
  const code = Number.parseInt(hex, 16).toString(36).toUpperCase();
  return code.slice(-4).padStart(4, "0");
}

function stateLabel(state: string): string {
  if (state === "being_prepared") return "Being prepared";
  if (state === "out_for_delivery") return "Out for delivery";
  return state.charAt(0).toUpperCase() + state.slice(1);
}

function ageLabel(iso: string): string {
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

function healthForLane(lane: SimHttpLane | undefined, used = 0, cap = 1): HealthTone {
  if (!lane) return "pressure";
  if (lane.timeout + lane.http_5xx > 0) return "fault";
  if (lane.http_429 > 0 || used / Math.max(cap, 1) >= 0.75) return "pressure";
  return "healthy";
}

function healthLabel(tone: HealthTone): string {
  return { healthy: "Healthy", pressure: "Pressure", fault: "Fault active" }[
    tone
  ];
}

function TicketButton({
  order,
  focused,
  onClick,
}: {
  order: OrderSummary;
  focused: boolean;
  onClick: (event: ReactMouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <button
      type="button"
      className={focused ? "order-ticket focused" : "order-ticket"}
      onClick={onClick}
      aria-label={`Order ${displayCode(order.id)}, ${stateLabel(order.state)}, ${order.items.join(", ")}`}
      aria-pressed={focused}
    >
      <span className="ticket-code">{displayCode(order.id)}</span>
      <span className="ticket-items">
        {order.items.length} {order.items.length === 1 ? "item" : "items"}
      </span>
    </button>
  );
}

function StageTickets({
  stage,
  count,
  orders,
  focusedOrderId,
  onOrder,
}: {
  stage: StageLabel;
  count: number;
  orders: OrderSummary[];
  focusedOrderId: string | null;
  onOrder: (orderId: string, trigger: HTMLButtonElement) => void;
}) {
  const state = API_STATE_BY_STAGE[stage];
  const inStage = orders.filter((order) => order.state === state);
  const focused = inStage.find((order) => order.id === focusedOrderId);
  const visible = [
    ...(focused ? [focused] : []),
    ...inStage.filter((order) => order.id !== focusedOrderId),
  ].slice(0, 3);
  const hidden = Math.max(0, count - visible.length);

  return (
    <div className="ticket-pile" aria-label={`${count} orders in ${stage}`}>
      {visible.map((order) => (
        <TicketButton
          key={order.id}
          order={order}
          focused={order.id === focusedOrderId}
          onClick={(event) => onOrder(order.id, event.currentTarget)}
        />
      ))}
      {hidden > 0 ? (
        <span className="ticket-stack" aria-label={`${hidden} additional orders`}>
          <span aria-hidden="true" />
          <strong>+{hidden}</strong>
          <small>stacked</small>
        </span>
      ) : null}
      {count === 0 ? <span className="empty-stage">Clear</span> : null}
    </div>
  );
}

function Metric({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone?: HealthTone | "neutral";
}) {
  return (
    <div className={`evidence-metric ${tone ?? "neutral"}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function ScenarioFacts({
  scenario,
  snapshot,
  simFaults,
}: {
  scenario: ScenarioId;
  snapshot: Snapshot | null;
  simFaults: SimFaults | null;
}) {
  const backlog = snapshot?.backlog;
  const backlogTotal =
    (backlog?.confirm ?? 0) +
    (backlog?.poll_cook ?? 0) +
    (backlog?.dispatch ?? 0) +
    (backlog?.poll_ride ?? 0);
  const busy = snapshot?.http_429s;
  const busyTotal = (busy?.door ?? 0) + (busy?.kitchen ?? 0) + (busy?.courier ?? 0);
  const restaurantErrors =
    (snapshot?.sim_http.restaurant.timeout ?? 0) +
    (snapshot?.sim_http.restaurant.http_5xx ?? 0);

  if (scenario === "rush") {
    return (
      <div className="scenario-facts" aria-label="Rush evidence">
        <span><b>{backlogTotal}</b><small>backlog</small></span>
        <span title={`Door ${busy?.door ?? 0}, kitchen ${busy?.kitchen ?? 0}, courier ${busy?.courier ?? 0}`}><b>{busyTotal}</b><small>busy 429s</small></span>
        <span><b>{fmt(snapshot?.stretching_etas.max_stretch_s)}s</b><small>ETA stretch</small></span>
      </div>
    );
  }
  if (scenario === "outage") {
    return (
      <div className="scenario-facts" aria-label="Outage evidence">
        <span><b>{fmt(snapshot?.retry_rate, 2)}</b><small>retry rate</small></span>
        <span><b>{restaurantErrors}</b><small>dependency errors</small></span>
        <span><b>{simFaults?.restaurant.confirm_unavailable.length ?? 0}</b><small>targeted confirms</small></span>
      </div>
    );
  }
  if (scenario === "worker_crash") {
    return (
      <div className="scenario-facts" aria-label="Worker crash evidence">
        <span><b>{fmt(snapshot?.currently_leased)}</b><small>leased</small></span>
        <span><b>{fmt(snapshot?.duplicate_attempts)}</b><small>retry attempts</small></span>
        <span><b>{fmt(snapshot?.duplicate_effects)}</b><small>duplicate effects</small></span>
      </div>
    );
  }
  if (scenario === "courier_failure") {
    return (
      <div className="scenario-facts" aria-label="Courier failure evidence">
        <span><b>{fmt(snapshot?.parked_list.length)}</b><small>parked</small></span>
        <span><b>{fmt(snapshot?.sim_http.courier.http_5xx)}</b><small>courier 5xx</small></span>
        <span><b>{fmt(snapshot?.backlog.dispatch)}</b><small>dispatch backlog</small></span>
      </div>
    );
  }
  return (
    <div className="scenario-facts" aria-label="Normal flow evidence">
      <span><b>{fmt(snapshot?.accept_reject.accepted)}</b><small>accepted</small></span>
      <span><b>{fmt(snapshot?.accept_reject.rejected)}</b><small>door rejects</small></span>
      <span><b>{fmt(snapshot?.conservation.in_flight)}</b><small>in flight</small></span>
    </div>
  );
}

export function HomePage() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loadgen, setLoadgen] = useState<LoadgenStatus | null>(null);
  const [simFaults, setSimFaults] = useState<SimFaults | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focusedOrderId, setFocusedOrderId] = useState<string | null>(null);
  const [lookupId, setLookupId] = useState("");
  const [detailPanel, setDetailPanel] = useState<DetailPanel | null>(null);
  const [railOpen, setRailOpen] = useState(false);
  const [redriving, setRedriving] = useState<string | null>(null);
  const [redriveStatus, setRedriveStatus] = useState<string | null>(null);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const [activeScenario, setActiveScenario] = useState<ScenarioId>(() => {
    const stored = window.sessionStorage.getItem("order-pipeline-scenario");
    return (
      stored &&
      ["ready", "normal", "rush", "outage", "worker_crash", "courier_failure"].includes(
        stored,
      )
        ? stored
        : "ready"
    ) as ScenarioId;
  });
  const cohortIdRef = useRef<string | null>(null);
  const presenterButtonRef = useRef<HTMLButtonElement>(null);
  const lastDetailTriggerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    const poll = async () => {
      let activeCohort = cohortIdRef.current ?? undefined;
      let loadgenWarning: string | null = null;
      let faultWarning: string | null = null;
      try {
        try {
          const status = await fetchLoadgenStatus(controller.signal);
          activeCohort = status.cohort_id;
          setLoadgen(status);
        } catch (err) {
          if (controller.signal.aborted) return;
          loadgenWarning =
            err instanceof Error
              ? `${err.message}; showing the last known cohort`
              : "loadgen status unavailable; showing the last known cohort";
        }
        try {
          setSimFaults(await fetchSimFaults(controller.signal));
        } catch (err) {
          if (controller.signal.aborted) return;
          faultWarning =
            err instanceof Error
              ? `${err.message}; showing the last known dependency state`
              : "dependency state unavailable; showing the last known state";
        }
        const body = await fetchSnapshot({
          cohortId: activeCohort,
          orderId:
            focusedOrderId && ORDER_ID_RE.test(focusedOrderId)
              ? focusedOrderId
              : undefined,
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setSnapshot(body);
          cohortIdRef.current = body.cohort_id;
          setError(
            [loadgenWarning, faultWarning].filter(Boolean).join("; ") || null,
          );
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : "snapshot poll failed");
        }
      } finally {
        if (!controller.signal.aborted) {
          timer = window.setTimeout(() => void poll(), POLL_MS);
        }
      }
    };

    void poll();
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [focusedOrderId, refreshEpoch]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (railOpen) {
        setRailOpen(false);
        window.requestAnimationFrame(() => presenterButtonRef.current?.focus());
      } else if (detailPanel) {
        setDetailPanel(null);
        window.requestAnimationFrame(() => lastDetailTriggerRef.current?.focus());
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [detailPanel, railOpen]);

  const orders = snapshot?.orders ?? [];
  const automaticFocus = orders.find(
    (order) => !["delivered", "cancelled", "failed"].includes(order.state),
  );
  const effectiveFocusId = focusedOrderId ?? automaticFocus?.id ?? orders[0]?.id ?? null;
  const focusedOrder = orders.find((order) => order.id === effectiveFocusId) ?? null;
  const trace =
    snapshot?.trace?.order_id === focusedOrderId ? snapshot.trace : null;

  const stageCounts = snapshot?.stages;
  const conservation = snapshot?.conservation;
  const rates = snapshot?.terminal_rates_per_min;
  const simHttp = snapshot?.sim_http;
  const slots = snapshot?.outbound_slots;
  const parked = snapshot?.parked_list ?? [];

  const restaurantTone = healthForLane(
    simHttp?.restaurant,
    slots?.restaurant.used,
    slots?.restaurant.cap,
  );
  const deliveryTone = healthForLane(
    simHttp?.courier,
    slots?.courier.used,
    slots?.courier.cap,
  );
  const workerTone: HealthTone =
    (snapshot?.no_progress_beyond_threshold.count ?? 0) > 0
      ? "fault"
      : (snapshot?.retry_rate ?? 0) > 0.2
        ? "pressure"
        : "healthy";

  const terminalOrders = useMemo(
    () => ({
      cancelled: orders.filter((order) => order.state === "cancelled").slice(0, 2),
      failed: orders.filter((order) => order.state === "failed").slice(0, 2),
    }),
    [orders],
  );

  const setScenario = (scenario: ScenarioId) => {
    setActiveScenario(scenario);
    window.sessionStorage.setItem("order-pipeline-scenario", scenario);
  };

  const openRail = () => {
    setDetailPanel(null);
    setRailOpen(true);
  };

  const closeRail = () => {
    setRailOpen(false);
    window.requestAnimationFrame(() => presenterButtonRef.current?.focus());
  };

  const openDetail = (panel: DetailPanel, trigger: HTMLElement) => {
    lastDetailTriggerRef.current = trigger;
    setRailOpen(false);
    setDetailPanel(panel);
  };

  const openOrder = (orderId: string, trigger: HTMLElement) => {
    setFocusedOrderId(orderId);
    setLookupId(orderId);
    openDetail({ kind: "order", orderId }, trigger);
  };

  const closeDetail = () => {
    setDetailPanel(null);
    window.requestAnimationFrame(() => lastDetailTriggerRef.current?.focus());
  };

  const submitLookup = () => {
    const trimmed = lookupId.trim();
    if (!ORDER_ID_RE.test(trimmed)) {
      setRedriveStatus("Paste a complete order UUID.");
      return;
    }
    setFocusedOrderId(trimmed);
    setDetailPanel({ kind: "order", orderId: trimmed });
  };

  const redrive = async (workItemId: string) => {
    setRedriving(workItemId);
    setRedriveStatus(null);
    try {
      const result = await redriveWorkItem(workItemId);
      setRedriveStatus(
        `Redrove ${result.work_type} for order ${result.order_id} with key ${result.idempotency_key}.`,
      );
      setRefreshEpoch((value) => value + 1);
    } catch (err) {
      setRedriveStatus(err instanceof Error ? err.message : "Redrive failed");
    } finally {
      setRedriving(null);
    }
  };

  const scenario = SCENARIO_COPY[activeScenario];
  const restaurantFault = simFaultActive(simFaults?.restaurant);
  const deliveryFault = simFaultActive(simFaults?.courier);

  return (
    <main className="presentation">
      <header className="presentation-header">
        <div className="header-left">
          <div className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">OP</span>
            <div>
              <strong>Order flow studio</strong>
              <span>Independent systems demonstration</span>
            </div>
          </div>

          <button
            ref={presenterButtonRef}
            className="presenter-button"
            type="button"
            onClick={openRail}
            aria-expanded={railOpen}
            aria-controls="presenter-rail"
          >
            <span aria-hidden="true">☷</span>
            Presenter controls
            <i aria-hidden="true">›</i>
          </button>
        </div>

        <div className="live-context" aria-label="Live demo context">
          <span className={`scenario-pill ${scenario.tone}`}>
            <i aria-hidden="true" />
            {scenarioLabel(activeScenario)}
          </span>
          <span className="arrival-rate">
            <strong>{fmt(loadgen?.rate_rps, 2)}</strong> orders / sec
          </span>
          <span className={error ? "connection-state warning" : "connection-state"}>
            <i aria-hidden="true" />
            {error ? "Snapshot delayed" : "Live"}
          </span>
        </div>

        <span className="header-balance" aria-hidden="true" />
      </header>

      <div className="presentation-body">
        <section className={`scenario-callout ${scenario.tone}`} aria-live="polite">
          <div>
            <span className="eyebrow">Now showing</span>
            <strong>{scenario.title}</strong>
          </div>
          <p>{scenario.body}</p>
          <ScenarioFacts
            scenario={activeScenario}
            snapshot={snapshot}
            simFaults={simFaults}
          />
        </section>

        <section className="lifecycle-surface" aria-labelledby="lifecycle-heading">
          <div className="surface-heading">
            <div>
              <p className="eyebrow">Live order lifecycle</p>
              <h1 id="lifecycle-heading">Every order, one visible journey</h1>
            </div>
            <p>
              Focused order{" "}
              <strong>{focusedOrder ? displayCode(focusedOrder.id) : "—"}</strong>
              {focusedOrder ? ` · ${ageLabel(focusedOrder.accepted_at)}` : ""}
            </p>
          </div>

          <div className="zone-labels" aria-label="System zones">
            <button
              type="button"
              className={`zone-label restaurant ${restaurantFault ? "fault" : ""}`}
              onClick={(event) =>
                openDetail(
                  { kind: "zone", zone: "restaurant" },
                  event.currentTarget,
                )
              }
            >
              <span>Restaurant</span>
              <small>
                {simFaultLabel(
                  simFaults?.restaurant,
                  "Single shared service · aggregate flow",
                )}
              </small>
            </button>
            <span className="handoff-label">
              <i aria-hidden="true">⇢</i>
              Handoff
            </span>
            <button
              type="button"
              className={`zone-label delivery ${deliveryFault ? "fault" : ""}`}
              onClick={(event) =>
                openDetail(
                  { kind: "zone", zone: "delivery" },
                  event.currentTarget,
                )
              }
            >
              <span>Delivery</span>
              <small>
                {simFaultLabel(
                  simFaults?.courier,
                  "Single shared service · aggregate flow",
                )}
              </small>
            </button>
          </div>

          <div className="lifecycle-track">
            <div className="flow-line" aria-hidden="true" />
            {STAGE_LABELS.map((stage, index) => (
              <article
                className={`lifecycle-stage stage-${index + 1}`}
                key={stage}
              >
                <div className="stage-node" aria-hidden="true" />
                <div className="stage-title">
                  <span>{STAGE_DESCRIPTIONS[stage]}</span>
                  <h2>{stateLabel(API_STATE_BY_STAGE[stage])}</h2>
                  <small>
                    {STAGE_SEAMS[stage] ??
                      (stage === "placed"
                        ? "accepted — waiting for restaurant"
                        : "current lifecycle state")}
                  </small>
                </div>
                <div className="stage-count">
                  <strong>{fmt(stageCounts?.[stage])}</strong>
                  <span>orders</span>
                </div>
                <StageTickets
                  stage={stage}
                  count={stageCounts?.[stage] ?? 0}
                  orders={orders}
                  focusedOrderId={effectiveFocusId}
                  onOrder={openOrder}
                />
              </article>
            ))}
          </div>

          <div className="terminal-branches" aria-label="Terminal branches">
            <div className="branch-spine" aria-hidden="true" />
            <section className="terminal-branch cancelled">
              <span className="branch-icon" aria-hidden="true">×</span>
              <div>
                <strong>Cancelled</strong>
                <small>Stopped before the preparation pivot</small>
              </div>
              <b>{fmt(conservation?.cancelled)}</b>
              <div className="branch-tickets">
                {terminalOrders.cancelled.map((order) => (
                  <TicketButton
                    key={order.id}
                    order={order}
                    focused={order.id === effectiveFocusId}
                    onClick={(event) => openOrder(order.id, event.currentTarget)}
                  />
                ))}
              </div>
            </section>
            <section className="terminal-branch failed">
              <span className="branch-icon" aria-hidden="true">!</span>
              <div>
                <strong>Failed</strong>
                <small>Retry budget or business rule ended work</small>
              </div>
              <b>{fmt(conservation?.failed)}</b>
              <div className="branch-tickets">
                {terminalOrders.failed.map((order) => (
                  <TicketButton
                    key={order.id}
                    order={order}
                    focused={order.id === effectiveFocusId}
                    onClick={(event) => openOrder(order.id, event.currentTarget)}
                  />
                ))}
              </div>
            </section>
          </div>
        </section>

        <section className="evidence-strip" aria-label="Essential system evidence">
          <div className="outcome-group">
            <div className="outcome-heading">
              <span>Overall performance</span>
              <strong>Cohort totals</strong>
              <small>Rates use the last 60 seconds</small>
            </div>
            <Metric
              label="Delivered total"
              value={fmt(conservation?.delivered)}
              detail={`Overall rate · ${fmt(rates?.delivered)} / min`}
              tone="healthy"
            />
            <Metric
              label="Failed total"
              value={fmt(conservation?.failed)}
              detail={`Overall rate · ${fmt(rates?.failed)} / min`}
              tone={(conservation?.failed ?? 0) > 0 ? "fault" : "neutral"}
            />
            <Metric
              label="Cancelled total"
              value={fmt(conservation?.cancelled)}
              detail={`Overall rate · ${fmt(rates?.cancelled)} / min`}
            />
            <Metric
              label="P95 end to end"
              value={`${fmt(snapshot?.e2e_latency_s.p95)}s`}
              detail={`oldest open ${fmt(snapshot?.oldest_open.age_s)}s`}
              tone={(snapshot?.oldest_open.age_s ?? 0) > 90 ? "pressure" : "neutral"}
            />
          </div>

          <div className="health-group">
            <button
              type="button"
              className={`health-chip ${restaurantTone}`}
              onClick={(event) =>
                openDetail(
                  { kind: "zone", zone: "restaurant" },
                  event.currentTarget,
                )
              }
            >
              <span>Restaurant</span>
              <strong>{healthLabel(restaurantTone)}</strong>
              <small>{fmt(slots?.restaurant.used)} / {fmt(slots?.restaurant.cap)} slots</small>
            </button>
            <button
              type="button"
              className={`health-chip ${workerTone}`}
              onClick={(event) =>
                openDetail(
                  { kind: "system", system: "worker" },
                  event.currentTarget,
                )
              }
            >
              <span>Workers</span>
              <strong>{healthLabel(workerTone)}</strong>
              <small>{fmt(snapshot?.currently_leased)} leased · {fmt(parked.length)} parked</small>
            </button>
            <button
              type="button"
              className={`health-chip ${deliveryTone}`}
              onClick={(event) =>
                openDetail(
                  { kind: "zone", zone: "delivery" },
                  event.currentTarget,
                )
              }
            >
              <span>Delivery</span>
              <strong>{healthLabel(deliveryTone)}</strong>
              <small>{fmt(slots?.courier.used)} / {fmt(slots?.courier.cap)} slots</small>
            </button>
          </div>

          <button
            type="button"
            className={
              (conservation?.residual ?? 0) === 0 &&
              snapshot?.duplicate_effects === 0
                ? "correctness-proof healthy"
                : "correctness-proof fault"
            }
            onClick={(event) =>
              openDetail({ kind: "correctness" }, event.currentTarget)
            }
          >
            <span className="proof-icon" aria-hidden="true">
              {(conservation?.residual ?? 0) === 0 ? "✓" : "!"}
            </span>
            <span>
              <small>Correctness proof</small>
              <strong>
                {snapshot
                  ? `${conservation?.accepted ?? 0} orders reconciled`
                  : "Connecting…"}
              </strong>
              <b>
                Residual {fmt(conservation?.residual)} · duplicate effects{" "}
                {fmt(snapshot?.duplicate_effects)}
              </b>
            </span>
            <i aria-hidden="true">›</i>
          </button>
        </section>
      </div>

      {railOpen ? (
        <>
          <button
            className="panel-scrim"
            type="button"
            aria-label="Hide presenter controls"
            onClick={closeRail}
          />
          <PresenterRail
            activeScenario={activeScenario}
            courierFaultActive={deliveryFault}
            loadgen={loadgen}
            readyOrders={stageCounts?.ready ?? 0}
            parkedCourierJobs={parked.filter((row) =>
              ["dispatch", "poll_ride"].includes(row.work_type),
            )}
            onClose={closeRail}
            onMutation={() => setRefreshEpoch((value) => value + 1)}
            onScenarioChange={setScenario}
          />
        </>
      ) : null}

      {detailPanel ? (
        <>
          <button
            className="panel-scrim"
            type="button"
            aria-label="Close details"
            onClick={closeDetail}
          />
          <DetailsDrawer
            panel={detailPanel}
            snapshot={snapshot}
            order={focusedOrder}
            trace={trace}
            lookupId={lookupId}
            setLookupId={setLookupId}
            submitLookup={submitLookup}
            redrive={redrive}
            redriving={redriving}
            redriveStatus={redriveStatus}
            onClose={closeDetail}
          />
        </>
      ) : null}
    </main>
  );
}

function DetailsDrawer({
  panel,
  snapshot,
  order,
  trace,
  lookupId,
  setLookupId,
  submitLookup,
  redrive,
  redriving,
  redriveStatus,
  onClose,
}: {
  panel: DetailPanel;
  snapshot: Snapshot | null;
  order: OrderSummary | null;
  trace: Snapshot["trace"];
  lookupId: string;
  setLookupId: (value: string) => void;
  submitLookup: () => void;
  redrive: (workItemId: string) => Promise<void>;
  redriving: string | null;
  redriveStatus: string | null;
  onClose: () => void;
}) {
  const title =
    panel.kind === "order"
      ? `Order ${order ? displayCode(order.id) : "details"}`
      : panel.kind === "zone"
        ? panel.zone === "restaurant"
          ? "Restaurant zone"
          : "Delivery zone"
        : panel.kind === "correctness"
          ? "Correctness proof"
          : "Worker system";

  return (
    <aside className="side-panel details-drawer" aria-label={title}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Contextual details</p>
          <h2>{title}</h2>
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={onClose}
          aria-label="Close details"
          autoFocus
        >
          ×
        </button>
      </div>

      {panel.kind === "order" ? (
        <OrderDetails
          order={order}
          trace={trace}
          snapshot={snapshot}
          lookupId={lookupId}
          setLookupId={setLookupId}
          submitLookup={submitLookup}
          redrive={redrive}
          redriving={redriving}
          redriveStatus={redriveStatus}
        />
      ) : null}
      {panel.kind === "zone" ? (
        <ZoneDetails zone={panel.zone} snapshot={snapshot} />
      ) : null}
      {panel.kind === "correctness" ? (
        <CorrectnessDetails snapshot={snapshot} />
      ) : null}
      {panel.kind === "system" ? (
        <WorkerDetails
          snapshot={snapshot}
          redrive={redrive}
          redriving={redriving}
          redriveStatus={redriveStatus}
        />
      ) : null}
    </aside>
  );
}

function OrderDetails({
  order,
  trace,
  snapshot,
  lookupId,
  setLookupId,
  submitLookup,
  redrive,
  redriving,
  redriveStatus,
}: {
  order: OrderSummary | null;
  trace: Snapshot["trace"];
  snapshot: Snapshot | null;
  lookupId: string;
  setLookupId: (value: string) => void;
  submitLookup: () => void;
  redrive: (workItemId: string) => Promise<void>;
  redriving: string | null;
  redriveStatus: string | null;
}) {
  const parked = snapshot?.parked_list.filter(
    (row) => row.order_id === order?.id,
  ) ?? [];
  return (
    <div className="drawer-content">
      <form
        className="order-lookup"
        onSubmit={(event) => {
          event.preventDefault();
          submitLookup();
        }}
      >
        <label htmlFor="order-lookup">Paste-an-ID order lookup</label>
        <div>
          <input
            id="order-lookup"
            value={lookupId}
            onChange={(event) => setLookupId(event.target.value)}
            placeholder="Full order UUID"
            spellCheck={false}
          />
          <button type="submit">Follow</button>
        </div>
      </form>

      {order ? (
        <section className="drawer-hero">
          <span className="large-ticket">{displayCode(order.id)}</span>
          <div>
            <span className="state-badge">{stateLabel(order.state)}</span>
            <p>{order.items.join(" · ")}</p>
          </div>
        </section>
      ) : (
        <p className="empty-message">
          This order is outside the recent presentation window. Its trace will
          appear when the snapshot returns it.
        </p>
      )}

      <dl className="detail-list">
        <div>
          <dt>Full UUID</dt>
          <dd className="mono">{order?.id ?? trace?.order_id ?? "—"}</dd>
        </div>
        <div>
          <dt>Accepted</dt>
          <dd>{order ? new Date(order.accepted_at).toLocaleTimeString() : "—"}</dd>
        </div>
        <div>
          <dt>Attempts</dt>
          <dd>{fmt(trace?.attempts.length)}</dd>
        </div>
      </dl>

      <section className="drawer-section">
        <div className="drawer-section-heading">
          <h3>Lifecycle timeline</h3>
          <span>{trace ? `${trace.order_events.length} events` : "Loading trace…"}</span>
        </div>
        <ol className="timeline">
          {trace?.order_events.map((event) => (
            <li key={event.id} className={event.applied ? "" : "not-applied"}>
              <span aria-hidden="true" />
              <div>
                <strong>{stateLabel(event.to_state)}</strong>
                <small>
                  {new Date(event.timestamp).toLocaleTimeString()} · {event.actor} ·{" "}
                  {event.cause}
                  {!event.applied ? " · not applied" : ""}
                </small>
              </div>
            </li>
          ))}
          {trace && trace.order_events.length === 0 ? (
            <li className="empty-message">No events found in this cohort.</li>
          ) : null}
        </ol>
      </section>

      <section className="drawer-section">
        <div className="drawer-section-heading">
          <h3>Attempts & ownership</h3>
          <span>Evidence stays visible</span>
        </div>
        <div className="attempt-list">
          {trace?.attempts.map((attempt) => (
            <article key={attempt.id}>
              <div>
                <strong>{attempt.work_type}</strong>
                <span className={attempt.outcome ? "attempt-outcome" : "attempt-outcome warning"}>
                  {attempt.outcome ?? "abandoned / in flight"}
                </span>
              </div>
              <dl>
                <div><dt>Owner</dt><dd>{attempt.lease_owner}</dd></div>
                <div><dt>Work item</dt><dd className="mono">{attempt.work_item_id}</dd></div>
                <div><dt>Idempotency key</dt><dd className="mono">{attempt.idempotency_key}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      </section>

      {parked.map((row) => (
        <section className="parked-action" key={row.id}>
          <span aria-hidden="true">!</span>
          <div>
            <strong>Parked {row.work_type}</strong>
            <p>{row.reason ?? "Retry budget exhausted"} · {row.next_action ?? "Redrive after recovery"}</p>
            <button
              type="button"
              disabled={redriving !== null}
              onClick={() => void redrive(row.id)}
            >
              {redriving === row.id ? "Redriving…" : "Redrive same work item"}
            </button>
          </div>
        </section>
      ))}
      {redriveStatus ? <p className="action-status" role="status">{redriveStatus}</p> : null}
    </div>
  );
}

function ZoneDetails({
  zone,
  snapshot,
}: {
  zone: "restaurant" | "delivery";
  snapshot: Snapshot | null;
}) {
  const lane =
    zone === "restaurant"
      ? snapshot?.sim_http.restaurant
      : snapshot?.sim_http.courier;
  const slots =
    zone === "restaurant"
      ? snapshot?.outbound_slots.restaurant
      : snapshot?.outbound_slots.courier;
  const backlog =
    zone === "restaurant"
      ? (snapshot?.backlog.confirm ?? 0) + (snapshot?.backlog.poll_cook ?? 0)
      : (snapshot?.backlog.dispatch ?? 0) + (snapshot?.backlog.poll_ride ?? 0);
  return (
    <div className="drawer-content">
      <section className="drawer-hero zone-hero">
        <span className="zone-glyph" aria-hidden="true">
          {zone === "restaurant" ? "R" : "D"}
        </span>
        <div>
          <span className="state-badge">
            {healthLabel(healthForLane(lane, slots?.used, slots?.cap))}
          </span>
          <p>
            {zone === "restaurant"
              ? "One shared restaurant simulator; traffic and capacity are aggregate."
              : "One shared courier simulator; traffic and capacity are aggregate."}
          </p>
        </div>
      </section>
      <div className="drawer-metrics">
        <Metric label="Requests" value={fmt(lane?.requests_per_min)} detail="per minute" />
        <Metric label="P95 latency" value={`${fmt(lane?.latency_p95_s)}s`} detail={`P50 ${fmt(lane?.latency_p50_s)}s`} />
        <Metric label="Capacity" value={`${fmt(slots?.used)} / ${fmt(slots?.cap)}`} detail={`${fmt(slots?.per_worker_cap)} per worker`} />
        <Metric label="Backlog" value={fmt(backlog)} detail="pending + leased" />
      </div>
      <section className="drawer-section">
        <h3>Dependency signals · last 60 seconds</h3>
        <dl className="detail-list">
          <div><dt>Timeout / unknown</dt><dd>{fmt(lane?.timeout)}</dd></div>
          <div><dt>HTTP 5xx</dt><dd>{fmt(lane?.http_5xx)}</dd></div>
          <div><dt>Busy 429</dt><dd>{fmt(lane?.http_429)}</dd></div>
        </dl>
      </section>
      <p className="explain-note">
        A fault changes the border, icon, label, and supporting evidence. Meaning
        never depends on color alone.
      </p>
    </div>
  );
}

function CorrectnessDetails({ snapshot }: { snapshot: Snapshot | null }) {
  const proof = snapshot?.conservation;
  return (
    <div className="drawer-content">
      <section className="proof-equation">
        <span>Accepted</span>
        <strong>{fmt(proof?.accepted)}</strong>
        <i>=</i>
        <span>Delivered + cancelled + failed + in flight</span>
        <strong>
          {fmt(proof?.delivered)} + {fmt(proof?.cancelled)} + {fmt(proof?.failed)} +{" "}
          {fmt(proof?.in_flight)}
        </strong>
      </section>
      <div className="invariant-list">
        <Metric label="Conservation residual" value={fmt(proof?.residual)} detail="must remain zero" tone={(proof?.residual ?? 0) === 0 ? "healthy" : "fault"} />
        <Metric label="Duplicate effects" value={fmt(snapshot?.duplicate_effects)} detail={`${fmt(snapshot?.duplicate_attempts)} retry attempts are allowed`} tone={snapshot?.duplicate_effects === 0 ? "healthy" : "fault"} />
        <Metric label="Event mismatches" value={fmt(snapshot?.state_vs_last_order_events_mismatches)} detail="state vs last applied event" tone={(snapshot?.state_vs_last_order_events_mismatches ?? 0) === 0 ? "healthy" : "fault"} />
        <Metric label="Invalid transitions" value={fmt(snapshot?.invalid_transitions)} detail="lifecycle guard" tone={(snapshot?.invalid_transitions ?? 0) === 0 ? "healthy" : "fault"} />
        <Metric label="Orphaned tickets" value={fmt(snapshot?.orphaned_tickets)} detail="downstream effects without live work" tone={(snapshot?.orphaned_tickets ?? 0) === 0 ? "healthy" : "fault"} />
        <Metric label="Startup scan" value={fmt(snapshot?.startup_scan)} detail="orders missing work" tone={(snapshot?.startup_scan ?? 0) === 0 ? "healthy" : "fault"} />
      </div>
      <p className="explain-note">
        Retries may create more attempts. Stable idempotency keys keep external
        restaurant and courier effects at one.
      </p>
    </div>
  );
}

function WorkerDetails({
  snapshot,
  redrive,
  redriving,
  redriveStatus,
}: {
  snapshot: Snapshot | null;
  redrive: (workItemId: string) => Promise<void>;
  redriving: string | null;
  redriveStatus: string | null;
}) {
  const leased = snapshot?.currently_leased_items ?? [];
  const parked = snapshot?.parked_list ?? [];
  return (
    <div className="drawer-content">
      <div className="drawer-metrics">
        <Metric label="Leased now" value={fmt(snapshot?.currently_leased)} detail={`${fmt(snapshot?.outbound_slots.task.cap)} task capacity`} />
        <Metric label="Retry rate" value={fmt(snapshot?.retry_rate, 2)} detail="failed / unknown re-execution" />
        <Metric label="No progress" value={fmt(snapshot?.no_progress_beyond_threshold.count)} detail={`beyond ${fmt(snapshot?.no_progress_beyond_threshold.threshold_s)}s`} />
        <Metric label="Parked" value={fmt(parked.length)} detail="not a lifecycle stage" />
      </div>

      <section className="drawer-section">
        <div className="drawer-section-heading">
          <h3>Currently leased</h3>
          <span>Owner is evidence for the crash beat</span>
        </div>
        <div className="row-list">
          {leased.map((row) => (
            <article key={row.id}>
              <strong>{displayCode(row.order_id)} · {row.work_type}</strong>
              <span>{row.owner ?? "unowned"}</span>
              <small>lease until {new Date(row.lease_until).toLocaleTimeString()}</small>
            </article>
          ))}
          {leased.length === 0 ? <p className="empty-message">No active leases.</p> : null}
        </div>
      </section>

      <section className="drawer-section">
        <div className="drawer-section-heading">
          <h3>Parked work</h3>
          <span>Recover dependency before Redrive</span>
        </div>
        <div className="row-list parked-rows">
          {parked.map((row) => (
            <article key={row.id}>
              <strong>{displayCode(row.order_id)} · {row.work_type}</strong>
              <span>{row.owner ?? "unowned"} · {row.reason ?? "budget exhausted"}</span>
              <small>{row.next_action ?? "Redrive after recovery"}</small>
              <button
                type="button"
                disabled={redriving !== null}
                onClick={() => void redrive(row.id)}
              >
                {redriving === row.id ? "Redriving…" : "Redrive"}
              </button>
            </article>
          ))}
          {parked.length === 0 ? <p className="empty-message">No parked work.</p> : null}
        </div>
      </section>
      {redriveStatus ? <p className="action-status" role="status">{redriveStatus}</p> : null}
    </div>
  );
}
