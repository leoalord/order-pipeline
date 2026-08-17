"""In-flight Place Order cap. Counted 429s, never silent drops.

Not kitchen fullness (sim 429, worker retries) and not stock. Saturated
requests never enter the accept transaction, so they create no order row.
"""

from __future__ import annotations

import threading
from uuid import UUID


class CohortRejects:
    """Door 429s keyed by cohort so GET /snapshot can filter them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rejected: dict[UUID, int] = {}

    def add(self, cohort_id: UUID) -> None:
        with self._lock:
            self._rejected[cohort_id] = self._rejected.get(cohort_id, 0) + 1

    def rejected(self, cohort_id: UUID) -> int:
        with self._lock:
            return self._rejected.get(cohort_id, 0)


class DoorCap:
    """Non-blocking slot gate for concurrent POST /orders."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("door cap must be >= 1")
        self._limit = limit
        self._in_flight = 0
        self._rejected = 0
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def rejected(self) -> int:
        """Counted door 429s since process start. Not kitchen/courier busy."""
        with self._lock:
            return self._rejected

    def admit(self) -> bool:
        """Take a slot. False means HTTP 429 — caller must not create an order."""
        with self._lock:
            if self._in_flight >= self._limit:
                self._rejected += 1
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight <= 0:
                raise RuntimeError("door cap released without admit")
            self._in_flight -= 1
