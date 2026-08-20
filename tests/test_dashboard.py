"""Dashboard SPA source and harness: cards, /control load group, tsc, reserved proxy."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"
ASSIGNMENT_STAGES = (
    "placed",
    "confirmed",
    "being prepared",
    "ready",
    "out for delivery",
    "delivered",
)
VITE_KNOB = "VITE_API_BASE_URL"
FORBIDDEN_VITE = ("VITE_RSIM", "VITE_CSIM", "VITE_LOADGEN")


def _dashboard_src() -> str:
    files = [
        DASHBOARD / "src" / "vite-env.d.ts",
        DASHBOARD / "src" / "snapshot.ts",
        DASHBOARD / "src" / "HomePage.tsx",
        DASHBOARD / "src" / "ControlPage.tsx",
        DASHBOARD / "src" / "Shell.tsx",
        DASHBOARD / "src" / "main.tsx",
        DASHBOARD / "vite.config.ts",
        DASHBOARD / "package.json",
    ]
    return "\n".join(path.read_text() for path in files)


def test_dashboard_source_has_unified_lifecycle_and_evidence() -> None:
    home = (DASHBOARD / "src" / "HomePage.tsx").read_text()
    snapshot = (DASHBOARD / "src" / "snapshot.ts").read_text()
    for label in ASSIGNMENT_STAGES:
        assert f'"{label}"' in snapshot, label
    for text in (
        "Every order, one visible journey",
        "Restaurant accepted",
        "Preparation underway",
        "Ready for pickup",
        "Restaurant",
        "Delivery",
        "Handoff",
        "Cancelled",
        "Failed",
        "Presenter controls",
        "Correctness proof",
    ):
        assert text in home
    assert "STAGE_LABELS.map" in home
    assert "STAGE_SEAMS" in home
    assert "StageTickets" in home
    assert "ticket-stack" in home
    assert "displayCode" in home
    assert "terminal-branches" in home
    assert "branch-spine" not in home
    assert "Independent systems demonstration" not in home
    assert "orders: OrderSummary[]" in home
    assert "orders: OrderSummary[]" in snapshot
    assert "accepted — queued for preparation" in snapshot
    assert "preparation underway" in snapshot
    assert "waiting for courier assignment or pickup" in snapshot
    assert "picked up — courier en route" in snapshot
    assert "{index + 1}</span>" not in home
    assert "<span>orders</span>" in home
    assert "stageCounts?.[stage]" in home
    for field in (
        "currently_leased",
        "currently_leased_items",
        "state_vs_last_order_events_mismatches",
        "duplicate_attempts",
        "duplicate_effects",
        "startup_scan",
        "invalid_transitions",
        "orphaned_tickets",
        "conservation",
        "terminal_rates_per_min",
        "e2e_latency_s",
        "oldest_open",
        "backlog",
        "retry_rate",
        "sim_http",
        "outbound_slots",
        "parked_list",
        "no_progress_beyond_threshold",
    ):
        assert field in home
    assert "orphaned_tickets" in snapshot
    assert "fetchLoadgenStatus" in home
    assert "fetchSimFaults" in home
    assert "fetchMenuStock" in home
    assert "Kitchen inventory" in home
    assert "kitchen-inventory" in home
    assert 'fetch("/rsim/admin/stock"' in snapshot
    assert "simFaultActive(simFaults?.restaurant)" in home
    assert "simFaultActive(simFaults?.courier)" in home
    assert "targeted confirms" in home
    assert "Blackout ·" in home
    assert "activeCohort = status.cohort_id" in home
    assert "showing the last known cohort" in home
    assert "setInterval" not in home
    assert 'fetch("/loadgen/status"' in snapshot
    assert "redriveWorkItem(row.id)" not in home
    assert "redrive(row.id)" in home
    assert "attempt.lease_owner" in home
    assert "attempt.idempotency_key" in home
    assert "attempt.work_item_id" in home
    assert "result.idempotency_key" in home
    assert "redriving === row.id" in home
    assert "Paste-an-ID" in home
    assert "last 60 seconds" in home
    assert "DetailsDrawer" in home
    assert "setDetailPanel(null)" in home
    assert "setRailOpen(false)" in home
    assert "sessionStorage" in home
    assert "<canvas" not in home.lower()
    assert "chart" not in home.lower()


def test_presenter_rail_posts_existing_scenarios_on_unified_surface() -> None:
    control = (DASHBOARD / "src" / "ControlPage.tsx").read_text()
    shell = (DASHBOARD / "src" / "Shell.tsx").read_text()
    assert "PresenterRail" in control
    assert "Presenter controls" in control
    assert "fetch(path, init)" in control
    # The fallback H is a guess, not a measurement: Normal may run on it, Rush
    # may not, and the card must not call it calibrated.
    assert "Fallback baseline" in control
    assert "Ready · H" not in control
    assert "disabled={disabled || !hasBaseline}" in control
    assert "disabled={disabled || !calibrated}" in control
    assert 'loadgen?.h_source === "calibrated"' in control
    assert "Promise.allSettled" in control
    assert "start += 8" in control
    assert "redriveWorkItem(job.id)" in control
    assert "courierFaultActive" in control
    assert "loadgen={loadgen}" in (DASHBOARD / "src" / "HomePage.tsx").read_text()
    for path in (
        "/loadgen/calibrate",
        "/loadgen/cohort/new",
        "/loadgen/scenario/steady",
        "/loadgen/scenario/rush",
        "/loadgen/stop",
        "/loadgen/beat/doom-confirm",
        "/rsim/admin/faults",
        "/csim/admin/faults",
        "/csim/admin/capacity",
        "/rsim/admin/capacity",
        "/loadgen/beat/cancel-race",
        "/loadgen/beat/place",
        "/rsim/admin/stock",
    ):
        assert path in control, path
    for label in (
        "Normal",
        "Rush",
        "Outage",
        "Worker crash",
        "Courier failure",
        "Courier capacity",
        "Fleet capacity",
        "Cooking capacity",
        "Meals at once",
        "parked courier jobs",
        "Redrive ${parkedCourierJobs.length} parked courier jobs",
        "Reset demo",
        "Setup & bonus beats",
        "Calibrate",
        "New cohort",
        "Cancel race",
        "Fail void",
        "Out of stock",
        "Restore stock",
        "Kitchen inventory",
        "Redrive",
    ):
        assert label in control
    assert "kill" not in control.lower()
    assert 'Navigate to="/" replace' in control
    assert 'target="_blank"' not in shell
    assert 'href="/control"' not in shell


def test_board_evidence_cannot_name_the_wrong_subsystem() -> None:
    """The chips and scenario facts must not restate a claim the data cannot support."""
    home = (DASHBOARD / "src" / "HomePage.tsx").read_text()

    # "Fault active" follows the armed fault, not the trailing error counters:
    # the always-on mix keeps those above zero and a blackout drains them.
    assert 'if (faultArmed) return "fault";' in home
    assert 'if (lane.timeout + lane.http_5xx > 0) return "fault";' not in home
    assert "healthForLane(\n    simHttp?.restaurant,\n    restaurantFault," in home
    assert "healthForLane(\n    simHttp?.courier,\n    deliveryFault," in home

    # Work that cannot progress is stalled work, attributed by type.
    assert "Stalled work" in home
    assert "workTypeSummary(parked)" in home

    # A courier blackout lands in timeout/unknown, so a 5xx-only headline is
    # structurally zero for the whole beat.
    assert "sim_http.courier.timeout" in home
    assert "dependency errors" in home
    assert "courier busy 429s" in home
    assert "{fmt(snapshot?.sim_http.courier.http_5xx)}</b><small>courier 5xx" not in home

    # Redrive is refused while the matching dependency fault is still armed.
    assert "redriveBlocker(" in home
    assert "disabled={redriving !== null || blocker !== null}" in home
    assert "disabled={redriving !== null}" not in home


def test_board_follows_one_ticket_and_clears_it_with_the_cohort() -> None:
    """Focus must survive a ticket's journey and not survive its cohort."""
    home = (DASHBOARD / "src" / "HomePage.tsx").read_text()

    # Re-picking the newest arrival every poll would park focus in Placed.
    assert "pinnedOrderId" in home
    assert "const effectiveFocusId = focusedOrderId ?? pinnedOrderId;" in home
    assert "automaticFocus" not in home

    # Reset demo / New cohort mint a cohort in which the old selection matches
    # nothing, which would otherwise leave the board with no focused ticket.
    assert "shownCohortRef" in home
    assert "setPinnedOrderId(null)" in home
    assert "setFocusedOrderId(null)" in home

    # The followed order is requested by id so it stays in the bounded window.
    assert "const followId = focusedOrderId ?? pinnedOrderIdRef.current;" in home
    assert "followId && ORDER_ID_RE.test(followId)" in home


def test_presentation_type_never_drops_below_the_screen_share_floor() -> None:
    """Anything under 11px is unreadable once Zoom compresses the shared screen."""
    css = (DASHBOARD / "src" / "styles.css").read_text()

    too_small = re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", css)
    assert [size for size in too_small if float(size) < 11] == []

    # Fluid sizes must not clamp their way back under the floor either.
    for lower, _upper in re.findall(
        r"font-size:\s*clamp\(\s*(\d+(?:\.\d+)?)px\s*,[^,]+,\s*(\d+(?:\.\d+)?)px",
        css,
    ):
        assert float(lower) >= 11, lower

    # Stage names are the labels the audience reads from across a call.
    assert "font-size: clamp(16px, 1.35vw, 20px)" in css
    # The narrow-viewport override used to push the seam copy down to 7px.
    assert ".stage-title small {\n    font-size: 7px;\n  }" not in css


def test_panels_scroll_as_one_body_and_contain_focus() -> None:
    """The rail must not cram five scenario cards into an inner well."""
    css = (DASHBOARD / "src" / "styles.css").read_text()
    control = (DASHBOARD / "src" / "ControlPage.tsx").read_text()
    home = (DASHBOARD / "src" / "HomePage.tsx").read_text()
    trap = (DASHBOARD / "src" / "focusTrap.ts").read_text()

    # One scroll container between the fixed heading and footer.
    assert '<div className="rail-body">' in control
    assert ".rail-body,\n.drawer-content {" in css
    assert "flex: 1;\n  min-height: 0;\n  overflow-y: auto;" in css
    assert ".scenario-list,\n.drawer-content {" not in css

    # The demo sequence comes before the secondary capacity controls.
    assert control.index('className="scenario-list"') < control.index('className="capacity-card"')

    # Dialog semantics, initial focus, and a Tab trap on both panels.
    for source in (control, home):
        assert 'role="dialog"' in source
        assert 'aria-modal="true"' in source
        assert "tabIndex={-1}" in source
        assert "useFocusTrap(" in source
    assert 'event.key !== "Tab"' in trap
    assert "event.preventDefault()" in trap

    # Escape and focus return must not depend on a frame that a hidden page
    # never paints.
    assert "restoreFocus(presenterButtonRef.current)" in home
    assert "restoreFocus(lastDetailTriggerRef.current)" in home
    assert "window.requestAnimationFrame(" not in home


def test_only_vite_knob_is_api_base_url() -> None:
    env_d_ts = (DASHBOARD / "src" / "vite-env.d.ts").read_text()
    snapshot = (DASHBOARD / "src" / "snapshot.ts").read_text()
    assert VITE_KNOB in env_d_ts
    assert VITE_KNOB in snapshot
    blob = _dashboard_src()
    for name in FORBIDDEN_VITE:
        assert name not in blob, name


def test_vite_proxy_reserves_loadgen_rsim_csim() -> None:
    config = (DASHBOARD / "vite.config.ts").read_text()
    assert '"/loadgen"' in config
    assert '"/rsim"' in config
    assert '"/csim"' in config
    assert '"/snapshot"' in config
    assert '"/work-items"' in config
    assert "VITE_RSIM" not in config
    assert "VITE_CSIM" not in config
    assert "VITE_LOADGEN" not in config


def test_correctness_drawer_uses_three_state_tones_and_honest_labels() -> None:
    """Null duplicate_effects is unknown; residual is a partition, not lost-order proof."""
    home = (DASHBOARD / "src" / "HomePage.tsx").read_text()
    css = (DASHBOARD / "src" / "styles.css").read_text()
    assert "function metricTone" in home
    assert 'if (value === null || value === undefined) return "unknown"' in home
    assert 'tone={snapshot?.duplicate_effects === 0 ? "healthy" : "fault"}' not in home
    assert "State vs last applied event" in home
    assert "accepted orders with no work item" in home
    assert "Simulator-ledger duplicate effects" in home
    assert "Parked / no-progress" in home
    assert "Cannot detect a lost insert" in home
    assert "ledgers unavailable — unknown, not a pass" in home
    assert "tone={metricTone(snapshot?.duplicate_effects)}" in home
    assert "tone={metricTone(snapshot?.invalid_transitions)}" in home
    assert "tone={metricTone(snapshot?.orphaned_tickets)}" in home
    assert "tone={metricTone(proof?.residual)}" in home
    assert ".evidence-metric.unknown" in css
    assert "Funnel partition" in home


def test_makefile_wires_tsc_into_check() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "tsc" in makefile
    assert "check: lint type-check tsc test" in makefile
    assert "npm --prefix dashboard run tsc" in makefile


def test_compose_dashboard_on_5173_with_loadgen_proxy() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "\n  dashboard:" in compose
    assert '"5173:5173"' in compose
    assert "API_PROXY_TARGET: http://api:8000" in compose
    assert "LOADGEN_PROXY_TARGET: http://loadgen:8090" in compose
    assert "RSIM_PROXY_TARGET: http://restaurant:8081" in compose
    assert "CSIM_PROXY_TARGET: http://courier:8082" in compose
    assert "\n  loadgen:" in compose
    dockerfile = (DASHBOARD / "Dockerfile").read_text()
    assert dockerfile.lstrip().startswith("FROM node:")
    python_df = (REPO_ROOT / "Dockerfile").read_text()
    assert python_df.lstrip().startswith("FROM python:")
