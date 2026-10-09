import json

import pytest

from iffs_ground_control.config import load_config, save_config
from iffs_ground_control.models import AppConfig, Component


def test_config_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    original = AppConfig(
        water_duration_seconds=12.5,
        shoot_duration_seconds=1.7,
        reload_delay_seconds=2.5,
        connection="udpin:0.0.0.0:14551",
    )

    save_config(original, path)
    loaded, warning = load_config(path)

    assert warning is None
    assert loaded.water_duration_seconds == 12.5
    assert loaded.connection == "udpin:0.0.0.0:14551"
    assert loaded.shoot_duration_seconds == 1.7
    assert loaded.reload_delay_seconds == 2.5
    assert loaded.actuators[Component.SHOOT].slot == 3
    assert loaded.rc_channels["stop_all"] == 15
    assert loaded.rc_timeout_seconds == 1.0


def test_invalid_config_falls_back_safely(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"actuators": {"water": {"slot": 99}}}), encoding="utf-8")

    config, warning = load_config(path)

    assert warning is not None
    assert config.actuators[Component.WATER].slot == 1


def test_rc_channel_mapping_must_be_unique_and_in_range() -> None:
    duplicate = AppConfig()
    duplicate.rc_channels["air"] = duplicate.rc_channels["water"]
    with pytest.raises(ValueError, match="must be unique"):
        duplicate.validate()

    out_of_range = AppConfig()
    out_of_range.rc_channels["stop_all"] = 19
    with pytest.raises(ValueError, match="between 1 and 18"):
        out_of_range.validate()


def test_rc_threshold_and_rate_validation() -> None:
    with pytest.raises(ValueError, match="low < high"):
        AppConfig(rc_low_threshold=1800, rc_high_threshold=1700).validate()
    with pytest.raises(ValueError, match="between 5 and 10 Hz"):
        AppConfig(rc_request_rate_hz=12).validate()
    with pytest.raises(ValueError, match="between 5 and 10 Hz"):
        AppConfig(rc_request_rate_hz=3).validate()


def test_reload_delay_validation() -> None:
    with pytest.raises(ValueError, match="between 0 and 3600"):
        AppConfig(reload_delay_seconds=-0.1).validate()
    with pytest.raises(ValueError, match="between 0 and 3600"):
        AppConfig(reload_delay_seconds=3600.1).validate()
