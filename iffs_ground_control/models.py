from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Component(str, Enum):
    WATER = "water"
    AIR = "air"
    SHOOT = "shoot"


class OperatingMode(str, Enum):
    SIMULATION = "simulation"
    PWM_BENCH = "pwm_bench"


@dataclass(frozen=True)
class ActuatorDefinition:
    slot: int
    active: float = 1.0
    inactive: float = -1.0

    def validate(self) -> None:
        if not 1 <= self.slot <= 6:
            raise ValueError("Actuator slot must be between 1 and 6")
        for name, value in (("active", self.active), ("inactive", self.inactive)):
            if not -1.0 <= value <= 1.0:
                raise ValueError(f"{name} value must be between -1 and 1")


def default_actuators() -> dict[Component, ActuatorDefinition]:
    return {
        Component.WATER: ActuatorDefinition(slot=1),
        Component.AIR: ActuatorDefinition(slot=2),
        Component.SHOOT: ActuatorDefinition(slot=3),
    }


def default_rc_channels() -> dict[str, int]:
    return {
        "water": 11,
        "air": 12,
        "fill_all": 13,
        "shoot": 14,
        "stop_all": 15,
    }


@dataclass
class AppConfig:
    connection: str = "udpin:127.0.0.1:14551"
    baud: int = 57600
    heartbeat_timeout_seconds: float = 3.0
    source_system: int = 252
    source_component: int = 190
    water_duration_seconds: float = 10.0
    air_duration_seconds: float = 10.0
    shoot_duration_seconds: float = 1.0
    shoot_duration_min_seconds: float = 0.1
    shoot_duration_max_seconds: float = 5.0
    reload_delay_seconds: float = 1.0
    actuators: dict[Component, ActuatorDefinition] = field(default_factory=default_actuators)
    rc_channels: dict[str, int] = field(default_factory=default_rc_channels)
    rc_low_threshold: int = 1250
    rc_high_threshold: int = 1750
    rc_timeout_seconds: float = 1.0
    rc_request_rate_hz: float = 0.0

    def validate(self) -> None:
        if not self.connection.strip():
            raise ValueError("Connection string cannot be empty")
        if self.baud <= 0:
            raise ValueError("Baud rate must be positive")
        if self.heartbeat_timeout_seconds <= 0:
            raise ValueError("Heartbeat timeout must be positive")
        if self.water_duration_seconds <= 0 or self.air_duration_seconds <= 0:
            raise ValueError("Fill durations must be positive")
        if self.shoot_duration_min_seconds <= 0:
            raise ValueError("Minimum Shoot duration must be positive")
        if self.shoot_duration_max_seconds < self.shoot_duration_min_seconds:
            raise ValueError("Maximum Shoot duration must not be below the minimum")
        if not (
            self.shoot_duration_min_seconds
            <= self.shoot_duration_seconds
            <= self.shoot_duration_max_seconds
        ):
            raise ValueError("Shoot duration must be within the configured limits")
        if not 0 <= self.reload_delay_seconds <= 3600:
            raise ValueError("Reload delay must be between 0 and 3600 seconds")
        if not 1 <= self.source_system <= 255:
            raise ValueError("Source system must be between 1 and 255")
        if not 1 <= self.source_component <= 255:
            raise ValueError("Source component must be between 1 and 255")
        slots: list[int] = []
        for component in Component:
            definition = self.actuators[component]
            definition.validate()
            slots.append(definition.slot)
        if len(slots) != len(set(slots)):
            raise ValueError("Actuator slots must be unique")
        expected_rc_actions = set(default_rc_channels())
        if set(self.rc_channels) != expected_rc_actions:
            raise ValueError(
                "RC channel mapping must define water, air, fill_all, shoot, and stop_all"
            )
        rc_channels = list(self.rc_channels.values())
        if any(not 1 <= channel <= 18 for channel in rc_channels):
            raise ValueError("RC channels must be between 1 and 18")
        if len(rc_channels) != len(set(rc_channels)):
            raise ValueError("RC channels must be unique")
        if not 800 < self.rc_low_threshold < self.rc_high_threshold < 2200:
            raise ValueError(
                "RC thresholds must satisfy 800 < low < high < 2200 microseconds"
            )
        if self.rc_timeout_seconds <= 0:
            raise ValueError("RC timeout must be positive")
        if self.rc_request_rate_hz != 0 and not 5 <= self.rc_request_rate_hz <= 10:
            raise ValueError("RC message request rate must be 0 (disabled) or between 5 and 10 Hz")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actuators"] = {
            component.value: asdict(definition)
            for component, definition in self.actuators.items()
        }
        data["rc_channels"] = dict(self.rc_channels)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        defaults = cls()
        raw_actuators = data.get("actuators", {})
        actuators: dict[Component, ActuatorDefinition] = {}
        for component, default in defaults.actuators.items():
            raw = raw_actuators.get(component.value, {})
            actuators[component] = ActuatorDefinition(
                slot=int(raw.get("slot", default.slot)),
                active=float(raw.get("active", default.active)),
                inactive=float(raw.get("inactive", default.inactive)),
            )
        raw_rc_channels = data.get("rc_channels", {})
        rc_channels = {
            action: int(raw_rc_channels.get(action, channel))
            for action, channel in defaults.rc_channels.items()
        }
        config = cls(
            connection=str(data.get("connection", defaults.connection)),
            baud=int(data.get("baud", defaults.baud)),
            heartbeat_timeout_seconds=float(
                data.get("heartbeat_timeout_seconds", defaults.heartbeat_timeout_seconds)
            ),
            source_system=int(data.get("source_system", defaults.source_system)),
            source_component=int(data.get("source_component", defaults.source_component)),
            water_duration_seconds=float(
                data.get("water_duration_seconds", defaults.water_duration_seconds)
            ),
            air_duration_seconds=float(
                data.get("air_duration_seconds", defaults.air_duration_seconds)
            ),
            shoot_duration_seconds=float(
                data.get("shoot_duration_seconds", defaults.shoot_duration_seconds)
            ),
            shoot_duration_min_seconds=float(
                data.get("shoot_duration_min_seconds", defaults.shoot_duration_min_seconds)
            ),
            shoot_duration_max_seconds=float(
                data.get("shoot_duration_max_seconds", defaults.shoot_duration_max_seconds)
            ),
            reload_delay_seconds=float(
                data.get("reload_delay_seconds", defaults.reload_delay_seconds)
            ),
            actuators=actuators,
            rc_channels=rc_channels,
            rc_low_threshold=int(data.get("rc_low_threshold", defaults.rc_low_threshold)),
            rc_high_threshold=int(data.get("rc_high_threshold", defaults.rc_high_threshold)),
            rc_timeout_seconds=float(
                data.get("rc_timeout_seconds", defaults.rc_timeout_seconds)
            ),
            rc_request_rate_hz=float(
                data.get("rc_request_rate_hz", defaults.rc_request_rate_hz)
            ),
        )
        config.validate()
        return config
