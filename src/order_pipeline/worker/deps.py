"""Per-dependency slot caps. rsim is live; csim is a no-op until dispatch_and_deliver."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from order_pipeline.worker.settings import WorkerSettings


class DepCaps:
    def __init__(self, settings: WorkerSettings) -> None:
        self._rsim = asyncio.Semaphore(settings.dep_cap_rsim)
        # Stored so Settings is complete; not acquired until dispatch_and_deliver.
        self.dep_cap_csim = settings.dep_cap_csim

    @asynccontextmanager
    async def rsim(self) -> AsyncIterator[None]:
        async with self._rsim:
            yield

    @asynccontextmanager
    async def csim(self) -> AsyncIterator[None]:
        yield
