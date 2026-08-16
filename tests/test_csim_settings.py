"""CSIMSettings code defaults and boot assertions."""

import pytest
from pydantic import ValidationError

from order_pipeline.courier.settings import CSIMSettings


@pytest.fixture(autouse=True)
def _clear_csim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CSIM_FLEET_SIZE",
        "CSIM_BUSY_MULTIPLE",
        "CSIM_FLAKY_5XX_PCT",
        "CSIM_FLAKY_DROP_PCT",
        "CSIM_SIM_TIMEOUT_S",
        "CSIM_LEDGER_PATH",
        "CSIM_TRIP_S__NEAR",
        "CSIM_TRIP_S__MID",
        "CSIM_TRIP_S__FAR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_code_defaults() -> None:
    settings = CSIMSettings()
    assert settings.fleet_size == 8
    assert settings.busy_multiple == 3
    assert settings.trip_s.near == 12.0
    assert settings.trip_s.mid == 20.0
    assert settings.trip_s.far == 35.0
    assert settings.flaky_5xx_pct == 3.0
    assert settings.flaky_drop_pct == 2.0
    assert settings.sim_timeout_s == 2.0
    assert settings.port == 8082


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAKY_5XX_PCT", "99")
    monkeypatch.setenv("BUSY_MULTIPLE", "1")
    settings = CSIMSettings()
    assert settings.flaky_5xx_pct == 3.0
    assert settings.busy_multiple == 3


def test_flakiness_sum_at_50_fails_boot() -> None:
    with pytest.raises(ValidationError):
        CSIMSettings(flaky_5xx_pct=30, flaky_drop_pct=20)


def test_busy_multiple_below_two_fails_boot() -> None:
    with pytest.raises(ValidationError):
        CSIMSettings(busy_multiple=1)


def test_trip_not_exceeding_sim_timeout_fails_boot() -> None:
    with pytest.raises(ValidationError):
        CSIMSettings(sim_timeout_s=12.0)


def test_prefixed_env_wrong_defaults_fail_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSIM_BUSY_MULTIPLE", "1")
    with pytest.raises(ValidationError):
        CSIMSettings()
