"""Loadgen knobs. Compose supplies wiring (API URL); defaults live here."""

from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LoadgenSettings(BaseSettings):
    """Scenario profiles. Mirrors the confirm deadline for the cross-service boot assertion."""

    model_config = SettingsConfigDict(env_prefix="LOADGEN_")

    api_base_url: str = "http://127.0.0.1:8000"
    restaurant_admin_url: str = "http://127.0.0.1:8081"
    host: str = "0.0.0.0"
    port: int = 8090
    # Mirrors WORKER_CONFIRM_DEADLINE_S via env when set.
    confirm_deadline_s: float = 120.0
    blackout_s: float = 60.0
    rush_multiplier: float = 1.5
    steady_fraction: float = 0.4
    rush_duration_s: float = 60.0
    calibrate_step_s: float = 25.0
    calibrate_start_rps: float = 0.25
    calibrate_factor: float = 1.35
    calibrate_max_rps: float = 8.0
    one_item_pct: float = 70.0
    two_item_pct: float = 20.0
    place_timeout_s: float = 5.0
    snapshot_timeout_s: float = 5.0

    @model_validator(mode="after")
    def enforce_design_constraints(self) -> Self:
        assert self.confirm_deadline_s > self.blackout_s, (
            "confirm_deadline_s must exceed blackout_s so outage shows recovery plus explicit fails"
        )
        assert self.rush_multiplier > 1.0, "rush_multiplier must be > 1.0 so rush crosses H"
        assert self.steady_fraction > 0, "steady_fraction must be > 0"
        assert self.rush_duration_s > 0, "rush_duration_s must be > 0"
        assert self.calibrate_step_s > 0, "calibrate_step_s must be > 0"
        assert self.calibrate_start_rps > 0, "calibrate_start_rps must be > 0"
        assert self.calibrate_factor > 1.0, "calibrate_factor must be > 1.0"
        assert self.calibrate_max_rps >= self.calibrate_start_rps, (
            "calibrate_max_rps must be >= calibrate_start_rps"
        )
        assert 0 < self.one_item_pct < 100, "one_item_pct must be in (0, 100)"
        assert 0 <= self.two_item_pct < 100, "two_item_pct must be in [0, 100)"
        assert self.one_item_pct + self.two_item_pct < 100, (
            "cart mix must leave room for some 3-item carts"
        )
        return self
