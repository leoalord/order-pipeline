"""Deterministic admin fault modes. Random mix stays off until a later slice."""

from __future__ import annotations

import threading
from enum import StrEnum
from typing import Literal

FaultCommand = Literal["clear", "5xx_before", "5xx_after", "drop"]


class FaultMode(StrEnum):
    OFF = "off"
    FIVE_XX_BEFORE = "5xx_before"
    FIVE_XX_AFTER = "5xx_after"
    DROP = "drop"


_COMMAND_TO_MODE: dict[FaultCommand, FaultMode] = {
    "clear": FaultMode.OFF,
    "5xx_before": FaultMode.FIVE_XX_BEFORE,
    "5xx_after": FaultMode.FIVE_XX_AFTER,
    "drop": FaultMode.DROP,
}


class FaultState:
    """Sticky admin mode. All modes off at boot; POST /admin/faults sets one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = FaultMode.OFF

    @property
    def mode(self) -> FaultMode:
        with self._lock:
            return self._mode

    def set_command(self, command: FaultCommand) -> FaultMode:
        mode = _COMMAND_TO_MODE[command]
        with self._lock:
            self._mode = mode
        return mode
