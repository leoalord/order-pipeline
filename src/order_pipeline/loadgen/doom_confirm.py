"""Deterministic doomed-confirm cohort coordinated across API and restaurant sim."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx

from order_pipeline.intake import confirm_idempotency_key

DOOM_CONFIRM_COUNT = 3


class DoomConfirmError(RuntimeError):
    """An upstream call or response made the fixture impossible to create."""


class DoomConfirmRace(DoomConfirmError):
    """A selected order/effect advanced before the targeted rule landed."""


@dataclass(frozen=True)
class PlacedOrder:
    id: UUID
    state: str
    accepted_at: datetime


@dataclass(frozen=True)
class ConfirmUnavailableTarget:
    idempotency_key: str
    until: datetime


@dataclass(frozen=True)
class DoomConfirmResult:
    order_ids: tuple[UUID, ...]
    cohort_id: UUID


class DoomConfirmClient(Protocol):
    async def place(
        self,
        *,
        items: list[str],
        cohort_id: UUID,
        place_key: str,
    ) -> PlacedOrder: ...

    async def get_order(self, order_id: UUID) -> PlacedOrder: ...

    async def replace_confirm_unavailable(
        self,
        targets: list[ConfirmUnavailableTarget],
    ) -> None: ...

    async def aclose(self) -> None: ...


class HttpDoomConfirmClient:
    """Loadgen-owned API + restaurant-admin wiring; the Order API stays unchanged."""

    def __init__(
        self,
        api_base_url: str,
        restaurant_admin_url: str,
        *,
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api = api_base_url.rstrip("/")
        self._restaurant = restaurant_admin_url.rstrip("/")
        self._timeout = timeout_s
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def place(
        self,
        *,
        items: list[str],
        cohort_id: UUID,
        place_key: str,
    ) -> PlacedOrder:
        response = await self._client.post(
            f"{self._api}/orders",
            json={"items": items, "cohort_id": str(cohort_id)},
            headers={"Idempotency-Key": place_key},
            timeout=self._timeout,
        )
        if response.status_code != 201:
            raise DoomConfirmError(
                f"place fixture order returned {response.status_code}: {response.text}"
            )
        return _parse_order(response.json(), source="POST /orders")

    async def get_order(self, order_id: UUID) -> PlacedOrder:
        response = await self._client.get(
            f"{self._api}/orders/{order_id}",
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise DoomConfirmError(
                f"read fixture order {order_id} returned {response.status_code}: {response.text}"
            )
        return _parse_order(response.json(), source=f"GET /orders/{order_id}")

    async def replace_confirm_unavailable(
        self,
        targets: list[ConfirmUnavailableTarget],
    ) -> None:
        response = await self._client.post(
            f"{self._restaurant}/admin/faults/confirm-unavailable",
            json={
                "targets": [
                    {
                        "idempotency_key": target.idempotency_key,
                        "until": target.until.isoformat(),
                    }
                    for target in targets
                ]
            },
            timeout=self._timeout,
        )
        if response.status_code == 409:
            raise DoomConfirmRace(f"restaurant rejected late doom-confirm rule: {response.text}")
        if response.status_code != 200:
            raise DoomConfirmError(
                "restaurant confirm-unavailable admin returned "
                f"{response.status_code}: {response.text}"
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class DoomConfirmFixture:
    """Create exactly one targeted cohort at a time and reject a lost race."""

    def __init__(
        self,
        client: DoomConfirmClient,
        *,
        confirm_deadline_s: float,
    ) -> None:
        self._client = client
        self._confirm_deadline_s = confirm_deadline_s
        self._lock = asyncio.Lock()

    async def create(self, cohort_id: UUID) -> DoomConfirmResult:
        async with self._lock:
            # A second fixture is a new doomed cohort: remove the previous keys
            # without touching global blackout or the random fault mix.
            await self._client.replace_confirm_unavailable([])
            raw = await asyncio.gather(
                *(
                    self._client.place(
                        items=["chips"],
                        cohort_id=cohort_id,
                        place_key=f"doom-confirm:{cohort_id}:{uuid.uuid4()}",
                    )
                    for _ in range(DOOM_CONFIRM_COUNT)
                ),
                return_exceptions=True,
            )
            failures = [result for result in raw if isinstance(result, BaseException)]
            placed = [result for result in raw if isinstance(result, PlacedOrder)]
            if failures or len(placed) != DOOM_CONFIRM_COUNT:
                await self._client.replace_confirm_unavailable([])
                detail = "; ".join(str(failure) for failure in failures)
                raise DoomConfirmError(detail or "did not create the full doom-confirm cohort")

            targets = [
                ConfirmUnavailableTarget(
                    idempotency_key=confirm_idempotency_key(order.id),
                    until=order.accepted_at + timedelta(seconds=self._confirm_deadline_s),
                )
                for order in placed
            ]
            try:
                # Restaurant performs this replacement under the same lock as
                # accept and refuses keys already present in its effect ledger.
                await self._client.replace_confirm_unavailable(targets)
                checked = await asyncio.gather(
                    *(self._client.get_order(order.id) for order in placed)
                )
            except Exception:
                await self._client.replace_confirm_unavailable([])
                raise

            advanced = [order for order in checked if order.state != "placed"]
            if advanced:
                await self._client.replace_confirm_unavailable([])
                states = ", ".join(f"{order.id}={order.state}" for order in advanced)
                raise DoomConfirmRace(
                    f"tagged order confirmed before doom-confirm rule landed: {states}"
                )

            return DoomConfirmResult(
                order_ids=tuple(order.id for order in placed),
                cohort_id=cohort_id,
            )

    async def clear(self) -> None:
        async with self._lock:
            await self._client.replace_confirm_unavailable([])

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_order(raw: Any, *, source: str) -> PlacedOrder:
    if not isinstance(raw, dict):
        raise DoomConfirmError(f"{source} did not return an object")
    try:
        order_id = UUID(str(raw["id"]))
        state = str(raw["state"])
        accepted_at = datetime.fromisoformat(str(raw["accepted_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise DoomConfirmError(f"{source} returned an invalid order") from exc
    if accepted_at.tzinfo is None:
        raise DoomConfirmError(f"{source} accepted_at omitted its timezone")
    return PlacedOrder(id=order_id, state=state, accepted_at=accepted_at)
