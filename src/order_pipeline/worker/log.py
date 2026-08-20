"""Structured worker logs. Compose currently shows almost only uvicorn health lines."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger("order_pipeline.worker")


def log_worker_event(
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: BaseException | bool | None = None,
    **fields: Any,
) -> None:
    parts = [event]
    extra: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is None:
            continue
        rendered = str(value) if isinstance(value, UUID) else value
        extra[key] = rendered
        parts.append(f"{key}={rendered}")
    logger.log(level, " ".join(str(part) for part in parts), extra=extra, exc_info=exc_info)
