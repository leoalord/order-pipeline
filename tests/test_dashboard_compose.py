"""Compose: dashboard on 5173; `/` cards poll snapshot; one order walks every stage.

Cards on `/` bind to GET /snapshot fields (assignment stage keys, terminal rates,
e2e, conservation residual, duplicate attempts/effects, startup scan, invalid
transitions, currently-leased, state-vs-last-event mismatch, paste-an-ID trace).
This test hits the same `/snapshot` the SPA polls (via the Vite proxy) so the
walk is the view `/` displays. Mix stays off. No Playwright required.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from order_pipeline.api.snapshot import STAGE_NAMES
from tests.sim_admin import mix_off

API_URL = "http://localhost:8000"
DASHBOARD_URL = "http://127.0.0.1:5173"
RSIM_URL = "http://localhost:8081"
CSIM_URL = "http://localhost:8082"
WALK_TIMEOUT_S = 180.0
POLL_EVERY_S = 1.0  # SPA cadence (dashboard/src/snapshot.ts POLL_MS)
REPO_ROOT = Path(__file__).resolve().parents[1]
STORED_TO_ASSIGNMENT = {
    "placed": "placed",
    "confirmed": "confirmed",
    "being_prepared": "being prepared",
    "ready": "ready",
    "out_for_delivery": "out for delivery",
    "delivered": "delivered",
}


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> httpx.Response:
    try:
        return httpx.request(
            method, url, json=json, headers=headers, params=params, timeout=timeout
        )
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def _dashboard_snapshot(
    *,
    cohort_id: uuid.UUID,
    order_id: uuid.UUID,
) -> dict[str, Any]:
    response = _http(
        "GET",
        f"{DASHBOARD_URL}/snapshot",
        params={"cohort_id": str(cohort_id), "order_id": str(order_id)},
        timeout=10.0,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_dashboard_serves_spa_and_control_load_group() -> None:
    home = _http("GET", f"{DASHBOARD_URL}/")
    assert home.status_code == 200, home.text
    html = home.text
    assert 'id="root"' in html
    assert "/src/main.tsx" in html or "/assets/" in html
    control = _http("GET", f"{DASHBOARD_URL}/control")
    assert control.status_code == 200, control.text
    assert 'id="root"' in control.text
    home_src = _http("GET", f"{DASHBOARD_URL}/src/HomePage.tsx")
    assert home_src.status_code == 200, home_src.text
    assert "STAGE_LABELS" in home_src.text
    assert "queued" in home_src.text
    assert "cooking" in home_src.text
    assert "waiting for" in home_src.text
    assert "accept_reject" in home_src.text
    assert "http_429s" in home_src.text
    assert "outbound_slots" in home_src.text
    assert "currently_leased_items" in home_src.text
    assert "http_5xx" in home_src.text
    assert "parked_list" in home_src.text
    assert "Redrive" in home_src.text
    assert "oldest_open" in home_src.text
    snap_src = _http("GET", f"{DASHBOARD_URL}/src/snapshot.ts")
    assert snap_src.status_code == 200, snap_src.text
    for label in STAGE_NAMES:
        assert f'"{label}"' in snap_src.text, label
    control_src = _http("GET", f"{DASHBOARD_URL}/src/ControlPage.tsx")
    assert control_src.status_code == 200, control_src.text
    assert "Control" in control_src.text
    assert "/loadgen" in control_src.text
    assert "/calibrate" in control_src.text
    assert "/scenario/steady" in control_src.text
    assert "/scenario/rush" in control_src.text
    assert "/loadgen/beat/doom-confirm" in control_src.text
    assert "/rsim/admin/faults" in control_src.text
    assert "blackout" in control_src.text.lower()
    assert "seconds: 60" in control_src.text
    assert "Crash assist" in control_src.text
    assert "/csim/admin/faults" in control_src.text
    assert "seconds: 30" in control_src.text
    assert "kill" not in control_src.text.lower()
    assert "redrive" not in control_src.text.lower()


def test_dashboard_proxy_snapshot_and_reserved_sim_paths() -> None:
    via_dash = _http("GET", f"{DASHBOARD_URL}/snapshot", timeout=10.0)
    via_api = _http("GET", f"{API_URL}/snapshot", timeout=10.0)
    assert via_dash.status_code == 200, via_dash.text
    assert via_api.status_code == 200, via_api.text
    assert set(via_dash.json()["stages"]) == set(via_api.json()["stages"]) == set(STAGE_NAMES)
    missing = _http("POST", f"{DASHBOARD_URL}/work-items/{uuid.uuid4()}/redrive")
    assert missing.status_code == 404, missing.text
    rsim = _http("GET", f"{DASHBOARD_URL}/rsim/health")
    assert rsim.status_code == 200, rsim.text
    assert rsim.json() == {"status": "ok"}
    csim = _http("GET", f"{DASHBOARD_URL}/csim/health")
    assert csim.status_code == 200, csim.text
    assert csim.json() == {"status": "ok"}
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "LOADGEN_PROXY_TARGET: http://loadgen:8090" in compose
    assert "\n  loadgen:" in compose
    loadgen = _http("GET", f"{DASHBOARD_URL}/loadgen/health")
    assert loadgen.status_code == 200, loadgen.text
    assert loadgen.json() == {"status": "ok"}


def test_dashboard_crash_assist_arms_exact_30s_courier_blackout() -> None:
    armed = _http(
        "POST",
        f"{DASHBOARD_URL}/csim/admin/faults",
        json={"mode": "blackout", "seconds": 30},
    )
    assert armed.status_code == 200, armed.text
    body = armed.json()
    assert body["mode"] == "blackout"
    assert 25 <= body["blackout_remaining_s"] <= 30

    cleared = _http(
        "POST",
        f"{DASHBOARD_URL}/csim/admin/faults",
        json={"mode": "clear", "mix": "off"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mode"] == "off"


def test_dashboard_outage_proxy_arms_exact_60s_and_clears_restaurant() -> None:
    """The `/control` write paths are executable through the same-origin proxies."""
    armed = _http(
        "POST",
        f"{DASHBOARD_URL}/rsim/admin/faults",
        json={"mode": "blackout", "seconds": 60},
    )
    assert armed.status_code == 200, armed.text
    body = armed.json()
    assert body["mode"] == "blackout"
    assert 55 <= body["blackout_remaining_s"] <= 60

    cleared = _http(
        "POST",
        f"{DASHBOARD_URL}/rsim/admin/faults",
        json={"mode": "clear"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mode"] == "off"
    assert cleared.json()["blackout_remaining_s"] == 0


def test_one_order_walks_every_stage_on_slash_cards() -> None:
    """Place chips, poll the snapshot `/` reads, every assignment stage in order."""
    mix_off()
    faults_r = _http("GET", f"{RSIM_URL}/admin/faults")
    faults_c = _http("GET", f"{CSIM_URL}/admin/faults")
    assert faults_r.json()["mix"] == "off"
    assert faults_c.json()["mix"] == "off"

    cohort_id = uuid.uuid4()
    posted = _http(
        "POST",
        f"{API_URL}/orders",
        json={"items": ["chips"], "cohort_id": str(cohort_id)},
        headers={"Idempotency-Key": f"dash-walk-{uuid.uuid4()}"},
    )
    assert posted.status_code == 201, posted.text
    order_id = uuid.UUID(posted.json()["id"])

    seen_current: list[str] = []
    seen_events: list[str] = []
    deadline = time.monotonic() + WALK_TIMEOUT_S
    last_body: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        body = _dashboard_snapshot(cohort_id=cohort_id, order_id=order_id)
        last_body = body
        stages = body["stages"]
        assert set(stages) == set(STAGE_NAMES)
        occupied = [label for label in STAGE_NAMES if stages.get(label, 0) >= 1]
        if len(occupied) == 1 and (not seen_current or seen_current[-1] != occupied[0]):
            seen_current.append(occupied[0])
        trace = body["trace"]
        assert isinstance(trace, dict)
        sequence: list[str] = []
        for event in trace["order_events"]:
            if not event["applied"]:
                continue
            label = STORED_TO_ASSIGNMENT.get(event["to_state"])
            if label and (not sequence or sequence[-1] != label):
                sequence.append(label)
        seen_events = sequence
        if seen_events[-1:] == ["delivered"] and "ready" in seen_current:
            break
        time.sleep(POLL_EVERY_S)

    assert last_body is not None
    assert seen_events[-1:] == ["delivered"], f"order {order_id} events={seen_events}"
    assert "ready" in seen_current, (
        f"ready card never appeared; seen_current={seen_current} events={seen_events}"
    )
    assert tuple(seen_events) == STAGE_NAMES, seen_events
    assert seen_current[-1] == "delivered"
    assert last_body["stages"]["delivered"] >= 1
    assert last_body["conservation"]["residual"] == 0
    assert last_body["startup_scan"] == 0
    assert last_body["invalid_transitions"] == 0
    assert last_body["duplicate_effects"] == 0
    snap = (REPO_ROOT / "dashboard" / "src" / "snapshot.ts").read_text()
    home = (REPO_ROOT / "dashboard" / "src" / "HomePage.tsx").read_text()
    for label in STAGE_NAMES:
        assert f'"{label}"' in snap
    assert "STAGE_LABELS.map" in home
    assert "currently_leased" in home
    assert "currently_leased_items" in home
    assert "state_vs_last_order_events_mismatches" in home
