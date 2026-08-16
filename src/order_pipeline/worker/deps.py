"""Per-dependency slot caps. Both rsim and csim are live (8 / 8 within task capacity 24)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from order_pipeline.worker.settings import WorkerSettings


class DepCaps:
    def __init__(self, settings: WorkerSettings) -> None:
        self._rsim = asyncio.Semaphore(settings.dep_cap_rsim)
        self._csim = asyncio.Semaphore(settings.dep_cap_csim)

    @asynccontextmanager
    async def rsim(self) -> AsyncIterator[None]:
        async with self._rsim:
            yield

    @asynccontextmanager
    async def csim(self) -> AsyncIterator[None]:
        async with self._csim:
            yield
