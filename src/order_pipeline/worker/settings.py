from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEP_CAP_RSIM = 8
DEFAULT_DEP_CAP_CSIM = 8
DEFAULT_TASK_CAPACITY = 24
DEFAULT_CONFIRM_DEADLINE_S = 120.0


class WorkerSettings(BaseSettings):
    """Complete at first appearance. Compose supplies wiring (DSN, sim URL); knobs live here."""

    model_config = SettingsConfigDict(env_prefix="WORKER_")

    database_url: str
    restaurant_base_url: str
    health_host: str = "0.0.0.0"
    health_port: int = 8083

    sim_timeout_s: float = 2.0
    lease_s: float = 15.0
    confirm_deadline_s: float = DEFAULT_CONFIRM_DEADLINE_S
    transient_retries: int = 5
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    dep_cap_rsim: int = DEFAULT_DEP_CAP_RSIM
    dep_cap_csim: int = DEFAULT_DEP_CAP_CSIM
    task_capacity: int = DEFAULT_TASK_CAPACITY
    poll_interval_s: float = 3.0
    poll_budget: int = 30
    void_retries: int = 3

    @model_validator(mode="after")
    def enforce_design_constraints(self) -> Self:
        assert self.lease_s > self.sim_timeout_s, (
            "lease must exceed sim timeout so a live call is not stolen"
        )
        assert self.dep_cap_rsim >= 1, "dep_cap_rsim must be >= 1"
        assert self.dep_cap_csim >= 1, "dep_cap_csim must be >= 1"
        assert self.task_capacity > self.dep_cap_rsim + self.dep_cap_csim, (
            "one slow sim must never occupy every slot"
        )
        return self
