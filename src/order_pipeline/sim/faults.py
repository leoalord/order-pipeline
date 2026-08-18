"""Admin fault modes plus timed blackout. Random mix lives on SimCore."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

FaultCommand = Literal["clear", "5xx_before", "5xx_after", "drop", "blackout", "fail_void"]
NowFn = Callable[[], datetime]


class FaultMode(StrEnum):
    OFF = "off"
    FIVE_XX_BEFORE = "5xx_before"
    FIVE_XX_AFTER = "5xx_after"
    DROP = "drop"
    BLACKOUT = "blackout"
    FAIL_VOID = "fail_void"


_COMMAND_TO_MODE: dict[FaultCommand, FaultMode] = {
    "clear": FaultMode.OFF,
    "5xx_before": FaultMode.FIVE_XX_BEFORE,
    "5xx_after": FaultMode.FIVE_XX_AFTER,
    "drop": FaultMode.DROP,
    "blackout": FaultMode.BLACKOUT,
    "fail_void": FaultMode.FAIL_VOID,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FaultState:
    """Sticky admin mode plus per-key confirm-unavailable rules.

    The targeted rules are separate from the global mode: a 60-second blackout
    may expire while selected confirm keys remain unavailable until their own
    order deadlines.
    """

    def __init__(self, now_fn: NowFn | None = None) -> None:
        self._lock = threading.Lock()
        self._mode = FaultMode.OFF
        self._blackout_until: datetime | None = None
        self._confirm_unavailable_until: dict[str, datetime] = {}
        self._now = now_fn or _utc_now

    def _expire_confirm_unavailable_unlocked(self, now: datetime) -> None:
        expired = [key for key, until in self._confirm_unavailable_until.items() if now >= until]
        for key in expired:
            del self._confirm_unavailable_until[key]

    def _expire_unlocked(self, now: datetime) -> None:
        if self._mode is not FaultMode.BLACKOUT:
            return
        if self._blackout_until is None or now >= self._blackout_until:
            self._mode = FaultMode.OFF
            self._blackout_until = None

    def effective_mode(self, now: datetime | None = None) -> FaultMode:
        stamp = now if now is not None else self._now()
        with self._lock:
            self._expire_unlocked(stamp)
            return self._mode

    def confirm_unavailable(self, idempotency_key: str, now: datetime | None = None) -> bool:
        stamp = now if now is not None else self._now()
        with self._lock:
            self._expire_confirm_unavailable_unlocked(stamp)
            return idempotency_key in self._confirm_unavailable_until

    def confirm_unavailable_targets(self, now: datetime | None = None) -> dict[str, datetime]:
        stamp = now if now is not None else self._now()
        with self._lock:
            self._expire_confirm_unavailable_unlocked(stamp)
            return dict(self._confirm_unavailable_until)

    def replace_confirm_unavailable(
        self,
        targets: Mapping[str, datetime],
        *,
        now: datetime | None = None,
    ) -> None:
        stamp = now if now is not None else self._now()
        if any(until.tzinfo is None for until in targets.values()):
            raise ValueError("confirm-unavailable deadlines must include a timezone")
        if any(until <= stamp for until in targets.values()):
            raise ValueError("confirm-unavailable deadlines must be in the future")
        with self._lock:
            self._confirm_unavailable_until = dict(targets)

    @property
    def mode(self) -> FaultMode:
        return self.effective_mode()

    def blackout_remaining_s(self, now: datetime | None = None) -> float:
        stamp = now if now is not None else self._now()
        with self._lock:
            self._expire_unlocked(stamp)
            if self._mode is not FaultMode.BLACKOUT or self._blackout_until is None:
                return 0.0
            remaining = (self._blackout_until - stamp).total_seconds()
            return max(0.0, remaining)

    def set_command(
        self,
        command: FaultCommand,
        *,
        seconds: float | None = None,
        now: datetime | None = None,
    ) -> FaultMode:
        stamp = now if now is not None else self._now()
        if command == "blackout":
            if seconds is None or seconds <= 0:
                raise ValueError("blackout requires seconds > 0")
            with self._lock:
                self._mode = FaultMode.BLACKOUT
                self._blackout_until = stamp + timedelta(seconds=seconds)
            return FaultMode.BLACKOUT

        mode = _COMMAND_TO_MODE[command]
        with self._lock:
            self._mode = mode
            self._blackout_until = None
            if command == "clear":
                self._confirm_unavailable_until.clear()
        return mode
