from __future__ import annotations

import math
from collections.abc import Mapping

MAV_CMD_DO_SET_ACTUATOR = 187
ACTUATORS_PER_SET = 6


def build_actuator_parameters(
    updates: Mapping[int, float], *, actuator_set_index: int = 0
) -> tuple[float, float, float, float, float, float, float]:
    """Build COMMAND_LONG params with NaN for every unchanged actuator."""
    if actuator_set_index < 0:
        raise ValueError("Actuator set index cannot be negative")
    params = [math.nan] * ACTUATORS_PER_SET
    for slot, value in updates.items():
        if not 1 <= slot <= ACTUATORS_PER_SET:
            raise ValueError("Actuator slot must be between 1 and 6")
        numeric = float(value)
        if not math.isfinite(numeric) or not -1.0 <= numeric <= 1.0:
            raise ValueError("Actuator value must be finite and between -1 and 1")
        params[slot - 1] = numeric
    return (*params, float(actuator_set_index))
