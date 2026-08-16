from dataclasses import dataclass


@dataclass
class WorkerCounters:
    """In-process stop-rule / guard metrics. Display lands in a later slice."""

    invalid_transitions: int = 0
