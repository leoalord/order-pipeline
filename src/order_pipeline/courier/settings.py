"""Courier sim knobs. Compose supplies wiring (ledger path); defaults live here."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TripTimes(BaseModel):
    """Per-band trip seconds. Config: near 12 / mid 20 / far 35."""

    near: float = 12.0
    mid: float = 20.0
    far: float = 35.0

    def as_map(self) -> dict[str, float]:
        return {"near": self.near, "mid": self.mid, "far": self.far}


class CSIMSettings(BaseSettings):
    """Courier knobs. Fleet size is parallelism; 3× busy is live per trip band."""

    model_config = SettingsConfigDict(env_prefix="CSIM_", env_nested_delimiter="__")

    fleet_size: int = 8
    busy_multiple: int = 3
    trip_s: TripTimes = Field(default_factory=TripTimes)
    flaky_5xx_pct: float = 3.0
    flaky_drop_pct: float = 2.0
    sim_timeout_s: float = 2.0
    ledger_path: Path = Path("courier-ledger.sqlite")
    host: str = "0.0.0.0"
    port: int = 8082

    @model_validator(mode="after")
    def enforce_design_constraints(self) -> Self:
        assert self.flaky_5xx_pct >= 0, "flaky_5xx_pct must be >= 0"
        assert self.flaky_drop_pct >= 0, "flaky_drop_pct must be >= 0"
        assert self.flaky_5xx_pct + self.flaky_drop_pct < 50, (
            "flakiness percentages must sum to < 50"
        )
        assert self.busy_multiple >= 2, "busy_multiple must be >= 2"
        assert self.fleet_size >= 1, "fleet_size must be >= 1"
        assert self.sim_timeout_s > 0, "sim_timeout_s must be > 0"
        min_trip = min(self.trip_s.near, self.trip_s.mid, self.trip_s.far)
        assert min_trip > self.sim_timeout_s, (
            "dispatch must hang up without waiting for travel; "
            "min trip time must exceed sim timeout (hang-up < sim timeout)"
        )
        return self
