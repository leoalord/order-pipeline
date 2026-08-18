"""Rehearsal: place one named-item order and return its id."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx

from order_pipeline.menu import MENU_ITEM_IDS


class BeatPlaceError(RuntimeError):
    """Place could not complete the out-of-stock rehearsal beat."""


class BeatPlaceRunner(Protocol):
    async def run(self, cohort_id: UUID, item: str) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class HttpBeatPlace:
    """Loadgen-owned API client. One order, not a new load scenario."""

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

    async def run(self, cohort_id: UUID, item: str) -> dict[str, Any]:
        if item not in MENU_ITEM_IDS:
            raise BeatPlaceError(f"unknown item id: {item!r}; menu is chips, taco, burrito")
        place_key = f"beat-place-{uuid4()}"
        placed = await self._client.post(
            f"{self._api}/orders",
            json={"items": [item], "cohort_id": str(cohort_id)},
            headers={"Idempotency-Key": place_key},
            timeout=self._timeout,
        )
        if placed.status_code != 201:
            raise BeatPlaceError(f"place returned {placed.status_code}: {placed.text[:200]}")
        body = placed.json()
        if not isinstance(body, dict) or "id" not in body:
            raise BeatPlaceError("place did not return an order id")
        return {
            "order_id": str(body["id"]),
            "cohort_id": str(cohort_id),
            "item": item,
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
