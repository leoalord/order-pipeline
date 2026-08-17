"""Compose: two workers, loadgen on 8090, snapshot dinner_rush keys, calibrate."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from order_pipeline.api.snapshot import BACKLOG_TYPES

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
LOADGEN_URL = "http://localhost:8090"
DASHBOARD_URL = "http://127.0.0.1:5173"
API_URL = "http://localhost:8000"
RSIM_URL = "http://localhost:8081"
CSIM_URL = "http://localhost:8082"
DINNER_RUSH_KEYS = (
    "accept_reject",
    "backlog",
    "retry_rate",
    "oldest_open",
    "http_429s",
    "stretching_etas",
    "parked_list",
    "sim_http",
    "no_progress_beyond_threshold",
)


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


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    try:
        return httpx.request(method, url, json=json, timeout=timeout)
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def test_compose_two_worker_replicas_no_host_8083() -> None:
    assert 'restart: "no"' in COMPOSE
    assert "replicas: 2" in COMPOSE
    assert 'command: ["worker"]' in COMPOSE
    assert '"8083:8083"' not in COMPOSE
    assert "8083/health" in COMPOSE
    ids = [line for line in _compose_stdout("ps", "-q", "worker").splitlines() if line.strip()]
    assert len(ids) == 2, ids


def test_loadgen_health_ready_and_proxy() -> None:
    health = _http("GET", f"{LOADGEN_URL}/health")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}
    ready = _http("GET", f"{LOADGEN_URL}/ready")
    assert ready.status_code == 200, ready.text
    via_dash = _http("GET", f"{DASHBOARD_URL}/loadgen/health")
    assert via_dash.status_code == 200, via_dash.text
    assert via_dash.json() == {"status": "ok"}
    assert "\n  loadgen:" in COMPOSE
    assert '"8090:8090"' in COMPOSE
    assert 'command: ["loadgen"]' in COMPOSE


def test_snapshot_json_has_dinner_rush_keys() -> None:
    response = _http("GET", f"{API_URL}/snapshot")
    assert response.status_code == 200, response.text
    body = response.json()
    for name in DINNER_RUSH_KEYS:
        assert name in body, name
    assert set(body["backlog"]) == set(BACKLOG_TYPES)
    assert set(body["http_429s"]) == {"door", "kitchen", "courier"}
    oldest = body["oldest_open"]
    assert "age_s" in oldest and "stage" in oldest
    assert set(body["accept_reject"]) == {"accepted", "rejected"}
    assert isinstance(body["parked_list"], list)
    assert set(body["sim_http"]) == {"restaurant", "courier"}
    assert "threshold_s" in body["no_progress_beyond_threshold"]
    assert "count" in body["stretching_etas"]


@pytest.mark.slow
def test_calibrate_reports_h_and_429_mix() -> None:
    """Live compose calibrate. Mix stays ON. Short steps so make check can finish."""
    for url in (RSIM_URL, CSIM_URL):
        armed = _http("POST", f"{url}/admin/faults", json={"mode": "clear", "mix": "on"})
        assert armed.status_code == 200, armed.text
        assert armed.json()["mix"] == "on"
        assert armed.json()["flaky_5xx_pct"] == 3.0
        assert armed.json()["flaky_drop_pct"] == 2.0
    faults_r = _http("GET", f"{RSIM_URL}/admin/faults")
    faults_c = _http("GET", f"{CSIM_URL}/admin/faults")
    assert faults_r.status_code == 200, faults_r.text
    assert faults_c.status_code == 200, faults_c.text
    assert faults_r.json()["flaky_5xx_pct"] == 3.0
    assert faults_r.json()["flaky_drop_pct"] == 2.0
    assert faults_c.json()["flaky_5xx_pct"] == 3.0
    assert faults_c.json()["flaky_drop_pct"] == 2.0
    cohort = _http("POST", f"{LOADGEN_URL}/cohort/new")
    assert cohort.status_code == 200, cohort.text
    started = time.monotonic()
    calibrated = _http(
        "POST",
        f"{LOADGEN_URL}/calibrate",
        json={"step_s": 8, "start_rps": 0.4, "factor": 1.5, "max_rps": 0.6},
        timeout=60.0,
    )
    assert calibrated.status_code == 200, calibrated.text
    body = calibrated.json()
    assert "h" in body
    assert isinstance(body["h"], (int, float))
    assert body["h"] >= 0
    mix = body["http_429s"]
    assert set(mix) == {"door", "kitchen", "courier"}
    for key in ("door", "kitchen", "courier"):
        assert isinstance(mix[key], int)
        assert mix[key] >= 0
    _http("POST", f"{LOADGEN_URL}/stop")
    elapsed = time.monotonic() - started
    assert elapsed < 55, f"calibrate took {elapsed:.1f}s"
