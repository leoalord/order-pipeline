"""Per-dependency slot caps. Both rsim and csim are live (8 / 8 within task capacity 24).

Admission happens *before* claim so a blacked-out sim cannot fill every task
slot with leased waiters. HTTP still goes through the matching semaphore.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from order_pipeline.worker.settings import WorkerSettings

RSIM_WORK_TYPES = frozenset({"confirm", "poll_cook", "void_ticket"})
CSIM_WORK_TYPES = frozenset({"dispatch", "poll_ride"})


class DepCaps:
    def __init__(self, settings: WorkerSettings) -> None:
        self._rsim = asyncio.Semaphore(settings.dep_cap_rsim)
        self._csim = asyncio.Semaphore(settings.dep_cap_csim)
        self._rsim_cap = settings.dep_cap_rsim
        self._csim_cap = settings.dep_cap_csim
        self._rsim_admitted = 0
        self._csim_admitted = 0

    def eligible_types(self, registered: Sequence[str]) -> tuple[str, ...]:
        """Work types whose dependency still has an admission slot."""
        rsim_open = self._rsim_admitted < self._rsim_cap
        csim_open = self._csim_admitted < self._csim_cap
        eligible: list[str] = []
        for work_type in registered:
            if work_type in RSIM_WORK_TYPES:
                if rsim_open:
                    eligible.append(work_type)
            elif work_type in CSIM_WORK_TYPES:
                if csim_open:
                    eligible.append(work_type)
            else:
                eligible.append(work_type)
        return tuple(eligible)

    def admit(self, work_type: str) -> None:
        if work_type in RSIM_WORK_TYPES:
            self._rsim_admitted += 1
        elif work_type in CSIM_WORK_TYPES:
            self._csim_admitted += 1

    def release_admit(self, work_type: str) -> None:
        if work_type in RSIM_WORK_TYPES:
            self._rsim_admitted = max(0, self._rsim_admitted - 1)
        elif work_type in CSIM_WORK_TYPES:
            self._csim_admitted = max(0, self._csim_admitted - 1)

    @asynccontextmanager
    async def rsim(self) -> AsyncIterator[None]:
        async with self._rsim:
            yield

    @asynccontextmanager
    async def csim(self) -> AsyncIterator[None]:
        async with self._csim:
            yield
