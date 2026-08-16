"""Restaurant sim knobs. Compose supplies wiring (ledger path); defaults live here."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CookTimes(BaseModel):
    """Per-item quiet cook seconds. Config: chips 12 / taco 18 / burrito 25."""

    chips: float = 12.0
    taco: float = 18.0
    burrito: float = 25.0

    def as_map(self) -> dict[str, float]:
        return {"chips": self.chips, "taco": self.taco, "burrito": self.burrito}


class RSIMSettings(BaseSettings):
    """Complete at first appearance, including dormant pans / 3× / fuse knobs."""

    model_config = SettingsConfigDict(env_prefix="RSIM_", env_nested_delimiter="__")

    kitchen_pans: int = 20
    busy_multiple: int = 3
    cook_s: CookTimes = Field(default_factory=CookTimes)
    extra_item_s: float = 5.0
    rail_fuse: int = 80
    # Config table lists 3/2; this slice keeps 0 so later kitchen tests don't flake.
    flaky_5xx_pct: float = 0.0
    flaky_drop_pct: float = 0.0
    sim_timeout_s: float = 2.0
    ledger_path: Path = Path("restaurant-ledger.sqlite")
    host: str = "0.0.0.0"
    port: int = 8081

    @model_validator(mode="after")
    def enforce_design_constraints(self) -> Self:
        assert self.flaky_5xx_pct >= 0, "flaky_5xx_pct must be >= 0"
        assert self.flaky_drop_pct >= 0, "flaky_drop_pct must be >= 0"
        assert self.flaky_5xx_pct + self.flaky_drop_pct < 50, (
            "flakiness percentages must sum to < 50"
        )
        assert self.busy_multiple >= 2, "busy_multiple must be >= 2"
        assert self.kitchen_pans >= 1, "kitchen_pans must be >= 1"
        assert self.rail_fuse >= 1, "rail_fuse must be >= 1"
        assert self.extra_item_s >= 0, "extra_item_s must be >= 0"
        assert self.sim_timeout_s > 0, "sim_timeout_s must be > 0"
        min_cook = min(self.cook_s.chips, self.cook_s.taco, self.cook_s.burrito)
        assert min_cook > self.sim_timeout_s, (
            "accept must hang up without waiting for cook; "
            "min cook time must exceed sim timeout (hang-up < sim timeout)"
        )
        return self
