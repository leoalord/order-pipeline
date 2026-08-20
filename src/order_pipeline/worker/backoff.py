"""Unleased retry delay: 0.5s ×2, cap 8s, full jitter."""

from __future__ import annotations

import random

from order_pipeline.worker.settings import WorkerSettings

# The claim loop counts consecutive failures without a ceiling; 2**n on a float
# raises OverflowError past 2**1024, which would kill the loop it backs off for.
_MAX_EXPONENT = 32


def full_jitter_delay_s(
    attempt_count: int,
    settings: WorkerSettings,
    rng: random.Random,
) -> float:
    exponent = min(max(0, attempt_count - 1), _MAX_EXPONENT)
    ceiling = min(settings.backoff_cap_s, settings.backoff_base_s * (2**exponent))
    return rng.uniform(0.0, ceiling)
