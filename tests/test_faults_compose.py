"""Compose: GET /admin/faults shows live mix; blackout on both 8081 and 8082."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.sim_admin import CSIM_URL, RSIM_URL, mix_off, post_sim_faults

ACCEPT_BODY = {
    RSIM_URL: {"items": ["chips"]},
    CSIM_URL: {"band": "near"},
}


def _get_faults(base_url: str) -> dict[str, object]:
    try:
        response = httpx.get(f"{base_url}/admin/faults", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"GET /admin/faults failed at {base_url}: {exc}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _accept(base_url: str, key: str, *, timeout: float = 5.0) -> httpx.Response:
    return httpx.post(
        f"{base_url}/accept",
        json=ACCEPT_BODY[base_url],
        headers={"Idempotency-Key": key, "Content-Type": "application/json"},
        timeout=timeout,
    )


@pytest.mark.parametrize("base_url", [RSIM_URL, CSIM_URL])
def test_get_faults_shows_live_mix_default_mode_off(base_url: str) -> None:
    restored = post_sim_faults(base_url, {"mode": "clear", "mix": "on"})
    assert restored["mode"] == "off"
    assert restored["mix"] == "on"
    assert restored["flaky_5xx_pct"] == 3.0
    assert restored["flaky_drop_pct"] == 2.0
    assert restored["blackout_remaining_s"] == 0
    fetched = _get_faults(base_url)
    assert fetched["mix"] == "on"
    assert fetched["flaky_5xx_pct"] == 3.0
    assert fetched["flaky_drop_pct"] == 2.0
    assert fetched["mode"] == "off"
    mix_off(base_url)
    off = _get_faults(base_url)
    assert off["mix"] == "off"
    assert off["flaky_5xx_pct"] == 0.0
    assert off["mode"] == "off"


@pytest.mark.parametrize("base_url", [RSIM_URL, CSIM_URL])
def test_blackout_post_get_expires_default_off(base_url: str) -> None:
    mix_off(base_url)
    idle = _get_faults(base_url)
    assert idle["mode"] == "off"
    assert idle["blackout_remaining_s"] == 0

    armed = post_sim_faults(base_url, {"mode": "blackout", "seconds": 1})
    assert armed["mode"] == "blackout"
    remaining = armed["blackout_remaining_s"]
    assert isinstance(remaining, int | float)
    assert remaining > 0
    fetched = _get_faults(base_url)
    assert fetched["mode"] == "blackout"
    assert fetched["blackout_remaining_s"] > 0  # type: ignore[operator]

    key = f"compose-blackout-{uuid.uuid4()}"
    try:
        dropped = _accept(base_url, key, timeout=1.0)
    except httpx.RequestError:
        pass
    else:
        assert dropped.status_code != 200, dropped.text
    ledger = httpx.get(f"{base_url}/admin/ledger", timeout=5.0)
    assert ledger.status_code == 200, ledger.text
    assert key not in ledger.json()["counts"]

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _get_faults(base_url)["mode"] == "off":
            break
        time.sleep(0.05)
    expired = _get_faults(base_url)
    assert expired["mode"] == "off"
    assert expired["blackout_remaining_s"] == 0

    ok = _accept(base_url, key)
    assert ok.status_code == 200, ok.text
    mix_off(base_url)
