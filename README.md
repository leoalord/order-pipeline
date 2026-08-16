# Order Pipeline

Single-machine order pipeline. One Python package; Compose runs Postgres and the API.

## Run

```bash
uv sync

docker compose down -v
docker compose up --wait
curl -sf http://localhost:8000/health
```

Place an order (tiny menu: `chips`, `taco`, `burrito`; at most 3 items). The place-key is the Stripe-style `Idempotency-Key` header. `accepted_at` is set on 201; confirm work sits unclaimed — the API never calls the kitchen.

```bash
curl -sS -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: diner-1' \
  -d '{"items":["burrito"]}'
```

Replay the same key and body to get the same order id (201). The same key with a different cart is `409`. Omit `cohort_id` to use the default cohort; send one when loadgen exists.

```bash
curl -sS http://localhost:8000/orders/<order-id>
```

`GET` returns `id`, `state` (`placed` at accept), `accepted_at`, `items`, and `cohort_id`. Missing ids are `404`.

```bash
make check   # uv run ruff + mypy + pytest
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. `make check` is the one command for lint, type-check, and tests. The health and place-order integration tests fail if the API or Postgres is down. After API code changes, rebuild so compose serves the new image: `docker compose up --build --wait`.

## Load

Loadgen and dinner-rush profiles land in a later slice.

## Faults

Sim fault admin and crash/outage beats land in later slices.

## Architecture

- One Python package (`order_pipeline`) with room for worker and sim entrypoints; one Dockerfile.
- Runtime and dev deps live in `pyproject.toml`; `uv.lock` pins versions. No `requirements.txt`.
- Compose carries wiring only (DSNs, hosts, ports). Knob defaults live in `APISettings`.
- One Alembic revision (`001_full_schema`) creates the full business schema: `orders` (including `items` JSONB), `order_events`, `work_items`, `attempts`, and `intake_keys`. Later slices must not add a second revision.
- The API container runs `alembic upgrade head` before uvicorn, so a clean Postgres volume is migrated before `/health` is ready.
- `POST /orders` validates the cart, requires `Idempotency-Key`, and commits the order (`placed`), `placed` event, confirm work item, and intake key in **one transaction**. A SHA-256 fingerprint of the canonical body is stored with the place-key (TTL `API_PLACE_KEY_TTL_H`, default 48h). Same key + same body replays the same order id; same key + different body → 409. The API never calls the kitchen.

## Trade-offs

- Work type and attempt outcome are `TEXT` plus `CHECK`, not native Postgres `ENUM`s, so bonus `void_ticket` does not need `ALTER TYPE` or a second revision.
- Columns later slices need (park/lease fields, `attempts.ended_at`, payload/result JSONB, `cohort_id`, `accepted_at`) land in this revision even while unused; missing columns would be migration churn.
- Intake is Stripe-style: a client place-key plus a body fingerprint, not a payload-hash-as-identity. Replay is the same order; a different cart under the same key is a conflict, not a silent merge.
- Order + event + confirm work item + intake key share one commit so an accepted order cannot exist without its work item (Design timeline B). Confirm work is stored with idempotency key `(order_id, confirm)` at insert time; the API does not execute confirm or open attempts.
- Python type-check only in this slice (`tsc` waits for the dashboard).
