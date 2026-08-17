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


def test_dashboard_source_has_assignment_stage_cards_and_lite_fields() -> None:
    home = (DASHBOARD / "src" / "HomePage.tsx").read_text()
    snapshot = (DASHBOARD / "src" / "snapshot.ts").read_text()
    for label in ASSIGNMENT_STAGES:
        assert f'"{label}"' in snapshot, label
    assert "STAGE_LABELS.map" in home
    assert "STAGE_SEAMS" in home
    assert "queued" in home
    assert "cooking" in home
    assert "waiting for" in home
    assert "on a pan" in home
    assert '"confirmed"' in snapshot
    assert '"being prepared"' in snapshot
    assert "queued — waiting for a pan" in snapshot
    assert "cooking — on a pan" in snapshot
    assert "stages?.[" in home
    assert "snapshot.stages" in home or "stages?.[" in home
    assert "currently_leased" in home
    assert "state_vs_last_order_events_mismatches" in home
    assert "duplicate_attempts" in home
    assert "duplicate_effects" in home
    assert "startup_scan" in home
    assert "invalid_transitions" in home
    assert "conservation" in home
    assert "terminal_rates_per_min" in home
    assert "e2e_latency_s" in home
    assert "oldest_open" in home
    assert "accept_reject" in home
    assert "backlog" in home
    assert "retry_rate" in home
    assert "http_429s" in home
    assert "stretching_etas" in home
    assert "sim_http" in home
    assert "outbound_slots" in home
    assert "http_5xx" in home
    assert "http_429" in home
    assert "timeout" in home
    assert "parked_list" in home
    assert "no_progress_beyond_threshold" in home
    assert "fetchLoadgenStatus" in home
    assert "cohortId: loadgen.cohort_id" in home
    assert 'fetch("/loadgen/status"' in snapshot
    assert "Pipeline" in home
    assert "parked list" in home
    assert "oldest open" in home
    assert "<button" not in home.lower()
    assert "redrive" not in home.lower()
    assert "outbound slots" in home.lower()
    assert "16 / 16 / 48" in home
    assert "paste-an-ID" in home
    assert "in each stage now" in home
    assert "last 60 seconds" in home
    assert "endings, not stages" in home
    assert "<canvas" not in home.lower()
    assert "chart" not in home.lower()


def test_control_load_group_posts_to_loadgen_proxy() -> None:
    control = (DASHBOARD / "src" / "ControlPage.tsx").read_text()
    shell = (DASHBOARD / "src" / "Shell.tsx").read_text()
    assert "<h1>Control</h1>" in control
    assert "fetch(path, init)" in control
    for path in (
        "/loadgen/calibrate",
        "/loadgen/cohort/new",
        "/loadgen/scenario/steady",
        "/loadgen/scenario/rush",
        "/loadgen/stop",
    ):
        assert path in control, path
    assert "Calibrate" in control
    assert "New cohort" in control
    assert "Steady" in control
    assert "Rush" in control
    assert "Stop" in control
    assert "mult" in control
    assert "Outage" in control
    assert "/loadgen/beat/doom-confirm" in control
    assert 'run("/rsim/admin/faults", { mode: "blackout", seconds: 60 })' in control
    assert 'run("/rsim/admin/faults", { mode: "clear" })' in control
    assert "Restaurant blackout (60s)" in control
    assert "redrive" not in control.lower()
    assert "fail_void" not in control.lower()
    assert "stock" not in control.lower()
    assert "kill" not in control.lower()
    assert 'target="_blank"' in shell
    assert 'href="/control"' in shell


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
