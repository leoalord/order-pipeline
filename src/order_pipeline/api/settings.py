from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class APISettings(BaseSettings):
    """Order API knobs. Compose supplies wiring (DSN); defaults live here."""

    model_config = SettingsConfigDict(env_prefix="API_")

    database_url: str
    accept_concurrency: int = 32
    place_key_ttl_h: int = 48
    # Compose wiring so GET /snapshot can read sim GET /admin/ledger (not Postgres).
    restaurant_admin_url: str = "http://restaurant:8081"
    courier_admin_url: str = "http://courier:8082"

    @model_validator(mode="after")
    def enforce_design_constraints(self) -> Self:
        assert self.place_key_ttl_h >= 24, (
            "place_key_ttl_h must be >= 24 so a retry cannot mint a second order mid-demo"
        )
        assert self.accept_concurrency >= 1, "accept_concurrency must be >= 1"
        return self
