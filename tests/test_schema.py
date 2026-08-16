"""Compose-backed schema inspection for the single business-schema revision."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import Enum as SAEnum

from order_pipeline.db import Base
from order_pipeline.models import WORK_ITEM_STATUSES, WORK_TYPES

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"


def _psql(sql: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "order_pipeline",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            sql,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        pytest.fail(f"schema inspection failed (compose/DB down?): {detail}")
    return result.stdout.strip()


def test_exactly_one_alembic_revision_file() -> None:
    files = sorted(path.name for path in VERSIONS_DIR.glob("*.py") if path.name != "__init__.py")
    assert files == ["001_full_schema.py"]


def test_models_use_text_not_native_enums() -> None:
    assert "parked" in WORK_ITEM_STATUSES
    assert "void_ticket" in WORK_TYPES
    tables = Base.metadata.tables
    assert "orders" in tables
    for table in tables.values():
        for column in table.columns:
            assert not isinstance(column.type, SAEnum), f"{table.name}.{column.name} is an Enum"


def test_orders_items_is_jsonb() -> None:
    row = _psql(
        "SELECT column_name, udt_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'orders' AND column_name = 'items'"
    )
    assert row == "items|jsonb|NO"


def test_order_id_on_work_items_and_order_events() -> None:
    work_items = _psql(
        "SELECT column_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'work_items' "
        "AND column_name = 'order_id'"
    )
    order_events = _psql(
        "SELECT column_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'order_events' "
        "AND column_name = 'order_id'"
    )
    assert work_items == "order_id|NO"
    assert order_events == "order_id|NO"


def test_attempts_ended_at_exists_and_is_nullable() -> None:
    row = _psql(
        "SELECT column_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'attempts' "
        "AND column_name = 'ended_at'"
    )
    assert row == "ended_at|YES"


def test_work_type_and_outcome_are_text_check_not_enums() -> None:
    type_col = _psql(
        "SELECT data_type, udt_name "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'work_items' AND column_name = 'type'"
    )
    outcome_col = _psql(
        "SELECT data_type, udt_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'attempts' AND column_name = 'outcome'"
    )
    assert type_col == "text|text"
    assert outcome_col == "text|text|YES"

    native_enums = _psql(
        "SELECT t.typname FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typtype = 'e'"
    )
    assert native_enums == ""

    type_check = _psql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'work_items'::regclass AND conname = 'ck_work_items_type'"
    )
    outcome_check = _psql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'attempts'::regclass AND conname = 'ck_attempts_outcome'"
    )
    status_check = _psql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'work_items'::regclass AND conname = 'ck_work_items_status'"
    )
    assert "void_ticket" in type_check
    assert "CHECK" in type_check
    assert "CHECK" in outcome_check
    assert "parked" in status_check


def test_idempotency_key_is_unique_stored_column() -> None:
    row = _psql(
        "SELECT column_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'work_items' "
        "AND column_name = 'idempotency_key'"
    )
    assert row == "idempotency_key|NO"
    unique_def = _psql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'work_items'::regclass AND contype = 'u' "
        "AND pg_get_constraintdef(oid) ILIKE '%idempotency_key%'"
    )
    assert "idempotency_key" in unique_def
    assert "UNIQUE" in unique_def


def test_intake_keys_support_place_key_fingerprint_and_ttl() -> None:
    columns = _psql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'intake_keys' "
        "ORDER BY column_name"
    )
    names = set(columns.split("\n"))
    assert {"place_key", "body_fingerprint", "order_id", "created_at", "expires_at"} <= names


def test_single_alembic_version_applied() -> None:
    applied = _psql("SELECT version_num FROM alembic_version")
    assert applied == "001_full_schema"
