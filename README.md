# Order Pipeline

Single-machine order pipeline. One Python package plus a Vite dashboard; Compose runs Postgres, the API, the restaurant sim, the courier sim, **two workers**, loadgen on 8090, and the SPA on 5173.

Design and delivery references: [task board](https://app.notion.com/p/b3cb466659e9457ca929285c2e7c945f?v=f462b190db9144088eb5217c76e90d51&source=copy_link), [order-pipeline design](https://app.notion.com/p/Design-Doc-Order-Pipeline-3bc2bade7f31815dac1ee7cb55bb6813?source=copy_link), [configuration](https://app.notion.com/p/Config-Reference-3bc2bade7f3181038c15c76196f447ee?source=copy_link), [demo runbook](https://app.notion.com/p/Demo-Runbook-3bc2bade7f31814da356f8f8c3d29a70?source=copy_link), [implementation guide](https://app.notion.com/p/Implementation-3bd2bade7f3181fe8ad4e2a96237e030?source=copy_link), and [easy wrong turns](https://app.notion.com/p/Easy-wrong-turns-3bd2bade7f3181118d66e60c508f8e1f?source=copy_link).

## Run

```bash
uv sync

docker compose down -v
docker compose up --wait
curl -sf http://localhost:8000/health
curl -sf http://localhost:8081/health
curl -sf http://localhost:8082/health
curl -sf http://localhost:8090/health
docker compose ps -q worker | wc -l   # 2
curl -sf http://127.0.0.1:5173/
```

Place an order (tiny menu: `chips`, `taco`, `burrito`; at most 3 items). The place-key is the Stripe-style `Idempotency-Key` header. `accepted_at` is set on 201; the API never calls the kitchen or courier. The worker confirms, cooks, dispatches, and polls the ride until `delivered`.

```bash
curl -sS -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: diner-1' \
  -d '{"items":["burrito"]}'
```

Replay the same key and body to get the same order id (201). The same key with a different cart is `409`. Omit `cohort_id` to use the default cohort; loadgen always sends the current `cohort_id` (`POST /cohort/new` mints a new one).

In-flight Place Order requests are capped at `API_ACCEPT_CONCURRENCY` (32, a loose door fuse). The API derives its SQLAlchemy pool as that cap plus four control-plane connections, so a request admitted by the door does not merely queue behind the library default pool. Extra requests get a counted HTTP `429` (`door busy`) and create **no** order row and no intake key. That is not kitchen fullness (sim `429` `kitchen busy` / `courier busy` — the order already exists, the worker retries the same key) and not stock (a later permanent fail). Place and cancel never call the sims. If calibrate later 429s at the door before kitchen 3×, raise the knob — do not treat door 429 as the rush beat.

A quiet 1-item chips order walks `placed` → `confirmed` → `being_prepared` → `ready` → `out_for_delivery` → `delivered`. Kitchen cook is 12s plus rail wait when pans are full (20 pans cook at once; a quiet kitchen has wait 0). Dispatch omits an explicit trip band, so the courier deterministically draws near / mid / far from the stable request body; retries draw the same band. First kitchen poll waits until a pan is free (`service_started_at`), then `being_prepared` is its own GET-visible commit before `ready`. Dispatch hangs up immediately; the first ride poll waits until the trip ETA. Even a quiet far trip finishes in well under a minute after `ready`. Prove that walk with mix **off** (`POST /admin/faults` `{"mode":"clear","mix":"off"}` on both sims) so it cannot flake; Settings defaults are the always-on 3% 5xx / 2% drop mix.

```bash
ORDER_ID=$(curl -sS -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: diner-chips' \
  -d '{"items":["chips"]}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

while true; do
  curl -sS "http://localhost:8000/orders/${ORDER_ID}"
  echo
  sleep 1
done
```

`GET` returns `id`, `state` (underscore names as stored: `placed`, `confirmed`, `being_prepared`, `ready`, `out_for_delivery`, `delivered`), `accepted_at`, `items`, and `cohort_id`. Missing ids are `404`. Dashboard labels say "out for delivery"; the stored name is `out_for_delivery`.

### Metrics snapshot

`GET /snapshot` is the one additive JSON `/` polls. Every query filters `cohort_id` (omit it to use the default cohort until `/cohort/new`). Optional `order_id` is the paste-an-ID trace. Happy-path snapshot walks run with mix **off**. The dashboard on **5173** shows the same JSON as cards.

```bash
# After the chips walk above (default cohort until /cohort/new):
curl -sS "http://localhost:8000/snapshot?order_id=${ORDER_ID}"
# Same JSON, no trace:
curl -sS http://localhost:8000/snapshot
```

**Business.** `stages` is keyed by assignment names (`placed`, `confirmed`, `being prepared`, `ready`, `out for delivery`, `delivered`) — current-state counts. On `/`, **confirmed** is kitchen `queued` (accepted, waiting for a pan) and **being prepared** is `cooking` (on a pan). That label seam stays on the existing stage cards. `terminal_rates_per_min` is delivered/cancelled/failed in the last 60s. `e2e_latency_s.p50` / `p95` are seconds from `accepted_at` to the applied `delivered` event (`null` until something has delivered).

**Correctness lite.** All of these are cohort-filtered.

- `conservation` — a current-state partition: `accepted = delivered + cancelled + failed + in_flight`. `parked` is orders with a parked work item and must be a subset of `in_flight` (park is not a lifecycle stage). `residual` checks that partition and the parked subset; `state_vs_last_order_events_mismatches` is the independent history check.
- `duplicate_attempts` — attempts that follow a failed, unknown, or abandoned call on the same work item. Successful not-ready polls are routine observations and do not inflate this count. Attempts may duplicate.
- `duplicate_effects` — extra sim-ledger rows **per order** in each sim (restaurant and courier counted separately), from `GET :8081/admin/ledger` and `GET :8082/admin/ledger`. **Not** extra rows per exact key — the ledger primary key makes that always 0 — and **not** from `work_items.result`. Two tickets for the same order under two retry keys is the duplicate. Effects must not duplicate. If a sim ledger cannot be read, this field is `null` (unknown), not a green 0. The API reads those admin URLs (compose wiring `API_RESTAURANT_ADMIN_URL` / `API_COURIER_ADMIN_URL`); place and cancel still never call the sims.
- `startup_scan` — orders with no work item (timeline B). 0 by construction (order + work item share one commit).
- `invalid_transitions` — `order_events` with cause `invalid_transition` (0-row guarded UPDATE, counted, never applied). Cancel-vs-confirm 0-row is `superseded_by_cancel`, not this count.
- `orphaned_tickets` — `order_events` with cause `orphaned_ticket` (void budget exhausted). Additive; Correctness on `/` renders the number.
- `state_vs_last_order_events_mismatches` — orders whose `orders.state` is not the last **applied** `order_events.to_state`.
- `currently_leased` — work items with a live lease (`status=leased` and `lease_until` in the future). Kill-timing instrument, not a utilization pane.
- `trace` — when `order_id` is set: `order_events` (applied and evidence rows) plus `attempts` (NULL-outcome abandoned rows included; lease timestamps are `started_at` / `ended_at`). A retry must not grow a second applied `confirmed` event.

JSON field names are frozen — later slices only add keys. Dinner-rush keys (the pipeline pane and parked list bind these; do not invent a second GET):

- `accept_reject` — `accepted` (orders in the cohort) and `rejected` (door 429s for that cohort; no order row).
- `backlog` — pending + leased work items by type (`confirm`, `poll_cook`, `dispatch`, `poll_ride`). Parked is not backlog.
- `retry_rate` — fraction of attempts in the last 60s that follow a failed, unknown, or abandoned call on that work item. Routine successful polls are not retries.
- `oldest_open` — `age_s` + assignment `stage` of the oldest non-terminal order (`null` when the cohort is idle).
- `http_429s` — `door` vs `kitchen` (confirm/poll_cook) vs `courier` (dispatch/poll_ride).
- `stretching_etas` — in-flight orders with positive pan/bike rail wait, plus the largest rail wait as `max_stretch_s`; ordinary kitchen time and quiet trips are not stretch.
- `parked_list` — additive work-item id plus order / work type / owner / reason / next action.
  Rendered on `/` (Correctness), where **Redrive** is the one write button.
- `sim_http` — per-sim requests, latency p50/p95, timeout/unknown, 5xx, and 429 over the same trailing 60-second window (restaurant = kitchen work, courier = dispatch/ride).
- `no_progress_beyond_threshold` — in-flight orders whose last applied event is older than 90s.

Calibrate reads `backlog`, `oldest_open`, and `http_429s` from this same GET. `outbound_slots` reports live lease use against configured fleet-scaled caps; it is not worker replica utilization.

### Dashboard

Vite SPA on **5173**. `/` is metrics cards plus the one operator write, **Redrive**. Three panes: **Business** (stages + terminal rates + e2e + oldest-age and stage), **Pipeline** (accept/reject, backlog by work type, retry rate, 429s, stretching ETAs, per-sim rate/latency), **Correctness** (lite fields + no-progress-beyond-threshold + parked list). The currently-leased card lists each active lease's order, work type, and worker owner so the crash beat can record the exact order before the terminal kill. The parked list shows work-item id-backed rows with order / work type / owner / reason / next action; clear the dependency fault before pressing Redrive. `/control` groups are load (Calibrate · New cohort · Steady · Rush with optional **mult** · Stop), outage (Doom-confirm · Restaurant blackout 60s · Clear restaurant), crash assist (**Courier blackout 30s only**), and **Bonuses** (Cancel race · Fail void · Out of stock · Place · Restore stock). Buttons POST through the same-origin `/loadgen`, `/rsim`, or `/csim` proxies. There is no browser Kill button. Abort leftover `fail_void` with **Clear restaurant** (`{"mode":"clear"}`) — it sticks if you skip that. Header **Controls** opens `/control` in a new tab; the two routes do not share React state. Watch independently reads `/loadgen/status` on every poll, so a cohort minted in Control appears there without cross-tab state.

Same-origin proxy (browser never needs extra `VITE_` knobs for sims/loadgen):

- `/snapshot` → Order API `:8000` (what `/` polls)
- `/work-items` → Order API `:8000` (the parked-row Redrive POST)
- `/loadgen` → loadgen `:8090` (calibrate / steady / rush / stop / cohort)
- `/rsim` → restaurant `:8081`
- `/csim` → courier `:8082`

The only `VITE_` knob is `VITE_API_BASE_URL` (Order API origin). Leave it unset in compose so the browser talks same-origin `/snapshot` and Vite forwards it. Direct curls to `:8000` / `:8081` / `:8082` / `:8090` remain valid.

```bash
# SPA
open http://127.0.0.1:5173/
open http://127.0.0.1:5173/control

# Same JSON `/` polls, via the dashboard proxy:
curl -sS "http://127.0.0.1:5173/snapshot?order_id=${ORDER_ID}"
```

A quiet chips order with knobs off walks every assignment stage on `/`: placed → confirmed → being prepared → ready → out for delivery → delivered. Confirmed is queued (waiting for a pan); being prepared is cooking (on a pan) — the existing stage cards. Watch that walk **first** (scenario 0). Stage cards bind to `snapshot.stages`. Oldest-age + stage bind to `oldest_open` on Business. Pipeline cards bind to `accept_reject`, `backlog`, `retry_rate`, `http_429s`, `stretching_etas`, `sim_http`. Correctness binds conservation residual, duplicate attempts vs effects, startup scan, invalid transitions, orphaned tickets, currently-leased, state-vs-last-event mismatch, `no_progress_beyond_threshold`, `parked_list`, and the paste-an-ID trace. The parked row's additive work-item `id` is what its Redrive button posts. Point at attempts vs effects **second** — attempts may duplicate under the 3%/2% mix; effects must not. Rush (scenario 1): backlog and oldest-age rise, then backlog falls during drain. Oldest-age also falls unless the oldest order is intentionally parked; in that case the parked row supplies the owner and next action.

Cancel is pre-pivot only: legal from `placed` or `confirmed`, rejected after the kitchen starts cooking (`being_prepared`). A live worker will confirm a new order quickly, so cancel-while-placed is a race unless you cancel before the worker's confirm lands. When cancel wins against an in-flight confirm, the losing worker enqueues `void_ticket` (`(order_id, void)`, budget `WORKER_VOID_RETRIES`). The diner book stays cancelled; the kitchen ticket is voided or counted as an orphan.

```bash
# Legal: diner cancel before the pivot (placed or confirmed)
curl -sS -X POST "http://localhost:8000/orders/${ORDER_ID}/cancel"

# After being_prepared, cancel is 409; the order stays in its current state
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  "http://localhost:8000/orders/${ORDER_ID}/cancel"
```

### Restaurant sim

The kitchen is a FastAPI sim on **8081**. Accept hangs up immediately with a ticket and `estimated_ready_at`; cooking runs on the sim's clock. The worker polls. Quiet cook is slowest item + 5s per extra item (chips 12 / taco 18 / burrito 25 / extra 5). **20 pans** are how many tickets cook at once. Rail wait extends the quiet cook: `estimated_ready_at = now + rail_wait + quiet_cook`. Ticket 21 is not a 429 — 429 fires when the quoted wait is **> 3× that ticket's quiet cook**. `RSIM_RAIL_FUSE` 80 is a hard occupancy cap so a bad knob cannot grow forever. Poll status is `queued` (waiting for a pan) then `cooking` (on a pan) then `ready`. The worker maps those to order `confirmed` then `being_prepared`.

```bash
curl -sS -X POST http://localhost:8081/accept \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: confirm-order-1' \
  -d '{"items":["burrito"]}'

curl -sS http://localhost:8081/tickets/<ticket-id>
curl -sS http://localhost:8081/keys/confirm-order-1
curl -sS -X POST http://localhost:8081/void \
  -H 'Idempotency-Key: (order-id, void)' \
  -H 'Content-Type: application/json' \
  -d '{"accept_key":"(order-id, confirm)"}'
curl -sS http://localhost:8081/admin/ledger
curl -sS http://localhost:8081/admin/stock
curl -sS -X POST http://localhost:8081/admin/stock \
  -H 'Content-Type: application/json' \
  -d '{"item":"burrito","count":0}'
```

The same `Idempotency-Key` replays the first result (Stripe-style). Timeout retries must reuse that key — never mint a new one. `GET /admin/ledger` returns effect counts by key from the sim's SQLite ledger (not Postgres). Menu-item stock (`chips` / `taco` / `burrito`, default 200) is restaurant-only. A cart with any zero-stock item is a business 4xx: no ledger row, no decrement, whole order fails. A successful new accept decrements each requested item once; the same stored confirm key replays without a second decrement. Courier has no `/admin/stock`.

### Courier sim

The courier is the same FastAPI sim contract on **8082**, same shared core, different quote and statuses. Dispatch hangs up immediately with a ticket and `estimated_ready_at`; travel runs on the sim's clock. Trip bands are near 12 / mid 20 / far 35 (`CSIM_TRIP_S`). **8 bikes** are parallelism. Fleet wait extends the trip: `estimated_ready_at = now + rail_wait + trip`. 429 when the quoted trip is **> 3× that band's normal**. Poll status is `assigned` (waiting for a bike) then `en_route` then `delivered`. HTTP paths match the restaurant (`POST /accept`, `GET /tickets/{id}`, `GET /keys/{key}`) — one core, not a fork.

```bash
curl -sS -X POST http://localhost:8082/accept \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: dispatch-order-1' \
  -d '{"band":"mid"}'

curl -sS http://localhost:8082/tickets/<ticket-id>
curl -sS http://localhost:8082/keys/dispatch-order-1
curl -sS http://localhost:8082/admin/ledger
```

Replay the same key → one row in `GET /admin/ledger`. Worker dispatch uses the stored `(order_id, dispatch)` key on `POST /accept`; poll-ride reuses that same ticket key on GET-by-key (never a per-poll key).

### Worker

Two replicas, `restart: "no"` (quoted — `restart: always` would close scenario 3's abandoned-attempt gap). Health is **container-internal** on 8083 so both replicas can start; compose does **not** publish 8083 to the host (`docker compose ps -q worker` returns two ids). `SKIP_MIGRATIONS=1`; the API already migrated. Compose wires `WORKER_DATABASE_URL`, `WORKER_RESTAURANT_BASE_URL=http://restaurant:8081`, and `WORKER_COURIER_BASE_URL=http://courier:8082`. The shared compose variables `ORDER_PIPELINE_WORKER_REPLICAS`, `ORDER_PIPELINE_DEP_CAP_RSIM`, `ORDER_PIPELINE_DEP_CAP_CSIM`, `ORDER_PIPELINE_TASK_CAPACITY`, and `ORDER_PIPELINE_CONFIRM_DEADLINE_S` feed every service that reports or enforces those values, so the dashboard, workers, and doom fixture cannot drift under the standard deployment. Confirm, poll_cook, void_ticket, dispatch, and poll_ride handlers are registered on the **same** plugin chassis — not a courier-only script. Confirm retries the stored `(order_id, confirm)` key until `accepted_at` + 120s, then the order fails. Poll cook reuses the accept key / ticket (the work-item key `(order_id, poll_cook)` is queue identity only); first poll is at `estimated_ready_at`, then every 3s within a budget of 30 — exhaust parks the work item (owner + reason + next action), not the order. Ready enqueues dispatch. Dispatch is count-bounded (`WORKER_TRANSIENT_RETRIES` 5) with stored key `(order_id, dispatch)`; exhaust parks (order stays `ready`). Poll ride reuses the dispatch ticket key; first poll at trip `estimated_ready_at`, then every 3s within budget 30 — exhaust parks (order stays `out_for_delivery`). Park is a work-item status, never a lifecycle stage. `POST /work-items/{id}/redrive` is its only exit: it resets `attempt_count` to 0, makes the same item immediately pending, clears lease/park metadata, and keeps the original work-item id, stored `idempotency_key`, payload/result, and attempt history. Redrive locks the order before its work item and rejects terminal orders; cancellation also converts parked work to cancelled. `GET /snapshot` includes that work-item id in `parked_list`; `/` posts the endpoint from its Redrive button.

```bash
docker compose ps -q worker | wc -l   # 2
```

```bash
make check   # uv run ruff + mypy + pytest, plus dashboard tsc
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. The dashboard is a separate Node/Vite app (`dashboard/`, `package-lock.json`). `make check` is the one command for lint, Python types, `tsc`, and tests. The health and place-order integration tests fail if the API or Postgres is down; restaurant tests fail if the sim on 8081 is down; courier tests fail if the sim on 8082 is down; worker tests fail if two worker replicas are not healthy; loadgen tests fail if 8090 is down; dashboard tests fail if the SPA on 5173 is down. After image changes, rebuild so compose serves the new image: `docker compose up --build --wait`. Happy-path tests turn the always-on mix off; pytest restores `{"mix":"on"}` (3% 5xx / 2% drop) when the suite exits so a later live demo still matches the pre-demo checklist. If a previous run left claimable fixture rows or a polluted volume, reset with `docker compose down -v` and bring the stack back up.

Compose publishes Postgres on `127.0.0.1:55432` — loopback only, and off the default port so it cannot collide with a Postgres already running on the host. The direct-session tests connect there; override with `TEST_DATABASE_URL` if you need to. Because `001_full_schema` is edited in place rather than superseded, a database that already applied it will **not** pick up schema changes from `alembic upgrade head` — reset the volume with `docker compose down -v` after pulling schema work.

## Load

Loadgen is the same Python image, `command: ["loadgen"]`, host **8090**. Open-loop: it keeps the offered rate even when the API returns 429 (counted rejects, never a silent drop, never a generator slowdown). Cart mix is mostly 1-item plus some 2–3. Every `POST /orders` sends the current `cohort_id`.

H is this machine's sustainable rate (highest stepped rate whose snapshot backlog stays flat), measured **once** at full topology (two workers, rail, door). Mix stays **on** during calibrate. A small absolute fill-in allowance applies only while WIP is at most four; a populated pipeline must show zero backlog growth. After finding the first overloaded step, calibrate keeps probing up to its cap until a kitchen/courier 429 proves the 3× brake (or a door 429 identifies the wrong first brake). If no positive H is found, steady and rush return 409 instead of starting a zero-rate scenario.

```bash
# Flakiness on (compose default) before calibrate:
curl -sS http://localhost:8081/admin/faults
curl -sS http://localhost:8082/admin/faults

curl -sS -X POST http://localhost:8090/calibrate
# → {"h": …, "downstream_429_observed": true, "http_429s": {…}, …}

# Calibration deliberately crosses the overload line and may leave probe work parked.
# Isolate the demo evidence before scenario 0; scenarios 0 and 1 then share this cohort.
curl -sS -X POST http://localhost:8090/cohort/new

curl -sS -X POST http://localhost:8090/scenario/steady     # 0.4×H
curl -sS -X POST http://localhost:8090/scenario/rush       # 60s @1.5×H, then drain to 0.4×H
curl -sS -X POST 'http://localhost:8090/scenario/rush?mult=2.0'
curl -sS -X POST http://localhost:8090/beat/doom-confirm   # returns 3 explicitly doomed ids
curl -sS -X POST http://localhost:8090/beat/cancel-race    # rehearsal: place + cancel vs confirm
curl -sS -X POST http://localhost:8090/beat/place \
  -H 'Content-Type: application/json' \
  -d '{"item":"burrito"}'   # returns order_id; not a new scenario
curl -sS -X POST http://localhost:8090/stop
curl -sS -X POST http://localhost:8090/cohort/new
```

Live `POST /beat/cancel-race` is rehearsal only — confirm is milliseconds. The automated pass holds confirm in-flight in pytest, then cancels.

Scenario 0: `POST /scenario/steady` then watch one order on `/` through every stage (no skips, no reversals). Backlog stays flat; conservation residual 0. Scenario 1: `POST /scenario/rush` (steady already running). Backlog and oldest-age rise then fall; duplicate effects and invalid transitions stay 0; parked list may be non-empty.

Rush assumes steady is already running and does **not** replay a minute of baseline. If calibrate 429s at the door first, raise `API_ACCEPT_CONCURRENCY` and recalibrate — kitchen/courier 3× is the intended busy beat. `/control` load buttons POST the same paths through `/loadgen`; curls stay the Method spec and the fallback.

`POST /beat/doom-confirm` creates three orders in the current cohort and returns `order_ids`. Loadgen installs a restaurant-owned rule for their stored `(order_id, confirm)` keys until each order's `accepted_at + 120s`; it does not enable global blackout. The rule lands before any selected key has a restaurant effect and the fixture returns `409` if an order/effect won that race. Untagged confirms remain available. Restaurant `{"mode":"clear"}`, a new loadgen cohort, or a later doom-confirm cohort removes the old targeted set.

Scenario 2 keeps steady arrivals at **0.4×H**. On `/control`, click **Doom-confirm**, then **Restaurant blackout (60s)**; the latter posts exactly `{"mode":"blackout","seconds":60}` through `/rsim/admin/faults`. **Clear restaurant** posts `{"mode":"clear"}` to that same proxy. On `/`, Pipeline splits each sim's trailing-60-second timeout/unknown responses from 5xx and 429, and shows active outbound leases against the configured two-worker fleet caps: restaurant `used/16`, courier `used/16`, all tasks `used/48` (per-worker 8 / 8 / 24). The used values are live leases; caps come from deployment configuration rather than live replica discovery. Doom-confirm never goes through `/rsim`.

Scenario 3 runs in two beats, in order. First, with steady arrivals still running and the always-on mix **on**, arm a catchable lease with the existing **Restaurant blackout (60s)** button on `/control` (the Outage group — do not add this to Crash assist). A healthy confirm is milliseconds; blackout holds the outbound call for `sim_timeout_s + 0.5s` so the currently-leased card stays readable. Then record an order id and its owner hostname. Match that hostname to the worker id from `docker compose ps -q worker`, stop that exact worker **from the Docker terminal** while the lease is in flight, and **Clear restaurant** immediately after the kill:

```bash
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"blackout","seconds":60}'
# Read Watch's currently-leased card, then:
docker kill <full-worker-id-whose-prefix-matches-the-visible-owner>
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear"}'
# Watch the recorded trace for the <=15s gap and survivor resume first, then:
docker compose up -d worker
```

The owner begins with the container hostname (the short prefix of the full id), so the row-to-container match is visible without a database query. Paste the order id: the trace shows the abandoned NULL-outcome attempt, the survivor's new attempt, both owners, both work-item ids, and the same stored `idempotency_key`. There is one applied confirm event, no lease-lifecycle `order_events`, and duplicate effects stays 0. Restaurant blackout is a pre-effect fault — the ledger row is written by the survivor. Timeline D (effect applied, response lost) is covered by `tests/test_confirm_faults.py`, not this beat. Worker replica utilization 2 → 1 → 2 is read from `docker compose ps`, not a dashboard pane.

Second, click **Courier blackout (30s)** in `/control`. A dispatch exhausts its budget of 5 and appears in Watch's parked list while its order remains `ready`. Observe its owner, reason, and next action. Clear the courier fault (or wait for the timed blackout to expire), then press **Redrive** on that parked row:

```bash
curl -sS -X POST http://localhost:8082/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear"}'
```

Watch—not a curl—is the runbook's operator path for `POST /work-items/{id}/redrive`. The item becomes immediately eligible with attempt count 0 and the same stored `(order_id, dispatch)` key, then the order delivers with one courier-ledger effect. Redriving before clearing the blackout simply exhausts and parks the same job again; that is expected, not a bug.

## Faults

Restaurant sim admin on `localhost:8081` and courier sim admin on `localhost:8082` share one router. Sticky mode defaults **off**. Always-on mix defaults **on**: `RSIM_FLAKY_5XX_PCT=3` / `RSIM_FLAKY_DROP_PCT=2`, mirrored on `CSIM_*`. The 5xx slice of that mix includes after-effect 5xx (write, then 500). `GET /admin/faults` shows the live mix (percentages + mode + `blackout_remaining_s`).

```bash
curl -sS http://localhost:8081/admin/faults
curl -sS http://localhost:8082/admin/faults
# Happy walk: mix off on both sims so the walk cannot flake
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear","mix":"off"}'
curl -sS -X POST http://localhost:8082/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear","mix":"off"}'
# Restore the always-on 3/2 mix
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear","mix":"on"}'
```

`mode` is `clear` (off), `5xx_before` (fail before the ledger write), `5xx_after` (write then 5xx), `drop` (apply the effect, then close without a body — timeline D), **`blackout`** (timed; accepts and ordinary ticket/key reads cannot succeed inside the 2s timeout — drop, classified unknown, same key), or restaurant-only **`fail_void`** (sticky 500s on new `POST /void` calls so the void budget of 3 can exhaust; same-key void replays stay cached, and accepts retain the always-on mix). Health and admin endpoints remain available so the dependency can be observed and cleared. Blackout defaults off on **both** 8081 and 8082. Doom-confirm's targeted restaurant rule is separate from this mode and is listed as `confirm_unavailable` by `GET /admin/faults`; it remains active after a 60s blackout expires. `clear` removes the global mode, `fail_void`, and the targeted set. **Abort leftover `fail_void` with `clear`** — leaving it on pollutes later voids and the orphan count. `/control` keeps the scenario-2 `/rsim` outage group unchanged and adds a separate crash-assist group whose only write is courier `{"mode":"blackout","seconds":30}` through `/csim`. Container kill/restart stays in the Docker terminal; Redrive stays on Watch. Bonuses: Cancel race (`/loadgen/beat/cancel-race`), Fail void (`/rsim` `fail_void`), Out of stock / Restore stock (`/rsim` `/admin/stock`), Place (`/loadgen/beat/place`). **Abort leftover burrito=0 with Restore stock (`count: 200`)** — leaving it at 0 poisons scenario 0.

```bash
# Timed blackout on either sim (seconds required)
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"blackout","seconds":60}'
curl -sS -X POST http://localhost:8082/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"blackout","seconds":30}'

# Restaurant-only; Abort with clear
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"fail_void"}'
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear"}'
```

Talk-track: watch the clean walk **first** (mix off). Then leave 3/2 on and point at duplicate attempts vs duplicate effects = 0. Duplicate attempts records actual re-execution after a failed, unknown, or abandoned call; routine successful cook/ride polls are excluded. Effects must stay 0.

Deterministic proofs (compose; always `clear` + `mix` off when finished so the walk and cancel tests stay unpoisoned):

**5xx_after / drop (timeline D / wrong turn 4).** Sticky 100% after-effect fault, one chips order, wait until `confirmed`. `GET /admin/ledger` counts that order's confirm key as **1** (duplicate effects = 0) and Postgres has exactly one applied `confirmed` event. Retry of the same stored `(order_id, confirm)` key replays the cached 200. Then `{"mode":"clear"}`.

```bash
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"5xx_after","mix":"off"}'   # or {"mode":"drop","mix":"off"}

# place chips, poll GET /orders/{id} until confirmed, then:
curl -sS http://localhost:8081/admin/ledger
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear","mix":"off"}'
```

**Reclaim (timeline C).** Mid-call lease loss after the kitchen effect is applied: one ledger row, the abandoned attempt stays `outcome IS NULL` (not rewritten to timeout), the survivor retries the same stored key, and `order_events` has no second confirm and no fake lease-lifecycle rows.

## Architecture

- One Python package (`order_pipeline`) with room for worker and sim entrypoints; **one Python Dockerfile**. Restaurant is `restaurant-sim` / `python -m order_pipeline.restaurant` via compose `command:` (the image CMD stays the API). Courier is `courier-sim` / `python -m order_pipeline.courier` the same way. Worker is `worker` / `python -m order_pipeline.worker`. Loadgen is `loadgen` / `python -m order_pipeline.loadgen` on **8090**. The dashboard is a **separate Node/Vite image** (`dashboard/Dockerfile`), not a second Python Dockerfile.
- Runtime and dev deps live in `pyproject.toml`; `uv.lock` pins versions. No `requirements.txt`.
- Compose carries wiring only (DSNs, hosts, ports, ledger paths, restaurant and courier base URLs, API sim admin URLs for `GET /snapshot`, loadgen API + restaurant-admin URLs, dashboard Vite proxy targets). Knob defaults live in `APISettings` / `RSIMSettings` / `CSIMSettings` / `WorkerSettings` / `LoadgenSettings`. The dashboard's only `VITE_` knob is `VITE_API_BASE_URL`; compose leaves it unset and proxies instead.
- One Alembic revision (`001_full_schema`) creates the full business schema: `orders` (including `items` JSONB), `order_events` (including an `applied` flag), `work_items`, `attempts`, and `intake_keys`. Later slices must not add a second revision.
- `order_events` is append-only evidence. `applied` separates transitions that actually moved the order from evidence rows for rejected or illegal attempts, so "current state equals the last applied event" stays checkable once workers start recording invalid transitions.
- The API container runs `alembic upgrade head` before uvicorn, so a clean Postgres volume is migrated before `/health` is ready. The restaurant sim, courier sim, worker, and loadgen set `SKIP_MIGRATIONS=1` — the sims have no Postgres DSN; the worker has one but must not race the API's migration; loadgen talks HTTP only.
- `POST /orders` validates the cart, requires `Idempotency-Key`, and commits the order (`placed`), `placed` event, confirm work item, and intake key in **one transaction**. A SHA-256 fingerprint of the canonical body is stored with the place-key (TTL `API_PLACE_KEY_TTL_H`, default 48h). Same key + same body replays the same order id; same key + different body → 409. Concurrent Place Order requests are capped at `API_ACCEPT_CONCURRENCY` (32): a full door returns counted `429` (`door busy`) without inserting an order. Kitchen/courier `429` is a different no (busy, retry, same key). Place and cancel never call the sims. `GET /snapshot` is the exception: it reads `GET /admin/ledger` on both sims so duplicate effects are not a Postgres-only count.
- `POST /orders/{id}/cancel` is a guarded `placed`/`confirmed` → `cancelled` (version bump, applied event, actor `api`, cause `cancel`). Pending and parked work are cancelled and unleased in the same txn so neither quiet work nor a later redrive can resurrect a cancelled order. Leased in-flight work is marked `cancelled` but keeps `lease_owner` so the returning worker can finalize as supersession (`superseded_by_cancel` evidence, not `invalid_transition`). That 0-row confirm finalize enqueues `void_ticket` under `(order_id, void)` with budget `WORKER_VOID_RETRIES`. After `being_prepared` the UPDATE is not applied: HTTP 409, state unchanged, evidence `order_events` row with `applied=false` and cause `invalid_transition`. Missing ids are `404`. Already-cancelled replays 200 without resurrecting.
- Shared sim core (`order_pipeline.sim`): accept, poll, Stripe key cache (ledger-backed), SQLite effect ledger, `/admin/faults` + `/admin/ledger`. Restaurant and courier each inject a quote function and a `status_at` callback into `SimCore` — they do not copy the fault router, ledger, or admin. Each sim's ledger is independently authoritative for applied effects and lives on its own compose volume.
- Worker chassis is a thin work-type **plugin loop**, not a kitchen-only script. Handlers register by work type (confirm, poll_cook, void_ticket, dispatch, poll_ride). Each cycle is three phases with **no DB transaction across HTTP**: short txn `SKIP LOCKED` claim + `INSERT` attempt (`outcome` NULL) → handler HTTP (lease covers that one call) → new short txn classifies the outcome, guarded state `UPDATE`, `order_events`, and complete/park/schedule the work item. Kill mid-call leaves the NULL attempt; a survivor opens a **new** attempt with the same stored key. `order_events` does not grow lease-lifecycle rows.
- Confirm HTTP uses the stored work-item key `(order_id, confirm)` on `POST /accept` (and GET-by-key). Poll cook's work-item key `(order_id, poll_cook)` is queue identity only — restaurant polls reuse the accept key / ticket id, never a per-poll HTTP key. Ticket id, `estimated_ready_at`, and the accept key live on the poll item's JSONB payload. Dispatch is the same shape with stored `(order_id, dispatch)`; poll ride's queue key `(order_id, poll_ride)` is not the courier HTTP key.
- First cook poll is scheduled at pan-start (`service_started_at`), then at `estimated_ready_at` once cooking has started. Confirm is time-bounded (`accepted_at` + 120s → order **fails**, no park). Poll budget exhaust **parks** the work item. Dispatch exhausts at `WORKER_TRANSIENT_RETRIES` 5 and parks; ride-poll exhausts at 30 and parks. Park is never a lifecycle stage.
- `confirmed` → `being_prepared` is cooking started (a pan, not a timer). `queued` on the kitchen sim stays `confirmed`. `being_prepared` → `ready` is a later commit. `ready` enqueues dispatch scheduled `WORKER_POLL_INTERVAL_S` later so `/` can observe the ready card (dispatch at `now` skipped it). Then `out_for_delivery`; poll ride commits `delivered`. Courier `assigned` / `en_route` stay `out_for_delivery` — there is no extra order stage. Collapsing kitchen arrows into one finalize would skip a stage on the walk.
- Outbound restaurant calls go through an `asyncio.Semaphore` sized to `WORKER_DEP_CAP_RSIM` (8). Outbound courier calls go through `WORKER_DEP_CAP_CSIM` (8). The worker **admits** kitchen work only while rsim has a free slot, and courier work only while csim has a free slot — it does not claim-then-wait, so one blacked-out sim cannot fill all 24 task slots with leased waiters. Both caps are live within `WORKER_TASK_CAPACITY` 24. Backoff is unleased (0.5s ×2, cap 8s, full jitter).

## Trade-offs

- Work type and attempt outcome are `TEXT` plus `CHECK`, not native Postgres `ENUM`s, so bonus `void_ticket` does not need `ALTER TYPE` or a second revision.
- Columns and indexes later slices need (park/lease fields, `order_events.applied`, `attempts.ended_at`, payload/result JSONB, `cohort_id`, `accepted_at`, and the `(status, lease_until)` index lease reclaim will scan) land in this revision even while unused; missing ones would be migration churn.
- Intake is Stripe-style: a client place-key plus a body fingerprint, not a payload-hash-as-identity. Replay is the same order; a different cart under the same key is a conflict, not a silent merge.
- The fingerprint covers the cart as the client sent it, so the same key with the items reordered is a 409 rather than a replay. A place-key identifies one intent; a body that differs at all is a conflict. Normalizing the cart first would make those two agree, but conflicting is the safer default — it can never merge two different intents into one order.
- The intake place-key unique constraint is named explicitly (`uq_intake_keys_place_key`) instead of inheriting Postgres's generated name, and the concurrent-replay path recovers from *that* constraint alone — any other unique violation propagates. A duplicate work-item key can never be mistaken for a place-key replay, and a future naming convention cannot silently break recovery.
- Order + event + confirm work item + intake key share one commit so an accepted order cannot exist without its work item (Design timeline B). Confirm work is stored with idempotency key `(order_id, confirm)` at insert time; the API does not execute confirm or open attempts.
- `make check` runs ruff, mypy, dashboard `tsc`, then pytest.
- `RSIMSettings` ships complete (pans, 3× busy, rail fuse, cook times, flaky pcts) at first appearance so Settings is not grown twice. Pans / 3× / fuse are **wired**: 20 pans cook at once, 429 when quoted wait > 3× that ticket's quiet cook, fuse 80. Flaky pcts default to Config's **3/2**. Happy-path tests POST `mix` off so they do not require random faults to pass.
- `CSIMSettings` ships complete (fleet 8, 3× busy, trip bands near/mid/far, flaky pcts) at first appearance. Fleet / 3× are **wired** — same shape as the kitchen, per trip band, no extra Settings field for a courier fuse (none exists). `CSIM_FLAKY_5XX_PCT` / `CSIM_FLAKY_DROP_PCT` default to **3/2**. Boot asserts flakiness sum < 50, `busy_multiple >= 2`, and min trip > sim timeout so dispatch hangs up without waiting for travel.
- Sim 5xx can fire before *or* after the ledger write (`5xx_before` / `5xx_after`); `drop` applies the effect then hangs up without a body. Replay of an existing key returns the cached success and skips the sticky fault, so a retry with the same key is safe (easy wrong turns 3 and 4).
- Doom-confirm is not a third blackout. Loadgen batches three ordinary `POST /orders` calls, then atomically replaces the restaurant's per-key unavailable set with those stored confirm keys and their individual deadlines. The sim refuses to arm a key already present in its effect ledger, and loadgen also verifies every returned order is still `placed`; either race aborts and clears the set. The worker's existing time-bounded confirm policy produces the explicit failure.
- Worker `restart: "no"` on purpose: `restart: always` would reap the abandoned NULL attempt that scenario 3 needs to show. Two replicas by default via `deploy.replicas: ${ORDER_PIPELINE_WORKER_REPLICAS:-2}` so H is measured at demo topology while the API reports the same configured topology. Worker health stays inside the container (8083) — publishing it to the host would collide when both replicas start.
- Two tabs, no shared React state: `/` polls `GET /snapshot`; `/control` POSTs `/loadgen/...` through the Vite proxy. A click changes the system; Watch notices on the next poll. Header Controls opens `/control` with `target="_blank"`.
- Pipeline pane binds the existing snapshot keys in place and additively shows the trailing-60-second outage split (`timeout` includes dropped/unknown responses, plus `http_5xx` and `http_429`) for each sim. `outbound_slots` derives live use from active DB leases and reports configured two-worker fleet caps 16 / 16 / 48 while retaining each per-worker cap. Oldest-age + stage live on Business. Parked list + no-progress live on Correctness; Redrive is the single write in that pane. There is no worker-replica utilization pane.
- Redrive locks the order and then the existing `work_items` row; it does not insert a replacement work item or delete attempts. Terminal orders are rejected, and cancel converts parked work to cancelled. Resetting the persisted counter is what restores the full count-bound budget. Preserving the stored key is what makes a post-timeout dispatch replay the same external operation rather than minting a second courier effect.
- `WorkerSettings` ships complete at first appearance, including `WORKER_DEP_CAP_CSIM` / `WORKER_VOID_RETRIES` / poll knobs, so Settings is not grown twice. Both rsim and csim semaphores are live (8 / 8 within task capacity 24). Kitchen work is not claimed when the rsim cap is full (same for courier / csim), so the leftover task slots can serve the other lane. Boot asserts `lease_s > sim_timeout_s` and `task_capacity > dep_cap_rsim + dep_cap_csim`. Courier base URL is compose wiring (`WORKER_COURIER_BASE_URL`), not a new Settings knob.
- First cook poll is not t=0: a 25s quiet burrito would burn the 30-poll window before any rail stretch exists. Waiting until a pan is free (`service_started_at`), then until `estimated_ready_at` once cooking, keeps the budget for the oven and the rail. Then 3s polls cover the 90s bound.
- Two keys on poll cook (and poll ride) on purpose: `work_items.idempotency_key` is UNIQUE, so the queue needs `(order_id, poll_cook)` / `(order_id, poll_ride)`. That must not be the sim HTTP key — minting a new key per poll would duplicate effects. The accept/dispatch key and ticket id travel in payload JSONB instead.
- Cancel is pre-pivot only (`placed`/`confirmed`). After `being_prepared`, a diner cancel is invalid evidence — not a state change and not a `void_ticket`. If the worker still finalizes a confirm after cancel won, that 0-row UPDATE is supersession (`superseded_by_cancel`), not `invalid_transition`, and enqueues `void_ticket`. Exhaustion records `orphaned_ticket`; `fail_void` is how the live beat forces that. Abort is restaurant `clear`.
