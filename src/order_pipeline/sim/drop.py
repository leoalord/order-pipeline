"""Lost-response (timeline D): effect applied, HTTP body never sent."""

from __future__ import annotations

from starlette.responses import Response
from starlette.types import Receive, Scope, Send


class DroppedResponse(Response):
    """Close without a complete HTTP response so the caller sees a drop."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        return
