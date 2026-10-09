from __future__ import annotations

import html
import queue
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .actuator import ActuatorController
from .auto_reload import AutoReloadController, AutoReloadState
from .config import save_config
from .mavlink_client import ACK_RESULT_NAMES, MAVLINK_MSG_ID_RC_CHANNELS, MavlinkClient
from .models import AppConfig, Component, OperatingMode
from .operations import OperationScheduler
from .rc import (
    RCAction,
    RCInputMonitor,
    RCInputSnapshot,
    RCStatus,
    RCSwitchState,
    RCTriggerController,
)
from .safety import SafetyMonitor
from .telemetry import FlightTelemetrySnapshot, TelemetryStore

STYLE = """
QWidget { background: #111827; color: #e5e7eb; font-family: "Segoe UI"; font-size: 10pt; }
QMainWindow { background: #0b1220; }
QGroupBox { background: #172033; border: 1px solid #2b3953; border-radius: 9px;
            margin-top: 10px; padding: 10px 9px 8px 9px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #b7c5dc; }
QPushButton { background: #2563eb; border: none; border-radius: 7px; padding: 9px 14px;
              color: white; font-weight: 600; min-height: 22px; }
QPushButton:hover { background: #3b82f6; }
QPushButton:pressed { background: #1d4ed8; }
QPushButton:disabled { background: #334155; color: #8190a5; }
QPushButton#stopButton { background: #dc2626; font-size: 13pt; padding: 13px 24px; }
QPushButton#stopButton:hover { background: #ef4444; }
QPushButton#shootButton { background: #b45309; }
QPushButton#shootButton:hover { background: #d97706; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #0f172a; border: 1px solid #3b4b67;
    border-radius: 6px; padding: 6px; selection-background-color: #2563eb; }
QTextEdit { background: #080f1c; border: 1px solid #2b3953; border-radius: 8px;
            color: #cbd5e1; font-family: Consolas; font-size: 9pt; }
QLabel#title { font-size: 18pt; font-weight: 700; color: white; }
QLabel#subtitle { color: #91a0b7; }
QLabel#warning { background: #3a2409; color: #fbbf24; border: 1px solid #8a5a16;
                 border-radius: 8px; padding: 7px; font-weight: 600; }
QLabel#countdown { font-size: 15pt; font-weight: 700; color: #dbeafe; }
QLabel#modeBanner { border-radius: 7px; padding: 8px; font-weight: 700; }
QLabel#connectionWarning { background: #541d25; color: #fecdd3; border: 2px solid #e11d48;
                           border-radius: 8px; padding: 11px; font-weight: 700; }
QPushButton#iffsArm { font-size: 13pt; min-width: 180px; padding: 12px 18px; }
QPushButton#autoReload { font-size: 11pt; min-width: 150px; padding: 9px 14px; }
"""


class StatusPill(QLabel):
    COLORS = {
        "neutral": ("#233047", "#aebbd0"),
        "good": ("#123b2b", "#6ee7b7"),
        "warn": ("#46320f", "#fcd34d"),
        "bad": ("#491c24", "#fda4af"),
        "active": ("#173c67", "#93c5fd"),
    }

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(105)
        self.set_state(text, "neutral")

    def set_state(self, text: str, state: str) -> None:
        background, foreground = self.COLORS[state]
        self.setText(text)
        self.setStyleSheet(
            f"background:{background};color:{foreground};border-radius:10px;"
            "padding:4px 9px;font-weight:600;"
        )


class OperationCard(QGroupBox):
    def __init__(self, title: str, button_text: str, *, duration: bool = True) -> None:
        super().__init__(title)
        self.setMinimumHeight(76)
        self.setMaximumHeight(88)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(8)
        self.button = QPushButton(button_text)
        self.button.setMinimumWidth(90)
        layout.addWidget(self.button)
        self.state = StatusPill("INACTIVE")
        self.state.setMinimumWidth(120)
        layout.addWidget(self.state)
        self.timer_label = QLabel("Timer")
        layout.addWidget(self.timer_label)
        self.countdown = QLabel("0.0 s")
        self.countdown.setObjectName("countdown")
        self.countdown.setMinimumWidth(72)
        layout.addWidget(self.countdown)

        self.duration_label = QLabel("Duration")
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.1, 3600.0)
        self.duration.setDecimals(1)
        self.duration.setSuffix(" s")
        self.duration.setMinimumWidth(88)
        if duration:
            layout.addWidget(self.duration_label)
            layout.addWidget(self.duration)
        else:
            self.duration_label.hide()
            self.duration.hide()
        layout.addStretch()

    def update_operation(self, active: bool, remaining: float, state: bool | None) -> None:
        if state is None:
            self.state.set_state("UNKNOWN", "bad")
        elif active or state:
            self.state.set_state("ACTIVE REQUESTED", "active")
        else:
            self.state.set_state("INACTIVE REQUESTED", "neutral")
        self.countdown.setText(f"{remaining:.1f} s")


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, startup_warning: str | None = None) -> None:
        super().__init__()
        self.config = config
        self.client = MavlinkClient()
        self.safety = SafetyMonitor()
        self.telemetry = TelemetryStore(
            stale_after=max(2.0, config.heartbeat_timeout_seconds), battery_stale_after=10.0
        )
        self.rc_monitor = RCInputMonitor(
            config.rc_channels, timeout_seconds=config.rc_timeout_seconds
        )
        self.rc_controller = RCTriggerController(
            self._dispatch_rc_action,
            low_threshold=config.rc_low_threshold,
            high_threshold=config.rc_high_threshold,
        )
        self._rc_started_components: set[Component] = set()
        self._auto_reload_rc_origin = False
        self._last_rc_status = RCStatus.NO_DATA
        self._rc_rate_requested = False
        self._ever_connected = False
        self.controller = ActuatorController(
            config.actuators,
            self.client,
            self.safety,
            log=self.add_log,
            state_changed=self._actuator_state_changed,
        )
        self.scheduler = OperationScheduler(self.controller, self.safety)
        self.auto_reload = AutoReloadController(
            self._start_auto_reload_fill_all,
            delay_seconds=config.reload_delay_seconds,
        )
        self._last_armed: bool | None = None
        self._build_ui()
        self._apply_values()
        self._set_mode(OperatingMode.SIMULATION)
        self._refresh_rc_ui()
        self._refresh_auto_reload_ui()

        self.operation_timer = QTimer(self)
        self.operation_timer.timeout.connect(self._tick)
        self.operation_timer.start(50)
        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self._poll_mavlink_events)
        self.event_timer.start(100)
        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self._refresh_telemetry)
        self.telemetry_timer.start(250)
        self.rc_timer = QTimer(self)
        self.rc_timer.timeout.connect(self._poll_rc_status)
        self.rc_timer.start(100)

        self.add_log("INFO", "Application started in Simulation Mode; no MAVLink commands will be sent")
        if startup_warning:
            self.add_log("ERROR", startup_warning)

    def _build_ui(self) -> None:
        self.setWindowTitle("IFFS Ground Control")
        self.resize(1180, 900)
        self.setMinimumSize(940, 720)
        self.setStyleSheet(STYLE)
        shell = QWidget()
        self.setCentralWidget(shell)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 8)
        shell_layout.setSpacing(6)
        scroll = QScrollArea()
        self.main_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        central = QWidget()
        scroll.setWidget(central)
        shell_layout.addWidget(scroll, 1)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 8)
        root.setSpacing(6)

        title_row = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("IFFS Ground Control")
        title.setObjectName("title")
        subtitle = QLabel("Impulse Fire Fighting System • operator console")
        subtitle.setObjectName("subtitle")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_row.addLayout(title_block)
        title_row.addStretch()
        self.link_pill = StatusPill("DISCONNECTED")
        self.arm_pill = StatusPill("ARM STATE —")
        self.system_pill = StatusPill("SYS ID —")
        title_row.addWidget(self.link_pill)
        title_row.addWidget(self.arm_pill)
        title_row.addWidget(self.system_pill)
        root.addLayout(title_row)

        self.connection_warning = QLabel()
        self.connection_warning.setObjectName("connectionWarning")
        self.connection_warning.setWordWrap(True)
        self.connection_warning.hide()
        root.addWidget(self.connection_warning)

        telemetry_group = QGroupBox("FLIGHT TELEMETRY")
        telemetry_grid = QGridLayout(telemetry_group)
        self.flight_mode_value = QLabel("--")
        self.gps_value = QLabel("--")
        self.battery_value = QLabel("--")
        self.altitude_value = QLabel("--")
        self.speed_value = QLabel("--")
        self.heading_value = QLabel("--")
        telemetry_items = (
            ("PX4 mode", self.flight_mode_value),
            ("GPS", self.gps_value),
            ("Battery", self.battery_value),
            ("Relative altitude", self.altitude_value),
            ("Ground speed", self.speed_value),
            ("Heading", self.heading_value),
        )
        for column, (label, value) in enumerate(telemetry_items):
            caption = QLabel(label.upper())
            caption.setObjectName("subtitle")
            value.setStyleSheet("font-size:12pt;font-weight:700;color:#dbeafe;")
            telemetry_grid.addWidget(caption, 0, column)
            telemetry_grid.addWidget(value, 1, column)
        telemetry_grid.setColumnStretch(2, 2)
        root.addWidget(telemetry_group)

        warning = QLabel(
            "⚠ Software timers, stop commands, and acknowledgements are not safety mechanisms. "
            "Validate the physical interlock and independent electrical/pneumatic maximum-time protection."
        )
        warning.setObjectName("warning")
        warning.setWordWrap(True)
        root.addWidget(warning)

        setup = QGroupBox("MODE & CONNECTION")
        setup_grid = QGridLayout(setup)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Simulation — no commands transmitted", OperatingMode.SIMULATION)
        self.mode_combo.addItem("PWM Bench Test — live MAVLink commands", OperatingMode.PWM_BENCH)
        self.mode_combo.currentIndexChanged.connect(self._mode_selected)
        self.mode_banner = QLabel()
        self.mode_banner.setObjectName("modeBanner")
        self.connection_edit = QLineEdit()
        self.connection_edit.setPlaceholderText("udpin:127.0.0.1:14551")
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 4000000)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        setup_grid.addWidget(QLabel("Operating mode"), 0, 0)
        setup_grid.addWidget(self.mode_combo, 0, 1, 1, 2)
        setup_grid.addWidget(self.mode_banner, 0, 3)
        setup_grid.addWidget(QLabel("MAVLink endpoint"), 1, 0)
        setup_grid.addWidget(self.connection_edit, 1, 1)
        setup_grid.addWidget(QLabel("Baud"), 1, 2)
        setup_grid.addWidget(self.baud_spin, 1, 3)
        setup_grid.addWidget(self.connect_button, 1, 4)
        root.addWidget(setup)

        authorization = QGroupBox("IFFS SOFTWARE AUTHORIZATION")
        authorization_layout = QGridLayout(authorization)
        authorization_layout.setVerticalSpacing(6)
        self.iffs_arm_button = QPushButton("IFFS SAFE")
        self.iffs_arm_button.setObjectName("iffsArm")
        self.iffs_arm_button.setCheckable(True)
        self.iffs_arm_button.toggled.connect(self._iffs_arm_changed)
        self.iffs_arm_status = QLabel(
            "SAFE — Shoot disabled. This software state does not verify the physical interlock."
        )
        self.iffs_arm_status.setWordWrap(True)
        authorization_layout.addWidget(QLabel("IFFS ARM"), 0, 0)
        authorization_layout.addWidget(self.iffs_arm_button, 0, 1)
        authorization_layout.addWidget(self.iffs_arm_status, 0, 2, 1, 4)

        self.auto_reload_button = QPushButton("AUTO RELOAD OFF")
        self.auto_reload_button.setObjectName("autoReload")
        self.auto_reload_button.setCheckable(True)
        self.auto_reload_button.toggled.connect(self._auto_reload_changed)
        self.reload_delay_spin = QDoubleSpinBox()
        self.reload_delay_spin.setRange(0.0, 3600.0)
        self.reload_delay_spin.setDecimals(1)
        self.reload_delay_spin.setSuffix(" s")
        self.reload_delay_spin.setMinimumWidth(90)
        self.reload_delay_spin.valueChanged.connect(self._auto_reload_delay_changed)
        self.auto_reload_state = StatusPill("IDLE")
        self.auto_reload_countdown = QLabel("Delay 0.0 s")
        self.auto_reload_detail = QLabel("Automatic reload is off")
        self.auto_reload_detail.setObjectName("subtitle")
        authorization_layout.addWidget(QLabel("AUTO RELOAD"), 1, 0)
        authorization_layout.addWidget(self.auto_reload_button, 1, 1)
        authorization_layout.addWidget(QLabel("Delay"), 1, 2)
        authorization_layout.addWidget(self.reload_delay_spin, 1, 3)
        authorization_layout.addWidget(self.auto_reload_state, 1, 4)
        authorization_layout.addWidget(self.auto_reload_countdown, 1, 5)
        authorization_layout.addWidget(self.auto_reload_detail, 1, 6)
        authorization_layout.setColumnStretch(6, 1)
        root.addWidget(authorization)

        self.rc_group = QGroupBox("RC CONTROL — DISABLED • NO DATA")
        self.rc_group.setMaximumHeight(82)
        rc_group_layout = QVBoxLayout(self.rc_group)
        self.rc_panel_toggle = QPushButton("Show RC diagnostics")
        self.rc_panel_toggle.setCheckable(True)
        self.rc_panel_toggle.setStyleSheet(
            "background:#233047;color:#b7c5dc;text-align:left;padding:7px 12px;"
        )
        self.rc_panel_toggle.toggled.connect(self._toggle_rc_panel)
        rc_group_layout.addWidget(self.rc_panel_toggle)
        self.rc_content = QWidget()
        rc_layout = QGridLayout(self.rc_content)
        self.rc_enable_button = QPushButton("Enable RC control")
        self.rc_enable_button.setCheckable(True)
        self.rc_enable_button.toggled.connect(self._rc_enable_changed)
        self.rc_status_pill = StatusPill("NO DATA")
        self.rc_status_detail = QLabel(
            "Waiting for RC_CHANNELS data. RC control is session-only and never enables itself."
        )
        self.rc_status_detail.setWordWrap(True)
        rc_layout.addWidget(self.rc_enable_button, 0, 0)
        rc_layout.addWidget(self.rc_status_pill, 0, 1)
        rc_layout.addWidget(self.rc_status_detail, 0, 2, 1, 2)
        rc_layout.addWidget(QLabel("ACTION"), 1, 0)
        rc_layout.addWidget(QLabel("CHANNEL"), 1, 1)
        rc_layout.addWidget(QLabel("PWM"), 1, 2)
        rc_layout.addWidget(QLabel("SWITCH STATE"), 1, 3)
        self.rc_pwm_labels: dict[RCAction, QLabel] = {}
        self.rc_state_labels: dict[RCAction, QLabel] = {}
        for row, action in enumerate(RCAction, start=2):
            pwm = QLabel("—")
            state = QLabel(RCSwitchState.WAITING_FOR_LOW.value)
            self.rc_pwm_labels[action] = pwm
            self.rc_state_labels[action] = state
            rc_layout.addWidget(QLabel(action.label), row, 0)
            rc_layout.addWidget(QLabel(f"CH{self.rc_monitor.channels[action]}"), row, 1)
            rc_layout.addWidget(pwm, row, 2)
            rc_layout.addWidget(state, row, 3)
        rc_layout.setColumnStretch(3, 1)
        self.rc_content.setVisible(False)
        rc_group_layout.addWidget(self.rc_content)
        controls = QGridLayout()
        controls.setSpacing(7)
        self.water_card = OperationCard("WATER FILL • MAIN 1 / SET 1", "Water Fill")
        self.air_card = OperationCard("AIR FILL • MAIN 2 / SET 2", "Air Fill")
        self.fill_all_card = OperationCard("FILL ALL", "Fill All", duration=False)
        self.fill_all_card.countdown.setText("W 0.0 • A 0.0 s")
        self.shoot_card = OperationCard("SHOOT • MAIN 3 / SET 3", "Shoot")
        self.shoot_card.duration.setRange(
            self.config.shoot_duration_min_seconds, self.config.shoot_duration_max_seconds
        )
        self.shoot_card.button.setObjectName("shootButton")
        self.water_card.button.clicked.connect(self._water_fill)
        self.air_card.button.clicked.connect(self._air_fill)
        self.fill_all_card.button.clicked.connect(self._fill_all)
        self.shoot_card.button.clicked.connect(self._shoot)
        controls.addWidget(self.water_card, 0, 0)
        controls.addWidget(self.air_card, 0, 1)
        controls.addWidget(self.fill_all_card, 1, 0)
        controls.addWidget(self.shoot_card, 1, 1)
        root.addLayout(controls)

        root.addWidget(self.rc_group)

        self.log_group = QGroupBox("EVENT & COMMAND LOG — click to expand")
        self.log_group.setCheckable(True)
        self.log_group.setChecked(False)
        self.log_group.setMaximumHeight(42)
        log_layout = QVBoxLayout(self.log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(1000)
        self.log_view.setVisible(False)
        self.log_group.toggled.connect(self._toggle_log)
        log_layout.addWidget(self.log_view)
        root.addWidget(self.log_group)
        root.addStretch()

        stop_bar = QHBoxLayout()
        stop_bar.setContentsMargins(14, 0, 14, 0)
        self.stop_button = QPushButton("STOP ALL")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.clicked.connect(lambda: self._stop_all("Operator pressed STOP ALL"))
        self.operation_status = QLabel("READY")
        self.operation_status.setWordWrap(True)
        self.operation_status.setObjectName("subtitle")
        stop_bar.addWidget(self.stop_button)
        stop_bar.addWidget(self.operation_status, 1)
        shell_layout.addLayout(stop_bar)

    def _toggle_log(self, expanded: bool) -> None:
        self.log_view.setVisible(expanded)
        self.log_group.setMaximumHeight(240 if expanded else 42)

    def _auto_reload_changed(self, checked: bool) -> None:
        self.auto_reload.set_enabled(checked)
        self.add_log(
            "SAFETY",
            f"Automatic reload set to {'ON' if checked else 'OFF'} for this session",
        )
        self._refresh_auto_reload_ui()
        self._refresh_controls()

    def _auto_reload_delay_changed(self, seconds: float) -> None:
        try:
            self.auto_reload.set_delay(seconds)
        except ValueError as exc:
            self.add_log("ERROR", str(exc))
            return
        self.config.reload_delay_seconds = seconds
        self._refresh_auto_reload_ui()

    def _start_auto_reload_fill_all(self) -> None:
        self.scheduler.start_fill_all(
            self.water_card.duration.value(), self.air_card.duration.value()
        )
        if self._auto_reload_rc_origin:
            self._rc_started_components.update({Component.WATER, Component.AIR})
        self.add_log(
            "COMMAND",
            "Automatic reload started through existing Fill All operation",
        )

    def _refresh_auto_reload_ui(self) -> None:
        snapshot = self.auto_reload.snapshot()
        self.auto_reload_button.blockSignals(True)
        self.auto_reload_button.setChecked(snapshot.enabled)
        self.auto_reload_button.blockSignals(False)
        self.auto_reload_button.setText(
            "AUTO RELOAD ON" if snapshot.enabled else "AUTO RELOAD OFF"
        )
        self.auto_reload_button.setStyleSheet(
            (
                "background:#9a3412;color:white;border:2px solid #fb923c;"
                if snapshot.enabled
                else "background:#334155;color:#dbeafe;border:1px solid #64748b;"
            )
        )
        state_style = {
            AutoReloadState.IDLE: "neutral",
            AutoReloadState.SHOOTING: "active",
            AutoReloadState.WAITING: "warn",
            AutoReloadState.RELOADING: "active",
            AutoReloadState.COMPLETE: "good",
            AutoReloadState.CANCELLED: "warn",
            AutoReloadState.FAULT: "bad",
        }[snapshot.state]
        self.auto_reload_state.set_state(snapshot.state.value, state_style)
        if snapshot.state is AutoReloadState.WAITING:
            countdown = f"Delay {snapshot.delay_remaining:.1f} s"
        elif snapshot.state is AutoReloadState.RELOADING:
            countdown = (
                f"W {self.scheduler.remaining(Component.WATER):.1f} • "
                f"A {self.scheduler.remaining(Component.AIR):.1f} s"
            )
        else:
            countdown = "Delay —"
        self.auto_reload_countdown.setText(countdown)
        self.auto_reload_detail.setText(snapshot.detail)

    def _toggle_rc_panel(self, expanded: bool) -> None:
        self.rc_content.setVisible(expanded)
        self.rc_group.setMaximumHeight(310 if expanded else 82)
        self.rc_panel_toggle.setText(
            "Hide RC diagnostics" if expanded else "Show RC diagnostics"
        )

    def _rc_enable_changed(self, checked: bool) -> None:
        snapshot = self.rc_monitor.snapshot()
        if checked and not snapshot.usable:
            self.rc_enable_button.blockSignals(True)
            self.rc_enable_button.setChecked(False)
            self.rc_enable_button.blockSignals(False)
            self.add_log("SAFETY", f"RC control not enabled: {snapshot.status.value}")
            self._refresh_rc_ui(snapshot)
            return
        if checked:
            self.rc_controller.enable()
            self.add_log(
                "SAFETY",
                "RC control enabled for this session; every switch must be observed low before use",
            )
            self.rc_controller.process(snapshot.values)
        else:
            self._disable_rc("operator disabled RC control", stop_rc_operations=True)
        self._refresh_rc_ui(snapshot)

    def _disable_rc(self, reason: str, *, stop_rc_operations: bool) -> None:
        was_enabled = self.rc_controller.enabled
        self.rc_controller.disable()
        if hasattr(self, "rc_enable_button"):
            self.rc_enable_button.blockSignals(True)
            self.rc_enable_button.setChecked(False)
            self.rc_enable_button.blockSignals(False)
        if stop_rc_operations and (
            self.safety.link_connected or self.safety.mode is OperatingMode.SIMULATION
        ):
            self._stop_rc_operations(reason)
        if was_enabled:
            self.add_log("SAFETY", f"RC control disabled: {reason}")

    def _stop_rc_operations(self, reason: str) -> None:
        active = self._rc_started_components.intersection(self.scheduler.active)
        pending_rc_reload = (
            self._auto_reload_rc_origin and self.auto_reload.sequence_active
        )
        if not active and not pending_rc_reload:
            self._rc_started_components.clear()
            return
        names = (
            ", ".join(
                component.value.upper()
                for component in sorted(active, key=lambda component: component.value)
            )
            if active
            else "pending automatic reload"
        )
        self._stop_all(f"RC input unavailable during {names}: {reason}")
        self.add_log(
            "SAFETY",
            f"Invoked existing STOP ALL because RC input was lost during {names}",
        )

    def _poll_rc_status(self) -> None:
        snapshot = self.rc_monitor.snapshot()
        if snapshot.status is not RCStatus.CONNECTED and self.rc_controller.enabled:
            self._disable_rc(
                f"RC input unavailable ({snapshot.status.value})",
                stop_rc_operations=True,
            )
        self._last_rc_status = snapshot.status
        self._refresh_rc_ui(snapshot)

    def _refresh_rc_ui(self, snapshot: RCInputSnapshot | None = None) -> None:
        rc_snapshot = self.rc_monitor.snapshot() if snapshot is None else snapshot
        status = rc_snapshot.status
        status_style = {
            RCStatus.CONNECTED: "good",
            RCStatus.NO_DATA: "neutral",
            RCStatus.MISSING: "warn",
            RCStatus.INVALID: "bad",
            RCStatus.STALE: "bad",
        }[status]
        self.rc_status_pill.set_state(status.value, status_style)
        enabled = self.rc_controller.enabled
        self.rc_group.setTitle(
            f"RC CONTROL — {'ENABLED' if enabled else 'DISABLED'} • {status.value}"
        )
        self.rc_enable_button.setEnabled(
            rc_snapshot.usable and not self.safety.recovery_required
        )
        self.rc_enable_button.setText(
            "Disable RC control" if enabled else "Enable RC control"
        )
        if enabled:
            detail = (
                "Enabled. Low < "
                f"{self.config.rc_low_threshold}; trigger > {self.config.rc_high_threshold} µs. "
                "STOP ALL has priority."
            )
        elif status is RCStatus.CONNECTED:
            detail = "RC signal is valid. Explicitly enable RC control to accept switch requests."
        else:
            detail = (
                f"Authorization unavailable: {status.value}. RC control remains disabled; "
                "restore valid RC_CHANNELS data, then enable it explicitly."
            )
        self.rc_status_detail.setText(detail)
        for action in RCAction:
            value = rc_snapshot.values[action]
            self.rc_pwm_labels[action].setText("—" if value is None else f"{value} µs")
            self.rc_state_labels[action].setText(self.rc_controller.states[action].value)

    def _dispatch_rc_action(self, action: RCAction) -> tuple[bool, str]:
        try:
            if action is RCAction.STOP_ALL:
                self._stop_all("RC STOP ALL switch")
                self.add_log("SAFETY", "RC switch requested STOP ALL")
                return True, "STOP ALL requested"
            if self.auto_reload.sequence_active:
                raise RuntimeError("Automatic reload sequence is active")
            if action is RCAction.WATER:
                self.scheduler.start_fill(Component.WATER, self.water_card.duration.value())
                started = {Component.WATER}
            elif action is RCAction.AIR:
                self.scheduler.start_fill(Component.AIR, self.air_card.duration.value())
                started = {Component.AIR}
            elif action is RCAction.FILL_ALL:
                self.scheduler.start_fill_all(
                    self.water_card.duration.value(), self.air_card.duration.value()
                )
                started = {Component.WATER, Component.AIR}
            else:
                self.scheduler.start_shoot(self.shoot_card.duration.value())
                started = {Component.SHOOT}
                self._auto_reload_rc_origin = self.auto_reload.enabled
                self.auto_reload.shoot_started()
        except (RuntimeError, ValueError, OSError) as exc:
            reason = str(exc)
            self.add_log("SAFETY", f"RC {action.label} request blocked: {reason}")
            return False, reason
        self._rc_started_components.update(started)
        self.add_log("COMMAND", f"RC switch requested {action.label}")
        self._refresh_controls()
        return True, "Operation started"

    def _apply_values(self) -> None:
        self.connection_edit.setText(self.config.connection)
        self.baud_spin.setValue(self.config.baud)
        self.water_card.duration.setValue(self.config.water_duration_seconds)
        self.air_card.duration.setValue(self.config.air_duration_seconds)
        self.shoot_card.duration.setValue(self.config.shoot_duration_seconds)
        self.reload_delay_spin.setValue(self.config.reload_delay_seconds)

    def add_log(self, level: str, message: str) -> None:
        if not hasattr(self, "log_view"):
            return
        colors = {
            "ERROR": "#fda4af",
            "SAFETY": "#fbbf24",
            "COMMAND": "#93c5fd",
            "SIM": "#c4b5fd",
            "INFO": "#a7f3d0",
            "DEBUG": "#64748b",
        }
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = colors.get(level, "#cbd5e1")
        safe_message = html.escape(message)
        self.log_view.append(
            f'<span style="color:#64748b">{timestamp}</span> '
            f'<b style="color:{color}">[{html.escape(level)}]</b> {safe_message}'
        )

    def _mode_selected(self) -> None:
        try:
            # Qt serializes our str-based Enum item data to a plain string.
            # Normalize at the widget boundary so every downstream identity
            # check receives an actual OperatingMode member.
            mode = OperatingMode(self.mode_combo.currentData())
        except (TypeError, ValueError):
            self.add_log("ERROR", "Invalid operating-mode selection; returning to Simulation Mode")
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(0)
            self.mode_combo.blockSignals(False)
            self._set_mode(OperatingMode.SIMULATION)
            return
        if mode is OperatingMode.PWM_BENCH:
            accepted = QMessageBox.warning(
                self,
                "Enable PWM Bench Test Mode?",
                "This mode can transmit real actuator commands. Use only with a PWM meter. "
                "The real pump and all solenoid drivers must be physically disconnected.\n\n"
                "The hardware power interlock and independent activation limits remain mandatory.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if accepted != QMessageBox.StandardButton.Yes:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(0)
                self.mode_combo.blockSignals(False)
                return
        self._stop_all("Operating mode changed: operations cancelled")
        self._set_mode(mode)

    def _set_mode(self, mode: OperatingMode | str) -> None:
        mode = OperatingMode(mode)
        self._set_iffs_safe("Operating mode changed")
        self.safety.mode = mode
        if mode is OperatingMode.SIMULATION:
            self.safety.bench_authorized = False
            self.mode_banner.setText("SIMULATION")
            self.mode_banner.setStyleSheet(
                "background:#312e57;color:#c4b5fd;border-radius:7px;padding:8px;font-weight:700;"
            )
            self.add_log("INFO", "Simulation Mode enabled; commands are not transmitted")
        else:
            # Selecting PWM Bench Test Mode and accepting its explicit warning
            # is the session authorization for non-firing bench operations.
            self.safety.bench_authorized = True
            self.mode_banner.setText("PWM BENCH TEST")
            self.mode_banner.setStyleSheet(
                "background:#4a2e0d;color:#fbbf24;border-radius:7px;padding:8px;font-weight:700;"
            )
            self.add_log(
                "SAFETY",
                "PWM Bench Test Mode authorized for this session; real drivers must remain disconnected",
            )
            if self.safety.link_connected:
                try:
                    self.controller.request_recovery_inactive()
                except (RuntimeError, OSError) as exc:
                    self.controller.mark_all_unknown(
                        f"PWM Bench Test Mode entered, but initial inactive request failed: {exc}"
                    )
            else:
                self.controller.mark_all_unknown(
                    "PWM Bench Test Mode entered without telemetry; physical actuator states UNKNOWN"
                )
        self._refresh_controls()

    def _iffs_arm_changed(self, checked: bool) -> None:
        if checked:
            allowed, reason = self.safety.can_operate()
            if not allowed:
                self.iffs_arm_button.blockSignals(True)
                self.iffs_arm_button.setChecked(False)
                self.iffs_arm_button.blockSignals(False)
                self._show_error(reason)
                return
        elif Component.SHOOT in self.scheduler.active:
            self._stop_all("IFFS authorization returned to SAFE")
        self.safety.iffs_armed = checked
        if checked:
            # A Shoot switch already high while SAFE must never fire merely
            # because software authorization changes to ARM.
            self.rc_controller.require_low(RCAction.SHOOT)
        self.add_log("SAFETY", f"IFFS software authorization set to {'ARM' if checked else 'SAFE'}")
        self._refresh_controls()

    def _set_iffs_safe(self, reason: str) -> None:
        was_armed = self.safety.iffs_armed
        self.safety.iffs_armed = False
        if hasattr(self, "iffs_arm_button"):
            self.iffs_arm_button.blockSignals(True)
            self.iffs_arm_button.setChecked(False)
            self.iffs_arm_button.blockSignals(False)
        if was_armed:
            self.add_log("SAFETY", f"IFFS automatically returned to SAFE: {reason}")

    def _toggle_connection(self) -> None:
        if self.client.snapshot.running:
            self._stop_all("MAVLink disconnect requested")
            self.client.disconnect()
            return
        self._capture_config()
        try:
            save_config(self.config)
            self.client.connect(
                self.config.connection,
                baud=self.config.baud,
                heartbeat_timeout=self.config.heartbeat_timeout_seconds,
                source_system=self.config.source_system,
                source_component=self.config.source_component,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
            return
        self.connect_button.setText("Disconnect")
        self.connection_edit.setEnabled(False)
        self.baud_spin.setEnabled(False)
        self.link_pill.set_state("CONNECTING", "warn")
        self.add_log("INFO", f"Connecting to {self.config.connection}")

    def _capture_config(self) -> None:
        self.config.connection = self.connection_edit.text().strip()
        self.config.baud = self.baud_spin.value()
        self.config.water_duration_seconds = self.water_card.duration.value()
        self.config.air_duration_seconds = self.air_card.duration.value()
        self.config.shoot_duration_seconds = self.shoot_card.duration.value()
        self.config.reload_delay_seconds = self.reload_delay_spin.value()
        self.config.validate()

    def _poll_mavlink_events(self) -> None:
        while True:
            try:
                event = self.client.events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "heartbeat":
                self.safety.link_connected = True
                self.safety.vehicle_armed = event.data["armed"]
                self._ever_connected = True
                self.link_pill.set_state("CONNECTED", "good")
                self.system_pill.set_state(f"SYS ID {event.data['system_id']}", "active")
                self._set_armed(event.data["armed"])
                self.add_log(
                    "INFO",
                    f"Heartbeat detected from system {event.data['system_id']}, "
                    f"component {event.data['component_id']}",
                )
                self._request_rc_message_rate()
                self._attempt_recovery_reset()
            elif event.kind == "armed":
                self.safety.link_connected = True
                self.safety.vehicle_armed = event.data["armed"]
                self._set_armed(event.data["armed"])
                self._attempt_recovery_reset()
            elif event.kind == "telemetry":
                self.telemetry.update(event.data["group"], event.data["values"])
            elif event.kind == "telemetry_error":
                self.add_log(
                    "DEBUG",
                    f"Ignored malformed {event.data['group']} telemetry: {event.data['message']}",
                )
            elif event.kind == "rc_channels":
                self.rc_monitor.ingest(event.data["channels"])
                snapshot = self.rc_monitor.snapshot()
                if snapshot.usable and self.rc_controller.enabled:
                    self.rc_controller.process(snapshot.values)
                self._poll_rc_status()
            elif event.kind == "ack":
                result = ACK_RESULT_NAMES.get(event.data["result"], f"RESULT {event.data['result']}")
                command_label = (
                    f"{event.data['command']} ({event.data.get('command_name', 'unknown')})"
                )
                if not event.data.get("owned", False):
                    recipient = (
                        f"recipient SYS {event.data.get('target_system', 0)} / "
                        f"COMP {event.data.get('target_component', 0)}"
                    )
                    self.add_log(
                        "DEBUG",
                        f"Unrelated MAVLink ACK for command {command_label}: {result}; {recipient}",
                    )
                else:
                    level = "INFO" if event.data["result"] in (0, 5) else "ERROR"
                    self.add_log(
                        level,
                        f"IFFS MAVLink ACK for command {command_label}: {result}. "
                        "This does not prove physical actuator state.",
                    )
            elif event.kind == "timeout":
                self._handle_connection_loss("MAVLink heartbeat timeout")
            elif event.kind == "error":
                self.add_log("ERROR", f"MAVLink connection error: {event.data['message']}")
                self._handle_connection_loss("MAVLink transport error")
                self._connection_ui_reset()
            elif event.kind == "disconnected":
                self._handle_connection_loss("MAVLink disconnected")
                self._connection_ui_reset()
            elif event.kind == "status":
                self.add_log("INFO", event.data["message"])
        self._refresh_controls()

    def _request_rc_message_rate(self) -> None:
        rate = self.config.rc_request_rate_hz
        if rate <= 0 or self._rc_rate_requested:
            return
        try:
            self.client.request_message_interval(MAVLINK_MSG_ID_RC_CHANNELS, rate)
        except (RuntimeError, ValueError, OSError) as exc:
            self.add_log("DEBUG", f"Optional RC_CHANNELS rate request was not sent: {exc}")
            return
        self._rc_rate_requested = True
        self.add_log("INFO", f"Requested RC_CHANNELS once at {rate:g} Hz")

    def _handle_connection_loss(self, reason: str) -> None:
        self.auto_reload.cancel(reason)
        self._disable_rc(reason, stop_rc_operations=False)
        self.rc_monitor.invalidate()
        self._rc_rate_requested = False
        self.safety.link_connected = False
        self.safety.vehicle_armed = None
        self.link_pill.set_state("LINK LOST", "bad")
        self.arm_pill.set_state("ARM STATE —", "neutral")
        self.telemetry.clear()
        self.client.clear_pending_commands()
        self._set_iffs_safe(reason)
        if self._ever_connected and not self.safety.recovery_required:
            self.safety.recovery_required = True
            self.scheduler.communication_failed()
            self.connection_warning.setStyleSheet(
                "background:#541d25;color:#fecdd3;border:2px solid #e11d48;"
                "border-radius:8px;padding:11px;font-weight:700;"
            )
            self.connection_warning.setText(
                "CONNECTION LOST — all operations were cancelled and physical actuator states "
                "are UNKNOWN. Use the independent hardware interlock. A software reset cannot "
                "be delivered until communication returns."
            )
            self.connection_warning.show()
            self.add_log(
                "SAFETY",
                f"{reason}: timers cancelled, pending commands cleared, actuator states UNKNOWN",
            )
        self._refresh_telemetry()
        self._refresh_controls()

    def _attempt_recovery_reset(self) -> None:
        if not self.safety.recovery_required or not self.safety.link_connected:
            return
        try:
            self.controller.request_recovery_inactive()
        except (RuntimeError, OSError) as exc:
            self.connection_warning.setText(
                "LINK RESTORED, RESET FAILED — operations remain blocked and actuator states "
                f"are UNKNOWN. Use the hardware interlock. Error: {exc}"
            )
            self.connection_warning.show()
            self.add_log("ERROR", f"Connection recovery inactive request failed: {exc}")
            return
        self.safety.recovery_required = False
        self._set_iffs_safe("connection recovery")
        self.connection_warning.setStyleSheet(
            "background:#46320f;color:#fcd34d;border:1px solid #d97706;"
            "border-radius:8px;padding:11px;font-weight:700;"
        )
        if self.safety.mode is OperatingMode.SIMULATION:
            recovery_message = (
                "LINK RECOVERED — simulated actuator states were reset to inactive; no actuator "
                "commands were transmitted. Interrupted operations were not resumed and IFFS remains SAFE."
            )
            recovery_log = "Simulation connection recovered; operations were not resumed"
        else:
            recovery_message = (
                "LINK RECOVERED — inactive values were requested for all three outputs. Physical "
                "state is not verified; interrupted operations were not resumed and IFFS remains SAFE."
            )
            recovery_log = (
                "Connection recovered; prioritized all-output inactive request sent, operations not resumed"
            )
        self.connection_warning.setText(recovery_message)
        self.connection_warning.show()
        self.add_log("SAFETY", recovery_log)
        self._refresh_controls()

    def _connection_ui_reset(self) -> None:
        self.link_pill.set_state("DISCONNECTED", "neutral")
        self.arm_pill.set_state("ARM STATE —", "neutral")
        self.system_pill.set_state("SYS ID —", "neutral")
        self.connect_button.setText("Connect")
        self.connection_edit.setEnabled(True)
        self.baud_spin.setEnabled(True)

    def _set_armed(self, armed: bool) -> None:
        if armed != self._last_armed:
            self.add_log("INFO", f"Vehicle is now {'ARMED' if armed else 'DISARMED'}")
            self._last_armed = armed
        self.arm_pill.set_state("ARMED" if armed else "DISARMED", "warn" if armed else "good")

    def _water_fill(self) -> None:
        self._run_operation(
            lambda: self.scheduler.start_fill(Component.WATER, self.water_card.duration.value())
        )

    def _air_fill(self) -> None:
        self._run_operation(
            lambda: self.scheduler.start_fill(Component.AIR, self.air_card.duration.value())
        )

    def _fill_all(self) -> None:
        self._run_operation(
            lambda: self.scheduler.start_fill_all(
                self.water_card.duration.value(), self.air_card.duration.value()
            )
        )

    def _shoot(self) -> None:
        allowed, reason = self.safety.can_shoot()
        if not allowed:
            self._show_error(reason)
            return
        if self._run_operation(
            lambda: self.scheduler.start_shoot(self.shoot_card.duration.value())
        ):
            self._auto_reload_rc_origin = False
            self.auto_reload.shoot_started()
            self._refresh_auto_reload_ui()
            self._refresh_controls()

    def _run_operation(self, action: object) -> bool:
        if self.auto_reload.sequence_active:
            message = "Automatic reload sequence is active"
            self.add_log("SAFETY", message)
            self._show_error(message)
            return False
        try:
            action()  # type: ignore[operator]
        except (RuntimeError, ValueError, OSError) as exc:
            self.add_log("ERROR", str(exc))
            self._show_error(str(exc))
            return False
        self._refresh_controls()
        return True

    def _stop_all(self, reason: str) -> None:
        self.auto_reload.cancel(reason)
        try:
            self.scheduler.stop_all(reason)
        except (RuntimeError, OSError) as exc:
            self.add_log("ERROR", f"STOP ALL command error: {exc}")
        self._rc_started_components.clear()
        self._auto_reload_rc_origin = False
        self._refresh_auto_reload_ui()
        self._refresh_controls()

    def _tick(self) -> None:
        completed: set[Component] = set()
        try:
            completed = self.scheduler.tick()
        except (RuntimeError, OSError) as exc:
            self.add_log("ERROR", f"Operation stop command failed: {exc}")
            self.auto_reload.fail(f"Operation completion failed: {exc}")
        active = self.scheduler.active
        previous_auto_state = self.auto_reload.state
        self.auto_reload.tick(completed, active)
        if self.auto_reload.state is not previous_auto_state:
            self.add_log(
                "INFO",
                f"Automatic reload state: {self.auto_reload.state.value}",
            )
        active = self.scheduler.active
        self._rc_started_components.intersection_update(active)
        if not self.auto_reload.sequence_active and Component.SHOOT not in active:
            self._auto_reload_rc_origin = False
        water_remaining = self.scheduler.remaining(Component.WATER)
        air_remaining = self.scheduler.remaining(Component.AIR)
        shoot_remaining = self.scheduler.remaining(Component.SHOOT)
        state = self.controller.state
        self.water_card.update_operation(
            Component.WATER in active, water_remaining, state[Component.WATER]
        )
        self.air_card.update_operation(Component.AIR in active, air_remaining, state[Component.AIR])
        self.shoot_card.update_operation(
            Component.SHOOT in active, shoot_remaining, state[Component.SHOOT]
        )
        fill_active = Component.WATER in active or Component.AIR in active
        fill_states = (state[Component.WATER], state[Component.AIR])
        if any(value is None for value in fill_states):
            self.fill_all_card.state.set_state("UNKNOWN", "bad")
        elif fill_active or any(value is True for value in fill_states):
            self.fill_all_card.state.set_state("ACTIVE REQUESTED", "active")
        else:
            self.fill_all_card.state.set_state("INACTIVE REQUESTED", "neutral")
        self.fill_all_card.countdown.setText(
            f"W {water_remaining:.1f} • A {air_remaining:.1f} s"
        )
        self._refresh_auto_reload_ui()
        self._refresh_controls()

    def _actuator_state_changed(self, _state: dict[Component, bool | None]) -> None:
        # The operation timer owns presentation; state means requested/simulated state only.
        pass

    def _refresh_controls(self) -> None:
        allowed, _reason = self.safety.can_operate()
        active = self.scheduler.active
        sequence_busy = self.auto_reload.sequence_active
        can_start = allowed and not sequence_busy
        self.water_card.button.setEnabled(
            can_start and Component.WATER not in active and Component.SHOOT not in active
        )
        self.air_card.button.setEnabled(
            can_start and Component.AIR not in active and Component.SHOOT not in active
        )
        self.fill_all_card.button.setEnabled(can_start and not active)
        shoot_allowed, _reason = self.safety.can_shoot()
        self.shoot_card.button.setEnabled(
            shoot_allowed and not active and not sequence_busy
        )
        self.water_card.duration.setEnabled(
            not sequence_busy and Component.WATER not in active
        )
        self.air_card.duration.setEnabled(
            not sequence_busy and Component.AIR not in active
        )
        self.shoot_card.duration.setEnabled(
            not sequence_busy and Component.SHOOT not in active
        )
        self.reload_delay_spin.setEnabled(not sequence_busy)
        self.mode_combo.setEnabled(not self.safety.recovery_required)

        self.iffs_arm_button.setEnabled(allowed)
        if self.safety.iffs_armed:
            self.iffs_arm_button.setText("IFFS ARMED")
            self.iffs_arm_button.setStyleSheet(
                "background:#b91c1c;color:white;border:2px solid #fb7185;"
            )
            self.iffs_arm_status.setText(
                "ARMED — Shoot enabled when no operation is active. This does not verify the "
                "physical hardware safety switch."
            )
            self.iffs_arm_status.setStyleSheet("color:#fda4af;font-weight:700;")
        else:
            self.iffs_arm_button.setText("IFFS SAFE")
            self.iffs_arm_button.setStyleSheet(
                "background:#166534;color:white;border:2px solid #4ade80;"
            )
            self.iffs_arm_status.setText(
                "SAFE — Shoot disabled. Water and Air controls remain independent. Software "
                "SAFE does not verify the physical hardware switch."
            )
            self.iffs_arm_status.setStyleSheet("color:#86efac;font-weight:600;")

        if self.safety.recovery_required:
            self.operation_status.setText("RECOVERY REQUIRED — operations blocked")
            self.operation_status.setStyleSheet("color:#fda4af;font-weight:700;")
        elif sequence_busy:
            self.operation_status.setText(
                f"AUTO RELOAD: {self.auto_reload.state.value}"
            )
            self.operation_status.setStyleSheet("color:#fcd34d;font-weight:700;")
        elif active:
            names = ", ".join(component.value.upper() for component in sorted(active, key=lambda c: c.value))
            self.operation_status.setText(f"ACTIVE: {names}")
            self.operation_status.setStyleSheet("color:#93c5fd;font-weight:700;")
        elif not allowed:
            self.operation_status.setText(_reason or "OPERATIONS UNAVAILABLE")
            self.operation_status.setStyleSheet("color:#fcd34d;font-weight:600;")
        else:
            self.operation_status.setText("READY")
            self.operation_status.setStyleSheet("color:#6ee7b7;font-weight:700;")

    def _refresh_telemetry(self) -> None:
        snapshot: FlightTelemetrySnapshot = self.telemetry.snapshot()
        self.flight_mode_value.setText(snapshot.flight_mode or "--")
        if snapshot.gps_fix is None and snapshot.satellites is None:
            self.gps_value.setText("--")
        else:
            satellites = "--" if snapshot.satellites is None else str(snapshot.satellites)
            self.gps_value.setText(f"{snapshot.gps_fix or '--'} • {satellites} sats")
        if snapshot.batteries:
            parts = []
            for battery in snapshot.batteries:
                voltage = "-- V" if battery.voltage is None else f"{battery.voltage:.1f} V"
                remaining = "--%" if battery.remaining is None else f"{battery.remaining}%"
                parts.append(f"B{battery.battery_id + 1} {voltage} / {remaining}")
            self.battery_value.setText(" • ".join(parts))
        else:
            self.battery_value.setText("--")
        self.altitude_value.setText(
            "--" if snapshot.relative_altitude is None else f"{snapshot.relative_altitude:.1f} m"
        )
        self.speed_value.setText(
            "--" if snapshot.ground_speed is None else f"{snapshot.ground_speed:.1f} m/s"
        )
        self.heading_value.setText(
            "--" if snapshot.heading is None else f"{snapshot.heading:.0f}°"
        )

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "IFFS Ground Control", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.scheduler.shutdown()
        except Exception as exc:
            self.add_log("ERROR", f"Shutdown stop error: {exc}")
        self.client.disconnect()
        try:
            self._capture_config()
            save_config(self.config)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Configuration", f"Configuration was not saved: {exc}")
        event.accept()


def configure_dark_palette(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0b1220"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e5e7eb"))
    app.setPalette(palette)
