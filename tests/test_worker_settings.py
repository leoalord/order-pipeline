"""WorkerSettings code defaults and boot assertions."""

import pytest
from pydantic import ValidationError

from order_pipeline.worker.settings import WorkerSettings

_DSN = "postgresql+psycopg://postgres:postgres@localhost:5432/order_pipeline"
_RSIM = "http://restaurant:8081"


@pytest.fixture(autouse=True)
def _clear_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WORKER_DATABASE_URL",
        "WORKER_RESTAURANT_BASE_URL",
        "WORKER_SIM_TIMEOUT_S",
        "WORKER_LEASE_S",
        "WORKER_CONFIRM_DEADLINE_S",
        "WORKER_TRANSIENT_RETRIES",
        "WORKER_BACKOFF_BASE_S",
        "WORKER_BACKOFF_CAP_S",
        "WORKER_DEP_CAP_RSIM",
        "WORKER_DEP_CAP_CSIM",
        "WORKER_TASK_CAPACITY",
        "WORKER_POLL_INTERVAL_S",
        "WORKER_POLL_BUDGET",
        "WORKER_VOID_RETRIES",
        "WORKER_HEALTH_HOST",
        "WORKER_HEALTH_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_code_defaults() -> None:
    settings = WorkerSettings(database_url=_DSN, restaurant_base_url=_RSIM)
    assert settings.sim_timeout_s == 2.0
    assert settings.lease_s == 15.0
    assert settings.confirm_deadline_s == 120.0
    assert settings.transient_retries == 5
    assert settings.backoff_base_s == 0.5
    assert settings.backoff_cap_s == 8.0
    assert settings.dep_cap_rsim == 8
    assert settings.dep_cap_csim == 8
    assert settings.task_capacity == 24
    assert settings.poll_interval_s == 3.0
    assert settings.poll_budget == 30
    assert settings.void_retries == 3
    assert settings.health_port == 8083


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored")
    monkeypatch.setenv("LEASE_S", "1")
    monkeypatch.setenv("TASK_CAPACITY", "1")
    with pytest.raises(ValidationError):
        WorkerSettings()
    settings = WorkerSettings(database_url=_DSN, restaurant_base_url=_RSIM)
    assert settings.lease_s == 15.0
    assert settings.task_capacity == 24
    assert settings.database_url == _DSN


def test_lease_not_exceeding_timeout_fails_boot() -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            database_url=_DSN,
            restaurant_base_url=_RSIM,
            lease_s=2.0,
            sim_timeout_s=2.0,
        )


def test_capacity_not_exceeding_cap_sum_fails_boot() -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            database_url=_DSN,
            restaurant_base_url=_RSIM,
            task_capacity=16,
            dep_cap_rsim=8,
            dep_cap_csim=8,
        )


@pytest.mark.parametrize(("dep_cap_rsim", "dep_cap_csim"), ((0, 1), (1, 0)))
def test_zero_dependency_cap_fails_boot(dep_cap_rsim: int, dep_cap_csim: int) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            database_url=_DSN,
            restaurant_base_url=_RSIM,
            dep_cap_rsim=dep_cap_rsim,
            dep_cap_csim=dep_cap_csim,
        )


def test_prefixed_env_wrong_defaults_fail_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_DATABASE_URL", _DSN)
    monkeypatch.setenv("WORKER_RESTAURANT_BASE_URL", _RSIM)
    monkeypatch.setenv("WORKER_LEASE_S", "1")
    monkeypatch.setenv("WORKER_SIM_TIMEOUT_S", "2")
    with pytest.raises(ValidationError):
        WorkerSettings()
    monkeypatch.setenv("WORKER_LEASE_S", "15")
    monkeypatch.setenv("WORKER_TASK_CAPACITY", "16")
    with pytest.raises(ValidationError):
        WorkerSettings()
