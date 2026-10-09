from __future__ import annotations

from dataclasses import dataclass

from .models import OperatingMode


@dataclass(frozen=True)
class SafetySnapshot:
    mode: OperatingMode
    link_connected: bool
    bench_authorized: bool
    vehicle_armed: bool | None
    iffs_armed: bool
    recovery_required: bool


class SafetyMonitor:
    """Session-only software interlocks; these are not safety-rated controls."""

    def __init__(self) -> None:
        self.mode = OperatingMode.SIMULATION
        self.link_connected = False
        self.bench_authorized = False
        self.vehicle_armed: bool | None = None
        self.iffs_armed = False
        self.recovery_required = False

    def snapshot(self) -> SafetySnapshot:
        return SafetySnapshot(
            mode=self.mode,
            link_connected=self.link_connected,
            bench_authorized=self.bench_authorized,
            vehicle_armed=self.vehicle_armed,
            iffs_armed=self.iffs_armed,
            recovery_required=self.recovery_required,
        )

    def can_operate(self) -> tuple[bool, str]:
        if self.recovery_required:
            return False, "Connection recovery reset is still required"
        if self.mode is OperatingMode.SIMULATION:
            return True, ""
        if not self.bench_authorized:
            return False, "PWM bench mode is not authorized for this session"
        if not self.link_connected:
            return False, "No current MAVLink heartbeat"
        return True, ""

    def can_shoot(self) -> tuple[bool, str]:
        allowed, reason = self.can_operate()
        if not allowed:
            return allowed, reason
        if not self.iffs_armed:
            return False, "IFFS software authorization is SAFE"
        return True, ""
