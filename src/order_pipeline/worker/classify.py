"""Map HTTP / transport failures onto ATTEMPT_OUTCOMES. Bonus B inherits this branch."""

from __future__ import annotations

import httpx

from order_pipeline.models import ATTEMPT_OUTCOMES

PERMANENT_OUTCOMES = frozenset({"http_4xx"})
TRANSIENT_OUTCOMES = frozenset({"http_429", "http_5xx", "timeout", "dropped", "unknown"})


def classify_status(status_code: int) -> str:
    if status_code == 429:
        return "http_429"
    if 400 <= status_code < 500:
        return "http_4xx"
    if 500 <= status_code < 600:
        return "http_5xx"
    if 200 <= status_code < 300:
        return "ok"
    return "unknown"


def classify_exception(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "dropped"
    if isinstance(exc, httpx.RequestError):
        return "unknown"
    return "unknown"


def result_from_status(status_code: int) -> str:
    outcome = classify_status(status_code)
    assert outcome in ATTEMPT_OUTCOMES or outcome == "ok"
    return outcome
