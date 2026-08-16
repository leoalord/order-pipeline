"""Unleased retry delay: 0.5s ×2, cap 8s, full jitter."""

from __future__ import annotations

import random

from order_pipeline.worker.settings import WorkerSettings


def full_jitter_delay_s(
    attempt_count: int,
    settings: WorkerSettings,
    rng: random.Random,
) -> float:
    exponent = max(0, attempt_count - 1)
    ceiling = min(settings.backoff_cap_s, settings.backoff_base_s * (2**exponent))
    return rng.uniform(0.0, ceiling)
