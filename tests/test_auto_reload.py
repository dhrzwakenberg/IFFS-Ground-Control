from __future__ import annotations

import pytest

from iffs_ground_control.auto_reload import AutoReloadController, AutoReloadState
from iffs_ground_control.models import Component


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_normal_shoot_delay_fill_all_sequence_uses_independent_completion() -> None:
    clock = FakeClock()
    fill_starts = 0

    def start_fill_all() -> None:
        nonlocal fill_starts
        fill_starts += 1

    controller = AutoReloadController(start_fill_all, delay_seconds=1.0, clock=clock)
    controller.set_enabled(True)
    controller.shoot_started()
    assert controller.state is AutoReloadState.SHOOTING

    controller.tick({Component.SHOOT}, set())
    assert controller.state is AutoReloadState.WAITING
    assert controller.snapshot().delay_remaining == pytest.approx(1.0)

    clock.advance(0.9)
    controller.tick(set(), set())
    assert fill_starts == 0
    clock.advance(0.1)
    controller.tick(set(), set())
    assert fill_starts == 1
    assert controller.state is AutoReloadState.RELOADING

    controller.tick({Component.AIR}, {Component.WATER})
    assert controller.state is AutoReloadState.RELOADING
    controller.tick({Component.WATER}, set())
    assert controller.state is AutoReloadState.COMPLETE
    clock.advance(1.0)
    controller.tick(set(), set())
    assert controller.state is AutoReloadState.IDLE


def test_disabled_auto_reload_does_not_track_shoot() -> None:
    starts: list[str] = []
    controller = AutoReloadController(lambda: starts.append("fill"))

    controller.shoot_started()
    controller.tick({Component.SHOOT}, set())

    assert controller.state is AutoReloadState.IDLE
    assert starts == []


@pytest.mark.parametrize(
    "stage",
    [
        AutoReloadState.SHOOTING,
        AutoReloadState.WAITING,
        AutoReloadState.RELOADING,
    ],
)
def test_stop_cancels_every_active_sequence_stage(stage: AutoReloadState) -> None:
    clock = FakeClock()
    controller = AutoReloadController(lambda: None, clock=clock)
    controller.set_enabled(True)
    controller.state = stage

    controller.cancel("STOP ALL")

    assert controller.state is AutoReloadState.CANCELLED
    assert controller.snapshot().detail == "STOP ALL"


def test_disabling_during_delay_cancels_pending_reload() -> None:
    starts: list[str] = []
    controller = AutoReloadController(lambda: starts.append("fill"))
    controller.set_enabled(True)
    controller.shoot_started()
    controller.tick({Component.SHOOT}, set())

    controller.set_enabled(False)
    controller.tick(set(), set())

    assert controller.state is AutoReloadState.CANCELLED
    assert starts == []


def test_failed_fill_all_enters_fault_and_does_not_retry() -> None:
    attempts = 0

    def fail() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("simulated command failure")

    clock = FakeClock()
    controller = AutoReloadController(fail, delay_seconds=0.0, clock=clock)
    controller.set_enabled(True)
    controller.shoot_started()

    controller.tick({Component.SHOOT}, set())
    controller.tick(set(), set())

    assert controller.state is AutoReloadState.FAULT
    assert "simulated command failure" in controller.snapshot().detail
    controller.tick(set(), set())
    assert attempts == 1


def test_repeated_shoot_cannot_queue_another_sequence() -> None:
    controller = AutoReloadController(lambda: None)
    controller.set_enabled(True)
    controller.shoot_started()

    with pytest.raises(RuntimeError, match="already active"):
        controller.shoot_started()


def test_invalid_reload_delay_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        AutoReloadController(lambda: None, delay_seconds=-0.1)

    controller = AutoReloadController(lambda: None)
    with pytest.raises(ValueError, match="cannot be negative"):
        controller.set_delay(-1.0)
