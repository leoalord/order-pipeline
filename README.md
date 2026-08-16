# Order Pipeline

Single-machine order pipeline. One Python package plus a Vite dashboard; Compose runs Postgres, the API, the restaurant sim, the courier sim, one worker, and the SPA on 5173.

## Run

```bash
uv sync

docker compose down -v
docker compose up --wait
curl -sf http://localhost:8000/health
curl -sf http://localhost:8081/health
curl -sf http://localhost:8082/health
curl -sf http://localhost:8083/health
curl -sf http://127.0.0.1:5173/
```

Place an order (tiny menu: `chips`, `taco`, `burrito`; at most 3 items). The place-key is the Stripe-style `Idempotency-Key` header. `accepted_at` is set on 201; the API never calls the kitchen or courier. The worker confirms, cooks, dispatches, and polls the ride until `delivered`.

```bash
curl -sS -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: diner-1' \
  -d '{"items":["burrito"]}'
```

Replay the same key and body to get the same order id (201). The same key with a different cart is `409`. Omit `cohort_id` to use the default cohort; send one when loadgen exists.

A quiet 1-item chips order walks `placed` → `confirmed` → `being_prepared` → `ready` → `out_for_delivery` → `delivered`. Kitchen cook is 12s; a quiet near trip is 12s. First kitchen poll waits until `estimated_ready_at`, then `being_prepared` is its own GET-visible commit before `ready`. Dispatch hangs up immediately; the first ride poll waits until the trip ETA. After `ready`, a near trip finishes in well under a minute. Prove that walk with mix **off** (`POST /admin/faults` `{"mode":"clear","mix":"off"}` on both sims) so it cannot flake; Settings defaults are the always-on 3% 5xx / 2% drop mix.

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

**Business.** `stages` is keyed by assignment names (`placed`, `confirmed`, `being prepared`, `ready`, `out for delivery`, `delivered`) — current-state counts. `terminal_rates_per_min` is delivered/cancelled/failed in the last 60s. `e2e_latency_s.p50` / `p95` are seconds from `accepted_at` to the applied `delivered` event (`null` until something has delivered).

**Correctness lite.** All of these are cohort-filtered.

- `conservation` — `accepted = delivered + cancelled + failed + in_flight`. `parked` is orders with a parked work item and must be a subset of `in_flight` (park is not a lifecycle stage). `residual` is 0 when the books balance and parked ⊂ in-flight.
- `duplicate_attempts` — extra `attempts` rows per work item, **including abandoned NULL-outcome rows**. Attempts may duplicate.
- `duplicate_effects` — extra sim-ledger rows per idempotency key, from restaurant `GET :8081/admin/ledger` and courier `GET :8082/admin/ledger`. **Not** from `work_items.result` or any other Postgres count. Effects must not duplicate. The API reads those admin URLs (compose wiring `API_RESTAURANT_ADMIN_URL` / `API_COURIER_ADMIN_URL`); place and cancel still never call the sims.
- `startup_scan` — orders with no work item (timeline B). 0 by construction (order + work item share one commit).
- `invalid_transitions` — `order_events` with cause `invalid_transition` (0-row guarded UPDATE, counted, never applied).
- `state_vs_last_order_events_mismatches` — orders whose `orders.state` is not the last **applied** `order_events.to_state`.
- `currently_leased` — work items with a live lease (`status=leased` and `lease_until` in the future). Kill-timing instrument, not a utilization pane.
- `trace` — when `order_id` is set: `order_events` (applied and evidence rows) plus `attempts` (NULL-outcome abandoned rows included; lease timestamps are `started_at` / `ended_at`). A retry must not grow a second applied `confirmed` event.

JSON field names are frozen — later slices only add keys.

### Dashboard

Vite SPA on **5173**. `/` is read-only metrics (cards, not charts; no pipeline pane). `/control` is an empty stub (heading only, no buttons) so dinner_rush can fill it without retrofitting Vite. Header **Controls** opens `/control` in a new tab; the two routes do not share React state.

Same-origin proxy (browser never needs extra `VITE_` knobs for sims/loadgen):

- `/snapshot` → Order API `:8000` (what `/` polls)
- `/loadgen` → `:8090` (reserved; loadgen is not a compose service yet)
- `/rsim` → restaurant `:8081`
- `/csim` → courier `:8082`

The only `VITE_` knob is `VITE_API_BASE_URL` (Order API origin). Leave it unset in compose so the browser talks same-origin `/snapshot` and Vite forwards it. Direct curls to `:8000` / `:8081` / `:8082` remain valid.

```bash
# SPA
open http://127.0.0.1:5173/
open http://127.0.0.1:5173/control

# Same JSON `/` polls, via the dashboard proxy:
curl -sS "http://127.0.0.1:5173/snapshot?order_id=${ORDER_ID}"
```

A quiet chips order with knobs off walks every assignment stage on `/`: placed → confirmed → being prepared → ready → out for delivery → delivered. Watch that walk **first**. Stage cards bind to `snapshot.stages` (those keys). Correctness-lite cards bind to conservation residual, duplicate attempts vs effects, startup scan, invalid transitions, currently-leased, state-vs-last-event mismatch, and the paste-an-ID trace. Point at attempts vs effects **second** — attempts may duplicate under the 3%/2% mix; effects must not.

Cancel is pre-pivot only: legal from `placed` or `confirmed`, rejected after the kitchen starts cooking (`being_prepared`). A live worker will confirm a new order quickly, so cancel-while-placed is a race unless you cancel before the worker's confirm lands.

```bash
# Legal: diner cancel before the pivot (placed or confirmed)
curl -sS -X POST "http://localhost:8000/orders/${ORDER_ID}/cancel"

# After being_prepared, cancel is 409; the order stays in its current state
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  "http://localhost:8000/orders/${ORDER_ID}/cancel"
```

### Restaurant sim

The kitchen is a FastAPI sim on **8081**. Accept hangs up immediately with a ticket and `estimated_ready_at`; cooking runs on the sim's clock. The worker polls. Quiet cook is slowest item + 5s per extra item (chips 12 / taco 18 / burrito 25 / extra 5). With no pans yet, a new ticket starts `cooking` immediately so poll can return `cooking` then `ready`.

```bash
curl -sS -X POST http://localhost:8081/accept \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: confirm-order-1' \
  -d '{"items":["burrito"]}'

curl -sS http://localhost:8081/tickets/<ticket-id>
curl -sS http://localhost:8081/keys/confirm-order-1
curl -sS http://localhost:8081/admin/ledger
```

The same `Idempotency-Key` replays the first result (Stripe-style). Timeout retries must reuse that key — never mint a new one. `GET /admin/ledger` returns effect counts by key from the sim's SQLite ledger (not Postgres).

### Courier sim

The courier is the same FastAPI sim contract on **8082**, same shared core, different quote and statuses. Dispatch hangs up immediately with a ticket and `estimated_ready_at`; travel runs on the sim's clock. Trip bands are near 12 / mid 20 / far 35 (`CSIM_TRIP_S`); `estimated_ready_at = now + trip`. With no fleet wait yet, a new dispatch starts `en_route` immediately so poll can return `assigned` (if the clock is before accept) then `en_route` then `delivered`. HTTP paths match the restaurant (`POST /accept`, `GET /tickets/{id}`, `GET /keys/{key}`) — one core, not a fork.

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

One replica on **8083**, `restart: "no"` (quoted — `restart: always` would close scenario 3's abandoned-attempt gap). `SKIP_MIGRATIONS=1`; the API already migrated. Compose wires `WORKER_DATABASE_URL`, `WORKER_RESTAURANT_BASE_URL=http://restaurant:8081`, and `WORKER_COURIER_BASE_URL=http://courier:8082`. Confirm, poll_cook, dispatch, and poll_ride handlers are registered on the **same** plugin chassis — not a courier-only script. Confirm retries the stored `(order_id, confirm)` key until `accepted_at` + 120s, then the order fails. Poll cook reuses the accept key / ticket (the work-item key `(order_id, poll_cook)` is queue identity only); first poll is at `estimated_ready_at`, then every 3s within a budget of 30 — exhaust parks the work item (owner + reason + next action), not the order. Ready enqueues dispatch. Dispatch is count-bounded (`WORKER_TRANSIENT_RETRIES` 5) with stored key `(order_id, dispatch)`; exhaust parks (order stays `ready`). Poll ride reuses the dispatch ticket key; first poll at trip `estimated_ready_at`, then every 3s within budget 30 — exhaust parks (order stays `out_for_delivery`). Park is a work-item status, never a lifecycle stage, and there is no parked-list UI yet.

```bash
curl -sf http://localhost:8083/health
curl -sf http://localhost:8083/ready
```

```bash
make check   # uv run ruff + mypy + pytest, plus dashboard tsc
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. The dashboard is a separate Node/Vite app (`dashboard/`, `package-lock.json`). `make check` is the one command for lint, Python types, `tsc`, and tests. The health and place-order integration tests fail if the API or Postgres is down; restaurant tests fail if the sim on 8081 is down; courier tests fail if the sim on 8082 is down; worker tests fail if the worker on 8083 is down; dashboard tests fail if the SPA on 5173 is down. After image changes, rebuild so compose serves the new image: `docker compose up --build --wait`.

Compose publishes Postgres on `127.0.0.1:55432` — loopback only, and off the default port so it cannot collide with a Postgres already running on the host. The direct-session tests connect there; override with `TEST_DATABASE_URL` if you need to. Because `001_full_schema` is edited in place rather than superseded, a database that already applied it will **not** pick up schema changes from `alembic upgrade head` — reset the volume with `docker compose down -v` after pulling schema work.

## Load

Loadgen and dinner-rush profiles land in a later slice.

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

`mode` is `clear` (off), `5xx_before` (fail before the ledger write), `5xx_after` (write then 5xx), `drop` (apply the effect, then close without a body — timeline D), or **`blackout`** (timed; no success inside the 2s timeout — drop, classified unknown, same key). Blackout is a fixture for later slices; default is off on **both** 8081 and 8082. Outage will POST restaurant blackout as the beat; crash will POST courier blackout as the park fixture. Direct curls; no `/control` buttons yet.

```bash
# Timed blackout on either sim (seconds required)
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"blackout","seconds":60}'
curl -sS -X POST http://localhost:8082/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"blackout","seconds":30}'
```

Talk-track: watch the clean walk **first** (mix off). Then leave 3/2 on and point at duplicate attempts vs duplicate effects = 0. Duplicate attempts under mix is a rehearsal observation, not an automated pass condition.

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

- One Python package (`order_pipeline`) with room for worker and sim entrypoints; **one Python Dockerfile**. Restaurant is `restaurant-sim` / `python -m order_pipeline.restaurant` via compose `command:` (the image CMD stays the API). Courier is `courier-sim` / `python -m order_pipeline.courier` the same way. Worker is `worker` / `python -m order_pipeline.worker`. The dashboard is a **separate Node/Vite image** (`dashboard/Dockerfile`), not a second Python Dockerfile.
- Runtime and dev deps live in `pyproject.toml`; `uv.lock` pins versions. No `requirements.txt`.
- Compose carries wiring only (DSNs, hosts, ports, ledger paths, restaurant and courier base URLs, API sim admin URLs for `GET /snapshot`, dashboard Vite proxy targets). Knob defaults live in `APISettings` / `RSIMSettings` / `CSIMSettings` / `WorkerSettings`. The dashboard's only `VITE_` knob is `VITE_API_BASE_URL`; compose leaves it unset and proxies instead.
- One Alembic revision (`001_full_schema`) creates the full business schema: `orders` (including `items` JSONB), `order_events` (including an `applied` flag), `work_items`, `attempts`, and `intake_keys`. Later slices must not add a second revision.
- `order_events` is append-only evidence. `applied` separates transitions that actually moved the order from evidence rows for rejected or illegal attempts, so "current state equals the last applied event" stays checkable once workers start recording invalid transitions.
- The API container runs `alembic upgrade head` before uvicorn, so a clean Postgres volume is migrated before `/health` is ready. The restaurant sim, courier sim, and worker set `SKIP_MIGRATIONS=1` — the sims have no Postgres DSN; the worker has one but must not race the API's migration.
- `POST /orders` validates the cart, requires `Idempotency-Key`, and commits the order (`placed`), `placed` event, confirm work item, and intake key in **one transaction**. A SHA-256 fingerprint of the canonical body is stored with the place-key (TTL `API_PLACE_KEY_TTL_H`, default 48h). Same key + same body replays the same order id; same key + different body → 409. Place and cancel never call the sims. `GET /snapshot` is the exception: it reads `GET /admin/ledger` on both sims so duplicate effects are not a Postgres-only count.
- `POST /orders/{id}/cancel` is a guarded `placed`/`confirmed` → `cancelled` (version bump, applied event, actor `api`, cause `cancel`). Pending work is cancelled and unleased in the same txn so a quiet pre-pivot cancel is not confirmed into a ticket. Leased in-flight work is marked `cancelled` but keeps `lease_owner` so the returning worker can finalize as supersession (`superseded_by_cancel` evidence, not `invalid_transition`). After `being_prepared` the UPDATE is not applied: HTTP 409, state unchanged, evidence `order_events` row with `applied=false` and cause `invalid_transition`. Missing ids are `404`. Already-cancelled replays 200 without resurrecting. The confirm-vs-cancel race and `void_ticket` wait for bonus A.
- Shared sim core (`order_pipeline.sim`): accept, poll, Stripe key cache (ledger-backed), SQLite effect ledger, `/admin/faults` + `/admin/ledger`. Restaurant and courier each inject a quote function and a `status_at` callback into `SimCore` — they do not copy the fault router, ledger, or admin. Each sim's ledger is independently authoritative for applied effects and lives on its own compose volume.
- Worker chassis is a thin work-type **plugin loop**, not a kitchen-only script. Handlers register by work type (confirm, poll_cook, dispatch, poll_ride). Each cycle is three phases with **no DB transaction across HTTP**: short txn `SKIP LOCKED` claim + `INSERT` attempt (`outcome` NULL) → handler HTTP (lease covers that one call) → new short txn classifies the outcome, guarded state `UPDATE`, `order_events`, and complete/park/schedule the work item. Kill mid-call leaves the NULL attempt; a survivor opens a **new** attempt with the same stored key. `order_events` does not grow lease-lifecycle rows.
- Confirm HTTP uses the stored work-item key `(order_id, confirm)` on `POST /accept` (and GET-by-key). Poll cook's work-item key `(order_id, poll_cook)` is queue identity only — restaurant polls reuse the accept key / ticket id, never a per-poll HTTP key. Ticket id, `estimated_ready_at`, and the accept key live on the poll item's JSONB payload. Dispatch is the same shape with stored `(order_id, dispatch)`; poll ride's queue key `(order_id, poll_ride)` is not the courier HTTP key.
- First cook poll is scheduled at `estimated_ready_at`, then every `WORKER_POLL_INTERVAL_S` (3s) within `WORKER_POLL_BUDGET` 30. Confirm is time-bounded (`accepted_at` + 120s → order **fails**, no park). Poll budget exhaust **parks** the work item. Dispatch exhausts at `WORKER_TRANSIENT_RETRIES` 5 and parks; ride-poll exhausts at 30 and parks. Park is never a lifecycle stage.
- `confirmed` → `being_prepared` is cooking started (its own guarded commit) so GET can observe it; `being_prepared` → `ready` is a later commit. `ready` enqueues dispatch → `out_for_delivery`; poll ride commits `delivered`. Courier `assigned` / `en_route` stay `out_for_delivery` — there is no extra order stage. Collapsing kitchen arrows into one finalize would skip a stage on the walk.
- Outbound restaurant calls go through an `asyncio.Semaphore` sized to `WORKER_DEP_CAP_RSIM` (8). Outbound courier calls go through `WORKER_DEP_CAP_CSIM` (8). Both are live within `WORKER_TASK_CAPACITY` 24. Backoff is unleased (0.5s ×2, cap 8s, full jitter).

## Trade-offs

- Work type and attempt outcome are `TEXT` plus `CHECK`, not native Postgres `ENUM`s, so bonus `void_ticket` does not need `ALTER TYPE` or a second revision.
- Columns and indexes later slices need (park/lease fields, `order_events.applied`, `attempts.ended_at`, payload/result JSONB, `cohort_id`, `accepted_at`, and the `(status, lease_until)` index lease reclaim will scan) land in this revision even while unused; missing ones would be migration churn.
- Intake is Stripe-style: a client place-key plus a body fingerprint, not a payload-hash-as-identity. Replay is the same order; a different cart under the same key is a conflict, not a silent merge.
- The fingerprint covers the cart as the client sent it, so the same key with the items reordered is a 409 rather than a replay. A place-key identifies one intent; a body that differs at all is a conflict. Normalizing the cart first would make those two agree, but conflicting is the safer default — it can never merge two different intents into one order.
- The intake place-key unique constraint is named explicitly (`uq_intake_keys_place_key`) instead of inheriting Postgres's generated name, and the concurrent-replay path recovers from *that* constraint alone — any other unique violation propagates. A duplicate work-item key can never be mistaken for a place-key replay, and a future naming convention cannot silently break recovery.
- Order + event + confirm work item + intake key share one commit so an accepted order cannot exist without its work item (Design timeline B). Confirm work is stored with idempotency key `(order_id, confirm)` at insert time; the API does not execute confirm or open attempts.
- `make check` runs ruff, mypy, dashboard `tsc`, then pytest.
- `RSIMSettings` ships complete (pans, 3× busy, rail fuse, cook times, flaky pcts) at first appearance so Settings is not grown twice. Pans / 3× / fuse are dormant this slice — quiet cook only. Flaky pcts default to Config's **3/2**. Happy-path tests POST `mix` off so they do not require random faults to pass.
- `CSIMSettings` ships complete (fleet 8, 3× busy, trip bands near/mid/far, flaky pcts) at first appearance. Fleet / 3× are dormant until dinner rush — trip-band quote only. `CSIM_FLAKY_5XX_PCT` / `CSIM_FLAKY_DROP_PCT` default to **3/2**. Boot asserts flakiness sum < 50, `busy_multiple >= 2`, and min trip > sim timeout so dispatch hangs up without waiting for travel.
- Sim 5xx can fire before *or* after the ledger write (`5xx_before` / `5xx_after`); `drop` applies the effect then hangs up without a body. Replay of an existing key returns the cached success and skips the sticky fault, so a retry with the same key is safe (easy wrong turns 3 and 4).
- Worker `restart: "no"` on purpose: `restart: always` would reap the abandoned NULL attempt that scenario 3 needs to show. One replica here; a second waits so H is measured at demo topology.
- `WorkerSettings` ships complete at first appearance, including `WORKER_DEP_CAP_CSIM` / `WORKER_VOID_RETRIES` / poll knobs, so Settings is not grown twice. Both rsim and csim semaphores are live (8 / 8 within task capacity 24). Boot asserts `lease_s > sim_timeout_s` and `task_capacity > dep_cap_rsim + dep_cap_csim`. Courier base URL is compose wiring (`WORKER_COURIER_BASE_URL`), not a new Settings knob.
- First cook poll is not t=0: a 25s quiet burrito would burn the 30-poll window before any rail stretch exists. Waiting until `estimated_ready_at` keeps the budget for the oven, then 3s polls cover the 90s bound.
- Two keys on poll cook (and poll ride) on purpose: `work_items.idempotency_key` is UNIQUE, so the queue needs `(order_id, poll_cook)` / `(order_id, poll_ride)`. That must not be the sim HTTP key — minting a new key per poll would duplicate effects. The accept/dispatch key and ticket id travel in payload JSONB instead.
- Cancel is pre-pivot only (`placed`/`confirmed`). After `being_prepared`, a diner cancel is invalid evidence — not a state change and not a `void_ticket`. If the worker still finalizes a confirm after cancel won, that 0-row UPDATE is supersession (`superseded_by_cancel`), not `invalid_transition`. The race + void stay later; dropping bonus A must not remove this endpoint.
