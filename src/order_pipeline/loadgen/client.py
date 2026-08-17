"""HTTP client for the Order API. Loadgen never calls the sims."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import httpx


class PipelineClient(Protocol):
    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int: ...

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class HttpPipelineClient:
    def __init__(
        self,
        base_url: str,
        *,
        place_timeout_s: float = 5.0,
        snapshot_timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._place_timeout = place_timeout_s
        self._snapshot_timeout = snapshot_timeout_s
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        response = await self._client.post(
            f"{self._base}/orders",
            json={"items": items, "cohort_id": str(cohort_id)},
            headers={"Idempotency-Key": place_key},
            timeout=self._place_timeout,
        )
        return response.status_code

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        response = await self._client.get(
            f"{self._base}/snapshot",
            params={"cohort_id": str(cohort_id)},
            timeout=self._snapshot_timeout,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("GET /snapshot did not return an object")
        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
