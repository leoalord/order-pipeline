"""RSIMSettings code defaults and boot assertions."""

import pytest
from pydantic import ValidationError

from order_pipeline.restaurant.settings import RSIMSettings


@pytest.fixture(autouse=True)
def _clear_rsim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RSIM_KITCHEN_PANS",
        "RSIM_BUSY_MULTIPLE",
        "RSIM_EXTRA_ITEM_S",
        "RSIM_RAIL_FUSE",
        "RSIM_STOCK_DEFAULT",
        "RSIM_FLAKY_5XX_PCT",
        "RSIM_FLAKY_DROP_PCT",
        "RSIM_SIM_TIMEOUT_S",
        "RSIM_LEDGER_PATH",
        "RSIM_COOK_S__CHIPS",
        "RSIM_COOK_S__TACO",
        "RSIM_COOK_S__BURRITO",
    ):
        monkeypatch.delenv(name, raising=False)


def test_code_defaults() -> None:
    settings = RSIMSettings()
    assert settings.kitchen_pans == 20
    assert settings.busy_multiple == 3
    assert settings.cook_s.chips == 12.0
    assert settings.cook_s.taco == 18.0
    assert settings.cook_s.burrito == 25.0
    assert settings.extra_item_s == 5.0
    assert settings.rail_fuse == 80
    assert settings.stock_default == 10_000
    assert settings.flaky_5xx_pct == 3.0
    assert settings.flaky_drop_pct == 2.0
    assert settings.sim_timeout_s == 2.0
    assert settings.port == 8081


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAKY_5XX_PCT", "99")
    monkeypatch.setenv("BUSY_MULTIPLE", "1")
    settings = RSIMSettings()
    assert settings.flaky_5xx_pct == 3.0
    assert settings.busy_multiple == 3


def test_flakiness_sum_at_50_fails_boot() -> None:
    with pytest.raises(ValidationError):
        RSIMSettings(flaky_5xx_pct=30, flaky_drop_pct=20)


def test_busy_multiple_below_two_fails_boot() -> None:
    with pytest.raises(ValidationError):
        RSIMSettings(busy_multiple=1)


def test_stock_default_below_one_fails_boot() -> None:
    with pytest.raises(ValidationError):
        RSIMSettings(stock_default=0)


def test_cook_not_exceeding_sim_timeout_fails_boot() -> None:
    with pytest.raises(ValidationError):
        RSIMSettings(sim_timeout_s=12.0)


def test_prefixed_env_wrong_defaults_fail_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RSIM_BUSY_MULTIPLE", "1")
    with pytest.raises(ValidationError):
        RSIMSettings()
