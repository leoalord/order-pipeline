"""Browser smoke: correctness pane labels and green / unknown / fault tones."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, Route, sync_playwright

DASHBOARD_URL = "http://127.0.0.1:5173"

_EMPTY_LANE = {
    "requests_per_min": 0,
    "latency_p50_s": None,
    "latency_p95_s": None,
    "timeout": 0,
    "http_5xx": 0,
    "http_429": 0,
}


def _snapshot(*, duplicate_effects: int | None, mismatches: int = 0) -> dict[str, Any]:
    return {
        "cohort_id": "11111111-1111-1111-1111-111111111111",
        "stages": {
            "placed": 1,
            "confirmed": 0,
            "being prepared": 0,
            "ready": 0,
            "out for delivery": 0,
            "delivered": 0,
        },
        "terminal_rates_per_min": {"delivered": 0, "cancelled": 0, "failed": 0},
        "e2e_latency_s": {"p50": None, "p95": None},
        "conservation": {
            "accepted": 1,
            "delivered": 0,
            "cancelled": 0,
            "failed": 0,
            "in_flight": 1,
            "parked": 0,
            "residual": 0,
        },
        "duplicate_attempts": 2,
        "duplicate_effects": duplicate_effects,
        "startup_scan": 0,
        "invalid_transitions": 0,
        "state_vs_last_order_events_mismatches": mismatches,
        "currently_leased": 0,
        "currently_leased_items": [],
        "orders": [],
        "trace": None,
        "accept_reject": {"accepted": 1, "rejected": 0},
        "backlog": {"confirm": 1, "poll_cook": 0, "dispatch": 0, "poll_ride": 0},
        "retry_rate": 0.0,
        "oldest_open": {"age_s": 1.0, "stage": "placed"},
        "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
        "stretching_etas": {"count": 0, "max_stretch_s": None},
        "parked_list": [],
        "sim_http": {"restaurant": _EMPTY_LANE, "courier": _EMPTY_LANE},
        "outbound_slots": {
            "worker_replicas": 2,
            "restaurant": {"used": 0, "cap": 16, "per_worker_cap": 8},
            "courier": {"used": 0, "cap": 16, "per_worker_cap": 8},
            "task": {"used": 0, "cap": 48, "per_worker_cap": 24},
        },
        "no_progress_beyond_threshold": {"threshold_s": 90.0, "count": 0},
        "orphaned_tickets": 0,
    }


def _open_correctness(page: Page) -> None:
    page.locator(".correctness-proof").click()
    page.get_by_role("dialog", name="Correctness proof").wait_for()


def _tone(page: Page, label: str) -> str | None:
    return page.locator(f'[data-metric="{label}"]').get_attribute("data-tone")


def test_correctness_pane_labels_and_three_state_tones() -> None:
    current = {"body": _snapshot(duplicate_effects=0)}

    try:
        playwright = sync_playwright().start()
    except Exception as exc:
        pytest.fail(f"Playwright is required for the correctness-pane smoke: {exc}")

    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()

        def fulfill_snapshot(route: Route) -> None:
            if "/src/" in route.request.url:
                route.continue_()
                return
            route.fulfill(status=200, json=current["body"])

        page.route("**/snapshot*", fulfill_snapshot)
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        page.locator(".correctness-proof").wait_for()
        _open_correctness(page)

        drawer = page.get_by_role("dialog", name="Correctness proof")
        assert drawer.get_by_text("State vs last applied event", exact=True).is_visible()
        assert drawer.get_by_text("accepted orders with no work item", exact=True).is_visible()
        assert drawer.get_by_text("Simulator-ledger duplicate effects", exact=True).is_visible()
        assert drawer.get_by_text("Parked / no-progress").first.is_visible()
        assert drawer.get_by_text("Conservation residual", exact=True).is_visible()
        assert drawer.get_by_text("Invalid transitions", exact=True).is_visible()
        assert drawer.get_by_text("Orphaned tickets", exact=True).is_visible()
        assert drawer.get_by_text("Cannot detect a lost insert").first.is_visible()

        assert _tone(page, "State vs last applied event") == "healthy"
        assert _tone(page, "accepted orders with no work item") == "healthy"
        assert _tone(page, "Simulator-ledger duplicate effects") == "healthy"
        assert _tone(page, "Conservation residual") == "healthy"
        assert _tone(page, "Invalid transitions") == "healthy"
        assert _tone(page, "Orphaned tickets") == "healthy"

        current["body"] = _snapshot(duplicate_effects=None)
        page.reload(wait_until="domcontentloaded")
        page.locator(".correctness-proof").wait_for()
        _open_correctness(page)
        assert _tone(page, "Simulator-ledger duplicate effects") == "unknown"
        assert _tone(page, "State vs last applied event") == "healthy"
        assert page.locator(".correctness-proof.unknown").count() == 1
        assert page.locator(".correctness-proof.fault").count() == 0
        assert page.locator(".correctness-proof.healthy").count() == 0

        current["body"] = _snapshot(duplicate_effects=2, mismatches=1)
        page.reload(wait_until="domcontentloaded")
        page.locator(".correctness-proof").wait_for()
        _open_correctness(page)
        assert _tone(page, "Simulator-ledger duplicate effects") == "fault"
        assert _tone(page, "State vs last applied event") == "fault"
        assert page.locator(".correctness-proof.fault").count() == 1
    finally:
        browser.close()
        playwright.stop()
