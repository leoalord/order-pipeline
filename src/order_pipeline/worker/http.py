"""Outbound restaurant calls: rsim semaphore wraps every request. Timeout = sim timeout."""

from __future__ import annotations

from typing import Any

import httpx

from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.settings import WorkerSettings


class RestaurantClient:
    def __init__(self, settings: WorkerSettings, caps: DepCaps) -> None:
        self._caps = caps
        self._client = httpx.AsyncClient(
            base_url=settings.restaurant_base_url,
            timeout=settings.sim_timeout_s,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._caps.rsim():
            return await self._client.request(method, url, **kwargs)

    async def accept(self, *, idempotency_key: str, items: list[str]) -> httpx.Response:
        return await self.request(
            "POST",
            "/accept",
            headers={"Idempotency-Key": idempotency_key},
            json={"items": items},
        )

    async def get_by_key(self, idempotency_key: str) -> httpx.Response:
        # httpx encodes the space in `(order_id, confirm)`; do not pre-quote.
        return await self.request("GET", f"/keys/{idempotency_key}")

    async def aclose(self) -> None:
        await self._client.aclose()
