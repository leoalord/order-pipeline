"""Dashboard SPA source and harness: cards, /control load group, tsc, reserved proxy."""

from __future__ import annotations

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
    assert "Required before Normal or Rush" in control
    assert "disabled={disabled || !calibrated}" in control
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
        "Redrive",
    ):
        assert label in control
    assert "kill" not in control.lower()
    assert 'Navigate to="/" replace' in control
    assert 'target="_blank"' not in shell
    assert 'href="/control"' not in shell


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
