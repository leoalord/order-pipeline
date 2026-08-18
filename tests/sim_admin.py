"""Compose helpers: POST mix off so happy-path walks stay deterministic."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

RSIM_URL = "http://localhost:8081"
CSIM_URL = "http://localhost:8082"


def post_sim_faults(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{base_url}/admin/faults",
            json=payload,
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"sim admin failed at {base_url}: {exc}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def mix_off(*base_urls: str) -> None:
    targets = base_urls or (RSIM_URL, CSIM_URL)
    for url in targets:
        body = post_sim_faults(url, {"mode": "clear", "mix": "off"})
        assert body["mode"] == "off", body
        assert body["mix"] == "off", body
        assert body["flaky_5xx_pct"] == 0.0, body
        assert body["flaky_drop_pct"] == 0.0, body
        assert body["blackout_remaining_s"] == 0, body


def mix_on(*base_urls: str) -> None:
    targets = base_urls or (RSIM_URL, CSIM_URL)
    for url in targets:
        body = post_sim_faults(url, {"mode": "clear", "mix": "on"})
        assert body["mix"] == "on", body
        assert body["flaky_5xx_pct"] == 3.0, body
        assert body["flaky_drop_pct"] == 2.0, body
        assert body["blackout_remaining_s"] == 0, body


def restore_demo_mix() -> None:
    """Best-effort: leave the live sims at the pre-demo 3%/2% mix."""
    for url in (RSIM_URL, CSIM_URL):
        try:
            httpx.post(
                f"{url}/admin/faults",
                json={"mode": "clear", "mix": "on"},
                timeout=5.0,
            )
        except httpx.RequestError:
            continue
