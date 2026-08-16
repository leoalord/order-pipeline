"""Accept / poll / Stripe-style key replay on top of the SQLite ledger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from order_pipeline.sim.faults import FaultCommand, FaultMode, FaultState
from order_pipeline.sim.ledger import Effect, EffectLedger


class QuoteError(Exception):
    """Permanent client error while quoting an accept/dispatch body."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class Quote:
    estimated_ready_at: datetime
    payload: dict[str, Any]


class QuoteFn(Protocol):
    def __call__(self, body: dict[str, Any], now: datetime) -> Quote: ...


class StatusFn(Protocol):
    def __call__(
        self,
        *,
        accepted_at: datetime,
        estimated_ready_at: datetime,
        now: datetime,
    ) -> str: ...


NowFn = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AcceptOutcome:
    action: Literal["ok", "replay", "conflict", "reject", "five_xx", "drop"]
    status_code: int
    body: dict[str, Any] | None = None
    detail: str | None = None


class SimCore:
    """Shared accept/poll/key-cache/ledger/faults. Restaurant and courier share this."""

    def __init__(
        self,
        *,
        ledger: EffectLedger,
        faults: FaultState,
        quote: QuoteFn,
        status_at: StatusFn,
        flaky_5xx_pct: float,
        flaky_drop_pct: float,
        now_fn: NowFn | None = None,
    ) -> None:
        self.ledger = ledger
        self.faults = faults
        self._quote = quote
        self._status_at = status_at
        self._flaky_5xx_pct = flaky_5xx_pct
        self._flaky_drop_pct = flaky_drop_pct
        self._now = now_fn or _utc_now

    def ping(self) -> None:
        self.ledger.ping()

    def faults_view(self) -> dict[str, Any]:
        mix_off = self._flaky_5xx_pct == 0 and self._flaky_drop_pct == 0
        return {
            "mode": self.faults.mode.value,
            "mix": "off" if mix_off else "on",
            "flaky_5xx_pct": self._flaky_5xx_pct,
            "flaky_drop_pct": self._flaky_drop_pct,
        }

    def set_fault_command(self, command: FaultCommand) -> dict[str, Any]:
        self.faults.set_command(command)
        return self.faults_view()

    def ledger_counts(self) -> dict[str, int]:
        return self.ledger.counts_by_key()

    def accept(self, idempotency_key: str, body: dict[str, Any]) -> AcceptOutcome:
        existing = self.ledger.get_by_key(idempotency_key)
        if existing is not None:
            return self._replay(existing, body)

        try:
            quote = self._quote(body, self._now())
        except QuoteError as exc:
            return AcceptOutcome(
                action="reject",
                status_code=exc.status_code,
                detail=exc.detail,
            )

        mode = self.faults.mode
        if mode is FaultMode.FIVE_XX_BEFORE:
            return AcceptOutcome(
                action="five_xx",
                status_code=500,
                detail="injected 5xx_before",
            )

        now = self._now()
        effect = Effect(
            idempotency_key=idempotency_key,
            ticket_id=str(uuid4()),
            accepted_at=now,
            estimated_ready_at=quote.estimated_ready_at,
            payload=quote.payload,
        )
        inserted = self.ledger.insert(effect)
        if not inserted:
            raced = self.ledger.get_by_key(idempotency_key)
            if raced is None:
                return AcceptOutcome(
                    action="reject",
                    status_code=500,
                    detail="ledger insert failed",
                )
            return self._replay(raced, body)

        if mode is FaultMode.FIVE_XX_AFTER:
            return AcceptOutcome(
                action="five_xx",
                status_code=500,
                detail="injected 5xx_after",
            )
        if mode is FaultMode.DROP:
            return AcceptOutcome(action="drop", status_code=0)

        return AcceptOutcome(
            action="ok",
            status_code=200,
            body=self._ticket_body(effect, now=now),
        )

    def poll(self, ticket_id: str) -> dict[str, Any] | None:
        effect = self.ledger.get_by_ticket(ticket_id)
        if effect is None:
            return None
        return self._ticket_body(effect, now=self._now())

    def get_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        effect = self.ledger.get_by_key(idempotency_key)
        if effect is None:
            return None
        return self._ticket_body(effect, now=self._now())

    def _replay(self, existing: Effect, body: dict[str, Any]) -> AcceptOutcome:
        try:
            quote = self._quote(body, existing.accepted_at)
        except QuoteError as exc:
            return AcceptOutcome(
                action="reject",
                status_code=exc.status_code,
                detail=exc.detail,
            )
        if quote.payload != existing.payload:
            return AcceptOutcome(
                action="conflict",
                status_code=409,
                detail="Idempotency-Key reused with a different body",
            )
        return AcceptOutcome(
            action="replay",
            status_code=200,
            body=self._ticket_body(existing, now=self._now()),
        )

    def _ticket_body(self, effect: Effect, *, now: datetime) -> dict[str, Any]:
        status = self._status_at(
            accepted_at=effect.accepted_at,
            estimated_ready_at=effect.estimated_ready_at,
            now=now,
        )
        return {
            "ticket_id": effect.ticket_id,
            "estimated_ready_at": isoformat_z(effect.estimated_ready_at),
            "status": status,
        }
