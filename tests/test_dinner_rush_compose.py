"""Compose: scenario 0 (steady walk) + scenario 1 (rush) Pass lines.

Mix stays ON. Pane bindings are the existing GET /snapshot keys (no second
shape). Parked list may be non-empty during rush — that is shedding, not a fail.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from order_pipeline.api.snapshot import STAGE_NAMES
from order_pipeline.loadgen.driver import (
    backlog_total,
    oldest_age_s,
    step_is_flat,
    waiting_backlog,
)
from tests.sim_admin import CSIM_URL, RSIM_URL, post_sim_faults

API_URL = "http://localhost:8000"
DASHBOARD_URL = "http://127.0.0.1:5173"
LOADGEN_URL = "http://localhost:8090"
WALK_TIMEOUT_S = 180.0
POLL_EVERY_S = 1.0
MIN_FALLBACK_PEAK_RPS = 3.0
REPO_ROOT = Path(__file__).resolve().parents[1]
STORED_TO_ASSIGNMENT = {
    "placed": "placed",
    "confirmed": "confirmed",
    "being_prepared": "being prepared",
    "ready": "ready",
    "out_for_delivery": "out for delivery",
    "delivered": "delivered",
}
PANE_KEYS = (
    "accept_reject",
    "backlog",
    "retry_rate",
    "oldest_open",
    "oldest_unparked",
    "http_429s",
    "stretching_etas",
    "parked_list",
    "sim_http",
    "no_progress_beyond_threshold",
)


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    try:
        return httpx.request(
            method, url, json=json, headers=headers, params=params, timeout=timeout
        )
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def _mix_on() -> None:
    for url in (RSIM_URL, CSIM_URL):
        body = post_sim_faults(url, {"mode": "clear", "mix": "on"})
        assert body["mix"] == "on", body
        assert body["flaky_5xx_pct"] == 3.0, body
        assert body["flaky_drop_pct"] == 2.0, body
        assert body["mode"] == "off", body
        assert body["blackout_remaining_s"] == 0, body


def _dashboard_snapshot(
    *,
    cohort_id: str,
    order_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, str] = {"cohort_id": cohort_id}
    if order_id is not None:
        params["order_id"] = order_id
    response = _http("GET", f"{DASHBOARD_URL}/snapshot", params=params, timeout=10.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _assert_conservation(body: dict[str, Any]) -> None:
    conservation = body["conservation"]
    assert conservation["residual"] == 0, conservation
    assert conservation["accepted"] == (
        conservation["delivered"]
        + conservation["cancelled"]
        + conservation["failed"]
        + conservation["in_flight"]
    )
    assert conservation["parked"] <= conservation["in_flight"]
    assert body["startup_scan"] == 0
    assert body["invalid_transitions"] == 0
    effects = body["duplicate_effects"]
    assert effects == 0, effects


def _applied_stages(trace: dict[str, Any] | None) -> list[str]:
    if not isinstance(trace, dict):
        return []
    sequence: list[str] = []
    for event in trace["order_events"]:
        if not event["applied"]:
            continue
        label = STORED_TO_ASSIGNMENT.get(event["to_state"])
        if label and (not sequence or sequence[-1] != label):
            sequence.append(label)
    return sequence


def test_faults_3_2_before_calibrate_and_pane_binds_429s() -> None:
    """Runbook pre-demo: GET /admin/faults is 3%/2% before calibrate.

    Calibrate reporting a kitchen/courier 429 is #16. This checks the pane
    binds snapshot http_429s (door / kitchen / courier) rather than inventing
    a second shape.
    """
    _mix_on()
    for url in (RSIM_URL, CSIM_URL):
        faults = _http("GET", f"{url}/admin/faults")
        assert faults.status_code == 200, faults.text
        body = faults.json()
        assert body["mix"] == "on"
        assert body["flaky_5xx_pct"] == 3.0
        assert body["flaky_drop_pct"] == 2.0
        assert body["mode"] == "off"
    home = (REPO_ROOT / "dashboard" / "src" / "HomePage.tsx").read_text()
    assert "http_429s" in home
    assert "busy?.door" in home
    assert "busy?.kitchen" in home
    assert "busy?.courier" in home
    assert "busy 429s" in home
    snap = _http("GET", f"{DASHBOARD_URL}/snapshot")
    assert snap.status_code == 200, snap.text
    body = snap.json()
    for name in PANE_KEYS:
        assert name in body, name
    assert set(body["http_429s"]) == {"door", "kitchen", "courier"}
    via_control = _http("GET", f"{DASHBOARD_URL}/src/ControlPage.tsx")
    assert via_control.status_code == 200, via_control.text
    assert "/loadgen" in via_control.text
    assert "/calibrate" in via_control.text


@pytest.mark.slow
def test_scenario_0_steady_walk_and_scenario_1_rush() -> None:
    """Scenario 0 Pass then scenario 1 Pass. Mix stays on. Stop in finally."""
    _mix_on()
    _http("POST", f"{LOADGEN_URL}/stop", timeout=240.0)
    minted = _http("POST", f"{LOADGEN_URL}/cohort/new")
    assert minted.status_code == 200, minted.text
    calibrated = _http(
        "POST",
        f"{LOADGEN_URL}/calibrate",
        json={"step_s": 8, "start_rps": 0.4, "factor": 1.5, "max_rps": 1.2},
        timeout=70.0,
    )
    assert calibrated.status_code == 200, calibrated.text
    h = calibrated.json()["h"]
    assert isinstance(h, (int, float))
    assert h > 0, calibrated.json()
    reset = _http("POST", f"{LOADGEN_URL}/cohort/new")
    assert reset.status_code == 200, reset.text
    cohort_id = reset.json()["cohort_id"]
    assert isinstance(cohort_id, str)

    try:
        steady = _http("POST", f"{LOADGEN_URL}/scenario/steady")
        assert steady.status_code == 200, steady.text
        assert steady.json()["cohort_id"] == cohort_id
        time.sleep(25.0)

        posted = _http(
            "POST",
            f"{API_URL}/orders",
            json={"items": ["chips"], "cohort_id": cohort_id},
            headers={"Idempotency-Key": f"sc0-walk-{uuid.uuid4()}"},
        )
        assert posted.status_code == 201, posted.text
        order_id = posted.json()["id"]
        assert isinstance(order_id, str)

        seen_events: list[str] = []
        backlog_samples: list[int] = []
        deadline = time.monotonic() + WALK_TIMEOUT_S
        last_body: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            body = _dashboard_snapshot(cohort_id=cohort_id, order_id=order_id)
            last_body = body
            _assert_conservation(body)
            backlog_samples.append(backlog_total(body))
            seen_events = _applied_stages(body.get("trace"))
            if seen_events[-1:] == ["delivered"]:
                break
            time.sleep(POLL_EVERY_S)

        assert last_body is not None
        assert tuple(seen_events) == STAGE_NAMES, seen_events
        indexes = [STAGE_NAMES.index(label) for label in seen_events]
        assert indexes == list(range(len(STAGE_NAMES))), indexes
        assert last_body["duplicate_effects"] == 0
        assert last_body["invalid_transitions"] == 0
        assert backlog_samples, "no backlog samples during the walk"
        tail = backlog_samples[len(backlog_samples) // 3 :] or backlog_samples
        assert max(tail) <= min(tail) + max(8, int(0.5 * max(tail[0], 1))), (
            f"steady backlog not flat: samples={backlog_samples}"
        )
        assert step_is_flat(start_backlog=min(tail), end_backlog=max(tail)) or (
            max(tail) - min(tail) <= 10
        ), tail

        home = (REPO_ROOT / "dashboard" / "src" / "HomePage.tsx").read_text()
        assert "oldest_open" in home
        assert "parked_list" in home
        assert "accept_reject" in home

        baseline = _dashboard_snapshot(cohort_id=cohort_id)
        _assert_conservation(baseline)
        base_backlog = backlog_total(baseline)
        base_waiting = waiting_backlog(baseline)
        base_age = oldest_age_s(baseline) or 0.0
        rush = _http("POST", f"{LOADGEN_URL}/scenario/rush")
        assert rush.status_code == 200, rush.text

        peak_backlog = base_backlog
        peak_age = base_age
        peak_waiting = base_waiting
        saw_rise_backlog = False
        saw_rise_waiting = False
        saw_rise_age = False
        accepted_floor = baseline["conservation"]["accepted"]
        rush_deadline = time.monotonic() + 75.0
        last_rush = baseline
        while time.monotonic() < rush_deadline:
            last_rush = _dashboard_snapshot(cohort_id=cohort_id)
            _assert_conservation(last_rush)
            accepted = last_rush["conservation"]["accepted"]
            assert accepted >= accepted_floor, (accepted, accepted_floor)
            accepted_floor = accepted
            depth = backlog_total(last_rush)
            age = oldest_age_s(last_rush) or 0.0
            peak_backlog = max(peak_backlog, depth)
            peak_age = max(peak_age, age)
            peak_waiting = max(peak_waiting, waiting_backlog(last_rush))
            if depth > base_backlog + 2:
                saw_rise_backlog = True
            if waiting_backlog(last_rush) > base_waiting + 2:
                saw_rise_waiting = True
            if age > base_age + 1.0:
                saw_rise_age = True
            time.sleep(POLL_EVERY_S)

        if not saw_rise_backlog or not saw_rise_waiting or not saw_rise_age:
            # The short test calibration deliberately caps its search at 1.2
            # rps. On faster machines that can yield a conservative H whose
            # fixed 2x fallback still sits below the overload line. Preserve
            # the runbook's 2x minimum while raising the offered peak enough
            # to make this automated overload proof deterministic.
            fallback_mult = max(2.0, MIN_FALLBACK_PEAK_RPS / (1.5 * float(h)))
            boosted = _http(
                "POST",
                f"{LOADGEN_URL}/scenario/rush",
                params={"mult": str(fallback_mult)},
            )
            assert boosted.status_code == 200, boosted.text
            assert boosted.json()["peak_rps"] >= MIN_FALLBACK_PEAK_RPS
            boost_deadline = time.monotonic() + 70.0
            while time.monotonic() < boost_deadline:
                last_rush = _dashboard_snapshot(cohort_id=cohort_id)
                _assert_conservation(last_rush)
                accepted = last_rush["conservation"]["accepted"]
                assert accepted >= accepted_floor
                accepted_floor = accepted
                depth = backlog_total(last_rush)
                age = oldest_age_s(last_rush) or 0.0
                peak_backlog = max(peak_backlog, depth)
                peak_age = max(peak_age, age)
                peak_waiting = max(peak_waiting, waiting_backlog(last_rush))
                if depth > base_backlog + 2:
                    saw_rise_backlog = True
                if waiting_backlog(last_rush) > base_waiting + 2:
                    saw_rise_waiting = True
                if age > base_age + 1.0:
                    saw_rise_age = True
                if saw_rise_backlog and saw_rise_waiting and saw_rise_age:
                    break
                time.sleep(POLL_EVERY_S)

        assert saw_rise_backlog, (
            f"backlog never rose; base={base_backlog} peak={peak_backlog} h={h}"
        )
        assert saw_rise_waiting, (
            f"waiting backlog never rose; base={base_waiting} peak={peak_waiting} h={h}"
        )
        assert peak_waiting > base_waiting, (peak_waiting, base_waiting)
        assert saw_rise_age, f"oldest-age never rose; base={base_age} peak={peak_age} h={h}"

        drain = _http(
            "POST",
            f"{LOADGEN_URL}/observe-drain",
            json={
                "baseline_backlog": base_waiting,
                "baseline_age_s": base_age,
                "peak_backlog": peak_waiting,
                "peak_age_s": peak_age,
                "timeout_s": 180.0,
            },
            timeout=190.0,
        )
        assert drain.status_code == 200, drain.text
        drain_body = drain.json()
        assert drain_body["recovered"] is True, drain_body
        assert drain_body["waiting_rose"] is True, drain_body
        assert drain_body["peak_backlog"] > drain_body["baseline_backlog"], drain_body
        assert drain_body["backlog_recovered"] is True, drain_body
        age_fell = (
            drain_body["oldest_age_s"] is not None
            and drain_body["peak_age_s"] is not None
            and drain_body["oldest_age_s"] < drain_body["peak_age_s"]
        )
        unparked_fell = (
            drain_body.get("oldest_unparked_age_s") is not None
            and drain_body.get("peak_unparked_age_s") is not None
            and drain_body["oldest_unparked_age_s"] < drain_body["peak_unparked_age_s"]
        )
        assert drain_body["age_recovered"] or drain_body["unparked_age_recovered"], drain_body
        assert age_fell or unparked_fell, drain_body
        assert "parked_count" in drain_body
        # Parking is shedding, reported separately — not a substitute for drain.
        last_rush = _dashboard_snapshot(cohort_id=cohort_id)
        _assert_conservation(last_rush)
        accepted = last_rush["conservation"]["accepted"]
        assert accepted >= accepted_floor
        assert last_rush["duplicate_effects"] == 0
        assert last_rush["invalid_transitions"] == 0
        assert last_rush["conservation"]["residual"] == 0
        parked = last_rush["parked_list"]
        assert isinstance(parked, list)
        assert isinstance(drain_body["parked_count"], int)
        for row in parked:
            assert "owner" in row and "reason" in row and "next_action" in row
    finally:
        _http("POST", f"{LOADGEN_URL}/stop", timeout=240.0)
