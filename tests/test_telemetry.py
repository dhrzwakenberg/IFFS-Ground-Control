from types import SimpleNamespace

import pytest
from pymavlink import mavutil

from iffs_ground_control.telemetry import (
    TelemetryStore,
    decode_battery_status,
    decode_global_position_int,
    decode_gps_raw_int,
    decode_px4_flight_mode,
    decode_sys_status,
    decode_vfr_hud,
)


def test_px4_custom_mode_decoding() -> None:
    heartbeat = mavutil.mavlink.MAVLink_heartbeat_message(
        mavutil.mavlink.MAV_TYPE_QUADROTOR,
        mavutil.mavlink.MAV_AUTOPILOT_PX4,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        3 << 16,  # PX4_CUSTOM_MAIN_MODE_POSCTL
        mavutil.mavlink.MAV_STATE_STANDBY,
        3,
    )
    assert decode_px4_flight_mode(heartbeat) == "POSCTL"


def test_gps_decoding() -> None:
    decoded = decode_gps_raw_int(SimpleNamespace(satellites_visible=14, fix_type=3))
    assert decoded == {"satellites": 14, "fix_type": 3, "fix_label": "3D fix"}


def test_battery_status_scaling_and_multiple_cell_sum() -> None:
    message = SimpleNamespace(
        id=1,
        voltages=[4100, 4090, 4080, 0xFFFF],
        voltages_ext=[],
        battery_remaining=76,
    )
    decoded = decode_battery_status(message)
    assert decoded["battery_id"] == 1
    assert decoded["voltage"] == pytest.approx(12.27)
    assert decoded["remaining"] == 76


def test_sys_status_battery_scaling_and_unknown_values() -> None:
    valid = decode_sys_status(SimpleNamespace(voltage_battery=16400, battery_remaining=55))
    unknown = decode_sys_status(SimpleNamespace(voltage_battery=0xFFFF, battery_remaining=-1))
    assert valid["voltage"] == pytest.approx(16.4)
    assert valid["remaining"] == 55
    assert unknown["voltage"] is None
    assert unknown["remaining"] is None


def test_position_and_vfr_scaling() -> None:
    position = decode_global_position_int(SimpleNamespace(relative_alt=12345, hdg=27123))
    vfr = decode_vfr_hud(SimpleNamespace(groundspeed=8.75, heading=271))
    assert position["relative_altitude"] == pytest.approx(12.345)
    assert position["heading"] == pytest.approx(271.23)
    assert vfr["ground_speed"] == pytest.approx(8.75)
    assert vfr["heading"] == 271.0


def test_multiple_batteries_are_kept_independently() -> None:
    store = TelemetryStore(stale_after=3.0, battery_stale_after=10.0)
    store.update("battery", {"battery_id": 0, "voltage": 16.4, "remaining": 80}, now=1.0)
    store.update("battery", {"battery_id": 1, "voltage": 12.2, "remaining": 60}, now=2.0)

    snapshot = store.snapshot(now=3.0)

    assert [battery.battery_id for battery in snapshot.batteries] == [0, 1]
    assert snapshot.batteries[1].voltage == pytest.approx(12.2)


def test_stale_telemetry_is_not_exposed() -> None:
    store = TelemetryStore(stale_after=3.0, battery_stale_after=5.0)
    store.update("heartbeat", {"flight_mode": "POSCTL"}, now=1.0)
    store.update("gps", {"satellites": 12, "fix_label": "3D fix"}, now=1.0)
    store.update("position", {"relative_altitude": 4.2, "heading": 90.0}, now=1.0)
    store.update("vfr", {"ground_speed": 2.5, "heading": 91.0}, now=1.0)
    store.update("battery", {"battery_id": 0, "voltage": 16.0, "remaining": 50}, now=1.0)

    fresh = store.snapshot(now=3.9)
    stale = store.snapshot(now=6.1)

    assert fresh.flight_mode == "POSCTL"
    assert fresh.ground_speed == 2.5
    assert stale.flight_mode is None
    assert stale.satellites is None
    assert stale.relative_altitude is None
    assert stale.ground_speed is None
    assert stale.heading is None
    assert stale.batteries == ()
