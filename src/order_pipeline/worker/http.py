"""Outbound sim calls. rsim wraps restaurant; csim wraps courier. Timeout = sim timeout."""

from __future__ import annotations

import os
from typing import Any

import httpx

from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.settings import WorkerSettings

DEFAULT_COURIER_BASE_URL = "http://courier:8082"


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

    async def void(self, *, idempotency_key: str, body: dict[str, Any]) -> httpx.Response:
        return await self.request(
            "POST",
            "/void",
            headers={"Idempotency-Key": idempotency_key},
            json=body,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def courier_base_url(*, override: str | None = None) -> str:
    """Compose wiring (`WORKER_COURIER_BASE_URL`); not a WorkerSettings knob."""
    if override is not None:
        return override
    return os.environ.get("WORKER_COURIER_BASE_URL", DEFAULT_COURIER_BASE_URL)


class CourierClient:
    def __init__(
        self,
        settings: WorkerSettings,
        caps: DepCaps,
        *,
        base_url: str | None = None,
    ) -> None:
        self._caps = caps
        self._client = httpx.AsyncClient(
            base_url=courier_base_url(override=base_url),
            timeout=settings.sim_timeout_s,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._caps.csim():
            return await self._client.request(method, url, **kwargs)

    async def accept(self, *, idempotency_key: str, body: dict[str, Any]) -> httpx.Response:
        return await self.request(
            "POST",
            "/accept",
            headers={"Idempotency-Key": idempotency_key},
            json=body,
        )

    async def get_by_key(self, idempotency_key: str) -> httpx.Response:
        # httpx encodes the space in `(order_id, dispatch)`; do not pre-quote.
        return await self.request("GET", f"/keys/{idempotency_key}")

    async def aclose(self) -> None:
        await self._client.aclose()
