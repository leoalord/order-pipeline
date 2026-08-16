"""Shared fixtures.

Most tests drive the API over HTTP. The intake paths that HTTP cannot reach
deterministically — the unique-violation recovery and the expired-TTL mint —
need a real `Session` against the compose Postgres, which is what lives here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Compose publishes Postgres on a non-default loopback port so this cannot
# collide with a Postgres already running on the host.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline",
)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        pytest.fail(f"Postgres is unreachable at {TEST_DATABASE_URL} (compose up --wait?): {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    """Sessions behave like the API's: committed rows stay readable on the instance."""
    return sessionmaker(bind=db_engine, expire_on_commit=False)
