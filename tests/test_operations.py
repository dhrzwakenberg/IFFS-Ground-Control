from __future__ import annotations

import math

import pytest

from iffs_ground_control.actuator import ActuatorController
from iffs_ground_control.models import Component, OperatingMode, default_actuators
from iffs_ground_control.operations import OperationScheduler
from iffs_ground_control.safety import SafetyMonitor


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    def __init__(self) -> None:
        self.commands: list[tuple[float, ...]] = []

    def send_actuator_command(self, params: tuple[float, ...]) -> None:
        self.commands.append(params)


def make_scheduler(
    *, mode: OperatingMode = OperatingMode.SIMULATION
) -> tuple[OperationScheduler, ActuatorController, SafetyMonitor, FakeTransport, FakeClock]:
    safety = SafetyMonitor()
    safety.mode = mode
    transport = FakeTransport()
    controller = ActuatorController(default_actuators(), transport, safety)
    clock = FakeClock()
    return OperationScheduler(controller, safety, clock), controller, safety, transport, clock


def test_fill_all_tracks_independent_monotonic_deadlines() -> None:
    scheduler, controller, _safety, _transport, clock = make_scheduler()
    scheduler.start_fill_all(2.0, 5.0)

    assert scheduler.active == {Component.WATER, Component.AIR}
    assert controller.state[Component.WATER]
    assert controller.state[Component.AIR]

    clock.advance(2.1)
    scheduler.tick()
    assert scheduler.active == {Component.AIR}
    assert not controller.state[Component.WATER]
    assert controller.state[Component.AIR]
    assert scheduler.remaining(Component.AIR) == pytest.approx(2.9)

    clock.advance(3.0)
    scheduler.tick()
    assert scheduler.active == set()
    assert not controller.state[Component.AIR]


def test_conflicting_shoot_is_prevented() -> None:
    scheduler, _controller, safety, _transport, _clock = make_scheduler()
    safety.iffs_armed = True
    scheduler.start_fill(Component.WATER, 3.0)

    with pytest.raises(RuntimeError, match="requires all other operations"):
        scheduler.start_shoot()


def test_shoot_requires_authorization_and_ends_after_one_second() -> None:
    scheduler, controller, safety, _transport, clock = make_scheduler()
    with pytest.raises(RuntimeError, match="SAFE"):
        scheduler.start_shoot()

    safety.iffs_armed = True
    scheduler.start_shoot()
    assert controller.state[Component.SHOOT]
    clock.advance(0.99)
    scheduler.tick()
    assert controller.state[Component.SHOOT]
    clock.advance(0.02)
    scheduler.tick()
    assert not controller.state[Component.SHOOT]


def test_bench_mode_transmits_normalized_values() -> None:
    scheduler, _controller, safety, transport, _clock = make_scheduler(
        mode=OperatingMode.PWM_BENCH
    )
    safety.link_connected = True
    safety.bench_authorized = True
    scheduler.start_fill(Component.AIR, 4.0)

    assert len(transport.commands) == 1
    command = transport.commands[0]
    assert math.isnan(command[0])
    assert command[1] == 1.0
    assert math.isnan(command[2])


def test_communication_failure_cancels_and_blocks_operations() -> None:
    scheduler, controller, safety, transport, _clock = make_scheduler(
        mode=OperatingMode.PWM_BENCH
    )
    safety.link_connected = True
    safety.bench_authorized = True
    scheduler.start_fill(Component.WATER, 10.0)
    assert len(transport.commands) == 1

    safety.link_connected = False
    scheduler.communication_failed()

    assert scheduler.active == set()
    assert all(value is None for value in controller.state.values())
    # No physical-state claim is made: the inactive request cannot be sent on a lost link.
    assert len(transport.commands) == 1
    with pytest.raises(RuntimeError, match="heartbeat"):
        scheduler.start_fill(Component.WATER, 1.0)


@pytest.mark.parametrize(
    ("starter", "expected_active"),
    [
        (lambda scheduler: scheduler.start_fill(Component.WATER, 5.0), {Component.WATER}),
        (lambda scheduler: scheduler.start_fill(Component.AIR, 5.0), {Component.AIR}),
        (
            lambda scheduler: scheduler.start_fill_all(5.0, 6.0),
            {Component.WATER, Component.AIR},
        ),
        (lambda scheduler: scheduler.start_shoot(2.0), {Component.SHOOT}),
    ],
    ids=["water", "air", "fill-all", "shoot"],
)
def test_connection_loss_marks_every_actuator_unknown(starter, expected_active) -> None:
    scheduler, controller, safety, _transport, _clock = make_scheduler()
    safety.iffs_armed = True
    starter(scheduler)
    assert scheduler.active == expected_active

    scheduler.communication_failed()

    assert scheduler.active == set()
    assert all(value is None for value in controller.state.values())


def test_reconnection_requests_all_outputs_inactive_without_resuming() -> None:
    scheduler, controller, safety, transport, _clock = make_scheduler(
        mode=OperatingMode.PWM_BENCH
    )
    safety.link_connected = True
    safety.bench_authorized = True
    scheduler.start_fill(Component.WATER, 10.0)
    safety.link_connected = False
    safety.recovery_required = True
    scheduler.communication_failed()

    safety.link_connected = True
    controller.request_recovery_inactive()
    safety.recovery_required = False

    assert scheduler.active == set()
    assert all(value is False for value in controller.state.values())
    assert transport.commands[-1][:3] == (-1.0, -1.0, -1.0)


def test_stop_all_during_link_recovery_preserves_unknown_state() -> None:
    scheduler, controller, safety, _transport, _clock = make_scheduler(
        mode=OperatingMode.PWM_BENCH
    )
    safety.link_connected = True
    safety.bench_authorized = True
    scheduler.start_fill(Component.WATER, 10.0)
    safety.link_connected = False
    safety.recovery_required = True
    scheduler.communication_failed()

    scheduler.stop_all()

    assert all(value is None for value in controller.state.values())


def test_repeated_disconnect_reconnect_cycles_remain_fail_closed() -> None:
    scheduler, controller, safety, transport, _clock = make_scheduler(
        mode=OperatingMode.PWM_BENCH
    )
    safety.link_connected = True
    safety.bench_authorized = True

    for _cycle in range(2):
        scheduler.start_fill(Component.AIR, 5.0)
        safety.link_connected = False
        safety.recovery_required = True
        scheduler.communication_failed()
        assert all(value is None for value in controller.state.values())
        safety.link_connected = True
        controller.request_recovery_inactive()
        safety.recovery_required = False
        assert scheduler.active == set()
        assert all(value is False for value in controller.state.values())

    recovery_commands = [command for command in transport.commands if command[:3] == (-1.0, -1.0, -1.0)]
    assert len(recovery_commands) == 2


def test_adjustable_shoot_duration_uses_monotonic_deadline() -> None:
    scheduler, controller, safety, _transport, clock = make_scheduler()
    safety.iffs_armed = True
    scheduler.start_shoot(2.5)

    clock.advance(2.49)
    scheduler.tick()
    assert controller.state[Component.SHOOT] is True
    clock.advance(0.02)
    scheduler.tick()
    assert controller.state[Component.SHOOT] is False


def test_stop_all_sends_one_inactive_request_when_connected() -> None:
    scheduler, controller, safety, transport, _clock = make_scheduler(
        mode=OperatingMode.PWM_BENCH
    )
    safety.link_connected = True
    safety.bench_authorized = True
    scheduler.start_fill_all(5.0, 5.0)
    scheduler.stop_all()

    assert scheduler.active == set()
    assert not any(controller.state.values())
    assert len(transport.commands) == 2
    stop = transport.commands[-1]
    assert stop[:3] == (-1.0, -1.0, -1.0)
