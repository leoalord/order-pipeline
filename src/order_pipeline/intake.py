"""Durable accept: order + placed event + confirm work item + intake key in one commit."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from order_pipeline.models import IntakeKey, Order, OrderEvent, WorkItem

# Stable until POST /cohort/new exists (dinner_rush / task #16).
DEFAULT_COHORT_ID = UUID("00000000-0000-4000-8000-000000000001")

CONFIRM_WORK_TYPE = "confirm"


class FingerprintConflict(Exception):
    """Same place-key reused with a different cart/body (easy wrong-turn 6)."""


def confirm_idempotency_key(order_id: UUID) -> str:
    """Stored unique key `(order_id, confirm)` — not recomputed later at call time."""
    return f"({order_id}, confirm)"


def body_fingerprint(*, items: Sequence[str], cohort_id: UUID | None) -> str:
    """Canonical SHA-256 of the cart (and cohort when the client sent one)."""
    payload: dict[str, object] = {"items": list(items)}
    if cohort_id is not None:
        payload["cohort_id"] = str(cohort_id)
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _new_accept_rows(
    *,
    place_key: str,
    items: Sequence[str],
    cohort_id: UUID,
    fingerprint: str,
    now: datetime,
    expires_at: datetime,
) -> tuple[Order, OrderEvent, WorkItem, IntakeKey]:
    """The only constructor for a new accept. Timeline B: work item is born with the order."""
    order_id = uuid.uuid4()
    cart = list(items)
    order = Order(
        id=order_id,
        state="placed",
        version=1,
        accepted_at=now,
        cohort_id=cohort_id,
        items=cart,
    )
    event = OrderEvent(
        order_id=order_id,
        from_state=None,
        to_state="placed",
        actor="api",
        cause="place",
        timestamp=now,
    )
    work_item = WorkItem(
        order_id=order_id,
        work_type=CONFIRM_WORK_TYPE,
        status="pending",
        idempotency_key=confirm_idempotency_key(order_id),
        attempt_count=0,
        next_attempt_at=now,
    )
    intake_key = IntakeKey(
        place_key=place_key,
        body_fingerprint=fingerprint,
        order_id=order_id,
        created_at=now,
        expires_at=expires_at,
    )
    return order, event, work_item, intake_key


def replay_existing(
    session: Session,
    *,
    place_key: str,
    fingerprint: str,
    now: datetime,
) -> Order:
    """Look up a place-key after a unique-constraint race (timeline A)."""
    existing = session.scalars(
        select(IntakeKey).where(IntakeKey.place_key == place_key)
    ).one_or_none()
    if existing is None:
        raise RuntimeError(f"place_key {place_key!r} unique violation but row is missing")
    if existing.expires_at > now and existing.body_fingerprint != fingerprint:
        raise FingerprintConflict()
    if existing.expires_at > now:
        order = session.get(Order, existing.order_id)
        if order is None:
            raise RuntimeError(f"intake key {place_key!r} points at a missing order")
        return order
    raise RuntimeError(f"place_key {place_key!r} raced on an expired row")


def place_order(
    session: Session,
    *,
    place_key: str,
    items: Sequence[str],
    cohort_id: UUID | None,
    ttl_hours: int,
    now: datetime | None = None,
) -> Order:
    """Insert or replay an accept. Caller must wrap this in a single transaction."""
    accepted_at = now or datetime.now(UTC)
    fingerprint = body_fingerprint(items=items, cohort_id=cohort_id)
    cohort = cohort_id if cohort_id is not None else DEFAULT_COHORT_ID
    expires_at = accepted_at + timedelta(hours=ttl_hours)

    existing = session.scalars(
        select(IntakeKey).where(IntakeKey.place_key == place_key).with_for_update()
    ).one_or_none()

    if existing is not None and existing.expires_at > accepted_at:
        if existing.body_fingerprint != fingerprint:
            raise FingerprintConflict()
        order = session.get(Order, existing.order_id)
        if order is None:
            raise RuntimeError(f"intake key {place_key!r} points at a missing order")
        return order

    order, event, work_item, intake_key = _new_accept_rows(
        place_key=place_key,
        items=items,
        cohort_id=cohort,
        fingerprint=fingerprint,
        now=accepted_at,
        expires_at=expires_at,
    )
    # Flush the order first so FKs on events / work items / intake keys resolve.
    # Still one transaction — commit happens at the caller.
    session.add(order)
    session.flush()
    session.add_all([event, work_item])
    if existing is not None:
        # TTL elapsed (Stripe-like): mint a new order and reuse the place-key row.
        existing.body_fingerprint = fingerprint
        existing.order_id = order.id
        existing.created_at = accepted_at
        existing.expires_at = expires_at
    else:
        session.add(intake_key)
    session.flush()
    return order
