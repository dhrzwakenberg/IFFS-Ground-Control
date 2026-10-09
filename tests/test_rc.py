from __future__ import annotations

from types import SimpleNamespace

from iffs_ground_control.rc import (
    RCAction,
    RCInputMonitor,
    RCStatus,
    RCSwitchState,
    RCTriggerController,
    decode_rc_channels,
)

CHANNELS = {
    "water": 11,
    "air": 12,
    "fill_all": 13,
    "shoot": 14,
    "stop_all": 15,
}


def values(default: int = 1000, **overrides: int) -> dict[RCAction, int]:
    result = {action: default for action in RCAction}
    result.update({RCAction(name): value for name, value in overrides.items()})
    return result


def test_decode_rc_channels_honors_count_and_unavailable_marker() -> None:
    message = SimpleNamespace(chancount=3, chan1_raw=1000, chan2_raw=0xFFFF, chan3_raw=1800)

    assert decode_rc_channels(message) == {1: 1000, 3: 1800}


def test_rc_input_monitor_distinguishes_missing_invalid_and_stale_data() -> None:
    monitor = RCInputMonitor(CHANNELS, timeout_seconds=1.0, clock=lambda: 10.0)
    assert monitor.snapshot().status is RCStatus.NO_DATA

    monitor.ingest({11: 1000}, now=10.0)
    assert monitor.snapshot(now=10.1).status is RCStatus.MISSING

    monitor.ingest({11: 1000, 12: 1000, 13: 0, 14: 1000, 15: 1000}, now=10.0)
    assert monitor.snapshot(now=10.1).status is RCStatus.INVALID

    monitor.ingest({channel: 1000 for channel in CHANNELS.values()}, now=10.0)
    assert monitor.snapshot(now=10.5).status is RCStatus.CONNECTED
    assert monitor.snapshot(now=11.01).status is RCStatus.STALE


def test_initial_high_is_ignored_until_switch_returns_low() -> None:
    dispatched: list[RCAction] = []
    controller = RCTriggerController(
        lambda action: (dispatched.append(action) is None, "ok")
    )
    controller.enable()

    controller.process(values(water=1900))
    controller.process(values(water=1900))
    assert dispatched == []
    assert controller.states[RCAction.WATER] is RCSwitchState.WAITING_FOR_LOW

    controller.process(values(water=1000))
    controller.process(values(water=1500))
    controller.process(values(water=1900))
    controller.process(values(water=1900))
    assert dispatched == [RCAction.WATER]
    assert controller.states[RCAction.WATER] is RCSwitchState.TRIGGERED


def test_hysteresis_requires_low_reset_before_retrigger() -> None:
    dispatched: list[RCAction] = []
    controller = RCTriggerController(
        lambda action: (not dispatched.append(action), "ok")
    )
    controller.enable()

    controller.process(values())
    controller.process(values(air=1800))
    controller.process(values(air=1600))
    controller.process(values(air=1800))
    assert dispatched == [RCAction.AIR]
    assert controller.states[RCAction.AIR] is RCSwitchState.WAITING_FOR_RESET

    controller.process(values(air=1200))
    controller.process(values(air=1800))
    assert dispatched == [RCAction.AIR, RCAction.AIR]


def test_stop_has_priority_over_simultaneous_activation_edges() -> None:
    dispatched: list[RCAction] = []
    controller = RCTriggerController(
        lambda action: (not dispatched.append(action), "ok")
    )
    controller.enable()
    controller.process(values())

    controller.process(values(water=1900, stop_all=1900))

    assert dispatched == [RCAction.STOP_ALL]
    assert controller.states[RCAction.WATER] is RCSwitchState.BLOCKED
    assert controller.states[RCAction.STOP_ALL] is RCSwitchState.TRIGGERED


def test_blocked_action_cannot_retry_until_low() -> None:
    attempts = 0

    def dispatch(_action: RCAction) -> tuple[bool, str]:
        nonlocal attempts
        attempts += 1
        return False, "safety interlock"

    controller = RCTriggerController(dispatch)
    controller.enable()
    controller.process(values())
    controller.process(values(shoot=1900))
    controller.process(values(shoot=1900))
    controller.process(values(shoot=1500))
    assert attempts == 1
    controller.process(values(shoot=1000))
    controller.process(values(shoot=1900))

    assert attempts == 2
    assert controller.states[RCAction.SHOOT] is RCSwitchState.BLOCKED


def test_reenable_requires_a_new_low_observation() -> None:
    dispatched: list[RCAction] = []
    controller = RCTriggerController(
        lambda action: (not dispatched.append(action), "ok")
    )
    controller.enable()
    controller.process(values())
    controller.process(values(fill_all=1900))
    controller.disable()
    controller.enable()
    controller.process(values(fill_all=1900))

    assert dispatched == [RCAction.FILL_ALL]
