"""Business tables. One Alembic revision owns this shape; later slices must not add another."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from order_pipeline.db import Base

ORDER_STATES = (
    "placed",
    "confirmed",
    "being_prepared",
    "ready",
    "out_for_delivery",
    "delivered",
    "cancelled",
    "failed",
)

# confirm/submit is one kitchen-accept type; both labels are allowed so a later
# slice cannot force ALTER TYPE / a second revision. void_ticket is here for bonus A.
WORK_TYPES = (
    "confirm",
    "submit",
    "poll_cook",
    "dispatch",
    "poll_ride",
    "void_ticket",
)

WORK_ITEM_STATUSES = (
    "pending",
    "leased",
    "completed",
    "parked",
    "failed",
    "cancelled",
)

ATTEMPT_OUTCOMES = (
    "ok",
    "timeout",
    "http_429",
    "http_4xx",
    "http_5xx",
    "dropped",
    "unknown",
)

INTAKE_PLACE_KEY_UNIQUE = "uq_intake_keys_place_key"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in_list(ORDER_STATES)})", name="ck_orders_state"),
        Index("ix_orders_cohort_id", "cohort_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cohort_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    items: Mapped[Any] = mapped_column(JSONB, nullable=False)


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order_id", "order_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
    )
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(f'"type" IN ({_in_list(WORK_TYPES)})', name="ck_work_items_type"),
        CheckConstraint(
            f"status IN ({_in_list(WORK_ITEM_STATUSES)})",
            name="ck_work_items_status",
        ),
        Index("ix_work_items_order_id", "order_id"),
        Index("ix_work_items_claim", "status", "next_attempt_at"),
        Index("ix_work_items_lease", "status", "lease_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
    )
    work_type: Mapped[str] = mapped_column("type", Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    park_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    park_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    park_next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_in_list(ATTEMPT_OUTCOMES)})",
            name="ck_attempts_outcome",
        ),
        Index("ix_attempts_work_item_id", "work_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_items.id"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)


class IntakeKey(Base):
    __tablename__ = "intake_keys"
    __table_args__ = (
        Index("ix_intake_keys_order_id", "order_id"),
        UniqueConstraint("place_key", name=INTAKE_PLACE_KEY_UNIQUE),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    place_key: Mapped[str] = mapped_column(Text, nullable=False)
    body_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
