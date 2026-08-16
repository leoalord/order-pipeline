from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from order_pipeline.api.schemas import OrderResponse, PlaceOrderRequest
from order_pipeline.api.settings import APISettings
from order_pipeline.cancel import CancelOutcome, OrderNotFound, cancel_order
from order_pipeline.intake import (
    FingerprintConflict,
    body_fingerprint,
    is_place_key_unique_violation,
    place_order,
    replay_existing,
)
from order_pipeline.models import Order

settings = APISettings()
engine: Engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

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


@app.post("/orders", status_code=201)
def post_orders(body: PlaceOrderRequest, idempotency_key: PlaceKeyHeader) -> OrderResponse:
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
