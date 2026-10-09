import math

import pytest

from iffs_ground_control.command_mapping import build_actuator_parameters


def test_single_actuator_uses_nan_for_unchanged_slots() -> None:
    params = build_actuator_parameters({2: 0.75})

    assert math.isnan(params[0])
    assert params[1] == 0.75
    assert all(math.isnan(value) for value in params[2:6])
    assert params[6] == 0.0


def test_multiple_actuators_map_to_command_long_parameters() -> None:
    params = build_actuator_parameters({1: -1.0, 3: 1.0})

    assert params[0] == -1.0
    assert math.isnan(params[1])
    assert params[2] == 1.0


@pytest.mark.parametrize("slot", [0, 7])
def test_invalid_slot_is_rejected(slot: int) -> None:
    with pytest.raises(ValueError):
        build_actuator_parameters({slot: 0.0})


@pytest.mark.parametrize("value", [-1.01, 1.01, math.nan, math.inf])
def test_invalid_normalized_value_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        build_actuator_parameters({1: value})
