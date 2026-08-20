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

_PARKED_ROW = {
    "id": "aaaaaaaa-1111-1111-1111-111111111111",
    "order_id": "bbbbbbbb-1111-1111-1111-111111111111",
    "work_type": "dispatch",
    "owner": "ops",
    "reason": "budget exhausted",
    "next_action": "Redrive after recovery",
}

# The card must settle on real snapshot data before its tone class is read —
# the "Connecting…" placeholder renders as unknown no matter what the fixture says.
_SETTLED = (
    "() => { const e = document.querySelector('.correctness-proof strong');"
    " return e && !e.textContent.includes('Connecting'); }"
)


def _snapshot(
    *,
    duplicate_effects: int | None,
    mismatches: int = 0,
    startup_scan: int = 0,
    orphaned_tickets: int = 0,
    parked_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "startup_scan": startup_scan,
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
        "parked_list": parked_list if parked_list is not None else [],
        "sim_http": {"restaurant": _EMPTY_LANE, "courier": _EMPTY_LANE},
        "outbound_slots": {
            "worker_replicas": 2,
            "restaurant": {"used": 0, "cap": 16, "per_worker_cap": 8},
            "courier": {"used": 0, "cap": 16, "per_worker_cap": 8},
            "task": {"used": 0, "cap": 48, "per_worker_cap": 24},
        },
        "no_progress_beyond_threshold": {"threshold_s": 90.0, "count": 0},
        "orphaned_tickets": orphaned_tickets,
    }


def _load(page: Page) -> None:
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
    page.locator(".correctness-proof").wait_for()
    page.wait_for_function(_SETTLED)


def _reload(page: Page) -> None:
    page.reload(wait_until="domcontentloaded")
    page.locator(".correctness-proof").wait_for()
    page.wait_for_function(_SETTLED)


def _open_correctness(page: Page) -> None:
    page.locator(".correctness-proof").click()
    page.get_by_role("dialog", name="Correctness proof").wait_for()


def _tone(page: Page, label: str) -> str | None:
    return page.locator(f'[data-metric="{label}"]').get_attribute("data-tone")


def _card_tone(page: Page) -> str:
    for tone in ("healthy", "fault", "unknown"):
        if page.locator(f".correctness-proof.{tone}").count() == 1:
            return tone
    raise AssertionError("correctness card carries no single tone class")


def test_correctness_pane_labels_and_three_state_tones() -> None:
    current = {"body": _snapshot(duplicate_effects=0)}

    try:
        playwright = sync_playwright().start()
    except Exception as exc:  # pragma: no cover - optional smoke
        pytest.skip(f"Playwright is not installed; correctness-pane smoke is optional: {exc}")

    try:
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - optional smoke
        playwright.stop()
        pytest.skip(f"Playwright browsers missing — run `make playwright-install`: {exc}")

    try:
        page = browser.new_page()

        def fulfill_snapshot(route: Route) -> None:
            if "/src/" in route.request.url:
                route.continue_()
                return
            route.fulfill(status=200, json=current["body"])

        page.route("**/snapshot*", fulfill_snapshot)
        _load(page)
        _open_correctness(page)

        drawer = page.get_by_role("dialog", name="Correctness proof")
        assert drawer.get_by_text("State vs last applied event", exact=True).is_visible()
        assert drawer.get_by_text("Accepted orders with no work item", exact=True).is_visible()
        assert drawer.get_by_text("Simulator-ledger duplicate effects", exact=True).is_visible()
        assert drawer.get_by_text("Parked work", exact=True).is_visible()
        assert drawer.get_by_text("No progress beyond threshold", exact=True).is_visible()
        assert drawer.get_by_text("Conservation residual", exact=True).is_visible()
        assert drawer.get_by_text("Invalid transitions", exact=True).is_visible()
        assert drawer.get_by_text("Orphaned tickets", exact=True).is_visible()
        assert drawer.get_by_text("Cannot detect a lost insert").first.is_visible()

        for label in (
            "State vs last applied event",
            "Accepted orders with no work item",
            "Simulator-ledger duplicate effects",
            "Parked work",
            "No progress beyond threshold",
            "Conservation residual",
            "Invalid transitions",
            "Orphaned tickets",
        ):
            assert _tone(page, label) == "healthy", label
        assert _card_tone(page) == "healthy"

        # Ledger unavailable is unknown in both the card and the drawer.
        current["body"] = _snapshot(duplicate_effects=None)
        _reload(page)
        _open_correctness(page)
        assert _tone(page, "Simulator-ledger duplicate effects") == "unknown"
        assert _tone(page, "State vs last applied event") == "healthy"
        assert _card_tone(page) == "unknown"

        # Each independent metric must redden the card on its own. Combining two
        # of them would let a card that ignores one of them still pass.
        for label, body in (
            (
                "State vs last applied event",
                _snapshot(duplicate_effects=0, mismatches=1),
            ),
            (
                "Accepted orders with no work item",
                _snapshot(duplicate_effects=0, startup_scan=1),
            ),
            (
                "Simulator-ledger duplicate effects",
                _snapshot(duplicate_effects=2),
            ),
            (
                "Orphaned tickets",
                _snapshot(duplicate_effects=0, orphaned_tickets=3),
            ),
        ):
            current["body"] = body
            _reload(page)
            assert _card_tone(page) == "fault", label
            _open_correctness(page)
            assert _tone(page, label) == "fault", label

        # A known fault outranks an unavailable ledger.
        current["body"] = _snapshot(duplicate_effects=None, mismatches=1)
        _reload(page)
        assert _card_tone(page) == "fault"

        # Parked work is expected shedding: never a fault, and never a green
        # claim that there is nothing to look at.
        current["body"] = _snapshot(duplicate_effects=0, parked_list=[_PARKED_ROW])
        _reload(page)
        assert _card_tone(page) == "healthy"
        _open_correctness(page)
        assert _tone(page, "Parked work") == "neutral"
        assert _tone(page, "No progress beyond threshold") == "healthy"
    finally:
        browser.close()
        playwright.stop()
