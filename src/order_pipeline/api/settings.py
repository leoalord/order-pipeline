from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from order_pipeline.worker.settings import (
    DEFAULT_DEP_CAP_CSIM,
    DEFAULT_DEP_CAP_RSIM,
    DEFAULT_TASK_CAPACITY,
)

API_DB_POOL_HEADROOM = 4


class APISettings(BaseSettings):
    """Order API knobs. Compose supplies wiring (DSN); defaults live here."""

    model_config = SettingsConfigDict(env_prefix="API_")

    database_url: str
    accept_concurrency: int = 32
    place_key_ttl_h: int = 48
    # Compose wiring so GET /snapshot can read sim GET /admin/ledger (not Postgres).
    restaurant_admin_url: str = "http://restaurant:8081"
    courier_admin_url: str = "http://courier:8082"
    # GET /snapshot reports fleet totals. Code defaults are shared with the
    # worker; compose feeds both services the same deployment-level values.
    worker_replicas: int = 2
    worker_dep_cap_rsim: int = DEFAULT_DEP_CAP_RSIM
    worker_dep_cap_csim: int = DEFAULT_DEP_CAP_CSIM
    worker_task_capacity: int = DEFAULT_TASK_CAPACITY

    @property
    def database_pool_size(self) -> int:
        """Connections for every admitted place plus control-plane headroom."""
        return self.accept_concurrency + API_DB_POOL_HEADROOM

    @model_validator(mode="after")
    def enforce_design_constraints(self) -> Self:
        assert self.place_key_ttl_h >= 24, (
            "place_key_ttl_h must be >= 24 so a retry cannot mint a second order mid-demo"
        )
        assert self.accept_concurrency >= 1, "accept_concurrency must be >= 1"
        assert self.worker_replicas >= 1, "worker_replicas must be >= 1"
        assert self.worker_dep_cap_rsim >= 1, "worker_dep_cap_rsim must be >= 1"
        assert self.worker_dep_cap_csim >= 1, "worker_dep_cap_csim must be >= 1"
        assert self.worker_task_capacity > (self.worker_dep_cap_rsim + self.worker_dep_cap_csim), (
            "one slow sim must never occupy every worker slot"
        )
        return self
