from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QMessageBox

from iffs_ground_control.auto_reload import AutoReloadState
from iffs_ground_control.command_mapping import MAV_CMD_DO_SET_ACTUATOR
from iffs_ground_control.gui import MainWindow
from iffs_ground_control.mavlink_client import MavlinkEvent
from iffs_ground_control.models import AppConfig, Component, OperatingMode
from iffs_ground_control.rc import RCAction, RCSwitchState


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication(["iffs-gui-tests"])
    return app


@pytest.fixture
def window(qt_app: QApplication) -> MainWindow:
    instance = MainWindow(AppConfig())
    yield instance
    instance.operation_timer.stop()
    instance.event_timer.stop()
    instance.telemetry_timer.stop()
    instance.rc_timer.stop()
    instance.deleteLater()
    qt_app.processEvents()


def test_iffs_authorization_defaults_to_safe(window: MainWindow) -> None:
    assert window.safety.mode is OperatingMode.SIMULATION
    assert not window.safety.iffs_armed
    assert not window.iffs_arm_button.isChecked()
    assert window.iffs_arm_button.text() == "IFFS SAFE"
    assert not window.shoot_card.button.isEnabled()


def test_combo_selection_normalizes_qt_string_data_and_authorizes_bench_mode(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QComboBox returns the str-based enum item data as a plain str. This is the
    # regression case that previously made the banner and checkbox disagree.
    assert isinstance(window.mode_combo.itemData(1), str)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    window.mode_combo.setCurrentIndex(1)

    assert window.safety.mode is OperatingMode.PWM_BENCH
    assert window.safety.bench_authorized
    assert not window.safety.iffs_armed


def test_iffs_arm_state_transitions_without_confirmation(window: MainWindow) -> None:
    window.iffs_arm_button.setChecked(True)
    assert window.safety.iffs_armed
    assert window.shoot_card.button.isEnabled()
    assert window.iffs_arm_button.text() == "IFFS ARMED"

    window.iffs_arm_button.setChecked(False)
    assert not window.safety.iffs_armed
    assert not window.shoot_card.button.isEnabled()


def test_connection_loss_automatically_returns_iffs_to_safe(window: MainWindow) -> None:
    window.iffs_arm_button.setChecked(True)
    window._ever_connected = True

    window._handle_connection_loss("test loss")

    assert not window.safety.iffs_armed
    assert window.safety.recovery_required
    assert not window.iffs_arm_button.isChecked()
    assert not window.connection_warning.isHidden()
    assert all(value is None for value in window.controller.state.values())
    assert not window.mode_combo.isEnabled()


def test_normal_heartbeat_does_not_clear_iffs_arm(window: MainWindow) -> None:
    window.iffs_arm_button.setChecked(True)
    window.client.events.put(MavlinkEvent("armed", {"armed": False}))

    window._poll_mavlink_events()

    assert window.safety.iffs_armed
    assert window.iffs_arm_button.isChecked()


def test_shoot_duration_control_uses_configured_limits(window: MainWindow) -> None:
    assert window.shoot_card.duration.minimum() == window.config.shoot_duration_min_seconds
    assert window.shoot_card.duration.maximum() == window.config.shoot_duration_max_seconds
    assert window.shoot_card.duration.value() == window.config.shoot_duration_seconds


def test_shoot_uses_iffs_arm_without_an_extra_confirmation(window: MainWindow) -> None:
    window.iffs_arm_button.setChecked(True)
    window.shoot_card.duration.setValue(1.4)

    window._shoot()

    assert Component.SHOOT in window.scheduler.active
    assert not window.shoot_card.duration.isEnabled()
    assert window.scheduler.remaining(Component.SHOOT) <= 1.4
    window._stop_all("test cleanup")


def test_unrelated_denied_ack_is_logged_as_debug(window: MainWindow) -> None:
    window.client.events.put(
        MavlinkEvent(
            "ack",
            {
                "command": 410,
                "command_name": "MAV_CMD_GET_HOME_POSITION",
                "result": 2,
                "owned": False,
                "target_system": 255,
                "target_component": 190,
            },
        )
    )

    window._poll_mavlink_events()

    last_line = window.log_view.toPlainText().splitlines()[-1]
    assert "[DEBUG]" in last_line
    assert "410 (MAV_CMD_GET_HOME_POSITION): DENIED" in last_line
    assert "recipient SYS 255 / COMP 190" in last_line


def test_denied_ack_for_iffs_command_is_logged_as_error(window: MainWindow) -> None:
    window.client.events.put(
        MavlinkEvent(
            "ack",
            {
                "command": MAV_CMD_DO_SET_ACTUATOR,
                "command_name": "MAV_CMD_DO_SET_ACTUATOR",
                "result": 2,
                "owned": True,
                "target_system": 252,
                "target_component": 190,
            },
        )
    )

    window._poll_mavlink_events()

    last_line = window.log_view.toPlainText().splitlines()[-1]
    assert "[ERROR]" in last_line
    assert "IFFS MAVLink ACK for command 187" in last_line


def _rc_channels(**overrides: int) -> dict[int, int]:
    by_action = {
        "water": 1000,
        "air": 1000,
        "fill_all": 1000,
        "shoot": 1000,
        "stop_all": 1000,
    }
    by_action.update(overrides)
    return {
        11: by_action["water"],
        12: by_action["air"],
        13: by_action["fill_all"],
        14: by_action["shoot"],
        15: by_action["stop_all"],
    }


def _send_rc(window: MainWindow, **overrides: int) -> None:
    window.client.events.put(
        MavlinkEvent("rc_channels", {"channels": _rc_channels(**overrides)})
    )
    window._poll_mavlink_events()


def test_rc_control_defaults_disabled_and_explains_no_data(window: MainWindow) -> None:
    assert not window.rc_controller.enabled
    assert not window.rc_enable_button.isChecked()
    assert not window.rc_enable_button.isEnabled()
    assert "RC CONTROL — DISABLED • NO DATA" in window.rc_group.title()
    assert "NO DATA" in window.rc_status_detail.text()


def test_high_rc_input_cannot_start_an_operation_while_disabled(window: MainWindow) -> None:
    _send_rc(window, water=1900)

    assert not window.rc_controller.enabled
    assert window.scheduler.active == set()


def test_rc_water_request_uses_existing_scheduler(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    assert window.rc_enable_button.isEnabled()
    window.rc_enable_button.setChecked(True)

    _send_rc(window, water=1900)

    assert window.rc_controller.enabled
    assert Component.WATER in window.scheduler.active
    assert Component.WATER in window._rc_started_components
    assert window.rc_controller.states[RCAction.WATER] is RCSwitchState.TRIGGERED
    window._stop_all("test cleanup")


def test_rc_loss_invokes_stop_all_when_an_rc_operation_is_active(
    window: MainWindow,
) -> None:
    window.safety.link_connected = True
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    _send_rc(window, water=1900)
    window.scheduler.start_fill(Component.AIR, 10.0)

    assert window.scheduler.active == {Component.WATER, Component.AIR}
    window.rc_monitor._last_update = 0.0
    window._poll_rc_status()

    assert not window.rc_controller.enabled
    assert window.scheduler.active == set()
    assert "SIGNAL STALE" in window.rc_status_detail.text()
    window._stop_all("test cleanup")


def test_rc_restoration_requires_explicit_reenable_and_a_fresh_low(
    window: MainWindow,
) -> None:
    window.safety.link_connected = True
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    window.rc_monitor._last_update = 0.0
    window._poll_rc_status()
    assert not window.rc_controller.enabled

    _send_rc(window, water=1900)
    assert window.scheduler.active == set()
    window.rc_enable_button.setChecked(True)
    assert window.scheduler.active == set()

    _send_rc(window, water=1000)
    _send_rc(window, water=1900)
    assert window.scheduler.active == {Component.WATER}
    window._stop_all("test cleanup")


def test_mavlink_loss_disables_rc_and_invalidates_received_data(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    window._ever_connected = True

    window._handle_connection_loss("test MAVLink loss")

    assert not window.rc_controller.enabled
    assert window.rc_monitor.snapshot().status.value == "NO DATA"
    assert window.safety.recovery_required


def test_mavlink_reconnect_does_not_restore_rc_authorization(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    window._ever_connected = True
    window._handle_connection_loss("test MAVLink loss")
    window.client.events.put(
        MavlinkEvent(
            "heartbeat", {"system_id": 1, "component_id": 1, "armed": False}
        )
    )
    window._poll_mavlink_events()

    assert not window.safety.recovery_required
    assert not window.rc_controller.enabled
    _send_rc(window, water=1900)
    window.rc_enable_button.setChecked(True)
    assert window.scheduler.active == set()

    _send_rc(window, water=1000)
    _send_rc(window, water=1900)
    assert window.scheduler.active == {Component.WATER}
    window._stop_all("test cleanup")


def test_rc_conflict_is_rejected_by_existing_scheduler(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    _send_rc(window, water=1900)
    _send_rc(window, water=1000, fill_all=1900)

    assert window.scheduler.active == {Component.WATER}
    assert window.rc_controller.states[RCAction.FILL_ALL] is RCSwitchState.BLOCKED
    window._stop_all("test cleanup")


def test_safe_to_arm_does_not_fire_a_shoot_switch_already_high(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    _send_rc(window, shoot=1900)
    assert Component.SHOOT not in window.scheduler.active
    assert window.rc_controller.states[RCAction.SHOOT] is RCSwitchState.BLOCKED

    window.iffs_arm_button.setChecked(True)
    _send_rc(window, shoot=1900)
    assert Component.SHOOT not in window.scheduler.active
    assert window.rc_controller.states[RCAction.SHOOT] is RCSwitchState.WAITING_FOR_LOW

    _send_rc(window, shoot=1000)
    _send_rc(window, shoot=1900)
    assert Component.SHOOT in window.scheduler.active
    window._stop_all("test cleanup")


def test_optional_rc_rate_request_is_sent_only_once_per_connection(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = MainWindow(AppConfig(rc_request_rate_hz=5.0))
    requests: list[tuple[int, float]] = []
    monkeypatch.setattr(
        instance.client,
        "request_message_interval",
        lambda message_id, rate: requests.append((message_id, rate)),
    )
    heartbeat = MavlinkEvent(
        "heartbeat", {"system_id": 1, "component_id": 1, "armed": False}
    )
    instance.client.events.put(heartbeat)
    instance.client.events.put(heartbeat)

    instance._poll_mavlink_events()

    assert requests == [(65, 5.0)]
    instance.operation_timer.stop()
    instance.event_timer.stop()
    instance.telemetry_timer.stop()
    instance.rc_timer.stop()
    instance.deleteLater()
    qt_app.processEvents()


def _prepare_auto_reload_stage(window: MainWindow, stage: str) -> None:
    window.auto_reload_button.setChecked(True)
    window.iffs_arm_button.setChecked(True)
    window._shoot()
    assert window.auto_reload.state is AutoReloadState.SHOOTING
    if stage in {"waiting", "reloading"}:
        window.scheduler._deadlines[Component.SHOOT] = 0.0
        window._tick()
        assert window.auto_reload.state is AutoReloadState.WAITING
    if stage == "reloading":
        window.auto_reload._deadline = 0.0
        window._tick()
        assert window.auto_reload.state is AutoReloadState.RELOADING


def test_auto_reload_defaults_off_and_delay_is_loaded(qt_app: QApplication) -> None:
    instance = MainWindow(AppConfig(reload_delay_seconds=2.5))

    assert not instance.auto_reload.enabled
    assert not instance.auto_reload_button.isChecked()
    assert instance.reload_delay_spin.value() == 2.5
    assert instance.auto_reload_state.text() == "IDLE"

    instance.operation_timer.stop()
    instance.event_timer.stop()
    instance.telemetry_timer.stop()
    instance.rc_timer.stop()
    instance.deleteLater()
    qt_app.processEvents()


def test_gui_shoot_runs_auto_reload_through_existing_fill_all(window: MainWindow) -> None:
    _prepare_auto_reload_stage(window, "reloading")

    assert window.scheduler.active == {Component.WATER, Component.AIR}
    assert not window.shoot_card.button.isEnabled()
    assert "W " in window.auto_reload_countdown.text()
    window._stop_all("test cleanup")


@pytest.mark.parametrize("stage", ["shooting", "waiting", "reloading"])
def test_stop_all_cancels_auto_reload_at_every_stage(
    window: MainWindow, stage: str
) -> None:
    _prepare_auto_reload_stage(window, stage)

    window._stop_all(f"test stop during {stage}")

    assert window.auto_reload.state is AutoReloadState.CANCELLED
    assert window.scheduler.active == set()


@pytest.mark.parametrize("stage", ["shooting", "waiting", "reloading"])
def test_connection_loss_cancels_auto_reload_without_restart(
    window: MainWindow, stage: str
) -> None:
    _prepare_auto_reload_stage(window, stage)
    window._ever_connected = True

    window._handle_connection_loss(f"test loss during {stage}")
    assert window.auto_reload.state is AutoReloadState.CANCELLED
    assert window.scheduler.active == set()

    window.client.events.put(
        MavlinkEvent(
            "heartbeat", {"system_id": 1, "component_id": 1, "armed": False}
        )
    )
    window._poll_mavlink_events()
    assert window.scheduler.active == set()
    assert window.auto_reload.state is not AutoReloadState.RELOADING


def test_disabling_auto_reload_during_delay_cancels_pending_fill(window: MainWindow) -> None:
    _prepare_auto_reload_stage(window, "waiting")

    window.auto_reload_button.setChecked(False)
    window.auto_reload._deadline = 0.0
    window._tick()

    assert window.auto_reload.state is AutoReloadState.CANCELLED
    assert window.scheduler.active == set()


def test_rc_shoot_starts_the_same_auto_reload_sequence(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    window.auto_reload_button.setChecked(True)
    window.iffs_arm_button.setChecked(True)

    _send_rc(window, shoot=1000)
    _send_rc(window, shoot=1900)

    assert window.scheduler.active == {Component.SHOOT}
    assert window.auto_reload.state is AutoReloadState.SHOOTING
    assert window._auto_reload_rc_origin
    window._stop_all("test cleanup")


def test_failed_shoot_does_not_start_auto_reload(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.auto_reload_button.setChecked(True)
    window.iffs_arm_button.setChecked(True)
    monkeypatch.setattr(
        window.scheduler,
        "start_shoot",
        lambda _duration: (_ for _ in ()).throw(RuntimeError("simulated failure")),
    )
    monkeypatch.setattr(window, "_show_error", lambda _message: None)

    window._shoot()

    assert window.auto_reload.state is AutoReloadState.IDLE
    assert window.scheduler.active == set()


def test_rc_stop_cancels_pending_auto_reload(window: MainWindow) -> None:
    window.rc_panel_toggle.setChecked(True)
    _send_rc(window)
    window.rc_enable_button.setChecked(True)
    window.auto_reload_button.setChecked(True)
    window.iffs_arm_button.setChecked(True)
    _send_rc(window, shoot=1000)
    _send_rc(window, shoot=1900)
    window.scheduler._deadlines[Component.SHOOT] = 0.0
    window._tick()
    assert window.auto_reload.state is AutoReloadState.WAITING

    _send_rc(window, shoot=1900, stop_all=1900)

    assert window.auto_reload.state is AutoReloadState.CANCELLED
    assert window.scheduler.active == set()


def test_compact_controls_and_collapsed_secondary_panels(
    window: MainWindow, qt_app: QApplication
) -> None:
    window.resize(1366, 768)
    window.show()
    qt_app.processEvents()

    assert window.water_card.duration.geometry().left() > window.water_card.countdown.geometry().left()
    assert window.water_card.duration.geometry().top() < window.water_card.countdown.geometry().bottom()
    assert not window.rc_content.isVisible()
    assert not window.log_view.isVisible()
    assert window.stop_button.isVisible()
    assert window.stop_button.mapTo(window, QPoint(0, 0)).y() + window.stop_button.height() <= window.height()


def test_primary_controls_fit_without_scrolling_at_1920_by_1080(
    window: MainWindow, qt_app: QApplication
) -> None:
    window.resize(1920, 1040)
    window.show()
    qt_app.processEvents()

    assert window.main_scroll.verticalScrollBar().maximum() == 0
