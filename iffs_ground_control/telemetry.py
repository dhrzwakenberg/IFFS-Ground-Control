from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

GPS_FIX_LABELS = {
    0: "No GPS",
    1: "No fix",
    2: "2D fix",
    3: "3D fix",
    4: "DGPS",
    5: "RTK float",
    6: "RTK fixed",
    7: "Static",
    8: "PPP",
}


def decode_px4_flight_mode(message: Any) -> str:
    """Decode HEARTBEAT custom_mode using pymavlink's PX4 mode mapping."""
    from pymavlink import mavutil

    return str(mavutil.mode_string_v10(message))


def _valid_uint16(value: int) -> bool:
    return value not in (0, 0xFFFF)


def decode_gps_raw_int(message: Any) -> dict[str, Any]:
    satellites = int(message.satellites_visible)
    fix_type = int(message.fix_type)
    return {
        "satellites": None if satellites == 0xFF else satellites,
        "fix_type": fix_type,
        "fix_label": GPS_FIX_LABELS.get(fix_type, f"Fix {fix_type}"),
    }


def decode_battery_status(message: Any) -> dict[str, Any]:
    values = [int(value) for value in getattr(message, "voltages", ())]
    values.extend(int(value) for value in getattr(message, "voltages_ext", ()))
    valid_millivolts = [value for value in values if _valid_uint16(value)]
    voltage = sum(valid_millivolts) / 1000.0 if valid_millivolts else None
    remaining = int(message.battery_remaining)
    return {
        "battery_id": int(message.id),
        "voltage": voltage,
        "remaining": None if remaining < 0 else remaining,
    }


def decode_sys_status(message: Any) -> dict[str, Any]:
    millivolts = int(message.voltage_battery)
    remaining = int(message.battery_remaining)
    return {
        "battery_id": 0,
        "voltage": millivolts / 1000.0 if _valid_uint16(millivolts) else None,
        "remaining": None if remaining < 0 else remaining,
    }


def decode_global_position_int(message: Any) -> dict[str, Any]:
    heading = int(message.hdg)
    return {
        "relative_altitude": int(message.relative_alt) / 1000.0,
        "heading": None if heading == 0xFFFF else heading / 100.0,
    }


def decode_vfr_hud(message: Any) -> dict[str, Any]:
    heading = int(message.heading)
    return {
        "ground_speed": float(message.groundspeed),
        "heading": float(heading) if 0 <= heading <= 360 else None,
    }


@dataclass(frozen=True)
class BatterySnapshot:
    battery_id: int
    voltage: float | None
    remaining: int | None


@dataclass(frozen=True)
class FlightTelemetrySnapshot:
    flight_mode: str | None = None
    satellites: int | None = None
    gps_fix: str | None = None
    batteries: tuple[BatterySnapshot, ...] = ()
    relative_altitude: float | None = None
    ground_speed: float | None = None
    heading: float | None = None


@dataclass
class _TimedGroup:
    values: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


class TelemetryStore:
    """Timestamped telemetry cache that never exposes stale values."""

    def __init__(self, *, stale_after: float = 3.0, battery_stale_after: float = 10.0) -> None:
        self.stale_after = stale_after
        self.battery_stale_after = battery_stale_after
        self._groups: dict[str, _TimedGroup] = {}
        self._batteries: dict[int, _TimedGroup] = {}

    def clear(self) -> None:
        self._groups.clear()
        self._batteries.clear()

    def update(self, group: str, values: dict[str, Any], *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        if group == "battery":
            battery_id = int(values["battery_id"])
            self._batteries[battery_id] = _TimedGroup(dict(values), timestamp)
            return
        self._groups[group] = _TimedGroup(dict(values), timestamp)

    def snapshot(self, *, now: float | None = None) -> FlightTelemetrySnapshot:
        timestamp = time.monotonic() if now is None else now

        def fresh(group: str) -> dict[str, Any]:
            item = self._groups.get(group)
            if item is None or timestamp - item.updated_at > self.stale_after:
                return {}
            return item.values

        heartbeat = fresh("heartbeat")
        gps = fresh("gps")
        position = fresh("position")
        vfr = fresh("vfr")
        batteries = tuple(
            BatterySnapshot(
                battery_id=battery_id,
                voltage=item.values.get("voltage"),
                remaining=item.values.get("remaining"),
            )
            for battery_id, item in sorted(self._batteries.items())
            if timestamp - item.updated_at <= self.battery_stale_after
        )
        position_heading = position.get("heading")
        return FlightTelemetrySnapshot(
            flight_mode=heartbeat.get("flight_mode"),
            satellites=gps.get("satellites"),
            gps_fix=gps.get("fix_label"),
            batteries=batteries,
            relative_altitude=position.get("relative_altitude"),
            ground_speed=vfr.get("ground_speed"),
            heading=position_heading if position_heading is not None else vfr.get("heading"),
        )
