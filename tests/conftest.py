"""Shared fixtures.

Most tests drive the API over HTTP. The intake paths that HTTP cannot reach
deterministically — the unique-violation recovery and the expired-TTL mint —
need a real `Session` against the compose Postgres, which is what lives here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.models import WorkItem

# Compose publishes Postgres on a non-default loopback port so this cannot
# collide with a Postgres already running on the host.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline",
)

# These deterministic live proofs need an idle simulator. Put them before HTTP
# tests that create background orders and can consume shared kitchen/fleet
# capacity. The crash proof clears its faults and finishes both orders.
IDLE_SIM_COMPOSE_TEST_ORDER = (
    "tests/test_snapshot_compose.py::test_snapshot_walk_shows_every_named_stage",
    "tests/test_worker_crash_compose.py::test_scenario_3_kill_resume_then_park_clear_redrive",
)
IDLE_SIM_COMPOSE_TESTS = frozenset(IDLE_SIM_COMPOSE_TEST_ORDER)

# Live-load scenarios can leave future sim occupancy or count-bounded parked
# work after arrivals stop. Run them, in this order, after deterministic compose
# tests so shared kitchen/fleet state cannot weaken later assertions.
STATEFUL_COMPOSE_TEST_ORDER = (
    "tests/test_loadgen_compose.py::test_calibrate_reports_h_and_429_mix",
    "tests/test_dinner_rush_compose.py::test_scenario_0_steady_walk_and_scenario_1_rush",
)
STATEFUL_COMPOSE_TESTS = frozenset(STATEFUL_COMPOSE_TEST_ORDER)

# Live workers poll the same Postgres as session tests. Fixture rows that stay
# pending/leased and due will be claimed and executed for real.
UNCLAIMABLE_AT = datetime.now(UTC) + timedelta(days=36500)


def hold_unclaimable(session: Session, *order_ids: UUID) -> None:
    """Keep leftover fixture work out of the compose claimer."""
    if not order_ids:
        return
    for item in session.scalars(select(WorkItem).where(WorkItem.order_id.in_(order_ids))):
        if item.status in {"pending", "leased"}:
            item.next_attempt_at = UNCLAIMABLE_AT


def hold_claimable_items(session: Session, *item_ids: UUID) -> None:
    if not item_ids:
        return
    for item in session.scalars(select(WorkItem).where(WorkItem.id.in_(item_ids))):
        if item.status in {"pending", "leased"}:
            item.next_attempt_at = UNCLAIMABLE_AT


def _work_item_ids(factory: sessionmaker[Session]) -> set[UUID]:
    with factory() as session:
        return set(session.scalars(select(WorkItem.id)).all())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    idle_sim = [item for item in items if item.nodeid in IDLE_SIM_COMPOSE_TESTS]
    regular = [
        item
        for item in items
        if item.nodeid not in IDLE_SIM_COMPOSE_TESTS and item.nodeid not in STATEFUL_COMPOSE_TESTS
    ]
    stateful = [item for item in items if item.nodeid in STATEFUL_COMPOSE_TESTS]
    idle_rank = {nodeid: index for index, nodeid in enumerate(IDLE_SIM_COMPOSE_TEST_ORDER)}
    rank = {nodeid: index for index, nodeid in enumerate(STATEFUL_COMPOSE_TEST_ORDER)}
    idle_sim.sort(key=lambda item: idle_rank[item.nodeid])
    stateful.sort(key=lambda item: rank[item.nodeid])
    items[:] = [*idle_sim, *regular, *stateful]


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


_EXISTING_CLAIMABLE_QUARANTINED = False


@pytest.fixture
def session_factory(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Sessions behave like the API's: committed rows stay readable on the instance.

    After each test, newly created leftover pending/leased rows are deferred so
    live compose workers cannot execute fixture work. The first use also
    quarantines claimable leftovers already in the shared database.
    """
    global _EXISTING_CLAIMABLE_QUARANTINED
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    if not _EXISTING_CLAIMABLE_QUARANTINED:
        with factory.begin() as session:
            existing = list(
                session.scalars(
                    select(WorkItem.id).where(WorkItem.status.in_(("pending", "leased")))
                )
            )
            hold_claimable_items(session, *existing)
        _EXISTING_CLAIMABLE_QUARANTINED = True
    before = _work_item_ids(factory)
    yield factory
    created = _work_item_ids(factory) - before
    if created:
        with factory.begin() as session:
            hold_claimable_items(session, *created)


@pytest.fixture(scope="session", autouse=True)
def restore_demo_mix_after_suite() -> Iterator[None]:
    """Happy-path tests turn the mix off; put the demo 3%/2% back when pytest exits."""
    yield
    from tests.sim_admin import restore_demo_mix

    restore_demo_mix()
