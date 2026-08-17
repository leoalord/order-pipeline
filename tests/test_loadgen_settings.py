"""LoadgenSettings code defaults and boot assertions."""

import pytest
from pydantic import ValidationError

from order_pipeline.loadgen.settings import LoadgenSettings


@pytest.fixture(autouse=True)
def _clear_loadgen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LOADGEN_API_BASE_URL",
        "LOADGEN_CONFIRM_DEADLINE_S",
        "LOADGEN_BLACKOUT_S",
        "LOADGEN_RUSH_MULTIPLIER",
        "LOADGEN_STEADY_FRACTION",
        "LOADGEN_RUSH_DURATION_S",
        "LOADGEN_CALIBRATE_STEP_S",
        "LOADGEN_CALIBRATE_START_RPS",
        "LOADGEN_CALIBRATE_FACTOR",
        "LOADGEN_CALIBRATE_MAX_RPS",
        "LOADGEN_ONE_ITEM_PCT",
        "LOADGEN_TWO_ITEM_PCT",
        "LOADGEN_HOST",
        "LOADGEN_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_code_defaults() -> None:
    settings = LoadgenSettings()
    assert settings.port == 8090
    assert settings.confirm_deadline_s == 120.0
    assert settings.blackout_s == 60.0
    assert settings.rush_multiplier == 1.5
    assert settings.steady_fraction == 0.4
    assert settings.rush_duration_s == 60.0
    assert settings.calibrate_step_s == 25.0
    assert settings.one_item_pct == 70.0
    assert settings.two_item_pct == 20.0


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUSH_MULTIPLIER", "1")
    monkeypatch.setenv("BLACKOUT_S", "180")
    settings = LoadgenSettings()
    assert settings.rush_multiplier == 1.5
    assert settings.blackout_s == 60.0


def test_confirm_deadline_not_exceeding_blackout_fails_boot() -> None:
    with pytest.raises(ValidationError):
        LoadgenSettings(confirm_deadline_s=60.0, blackout_s=60.0)
    with pytest.raises(ValidationError):
        LoadgenSettings(confirm_deadline_s=30.0, blackout_s=60.0)


def test_rush_multiplier_not_above_one_fails_boot() -> None:
    with pytest.raises(ValidationError):
        LoadgenSettings(rush_multiplier=1.0)
    with pytest.raises(ValidationError):
        LoadgenSettings(rush_multiplier=0.5)


def test_prefixed_env_wrong_defaults_fail_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOADGEN_CONFIRM_DEADLINE_S", "60")
    monkeypatch.setenv("LOADGEN_BLACKOUT_S", "60")
    with pytest.raises(ValidationError):
        LoadgenSettings()
    monkeypatch.setenv("LOADGEN_CONFIRM_DEADLINE_S", "120")
    monkeypatch.setenv("LOADGEN_RUSH_MULTIPLIER", "1")
    with pytest.raises(ValidationError):
        LoadgenSettings()
