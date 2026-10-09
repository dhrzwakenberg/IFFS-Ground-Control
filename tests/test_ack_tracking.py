from iffs_ground_control.command_mapping import MAV_CMD_DO_SET_ACTUATOR
from iffs_ground_control.mavlink_client import (
    MAV_CMD_SET_MESSAGE_INTERVAL,
    MAVLINK_MSG_ID_RC_CHANNELS,
    CommandAckTracker,
    LinkSnapshot,
    MavlinkClient,
)


def test_ack_for_recorded_iffs_command_is_owned_and_consumed() -> None:
    tracker = CommandAckTracker(252, 190)
    tracker.record(MAV_CMD_DO_SET_ACTUATOR, now=10.0)

    assert tracker.owns_ack(
        MAV_CMD_DO_SET_ACTUATOR,
        0,
        target_system=252,
        target_component=190,
        now=10.1,
    )
    assert tracker.pending_count(MAV_CMD_DO_SET_ACTUATOR) == 0


def test_command_410_without_iffs_request_is_unrelated() -> None:
    tracker = CommandAckTracker(252, 190)

    assert not tracker.owns_ack(
        410,
        2,
        target_system=255,
        target_component=190,
        now=10.0,
    )


def test_ack_addressed_to_another_gcs_does_not_consume_pending_command() -> None:
    tracker = CommandAckTracker(252, 190)
    tracker.record(MAV_CMD_DO_SET_ACTUATOR, now=10.0)

    assert not tracker.owns_ack(
        MAV_CMD_DO_SET_ACTUATOR,
        2,
        target_system=255,
        target_component=190,
        now=10.1,
    )
    assert tracker.pending_count(MAV_CMD_DO_SET_ACTUATOR) == 1
    assert tracker.owns_ack(
        MAV_CMD_DO_SET_ACTUATOR,
        0,
        target_system=252,
        target_component=190,
        now=10.2,
    )


def test_untargeted_ack_falls_back_to_pending_command_correlation() -> None:
    tracker = CommandAckTracker(252, 190)
    tracker.record(MAV_CMD_DO_SET_ACTUATOR, now=10.0)

    assert tracker.owns_ack(
        MAV_CMD_DO_SET_ACTUATOR,
        0,
        target_system=0,
        target_component=0,
        now=10.1,
    )


def test_in_progress_ack_keeps_command_pending_until_final_result() -> None:
    tracker = CommandAckTracker(252, 190)
    tracker.record(MAV_CMD_DO_SET_ACTUATOR, now=10.0)

    assert tracker.owns_ack(MAV_CMD_DO_SET_ACTUATOR, 5, now=10.1)
    assert tracker.pending_count(MAV_CMD_DO_SET_ACTUATOR) == 1
    assert tracker.owns_ack(MAV_CMD_DO_SET_ACTUATOR, 0, now=10.2)
    assert tracker.pending_count(MAV_CMD_DO_SET_ACTUATOR) == 0


def test_expired_pending_command_does_not_claim_late_ack() -> None:
    tracker = CommandAckTracker(252, 190, pending_timeout=2.0)
    tracker.record(MAV_CMD_DO_SET_ACTUATOR, now=10.0)

    assert not tracker.owns_ack(MAV_CMD_DO_SET_ACTUATOR, 2, now=12.1)


def test_cancel_removes_command_when_send_fails() -> None:
    tracker = CommandAckTracker(252, 190)
    token = tracker.record(MAV_CMD_DO_SET_ACTUATOR, now=10.0)
    tracker.cancel(MAV_CMD_DO_SET_ACTUATOR, token)

    assert tracker.pending_count(MAV_CMD_DO_SET_ACTUATOR) == 0


def test_rc_message_interval_request_uses_expected_command_parameters() -> None:
    calls: list[tuple[object, ...]] = []

    class FakeMav:
        def command_long_send(self, *args: object) -> None:
            calls.append(args)

    class FakeConnection:
        mav = FakeMav()

    client = MavlinkClient()
    client._snapshot = LinkSnapshot(
        running=True, connected=True, target_system=1, target_component=1, armed=False
    )
    client._connection = FakeConnection()

    client.request_message_interval(MAVLINK_MSG_ID_RC_CHANNELS, 5.0)

    assert calls == [
        (1, 1, MAV_CMD_SET_MESSAGE_INTERVAL, 0, 65.0, 200000.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ]
    assert client._ack_tracker.pending_count(MAV_CMD_SET_MESSAGE_INTERVAL) == 1
