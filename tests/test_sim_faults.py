"""Shared sim core: always-on mix (incl. after-effect 5xx) and timed blackout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from order_pipeline.sim.core import Quote, SimCore
from order_pipeline.sim.faults import FaultMode, FaultState
from order_pipeline.sim.ledger import EffectLedger


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ScriptedRng:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def random(self) -> float:
        return next(self._values)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 8, 16, 12, 0, tzinfo=UTC))


def _core(
    tmp_path: Path,
    clock: MutableClock,
    *,
    flaky_5xx_pct: float = 0.0,
    flaky_drop_pct: float = 0.0,
    rng: ScriptedRng | None = None,
) -> SimCore:
    def quote(body: dict[str, Any], now: datetime) -> Quote:
        return Quote(estimated_ready_at=now + timedelta(seconds=12), payload=dict(body))

    def status_at(
        *,
        accepted_at: datetime,
        estimated_ready_at: datetime,
        now: datetime,
        payload: dict[str, Any],
    ) -> str:
        _ = accepted_at, payload
        return "ready" if now >= estimated_ready_at else "cooking"

    return SimCore(
        ledger=EffectLedger(tmp_path / "ledger.sqlite"),
        faults=FaultState(now_fn=clock),
        quote=quote,
        status_at=status_at,
        flaky_5xx_pct=flaky_5xx_pct,
        flaky_drop_pct=flaky_drop_pct,
        now_fn=clock,
        rng=rng,
    )


def test_blackout_drops_without_ledger_then_expires(tmp_path: Path, clock: MutableClock) -> None:
    core = _core(tmp_path, clock)
    view = core.set_fault_command("blackout", seconds=5)
    assert view["mode"] == "blackout"
    assert view["blackout_remaining_s"] == 5.0
    assert view["mix"] == "off"

    dropped = core.accept("k1", {"items": ["chips"]})
    assert dropped.action == "blackout"
    assert core.ledger.get_by_key("k1") is None

    clock.now = clock.now + timedelta(seconds=5)
    expired = core.faults_view()
    assert expired["mode"] == "off"
    assert expired["blackout_remaining_s"] == 0.0

    ok = core.accept("k1", {"items": ["chips"]})
    assert ok.action == "ok"
    assert core.ledger.counts_by_key()["k1"] == 1


def test_clear_cancels_blackout_immediately(tmp_path: Path, clock: MutableClock) -> None:
    core = _core(tmp_path, clock)
    core.set_fault_command("blackout", seconds=30)
    cleared = core.set_fault_command("clear")
    assert cleared["mode"] == "off"
    assert cleared["blackout_remaining_s"] == 0.0
    ok = core.accept("k-clear", {"items": ["taco"]})
    assert ok.action == "ok"


def test_mix_off_skips_random_faults(tmp_path: Path, clock: MutableClock) -> None:
    rng = ScriptedRng([0.0, 0.0])
    core = _core(tmp_path, clock, flaky_5xx_pct=3, flaky_drop_pct=2, rng=rng)
    core.set_fault_command("clear", mix="off")
    ok = core.accept("k-off", {"items": ["chips"]})
    assert ok.action == "ok"
    assert core.faults_view()["mix"] == "off"


def test_mix_includes_5xx_after_effect(tmp_path: Path, clock: MutableClock) -> None:
    # roll=3.0 is inside the 2% drop + 3% 5xx window after drop; second draw >= 0.5 → after.
    rng = ScriptedRng([0.03, 0.9])
    core = _core(tmp_path, clock, flaky_5xx_pct=3, flaky_drop_pct=2, rng=rng)
    failed = core.accept("k-after", {"items": ["burrito"]})
    assert failed.action == "five_xx"
    assert failed.status_code == 500
    assert core.ledger.counts_by_key()["k-after"] == 1


def test_mix_includes_5xx_before_effect(tmp_path: Path, clock: MutableClock) -> None:
    rng = ScriptedRng([0.03, 0.1])
    core = _core(tmp_path, clock, flaky_5xx_pct=3, flaky_drop_pct=2, rng=rng)
    failed = core.accept("k-before", {"items": ["burrito"]})
    assert failed.action == "five_xx"
    assert core.ledger.get_by_key("k-before") is None


def test_mix_drop_writes_then_drops(tmp_path: Path, clock: MutableClock) -> None:
    rng = ScriptedRng([0.01])
    core = _core(tmp_path, clock, flaky_5xx_pct=3, flaky_drop_pct=2, rng=rng)
    dropped = core.accept("k-drop", {"items": ["chips"]})
    assert dropped.action == "drop"
    assert core.ledger.counts_by_key()["k-drop"] == 1


def test_mix_on_restores_boot_percentages(tmp_path: Path, clock: MutableClock) -> None:
    core = _core(tmp_path, clock, flaky_5xx_pct=3, flaky_drop_pct=2)
    core.set_fault_command("clear", mix="off")
    restored = core.set_fault_command("clear", mix="on")
    assert restored["mix"] == "on"
    assert restored["flaky_5xx_pct"] == 3.0
    assert restored["flaky_drop_pct"] == 2.0
    assert restored["mode"] == FaultMode.OFF.value


def test_sticky_mode_beats_mix(tmp_path: Path, clock: MutableClock) -> None:
    rng = ScriptedRng([0.99, 0.99])
    core = _core(tmp_path, clock, flaky_5xx_pct=3, flaky_drop_pct=2, rng=rng)
    core.set_fault_command("5xx_before")
    failed = core.accept("k-sticky", {"items": ["chips"]})
    assert failed.action == "five_xx"
    assert core.ledger.get_by_key("k-sticky") is None
