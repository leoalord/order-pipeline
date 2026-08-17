"""Door cap: in-flight Place Order fuse. Counted 429s, never silent drops."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from order_pipeline.api.door import CohortRejects, DoorCap


def test_admit_up_to_limit_then_reject_is_counted() -> None:
    door = DoorCap(2)
    assert door.admit() is True
    assert door.admit() is True
    assert door.in_flight == 2
    assert door.admit() is False
    assert door.admit() is False
    assert door.rejected == 2
    assert door.in_flight == 2


def test_release_frees_a_slot_without_clearing_rejects() -> None:
    door = DoorCap(1)
    assert door.admit() is True
    assert door.admit() is False
    door.release()
    assert door.in_flight == 0
    assert door.admit() is True
    assert door.rejected == 1
    door.release()


def test_saturated_rejects_create_no_slot() -> None:
    door = DoorCap(1)
    assert door.admit() is True
    assert door.admit() is False
    assert door.in_flight == 1
    door.release()
    assert door.in_flight == 0


def test_release_without_admit_raises() -> None:
    door = DoorCap(1)
    with pytest.raises(RuntimeError, match="released without admit"):
        door.release()


def test_limit_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="door cap must be >= 1"):
        DoorCap(0)


def test_concurrent_admit_never_exceeds_limit() -> None:
    door = DoorCap(4)
    hold = threading.Event()
    all_holding = threading.Barrier(5)

    def holder() -> None:
        assert door.admit() is True
        all_holding.wait()
        hold.wait()
        door.release()

    threads = [threading.Thread(target=holder) for _ in range(4)]
    for thread in threads:
        thread.start()
    all_holding.wait()
    assert door.in_flight == 4
    assert door.admit() is False
    assert door.rejected == 1
    hold.set()
    for thread in threads:
        thread.join()
    assert door.in_flight == 0


def test_cohort_rejects_are_filtered_by_cohort() -> None:
    counts = CohortRejects()
    first = uuid4()
    second = uuid4()
    counts.add(first)
    counts.add(first)
    counts.add(second)
    assert counts.rejected(first) == 2
    assert counts.rejected(second) == 1
    assert counts.rejected(uuid4()) == 0
