"""Dashboard SPA source and harness: cards, stub /control, tsc, reserved proxy."""

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
    assert "paste-an-ID" in home
    assert "in each stage now" in home
    assert "last 60 seconds" in home
    assert "endings, not stages" in home
    assert "<canvas" not in home.lower()
    assert "chart" not in home.lower()


def test_control_is_empty_stub_without_buttons() -> None:
    control = (DASHBOARD / "src" / "ControlPage.tsx").read_text()
    assert "<h1>Control</h1>" in control
    assert "later slice" in control
    assert "<button" not in control.lower()
    assert "pipeline" not in control.lower()


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


def test_compose_dashboard_on_5173_no_loadgen_service() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "\n  dashboard:" in compose
    assert '"5173:5173"' in compose
    assert "API_PROXY_TARGET: http://api:8000" in compose
    assert "LOADGEN_PROXY_TARGET: http://loadgen:8090" in compose
    assert "RSIM_PROXY_TARGET: http://restaurant:8081" in compose
    assert "CSIM_PROXY_TARGET: http://courier:8082" in compose
    assert "\n  loadgen:" not in compose
    dockerfile = (DASHBOARD / "Dockerfile").read_text()
    assert dockerfile.lstrip().startswith("FROM node:")
    python_df = (REPO_ROOT / "Dockerfile").read_text()
    assert python_df.lstrip().startswith("FROM python:")
