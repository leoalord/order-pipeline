"""Compose-backed worker health so `compose --wait` is real."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_URL = "http://localhost:8083"
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()


def _worker(path: str) -> httpx.Response:
    try:
        return httpx.get(f"{WORKER_URL}{path}", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"worker is down at {WORKER_URL}: {exc}")


def test_worker_health_and_ready() -> None:
    health = _worker("/health")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}
    ready = _worker("/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ok"}


def test_compose_worker_is_one_replica_restart_no() -> None:
    assert 'restart: "no"' in COMPOSE
    assert 'command: ["worker"]' in COMPOSE or "command: ['worker']" in COMPOSE
    assert "SKIP_MIGRATIONS" in COMPOSE
    assert "WORKER_DATABASE_URL" in COMPOSE
    assert "WORKER_RESTAURANT_BASE_URL: http://restaurant:8081" in COMPOSE
    assert "replicas:" not in COMPOSE
    assert "8083/health" in COMPOSE
