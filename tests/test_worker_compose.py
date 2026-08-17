"""Compose-backed worker health so `compose --wait` is real.

Two replicas share container-internal 8083. The host does not publish 8083.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()


def _compose_stdout(*args: str, timeout: float = 30.0) -> str:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        pytest.fail(f"docker compose {' '.join(args)} failed: {detail}")
    return result.stdout


def _health_status(container_id: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if result.returncode != 0:
        pytest.fail(f"docker inspect {container_id} failed: {result.stderr}")
    return result.stdout.strip()


def test_worker_replicas_healthy_without_host_8083() -> None:
    ids = [line for line in _compose_stdout("ps", "-q", "worker").splitlines() if line.strip()]
    assert len(ids) == 2, ids
    statuses = [_health_status(cid) for cid in ids]
    assert statuses == ["healthy", "healthy"], statuses


def test_compose_worker_is_two_replicas_restart_no() -> None:
    assert 'restart: "no"' in COMPOSE
    assert 'command: ["worker"]' in COMPOSE or "command: ['worker']" in COMPOSE
    assert "SKIP_MIGRATIONS" in COMPOSE
    assert "WORKER_DATABASE_URL" in COMPOSE
    assert "WORKER_RESTAURANT_BASE_URL: http://restaurant:8081" in COMPOSE
    assert "WORKER_COURIER_BASE_URL: http://courier:8082" in COMPOSE
    assert "replicas: 2" in COMPOSE
    assert "8083/health" in COMPOSE
    assert '"8083:8083"' not in COMPOSE
