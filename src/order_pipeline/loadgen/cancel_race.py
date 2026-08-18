"""Live rehearsal: place + cancel timed to collide with in-flight confirm."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx


class CancelRaceError(RuntimeError):
    """Place or cancel could not complete the rehearsal beat."""


class CancelRaceRunner(Protocol):
    async def run(self, cohort_id: UUID) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class HttpCancelRace:
    """Loadgen-owned API client. Confirm is milliseconds; this is not the pytest."""

    def __init__(
        self,
        api_base_url: str,
        *,
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api = api_base_url.rstrip("/")
        self._timeout = timeout_s
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def run(self, cohort_id: UUID) -> dict[str, Any]:
        place_key = f"cancel-race-{uuid4()}"
        placed = await self._client.post(
            f"{self._api}/orders",
            json={"items": ["chips"], "cohort_id": str(cohort_id)},
            headers={"Idempotency-Key": place_key},
            timeout=self._timeout,
        )
        if placed.status_code != 201:
            raise CancelRaceError(f"place returned {placed.status_code}: {placed.text[:200]}")
        body = placed.json()
        if not isinstance(body, dict) or "id" not in body:
            raise CancelRaceError("place did not return an order id")
        order_id = str(body["id"])
        cancelled = await self._client.post(
            f"{self._api}/orders/{order_id}/cancel",
            timeout=self._timeout,
        )
        state = None
        if cancelled.headers.get("content-type", "").startswith("application/json"):
            payload = cancelled.json()
            if isinstance(payload, dict):
                state = payload.get("state")
        return {
            "order_id": order_id,
            "cohort_id": str(cohort_id),
            "cancel_status": cancelled.status_code,
            "state": state,
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
