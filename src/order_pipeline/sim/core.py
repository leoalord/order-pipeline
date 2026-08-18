"""Accept / poll / Stripe-style key replay on top of the SQLite ledger."""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

from order_pipeline.sim.faults import FaultCommand, FaultMode, FaultState
from order_pipeline.sim.ledger import Effect, EffectLedger

MixSetting = Literal["off", "on"]


class QuoteError(Exception):
    """Permanent client error while quoting an accept/dispatch body."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class ExistingEffectConflict(Exception):
    """A target already reached the sim before its fixture rule was installed."""

    def __init__(self, idempotency_keys: list[str]) -> None:
        super().__init__("confirm effects already exist for targeted keys")
        self.idempotency_keys = idempotency_keys


@dataclass(frozen=True)
class Quote:
    estimated_ready_at: datetime
    payload: dict[str, Any]
    # Set on new accepts only. Replay compares payload and ignores this — a later
    # 3×/fuse 429 must not 409 or refuse a Stripe replay of an already-accepted key.
    reject_status: int | None = None
    reject_detail: str | None = None


class QuoteFn(Protocol):
    def __call__(self, body: dict[str, Any], now: datetime) -> Quote: ...


class StatusFn(Protocol):
    def __call__(
        self,
        *,
        accepted_at: datetime,
        estimated_ready_at: datetime,
        now: datetime,
        payload: dict[str, Any],
    ) -> str: ...


NowFn = Callable[[], datetime]


class Rng(Protocol):
    def random(self) -> float: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AcceptOutcome:
    action: Literal["ok", "replay", "conflict", "reject", "five_xx", "drop", "blackout"]
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
        rng: Rng | None = None,
        blackout_hang_s: float = 0.0,
    ) -> None:
        self.ledger = ledger
        self.faults = faults
        self._quote = quote
        self._status_at = status_at
        self._boot_5xx_pct = flaky_5xx_pct
        self._boot_drop_pct = flaky_drop_pct
        self._flaky_5xx_pct = flaky_5xx_pct
        self._flaky_drop_pct = flaky_drop_pct
        self._now = now_fn or _utc_now
        self._rng = rng or random.Random()
        self.blackout_hang_s = blackout_hang_s
        # Quote reads current rail occupancy and insert reserves the chosen slot.
        # Keep that read/quote/write sequence atomic across concurrent HTTP accepts.
        self._accept_lock = threading.Lock()

    def ping(self) -> None:
        self.ledger.ping()

    def blackout_active(self) -> bool:
        """Whether ordinary dependency traffic is currently unavailable."""
        return self.faults.effective_mode(self._now()) is FaultMode.BLACKOUT

    def faults_view(self) -> dict[str, Any]:
        now = self._now()
        mix_off = self._flaky_5xx_pct == 0 and self._flaky_drop_pct == 0
        remaining = self.faults.blackout_remaining_s(now)
        targets = self.faults.confirm_unavailable_targets(now)
        return {
            "mode": self.faults.effective_mode(now).value,
            "mix": "off" if mix_off else "on",
            "flaky_5xx_pct": self._flaky_5xx_pct,
            "flaky_drop_pct": self._flaky_drop_pct,
            "blackout_remaining_s": remaining,
            "confirm_unavailable": [
                {
                    "idempotency_key": key,
                    "until": isoformat_z(until),
                }
                for key, until in sorted(targets.items())
            ],
        }

    def set_fault_command(
        self,
        command: FaultCommand,
        *,
        seconds: float | None = None,
        mix: MixSetting | None = None,
    ) -> dict[str, Any]:
        self.faults.set_command(command, seconds=seconds, now=self._now())
        if mix == "off":
            self._flaky_5xx_pct = 0.0
            self._flaky_drop_pct = 0.0
        elif mix == "on":
            self._flaky_5xx_pct = self._boot_5xx_pct
            self._flaky_drop_pct = self._boot_drop_pct
        return self.faults_view()

    def replace_confirm_unavailable(
        self,
        targets: dict[str, datetime],
    ) -> dict[str, Any]:
        """Atomically replace the targeted rule before any later accept can pass.

        Existing effects mean a worker beat the fixture to the restaurant. Refuse
        the cohort instead of pretending those orders are still safely doomed.
        """
        with self._accept_lock:
            existing = [key for key in targets if self.ledger.get_by_key(key) is not None]
            if existing:
                raise ExistingEffectConflict(existing)
            self.faults.replace_confirm_unavailable(targets, now=self._now())
        return self.faults_view()

    def ledger_counts(self) -> dict[str, int]:
        return self.ledger.counts_by_key()

    def accept(self, idempotency_key: str, body: dict[str, Any]) -> AcceptOutcome:
        with self._accept_lock:
            return self._accept_locked(idempotency_key, body)

    def _accept_locked(self, idempotency_key: str, body: dict[str, Any]) -> AcceptOutcome:
        now = self._now()
        mode = self.faults.effective_mode(now)
        if mode is FaultMode.BLACKOUT:
            return AcceptOutcome(
                action="blackout",
                status_code=0,
                detail="injected blackout",
            )

        # This check precedes ledger replay deliberately. A response-lost effect
        # must not let a doomed key recover before the order's confirm deadline.
        if self.faults.confirm_unavailable(idempotency_key, now):
            return AcceptOutcome(
                action="five_xx",
                status_code=503,
                detail="targeted confirm unavailable",
            )

        existing = self.ledger.get_by_key(idempotency_key)
        if existing is not None:
            return self._replay(existing, body)

        try:
            quote = self._quote(body, now)
        except QuoteError as exc:
            return AcceptOutcome(
                action="reject",
                status_code=exc.status_code,
                detail=exc.detail,
            )
        if quote.reject_status is not None:
            return AcceptOutcome(
                action="reject",
                status_code=quote.reject_status,
                detail=quote.reject_detail or "busy",
            )

        # fail_void is scoped to the compensation endpoint. Accepts retain the
        # configured 3%/2% mix while that sticky mode is armed.
        injected = self._roll_mix() if mode in {FaultMode.OFF, FaultMode.FAIL_VOID} else mode
        if injected is FaultMode.FIVE_XX_BEFORE:
            return AcceptOutcome(
                action="five_xx",
                status_code=500,
                detail="injected 5xx_before",
            )

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

        if injected is FaultMode.FIVE_XX_AFTER:
            return AcceptOutcome(
                action="five_xx",
                status_code=500,
                detail="injected 5xx_after",
            )
        if injected is FaultMode.DROP:
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

    def void(self, idempotency_key: str, body: dict[str, Any]) -> AcceptOutcome:
        """Stripe-style void under `(order_id, void)`. fail_void 500s before the write."""
        with self._accept_lock:
            return self._void_locked(idempotency_key, body)

    def _void_locked(self, idempotency_key: str, body: dict[str, Any]) -> AcceptOutcome:
        now = self._now()
        mode = self.faults.effective_mode(now)
        if mode is FaultMode.BLACKOUT:
            return AcceptOutcome(
                action="blackout",
                status_code=0,
                detail="injected blackout",
            )

        existing = self.ledger.get_by_key(idempotency_key)
        if existing is not None:
            return self._replay_void(existing, body)

        request_payload = self._void_request_payload(body)
        accept_key = request_payload["accept_key"]
        ticket_id = request_payload["ticket_id"]
        original: Effect | None = None
        if isinstance(accept_key, str) and accept_key:
            original = self.ledger.get_by_key(accept_key)
        if original is None and isinstance(ticket_id, str) and ticket_id:
            original = self.ledger.get_by_ticket(ticket_id)

        # A pre-effect failure has nothing to compensate. Record a replayable
        # no-op even while fail_void is armed so it cannot create a false orphan.
        if original is not None and mode is FaultMode.FAIL_VOID:
            return AcceptOutcome(
                action="five_xx",
                status_code=500,
                detail="injected fail_void",
            )

        effect = Effect(
            idempotency_key=idempotency_key,
            ticket_id=str(uuid4()),
            accepted_at=now,
            estimated_ready_at=now,
            payload={
                "kind": "void",
                "request": request_payload,
                "accept_key": original.idempotency_key if original is not None else accept_key,
                "voided_ticket_id": original.ticket_id if original is not None else ticket_id,
                "voided": original is not None,
            },
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
            return self._replay_void(raced, body)
        return AcceptOutcome(action="ok", status_code=200, body=self._void_body(effect))

    @staticmethod
    def _void_request_payload(body: dict[str, Any]) -> dict[str, str | None]:
        accept_key = body.get("accept_key")
        ticket_id = body.get("ticket_id")
        return {
            "accept_key": accept_key if isinstance(accept_key, str) and accept_key else None,
            "ticket_id": ticket_id if isinstance(ticket_id, str) and ticket_id else None,
        }

    def _replay_void(self, existing: Effect, body: dict[str, Any]) -> AcceptOutcome:
        if existing.payload.get("kind") != "void":
            return AcceptOutcome(
                action="conflict",
                status_code=409,
                detail="Idempotency-Key already belongs to a non-void effect",
            )
        if existing.payload.get("request") != self._void_request_payload(body):
            return AcceptOutcome(
                action="conflict",
                status_code=409,
                detail="Idempotency-Key reused with a different body",
            )
        return AcceptOutcome(
            action="replay",
            status_code=200,
            body=self._void_body(existing),
        )

    def _roll_mix(self) -> FaultMode | None:
        """Always-on mix: drop%, then 5xx% split before/after so after-effect is in the mix."""
        drop_pct = self._flaky_drop_pct
        five_xx_pct = self._flaky_5xx_pct
        if drop_pct <= 0 and five_xx_pct <= 0:
            return None
        roll = self._rng.random() * 100.0
        if roll < drop_pct:
            return FaultMode.DROP
        if roll < drop_pct + five_xx_pct:
            # Half the 5xx budget is after-effect (easy wrong turn 4).
            if self._rng.random() < 0.5:
                return FaultMode.FIVE_XX_BEFORE
            return FaultMode.FIVE_XX_AFTER
        return None

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
            payload=effect.payload,
        )
        body: dict[str, Any] = {
            "ticket_id": effect.ticket_id,
            "accepted_at": isoformat_z(effect.accepted_at),
            "estimated_ready_at": isoformat_z(effect.estimated_ready_at),
            "status": status,
        }
        started = _service_started_at(effect)
        if started is not None:
            body["service_started_at"] = isoformat_z(started)
        return body

    def _void_body(self, effect: Effect) -> dict[str, Any]:
        voided_ticket = effect.payload.get("voided_ticket_id")
        if isinstance(voided_ticket, str) and voided_ticket:
            ticket_id = voided_ticket
        else:
            ticket_id = effect.ticket_id
        voided = effect.payload.get("voided") is True
        return {
            "ticket_id": ticket_id,
            "voided": voided,
            "absent": not voided,
            "accepted_at": isoformat_z(effect.accepted_at),
        }


def _service_started_at(effect: Effect) -> datetime | None:
    """Pan/bike start = ETA − quiet service. Derived from payload, not occupancy."""
    raw = effect.payload.get("quiet_cook_s", effect.payload.get("trip_s"))
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return None
    return effect.estimated_ready_at - timedelta(seconds=float(raw))
