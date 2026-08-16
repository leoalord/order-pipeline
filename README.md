# Order Pipeline

Single-machine order pipeline. One Python package; Compose runs Postgres, the API, the restaurant sim, and one worker.

## Run

```bash
uv sync

docker compose down -v
docker compose up --wait
curl -sf http://localhost:8000/health
curl -sf http://localhost:8081/health
curl -sf http://localhost:8083/health
```

Place an order (tiny menu: `chips`, `taco`, `burrito`; at most 3 items). The place-key is the Stripe-style `Idempotency-Key` header. `accepted_at` is set on 201; the API never calls the kitchen. The worker confirms and polls cook until `ready`.

```bash
curl -sS -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: diner-1' \
  -d '{"items":["burrito"]}'
```

Replay the same key and body to get the same order id (201). The same key with a different cart is `409`. Omit `cohort_id` to use the default cohort; send one when loadgen exists.

A quiet 1-item burrito walks `placed` → `confirmed` → `being_prepared` → `ready` in a couple of minutes (cook is 25s; the worker's first kitchen poll waits until `estimated_ready_at`, then `being_prepared` is its own GET-visible commit before `ready`).

```bash
ORDER_ID=$(curl -sS -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: diner-burrito' \
  -d '{"items":["burrito"]}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

while true; do
  curl -sS "http://localhost:8000/orders/${ORDER_ID}"
  echo
  sleep 1
done
```

`GET` returns `id`, `state` (underscore names as stored: `placed`, `confirmed`, `being_prepared`, `ready`), `accepted_at`, `items`, and `cohort_id`. Missing ids are `404`.

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

### Worker

One replica on **8083**, `restart: "no"` (quoted — `restart: always` would close scenario 3's abandoned-attempt gap). `SKIP_MIGRATIONS=1`; the API already migrated. Compose wires `WORKER_DATABASE_URL` and `WORKER_RESTAURANT_BASE_URL=http://restaurant:8081`. Confirm and poll_cook handlers are registered on the plugin chassis. Confirm retries the stored `(order_id, confirm)` key until `accepted_at` + 120s, then the order fails. Poll cook reuses the accept key / ticket (the work-item key `(order_id, poll_cook)` is queue identity only); first poll is at `estimated_ready_at`, then every 3s within a budget of 30 — exhaust parks the work item (owner + reason + next action), not the order.

```bash
curl -sf http://localhost:8083/health
curl -sf http://localhost:8083/ready
```

```bash
make check   # uv run ruff + mypy + pytest
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. `make check` is the one command for lint, type-check, and tests. The health and place-order integration tests fail if the API or Postgres is down; restaurant tests fail if the sim on 8081 is down; worker tests fail if the worker on 8083 is down. After image changes, rebuild so compose serves the new image: `docker compose up --build --wait`.

Compose publishes Postgres on `127.0.0.1:55432` — loopback only, and off the default port so it cannot collide with a Postgres already running on the host. The direct-session tests connect there; override with `TEST_DATABASE_URL` if you need to. Because `001_full_schema` is edited in place rather than superseded, a database that already applied it will **not** pick up schema changes from `alembic upgrade head` — reset the volume with `docker compose down -v` after pulling schema work.

## Load

Loadgen and dinner-rush profiles land in a later slice.

## Faults

Restaurant sim admin on `localhost:8081` (sticky mode, all off by default):

```bash
curl -sS http://localhost:8081/admin/faults
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear"}'
```

`mode` is `clear` (off), `5xx_before` (fail before the ledger write), `5xx_after` (write then 5xx), or `drop` (apply the effect, then close without a body — timeline D). Random mix (`RSIM_FLAKY_5XX_PCT` / `RSIM_FLAKY_DROP_PCT`) defaults to **0** in this slice so the happy walk does not flake — keep it off for these proofs. Blackout lands later. Crash/outage beats land in later slices.

Deterministic proofs (compose; always `clear` when finished so the walk and cancel tests stay unpoisoned):

**5xx_after / drop (timeline D / wrong turn 4).** Sticky 100% after-effect fault, one chips order, wait until `confirmed`. `GET /admin/ledger` counts that order's confirm key as **1** (duplicate effects = 0) and Postgres has exactly one applied `confirmed` event. Retry of the same stored `(order_id, confirm)` key replays the cached 200. Then `{"mode":"clear"}`.

```bash
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"5xx_after"}'   # or {"mode":"drop"}

# place chips, poll GET /orders/{id} until confirmed, then:
curl -sS http://localhost:8081/admin/ledger
curl -sS -X POST http://localhost:8081/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"mode":"clear"}'
```

**Reclaim (timeline C).** Mid-call lease loss after the kitchen effect is applied: one ledger row, the abandoned attempt stays `outcome IS NULL` (not rewritten to timeout), the survivor retries the same stored key, and `order_events` has no second confirm and no fake lease-lifecycle rows.

## Architecture

- One Python package (`order_pipeline`) with room for worker and sim entrypoints; one Dockerfile. Restaurant is `restaurant-sim` / `python -m order_pipeline.restaurant` via compose `command:` (the image CMD stays the API). Worker is `worker` / `python -m order_pipeline.worker` the same way.
- Runtime and dev deps live in `pyproject.toml`; `uv.lock` pins versions. No `requirements.txt`.
- Compose carries wiring only (DSNs, hosts, ports, ledger path, restaurant base URL). Knob defaults live in `APISettings` / `RSIMSettings` / `WorkerSettings`.
- One Alembic revision (`001_full_schema`) creates the full business schema: `orders` (including `items` JSONB), `order_events` (including an `applied` flag), `work_items`, `attempts`, and `intake_keys`. Later slices must not add a second revision.
- `order_events` is append-only evidence. `applied` separates transitions that actually moved the order from evidence rows for rejected or illegal attempts, so "current state equals the last applied event" stays checkable once workers start recording invalid transitions.
- The API container runs `alembic upgrade head` before uvicorn, so a clean Postgres volume is migrated before `/health` is ready. The restaurant sim and worker set `SKIP_MIGRATIONS=1` — the sim has no Postgres DSN; the worker has one but must not race the API's migration.
- `POST /orders` validates the cart, requires `Idempotency-Key`, and commits the order (`placed`), `placed` event, confirm work item, and intake key in **one transaction**. A SHA-256 fingerprint of the canonical body is stored with the place-key (TTL `API_PLACE_KEY_TTL_H`, default 48h). Same key + same body replays the same order id; same key + different body → 409. The API never calls the kitchen.
- `POST /orders/{id}/cancel` is a guarded `placed`/`confirmed` → `cancelled` (version bump, applied event, actor `api`, cause `cancel`). Pending/leased work items are marked `cancelled` in the same txn so a quiet pre-pivot cancel is not confirmed into a ticket. After `being_prepared` the UPDATE is not applied: HTTP 409, state unchanged, evidence `order_events` row with `applied=false` and cause `invalid_transition`. Missing ids are `404`. Already-cancelled replays 200 without resurrecting. The confirm-vs-cancel race and `void_ticket` wait for bonus A.
- Shared sim core (`order_pipeline.sim`): accept, poll, Stripe key cache (ledger-backed), SQLite effect ledger, `/admin/faults` + `/admin/ledger`. The restaurant sim is the first implementation; courier will extract and reuse this package, not copy it. Each sim's ledger is independently authoritative for applied effects and lives on a compose volume.
- Worker chassis is a thin work-type **plugin loop**, not a kitchen-only script. Handlers register by work type. Each cycle is three phases with **no DB transaction across HTTP**: short txn `SKIP LOCKED` claim + `INSERT` attempt (`outcome` NULL) → handler HTTP (lease covers that one call) → new short txn classifies the outcome, guarded state `UPDATE`, `order_events`, and complete/park/schedule the work item. Kill mid-call leaves the NULL attempt; a survivor opens a **new** attempt with the same stored key. `order_events` does not grow lease-lifecycle rows.
- Confirm HTTP uses the stored work-item key `(order_id, confirm)` on `POST /accept` (and GET-by-key). Poll cook's work-item key `(order_id, poll_cook)` is queue identity only — restaurant polls reuse the accept key / ticket id, never a per-poll HTTP key. Ticket id, `estimated_ready_at`, and the accept key live on the poll item's JSONB payload.
- First cook poll is scheduled at `estimated_ready_at`, then every `WORKER_POLL_INTERVAL_S` (3s) within `WORKER_POLL_BUDGET` 30. Confirm is time-bounded (`accepted_at` + 120s → order **fails**, no park). Poll budget exhaust **parks** the work item.
- `confirmed` → `being_prepared` is cooking started (its own guarded commit) so GET can observe it; `being_prepared` → `ready` is a later commit. Collapsing both arrows into one finalize would skip a stage on the walk.
- Outbound restaurant calls go through an `asyncio.Semaphore` sized to `WORKER_DEP_CAP_RSIM` (8). The courier semaphore is a no-op until that slice. Backoff is unleased (0.5s ×2, cap 8s, full jitter).

## Trade-offs

- Work type and attempt outcome are `TEXT` plus `CHECK`, not native Postgres `ENUM`s, so bonus `void_ticket` does not need `ALTER TYPE` or a second revision.
- Columns and indexes later slices need (park/lease fields, `order_events.applied`, `attempts.ended_at`, payload/result JSONB, `cohort_id`, `accepted_at`, and the `(status, lease_until)` index lease reclaim will scan) land in this revision even while unused; missing ones would be migration churn.
- Intake is Stripe-style: a client place-key plus a body fingerprint, not a payload-hash-as-identity. Replay is the same order; a different cart under the same key is a conflict, not a silent merge.
- The fingerprint covers the cart as the client sent it, so the same key with the items reordered is a 409 rather than a replay. A place-key identifies one intent; a body that differs at all is a conflict. Normalizing the cart first would make those two agree, but conflicting is the safer default — it can never merge two different intents into one order.
- The intake place-key unique constraint is named explicitly (`uq_intake_keys_place_key`) instead of inheriting Postgres's generated name, and the concurrent-replay path recovers from *that* constraint alone — any other unique violation propagates. A duplicate work-item key can never be mistaken for a place-key replay, and a future naming convention cannot silently break recovery.
- Order + event + confirm work item + intake key share one commit so an accepted order cannot exist without its work item (Design timeline B). Confirm work is stored with idempotency key `(order_id, confirm)` at insert time; the API does not execute confirm or open attempts.
- Python type-check only in this slice (`tsc` waits for the dashboard).
- `RSIMSettings` ships complete (pans, 3× busy, rail fuse, cook times, flaky pcts) at first appearance so Settings is not grown twice. Pans / 3× / fuse are dormant this slice — quiet cook only. Flaky pcts default to 0 (Config's 3/2 wait) so later confirm tests don't require random faults to pass.
- Sim 5xx can fire before *or* after the ledger write (`5xx_before` / `5xx_after`); `drop` applies the effect then hangs up without a body. Replay of an existing key returns the cached success and skips the sticky fault, so a retry with the same key is safe (easy wrong turns 3 and 4).
- Worker `restart: "no"` on purpose: `restart: always` would reap the abandoned NULL attempt that scenario 3 needs to show. One replica here; a second waits so H is measured at demo topology.
- `WorkerSettings` ships complete at first appearance, including unused `WORKER_DEP_CAP_CSIM` / `WORKER_VOID_RETRIES` / poll knobs, so Settings is not grown twice. The rsim semaphore is live; the csim semaphore is a no-op until dispatch. Boot asserts `lease_s > sim_timeout_s` and `task_capacity > dep_cap_rsim + dep_cap_csim`.
- First cook poll is not t=0: a 25s quiet burrito would burn the 30-poll window before any rail stretch exists. Waiting until `estimated_ready_at` keeps the budget for the oven, then 3s polls cover the 90s bound.
- Two keys on poll cook on purpose: `work_items.idempotency_key` is UNIQUE, so the queue needs `(order_id, poll_cook)`. That must not be the restaurant HTTP key — minting a new kitchen key per poll would duplicate effects. The accept key and ticket id travel in payload JSONB instead.
- Cancel is pre-pivot only (`placed`/`confirmed`). After `being_prepared`, a diner cancel is invalid evidence — not a state change and not a `void_ticket`. If the worker still finalizes a confirm after cancel won, that 0-row UPDATE is supersession (`superseded_by_cancel`), not `invalid_transition`. The race + void stay later; dropping bonus A must not remove this endpoint.
