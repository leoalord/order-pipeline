from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from order_pipeline.api.door import CohortRejects, DoorCap
from order_pipeline.api.schemas import (
    OrderResponse,
    PlaceOrderRequest,
    RedriveResponse,
    SnapshotResponse,
)
from order_pipeline.api.settings import APISettings
from order_pipeline.api.snapshot import build_snapshot, fetch_ledger_counts
from order_pipeline.cancel import CancelOutcome, OrderNotFound, cancel_order
from order_pipeline.intake import (
    DEFAULT_COHORT_ID,
    FingerprintConflict,
    body_fingerprint,
    is_place_key_unique_violation,
    place_order,
    replay_existing,
)
from order_pipeline.models import Order
from order_pipeline.redrive import WorkItemNotFound, WorkItemNotParked, redrive_work_item

settings = APISettings()
# Every request admitted through the door must be able to reach Postgres. The
# derived pool also keeps headroom for health, snapshot, lookup, and cancel.
engine: Engine = create_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=0,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
door = DoorCap(settings.accept_concurrency)
door_rejects = CohortRejects()

app = FastAPI(title="Order Pipeline API")

PlaceKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        description="Stripe-style intake place-key",
    ),
]


def _order_response(order: Order) -> OrderResponse:
    raw_items = order.items
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=500, detail="order items are not a string list")
    items = [item for item in raw_items if isinstance(item, str)]
    if len(items) != len(raw_items):
        raise HTTPException(status_code=500, detail="order items are not a string list")
    return OrderResponse(
        id=order.id,
        state=order.state,
        accepted_at=order.accepted_at,
        items=items,
        cohort_id=order.cohort_id,
    )


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


def _commit_place(body: PlaceOrderRequest, idempotency_key: str) -> OrderResponse:
    """Durable accept. Runs in a thread so the event loop can still 429 the door."""
    fingerprint = body_fingerprint(items=body.items, cohort_id=body.cohort_id)
    try:
        with SessionLocal.begin() as session:
            order = place_order(
                session,
                place_key=idempotency_key,
                items=body.items,
                cohort_id=body.cohort_id,
                ttl_hours=settings.place_key_ttl_h,
            )
            return _order_response(order)
    except FingerprintConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key reused with a different body",
        ) from exc
    except IntegrityError as exc:
        if not is_place_key_unique_violation(exc):
            raise
        with SessionLocal.begin() as session:
            try:
                order = replay_existing(
                    session,
                    place_key=idempotency_key,
                    fingerprint=fingerprint,
                    now=datetime.now(UTC),
                )
                return _order_response(order)
            except FingerprintConflict as conflict:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key reused with a different body",
                ) from conflict
        raise RuntimeError("place-key unique violation without a replay")


@app.post("/orders", status_code=201)
async def post_orders(body: PlaceOrderRequest, idempotency_key: PlaceKeyHeader) -> OrderResponse:
    # Async so a full door can 429 on the event loop without waiting for a
    # threadpool slot. Kitchen/courier 429s are a different brake (order exists).
    if not door.admit():
        door_rejects.add(body.cohort_id if body.cohort_id is not None else DEFAULT_COHORT_ID)
        raise HTTPException(status_code=429, detail="door busy")
    try:
        return await run_in_threadpool(_commit_place, body, idempotency_key)
    finally:
        door.release()


@app.get("/snapshot")
def get_snapshot(
    cohort_id: UUID | None = None,
    order_id: UUID | None = None,
) -> SnapshotResponse:
    """Named metrics GET. Every query filters cohort_id. Optional order_id is the paste-an-ID trace.

    Duplicate effects are read from sim GET /admin/ledger (HTTP with no DB
    session open — the API still never calls sims on place/cancel).
    """
    cohort = cohort_id if cohort_id is not None else DEFAULT_COHORT_ID
    now = datetime.now(UTC)
    restaurant_counts, restaurant_ok = fetch_ledger_counts(settings.restaurant_admin_url)
    courier_counts, courier_ok = fetch_ledger_counts(settings.courier_admin_url)
    with SessionLocal() as session:
        return build_snapshot(
            session,
            cohort_id=cohort,
            now=now,
            ledger_counts=(restaurant_counts, courier_counts),
            order_id=order_id,
            ledgers_ok=restaurant_ok and courier_ok,
            door_429s=door_rejects.rejected(cohort),
            worker_replicas=settings.worker_replicas,
            worker_dep_cap_rsim=settings.worker_dep_cap_rsim,
            worker_dep_cap_csim=settings.worker_dep_cap_csim,
            worker_task_capacity=settings.worker_task_capacity,
        )


@app.get("/orders/{order_id}")
def get_order(order_id: UUID) -> OrderResponse:
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return _order_response(order)


@app.post("/orders/{order_id}/cancel")
def post_cancel(order_id: UUID) -> OrderResponse:
    try:
        with SessionLocal.begin() as session:
            result = cancel_order(session, order_id)
            order = result.order
            outcome = result.outcome
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail="order not found") from exc
    if outcome is CancelOutcome.REJECTED:
        raise HTTPException(
            status_code=409,
            detail="cancel is not legal after being prepared",
        )
    return _order_response(order)


@app.post("/work-items/{work_item_id}/redrive")
def post_redrive(work_item_id: UUID) -> RedriveResponse:
    now = datetime.now(UTC)
    try:
        with SessionLocal.begin() as session:
            item = redrive_work_item(session, work_item_id, now=now)
            return RedriveResponse(
                id=item.id,
                order_id=item.order_id,
                work_type=item.work_type,
                status=item.status,
                attempt_count=item.attempt_count,
                next_attempt_at=item.next_attempt_at,
                idempotency_key=item.idempotency_key,
            )
    except WorkItemNotFound as exc:
        raise HTTPException(status_code=404, detail="work item not found") from exc
    except WorkItemNotParked as exc:
        raise HTTPException(status_code=409, detail="work item is not parked") from exc
