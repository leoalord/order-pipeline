"""Compose-backed courier sim: health, replay-by-key is one ledger dispatch."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.sim_admin import mix_off

CSIM_URL = "http://localhost:8082"


def _csim(
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        return httpx.request(
            method,
            f"{CSIM_URL}{path}",
            timeout=5.0,
            json=json,
            headers=headers,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"courier sim is down at {CSIM_URL}: {exc}")


def test_courier_health_and_ready() -> None:
    health = _csim("GET", "/health")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}
    ready = _csim("GET", "/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ok"}


def test_replay_same_key_is_one_dispatch_in_ledger() -> None:
    mix_off(CSIM_URL)
    key = f"compose-dispatch-{uuid.uuid4()}"
    headers = {"Idempotency-Key": key, "Content-Type": "application/json"}
    body: dict[str, object] = {"band": "mid"}
    first = _csim("POST", "/accept", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["ticket_id"]
    assert first.json()["estimated_ready_at"]
    assert first.json()["status"] in {"assigned", "en_route", "delivered"}
    second = _csim("POST", "/accept", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["ticket_id"] == first.json()["ticket_id"]
    ledger = _csim("GET", "/admin/ledger")
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["counts"][key] == 1


def test_faults_clear_shows_mix_off() -> None:
    mix_off(CSIM_URL)
    armed = _csim("POST", "/admin/faults", json={"mode": "5xx_before"})
    assert armed.status_code == 200, armed.text
    assert armed.json()["mode"] == "5xx_before"
    cleared = _csim("POST", "/admin/faults", json={"mode": "clear", "mix": "off"})
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["mode"] == "off"
    assert body["mix"] == "off"
    assert body["flaky_5xx_pct"] == 0.0
    assert body["flaky_drop_pct"] == 0.0
    assert body["blackout_remaining_s"] == 0
    fetched = _csim("GET", "/admin/faults")
    assert fetched.json()["mix"] == "off"
    assert fetched.json()["mode"] == "off"
