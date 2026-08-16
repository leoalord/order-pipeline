import { useEffect, useState } from "react";

import {
  fetchSnapshot,
  POLL_MS,
  STAGE_LABELS,
  type Snapshot,
} from "./snapshot";

const ORDER_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function fmt(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

export function HomePage() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [orderId, setOrderId] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    const poll = async () => {
      try {
        const body = await fetchSnapshot({
          orderId: ORDER_ID_RE.test(orderId.trim()) ? orderId.trim() : undefined,
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setSnapshot(body);
          setError(null);
        }
      } catch (err) {
        if (controller.signal.aborted) {
          return;
        }
        setError(err instanceof Error ? err.message : "snapshot poll failed");
      }
    };

    void poll();
    timer = window.setInterval(() => {
      void poll();
    }, POLL_MS);

    return () => {
      controller.abort();
      if (timer !== undefined) {
        window.clearInterval(timer);
      }
    };
  }, [orderId]);

  const stages = snapshot?.stages;
  const rates = snapshot?.terminal_rates_per_min;
  const e2e = snapshot?.e2e_latency_s;
  const conservation = snapshot?.conservation;
  const trace = snapshot?.trace;

  return (
    <main className="page">
      <h1>Watch</h1>
      <p className="lede">
        Live counts for this cohort. Cards refresh on their own; nothing places
        orders from here.
      </p>
      {error ? <p className="error">{error}</p> : null}

      <section className="pane">
        <h2>Business</h2>
        <p className="pane-intro">
          The top row is how many orders sit in each happy-path stage{" "}
          <em>right now</em>. Cancelled and failed are endings, not stages —
          they show up in rates and conservation.
        </p>
        <p className="group-label">in each stage now</p>
        <div className="card-grid stages">
          {STAGE_LABELS.map((label) => (
            <article className="card" key={label}>
              <h3>{label}</h3>
              <p className="metric">{fmt(stages?.[label])}</p>
            </article>
          ))}
        </div>
        <div className="card-grid">
          <article className="card">
            <h3>terminal rates / min</h3>
            <p className="hint">
              How many orders reached an ending in the last 60 seconds. This is
              the heartbeat when a single order is too small to move the row
              above.
            </p>
            <dl>
              <div>
                <dt>delivered</dt>
                <dd>{fmt(rates?.delivered)}</dd>
              </div>
              <div>
                <dt>cancelled</dt>
                <dd>{fmt(rates?.cancelled)}</dd>
              </div>
              <div>
                <dt>failed</dt>
                <dd>{fmt(rates?.failed)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>e2e latency (s)</h3>
            <p className="hint">
              Accept to delivered, among orders that have already delivered.
              Empty until the first one finishes.
            </p>
            <dl>
              <div>
                <dt>p50</dt>
                <dd>{fmt(e2e?.p50)}</dd>
              </div>
              <div>
                <dt>p95</dt>
                <dd>{fmt(e2e?.p95)}</dd>
              </div>
            </dl>
          </article>
        </div>
      </section>

      <section className="pane">
        <h2>Correctness</h2>
        <p className="pane-intro">
          Residual and duplicate effects should stay 0. Attempts may retry;
          kitchen and courier tickets must not.
        </p>
        <div className="card-grid">
          <article className="card">
            <h3>conservation residual</h3>
            <p className="metric">{fmt(conservation?.residual)}</p>
            <p className="hint">
              0 means the books balance. accepted {fmt(conservation?.accepted)}{" "}
              = delivered {fmt(conservation?.delivered)} + cancelled{" "}
              {fmt(conservation?.cancelled)} + failed {fmt(conservation?.failed)}{" "}
              + in_flight {fmt(conservation?.in_flight)}; parked{" "}
              {fmt(conservation?.parked)} ⊂ in_flight. Parked work is stalled,
              not a stage.
            </p>
          </article>
          <article className="card">
            <h3>duplicate attempts vs duplicate effects</h3>
            <p className="hint">
              Extra worker calls vs extra tickets in the sim ledgers. Effects
              must stay 0.
            </p>
            <dl>
              <div>
                <dt>attempts</dt>
                <dd>{fmt(snapshot?.duplicate_attempts)}</dd>
              </div>
              <div>
                <dt>effects</dt>
                <dd>{fmt(snapshot?.duplicate_effects)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>startup scan</h3>
            <p className="metric">{fmt(snapshot?.startup_scan)}</p>
            <p className="hint">Orders accepted with no work item. Should stay 0.</p>
          </article>
          <article className="card">
            <h3>invalid transitions</h3>
            <p className="metric">{fmt(snapshot?.invalid_transitions)}</p>
            <p className="hint">
              Illegal moves, counted and not applied — for example cancel after
              cooking started.
            </p>
          </article>
          <article className="card">
            <h3>currently-leased</h3>
            <p className="metric">{fmt(snapshot?.currently_leased)}</p>
            <p className="hint">
              Work items in the middle of an outbound call. 0 means the worker
              is idle or down.
            </p>
          </article>
          <article className="card">
            <h3>state-vs-last-event mismatch</h3>
            <p className="metric">{fmt(snapshot?.state_vs_last_order_events_mismatches)}</p>
            <p className="hint">
              Current order state does not match the last applied event. Should
              stay 0 on a clean walk.
            </p>
          </article>
        </div>
        <article className="card trace">
          <h3>paste-an-ID trace</h3>
          <p className="hint">
            Watch one order walk. The stage row is a crowd; this is the
            per-order story.
          </p>
          <label>
            order id
            <input
              value={orderId}
              onChange={(event) => setOrderId(event.target.value)}
              placeholder="uuid"
              spellCheck={false}
              autoComplete="off"
            />
          </label>
          {trace ? (
            <div className="trace-body">
              <p>
                order {trace.order_id} · {trace.order_events.length} events ·{" "}
                {trace.attempts.length} attempts
              </p>
              <ol>
                {trace.order_events.map((event) => (
                  <li key={event.id}>
                    {event.from_state ?? "—"} → {event.to_state} ({event.cause}
                    {event.applied ? "" : ", evidence"})
                  </li>
                ))}
              </ol>
              <ol>
                {trace.attempts.map((attempt) => (
                  <li key={attempt.id}>
                    {attempt.work_type} {attempt.outcome ?? "NULL"} {attempt.started_at}
                    {attempt.ended_at ? ` → ${attempt.ended_at}` : ""}
                  </li>
                ))}
              </ol>
            </div>
          ) : (
            <p className="hint">Paste an order id to load events and attempts.</p>
          )}
        </article>
      </section>
    </main>
  );
}
