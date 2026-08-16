"""Compose-backed restaurant sim: health, ledger volume, faults mix off."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RSIM_URL = "http://localhost:8081"


def _rsim(
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        return httpx.request(
            method,
            f"{RSIM_URL}{path}",
            timeout=5.0,
            json=json,
            headers=headers,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"restaurant sim is down at {RSIM_URL}: {exc}")


def _wait_healthy(*, attempts: int = 40) -> None:
    last_error = "no attempts"
    for _ in range(attempts):
        try:
            response = httpx.get(f"{RSIM_URL}/health", timeout=2.0)
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
            last_error = f"status {response.status_code}: {response.text}"
        except httpx.RequestError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    pytest.fail(f"restaurant sim never became healthy: {last_error}")


def test_restaurant_health_and_ready() -> None:
    health = _rsim("GET", "/health")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}
    ready = _rsim("GET", "/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ok"}


def test_replay_same_key_is_one_ledger_row_on_compose() -> None:
    key = f"compose-replay-{uuid.uuid4()}"
    headers = {"Idempotency-Key": key, "Content-Type": "application/json"}
    first = _rsim("POST", "/accept", json={"items": ["burrito"]}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["ticket_id"]
    assert first.json()["estimated_ready_at"]
    second = _rsim("POST", "/accept", json={"items": ["burrito"]}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["ticket_id"] == first.json()["ticket_id"]
    ledger = _rsim("GET", "/admin/ledger")
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["counts"][key] == 1


def test_faults_clear_shows_mix_off() -> None:
    armed = _rsim("POST", "/admin/faults", json={"mode": "5xx_before"})
    assert armed.status_code == 200, armed.text
    assert armed.json()["mode"] == "5xx_before"
    cleared = _rsim("POST", "/admin/faults", json={"mode": "clear"})
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["mode"] == "off"
    assert body["mix"] == "off"
    assert body["flaky_5xx_pct"] == 0.0
    assert body["flaky_drop_pct"] == 0.0
    fetched = _rsim("GET", "/admin/faults")
    assert fetched.json()["mix"] == "off"
    assert fetched.json()["mode"] == "off"


def test_ledger_survives_compose_restart() -> None:
    key = f"compose-restart-{uuid.uuid4()}"
    headers = {"Idempotency-Key": key, "Content-Type": "application/json"}
    accepted = _rsim("POST", "/accept", json={"items": ["taco"]}, headers=headers)
    assert accepted.status_code == 200, accepted.text
    ticket_id = accepted.json()["ticket_id"]
    before = _rsim("GET", "/admin/ledger")
    assert before.json()["counts"][key] == 1

    restart = subprocess.run(
        ["docker", "compose", "restart", "restaurant"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if restart.returncode != 0:
        detail = (restart.stderr or restart.stdout).strip() or "no output"
        pytest.fail(f"docker compose restart restaurant failed: {detail}")
    wait = subprocess.run(
        ["docker", "compose", "up", "--wait", "restaurant"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if wait.returncode != 0:
        detail = (wait.stderr or wait.stdout).strip() or "no output"
        pytest.fail(f"docker compose up --wait restaurant failed: {detail}")
    _wait_healthy()

    after = _rsim("GET", "/admin/ledger")
    assert after.status_code == 200, after.text
    assert after.json()["counts"][key] == 1
    polled = _rsim("GET", f"/tickets/{ticket_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["ticket_id"] == ticket_id
    by_key = _rsim("GET", f"/keys/{key}")
    assert by_key.status_code == 200, by_key.text
    assert by_key.json()["ticket_id"] == ticket_id
