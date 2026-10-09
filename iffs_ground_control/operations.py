from __future__ import annotations

import time
from collections.abc import Callable

from .actuator import ActuatorController
from .models import Component
from .safety import SafetyMonitor


class OperationScheduler:
    """Monotonic, non-blocking operation state machine."""

    def __init__(
        self,
        controller: ActuatorController,
        safety: SafetyMonitor,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._safety = safety
        self._clock = clock
        self._deadlines: dict[Component, float] = {}

    @property
    def active(self) -> set[Component]:
        return set(self._deadlines)

    def remaining(self, component: Component) -> float:
        deadline = self._deadlines.get(component)
        return max(0.0, deadline - self._clock()) if deadline is not None else 0.0

    def start_fill(self, component: Component, duration: float) -> None:
        if component not in (Component.WATER, Component.AIR):
            raise ValueError("Only water and air are fill operations")
        if duration <= 0:
            raise ValueError("Duration must be positive")
        if Component.SHOOT in self._deadlines:
            raise RuntimeError("Filling is blocked while Shoot is active")
        if component in self._deadlines:
            raise RuntimeError(f"{component.value.title()} fill is already active")
        self._controller.set_components({component: True})
        self._deadlines[component] = self._clock() + duration

    def start_fill_all(self, water_duration: float, air_duration: float) -> None:
        if water_duration <= 0 or air_duration <= 0:
            raise ValueError("Durations must be positive")
        if self._deadlines:
            raise RuntimeError("Fill All requires all operations to be inactive")
        self._controller.set_components({Component.WATER: True, Component.AIR: True})
        now = self._clock()
        self._deadlines[Component.WATER] = now + water_duration
        self._deadlines[Component.AIR] = now + air_duration

    def start_shoot(self, duration: float = 1.0) -> None:
        if duration <= 0:
            raise ValueError("Duration must be positive")
        allowed, reason = self._safety.can_shoot()
        if not allowed:
            raise RuntimeError(reason)
        if self._deadlines:
            raise RuntimeError("Shoot requires all other operations to be inactive")
        self._controller.set_components({Component.SHOOT: True})
        self._deadlines[Component.SHOOT] = self._clock() + duration

    def tick(self) -> set[Component]:
        now = self._clock()
        due = [component for component, deadline in self._deadlines.items() if now >= deadline]
        completed: set[Component] = set()
        for component in due:
            try:
                self._controller.set_components({component: False})
            finally:
                self._deadlines.pop(component, None)
            completed.add(component)
        return completed

    def stop_all(self, reason: str = "Operator STOP ALL") -> None:
        self._deadlines.clear()
        self._controller.stop_all(reason)

    def communication_failed(self) -> None:
        self._deadlines.clear()
        self._controller.mark_all_unknown(
            "Telemetry lost: operations cancelled; physical actuator states UNKNOWN"
        )

    def shutdown(self) -> None:
        self.stop_all("Application shutdown: inactive state requested")
