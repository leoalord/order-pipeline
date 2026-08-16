"""Full business schema.

Revision ID: 001_full_schema
Revises:
Create Date: 2026-08-16

The only business-schema revision. Later slices must not add another.
Work type and attempt outcome are TEXT + CHECK, not native PG ENUMs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_full_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "state IN ('placed', 'confirmed', 'being_prepared', 'ready', "
            "'out_for_delivery', 'delivered', 'cancelled', 'failed')",
            name="ck_orders_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_cohort_id", "orders", ["cohort_id"])

    op.create_table(
        "order_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])

    op.create_table(
        "work_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("park_owner", sa.Text(), nullable=True),
        sa.Column("park_reason", sa.Text(), nullable=True),
        sa.Column("park_next_action", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "\"type\" IN ('confirm', 'submit', 'poll_cook', 'dispatch', "
            "'poll_ride', 'void_ticket')",
            name="ck_work_items_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'completed', 'parked', 'failed', 'cancelled')",
            name="ck_work_items_status",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_work_items_order_id", "work_items", ["order_id"])
    op.create_index("ix_work_items_claim", "work_items", ["status", "next_attempt_at"])
    op.create_index("ix_work_items_lease", "work_items", ["status", "lease_until"])

    op.create_table(
        "attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('ok', 'timeout', 'http_429', "
            "'http_4xx', 'http_5xx', 'dropped', 'unknown')",
            name="ck_attempts_outcome",
        ),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attempts_work_item_id", "attempts", ["work_item_id"])

    op.create_table(
        "intake_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("place_key", sa.Text(), nullable=False),
        sa.Column("body_fingerprint", sa.Text(), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("place_key", name="uq_intake_keys_place_key"),
    )
    op.create_index("ix_intake_keys_order_id", "intake_keys", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_intake_keys_order_id", table_name="intake_keys")
    op.drop_table("intake_keys")
    op.drop_index("ix_attempts_work_item_id", table_name="attempts")
    op.drop_table("attempts")
    op.drop_index("ix_work_items_lease", table_name="work_items")
    op.drop_index("ix_work_items_claim", table_name="work_items")
    op.drop_index("ix_work_items_order_id", table_name="work_items")
    op.drop_table("work_items")
    op.drop_index("ix_order_events_order_id", table_name="order_events")
    op.drop_table("order_events")
    op.drop_index("ix_orders_cohort_id", table_name="orders")
    op.drop_table("orders")
