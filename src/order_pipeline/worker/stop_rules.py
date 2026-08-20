"""Stop-rule helpers. Kitchen handlers call these in the next task; chassis owns them now."""

from __future__ import annotations

from datetime import datetime, timedelta

from order_pipeline.compensation import CONFIRM_WORK_TYPES, VOID_TICKET_WORK_TYPE
from order_pipeline.worker.settings import WorkerSettings

__all__ = [
    "CONFIRM_WORK_TYPES",
    "COUNT_BOUNDED_WORK_TYPES",
    "POLL_WORK_TYPES",
    "confirm_deadline_exceeded",
    "count_budget_exhausted",
    "count_budget_for",
    "poll_budget_exhausted",
]

POLL_WORK_TYPES = frozenset({"poll_cook", "poll_ride"})
COUNT_BOUNDED_WORK_TYPES = frozenset({"dispatch", VOID_TICKET_WORK_TYPE, "poll_cook", "poll_ride"})


def confirm_deadline_exceeded(
    accepted_at: datetime,
    now: datetime,
    deadline_s: float,
) -> bool:
    return now >= accepted_at + timedelta(seconds=deadline_s)


def poll_budget_exhausted(attempt_count: int, poll_budget: int) -> bool:
    return attempt_count >= poll_budget


def count_budget_for(work_type: str, settings: WorkerSettings) -> int | None:
    if work_type in POLL_WORK_TYPES:
        return settings.poll_budget
    if work_type == VOID_TICKET_WORK_TYPE:
        return settings.void_retries
    if work_type == "dispatch":
        return settings.transient_retries
    return None


def count_budget_exhausted(attempt_count: int, budget: int) -> bool:
    return attempt_count >= budget
