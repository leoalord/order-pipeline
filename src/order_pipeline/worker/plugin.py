"""Work-type plugin types. Kitchen and courier handlers register at boot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol
from uuid import UUID


class WorkDisposition(Enum):
    COMPLETE = "complete"
    RETRY = "retry"
    PARK = "park"
    FAIL_ORDER = "fail_order"


@dataclass(frozen=True)
class ClaimedWork:
    work_item_id: UUID
    order_id: UUID
    work_type: str
    idempotency_key: str
    attempt_id: UUID
    lease_owner: str
    attempt_count: int
    payload: Any
    order_state: str
    order_version: int
    accepted_at: datetime
    items: Any = None


@dataclass(frozen=True)
class GuardedTransition:
    expected_state: str
    to_state: str
    cause: str
    actor: str = "worker"


@dataclass(frozen=True)
class NextWork:
    work_type: str
    idempotency_key: str
    payload: Any = None
    next_attempt_at: datetime | None = None


@dataclass(frozen=True)
class HandlerResult:
    """HTTP is already done. Chassis classifies, guards, and commits."""

    outcome: str
    disposition: WorkDisposition | None = None
    transition: GuardedTransition | None = None
    next_work: tuple[NextWork, ...] = field(default_factory=tuple)
    result_payload: Any = None
    park_reason: str | None = None
    park_next_action: str | None = None
    next_attempt_at: datetime | None = None


class WorkHandler(Protocol):
    async def __call__(self, claimed: ClaimedWork) -> HandlerResult: ...
