import { useEffect, useState } from "react";

import {
  fetchLoadgenStatus,
  fetchSnapshot,
  POLL_MS,
  STAGE_LABELS,
  STAGE_SEAMS,
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
  const [cohortId, setCohortId] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    const poll = async () => {
      try {
        // Each tab discovers the loadgen's current cohort independently. There is
        // no cross-tab React state for New cohort to keep in sync.
        const loadgen = await fetchLoadgenStatus(controller.signal);
        const body = await fetchSnapshot({
          cohortId: loadgen.cohort_id,
          orderId: ORDER_ID_RE.test(orderId.trim()) ? orderId.trim() : undefined,
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setSnapshot(body);
          setCohortId(loadgen.cohort_id);
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
  const oldest = snapshot?.oldest_open;
  const acceptReject = snapshot?.accept_reject;
  const backlog = snapshot?.backlog;
  const http429s = snapshot?.http_429s;
  const stretching = snapshot?.stretching_etas;
  const simHttp = snapshot?.sim_http;
  const parked = snapshot?.parked_list ?? [];
  const noProgress = snapshot?.no_progress_beyond_threshold;

  return (
    <main className="page">
      <h1>Watch</h1>
      <p className="lede">
        Live counts for this cohort. Cards refresh on their own; nothing places
        orders from here. Cohort: {cohortId ?? "discovering…"}
      </p>
      {error ? <p className="error">{error}</p> : null}

      <section className="pane">
        <h2>Business</h2>
        <p className="pane-intro">
          The top row is how many orders sit in each happy-path stage{" "}
          <em>right now</em>. Confirmed is kitchen <em>queued</em> (waiting for
          a pan); being prepared is <em>cooking</em> (on a pan). Cancelled and
          failed are endings, not stages — they show up in rates and
          conservation.
        </p>
        <p className="group-label">in each stage now</p>
        <div className="card-grid stages">
          {STAGE_LABELS.map((label) => (
            <article className="card" key={label}>
              <h3>{label}</h3>
              {STAGE_SEAMS[label] ? (
                <p className="hint">{STAGE_SEAMS[label]}</p>
              ) : null}
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
          <article className="card">
            <h3>oldest open</h3>
            <p className="hint">
              Age and stage of the oldest non-terminal order in this cohort.
              Rises in a rush, then falls as the drain completes.
            </p>
            <dl>
              <div>
                <dt>age (s)</dt>
                <dd>{fmt(oldest?.age_s)}</dd>
              </div>
              <div>
                <dt>stage</dt>
                <dd>{oldest?.stage ?? "—"}</dd>
              </div>
            </dl>
          </article>
        </div>
      </section>

      <section className="pane">
        <h2>Pipeline</h2>
        <p className="pane-intro">
          Intake, work-item backlog, retries, counted 429s, stretching ETAs, and
          per-sim HTTP. Same GET /snapshot keys the walk already polls.
        </p>
        <div className="card-grid">
          <article className="card">
            <h3>accept / reject</h3>
            <p className="hint">
              Accepted orders vs door 429s (no order row). Kitchen and courier
              busy are a different no.
            </p>
            <dl>
              <div>
                <dt>accepted</dt>
                <dd>{fmt(acceptReject?.accepted)}</dd>
              </div>
              <div>
                <dt>rejected</dt>
                <dd>{fmt(acceptReject?.rejected)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>backlog by work type</h3>
            <p className="hint">
              Pending + leased work items. Parked is not backlog.
            </p>
            <dl>
              <div>
                <dt>confirm</dt>
                <dd>{fmt(backlog?.confirm)}</dd>
              </div>
              <div>
                <dt>poll_cook</dt>
                <dd>{fmt(backlog?.poll_cook)}</dd>
              </div>
              <div>
                <dt>dispatch</dt>
                <dd>{fmt(backlog?.dispatch)}</dd>
              </div>
              <div>
                <dt>poll_ride</dt>
                <dd>{fmt(backlog?.poll_ride)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>retry rate</h3>
            <p className="metric">{fmt(snapshot?.retry_rate)}</p>
            <p className="hint">
              Fraction of attempts in the last 60s that are not the first
              attempt on that work item.
            </p>
          </article>
          <article className="card">
            <h3>429s</h3>
            <p className="hint">
              Counted busy. Door = intake fuse. Kitchen / courier = quoted wait
              &gt; 3× that ticket&apos;s quiet time.
            </p>
            <dl>
              <div>
                <dt>door</dt>
                <dd>{fmt(http429s?.door)}</dd>
              </div>
              <div>
                <dt>kitchen</dt>
                <dd>{fmt(http429s?.kitchen)}</dd>
              </div>
              <div>
                <dt>courier</dt>
                <dd>{fmt(http429s?.courier)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>stretching ETAs</h3>
            <p className="hint">
              In-flight orders whose quoted wait exceeds quiet cook. The rush
              beat is promises stretching, not ticket 21.
            </p>
            <dl>
              <div>
                <dt>count</dt>
                <dd>{fmt(stretching?.count)}</dd>
              </div>
              <div>
                <dt>max stretch (s)</dt>
                <dd>{fmt(stretching?.max_stretch_s)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>restaurant HTTP</h3>
            <p className="hint">Kitchen work: confirm and poll_cook.</p>
            <dl>
              <div>
                <dt>req / min</dt>
                <dd>{fmt(simHttp?.restaurant.requests_per_min)}</dd>
              </div>
              <div>
                <dt>p50 (s)</dt>
                <dd>{fmt(simHttp?.restaurant.latency_p50_s)}</dd>
              </div>
              <div>
                <dt>p95 (s)</dt>
                <dd>{fmt(simHttp?.restaurant.latency_p95_s)}</dd>
              </div>
            </dl>
          </article>
          <article className="card">
            <h3>courier HTTP</h3>
            <p className="hint">Dispatch and poll_ride.</p>
            <dl>
              <div>
                <dt>req / min</dt>
                <dd>{fmt(simHttp?.courier.requests_per_min)}</dd>
              </div>
              <div>
                <dt>p50 (s)</dt>
                <dd>{fmt(simHttp?.courier.latency_p50_s)}</dd>
              </div>
              <div>
                <dt>p95 (s)</dt>
                <dd>{fmt(simHttp?.courier.latency_p95_s)}</dd>
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
              must stay 0. A dash means a sim ledger could not be read.
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
          <article className="card">
            <h3>no progress beyond threshold</h3>
            <p className="metric">{fmt(noProgress?.count)}</p>
            <p className="hint">
              In-flight orders whose last applied event is older than{" "}
              {fmt(noProgress?.threshold_s)}s. Park is a work-item status, not a
              stage.
            </p>
          </article>
        </div>
        <article className="card parked">
          <h3>parked list</h3>
          <p className="hint">
            Read-only. Owner, reason, and next action. Parked work is stalled,
            not lost, and not a lifecycle stage.
          </p>
          {parked.length === 0 ? (
            <p className="hint">None in this cohort.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>order</th>
                    <th>work</th>
                    <th>owner</th>
                    <th>reason</th>
                    <th>next action</th>
                  </tr>
                </thead>
                <tbody>
                  {parked.map((row) => (
                    <tr key={`${row.order_id}-${row.work_type}`}>
                      <td>{row.order_id}</td>
                      <td>{row.work_type}</td>
                      <td>{row.owner ?? "—"}</td>
                      <td>{row.reason ?? "—"}</td>
                      <td>{row.next_action ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>
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
