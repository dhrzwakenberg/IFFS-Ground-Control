from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from .command_mapping import build_actuator_parameters
from .models import ActuatorDefinition, Component, OperatingMode
from .safety import SafetyMonitor


class ActuatorTransport(Protocol):
    def send_actuator_command(self, params: tuple[float, ...]) -> None: ...


LogCallback = Callable[[str, str], None]
StateCallback = Callable[[dict[Component, bool | None]], None]


class ActuatorController:
    """The sole application boundary allowed to issue actuator commands."""

    def __init__(
        self,
        definitions: Mapping[Component, ActuatorDefinition],
        transport: ActuatorTransport,
        safety: SafetyMonitor,
        log: LogCallback | None = None,
        state_changed: StateCallback | None = None,
    ) -> None:
        self._definitions = dict(definitions)
        self._transport = transport
        self._safety = safety
        self._log = log or (lambda _level, _message: None)
        self._state_changed = state_changed or (lambda _state: None)
        self._state = {component: False for component in Component}

    @property
    def state(self) -> dict[Component, bool | None]:
        return dict(self._state)

    def set_components(self, updates: Mapping[Component, bool]) -> None:
        allowed, reason = self._safety.can_operate()
        activating = any(updates.values())
        if activating and not allowed:
            raise RuntimeError(reason)
        if self._safety.recovery_required:
            self._log(
                "SAFETY",
                "Operational command suppressed while connection recovery reset is pending",
            )
            return

        values = {
            self._definitions[component].slot: (
                self._definitions[component].active
                if active
                else self._definitions[component].inactive
            )
            for component, active in updates.items()
        }
        params = build_actuator_parameters(values)

        command_applied = False
        if self._safety.mode is OperatingMode.PWM_BENCH:
            # The GUI requests inactive values before withdrawing an existing
            # authorization. Without authorization, even inactive live commands
            # remain blocked because wiring/polarity has not been validated.
            if not self._safety.bench_authorized:
                self._log("SAFETY", "Inactive command not transmitted: bench mode unauthorized")
            elif not self._safety.link_connected:
                if activating:
                    raise RuntimeError("No current MAVLink heartbeat")
                self._log("ERROR", "Inactive command not sent: MAVLink link unavailable")
            else:
                self._transport.send_actuator_command(params)
                command_applied = True
                changed = ", ".join(
                    f"{component.value}={'active' if active else 'inactive'}"
                    for component, active in updates.items()
                )
                self._log("COMMAND", f"Sent actuator request: {changed}")
        else:
            command_applied = True
            changed = ", ".join(
                f"{component.value}={'active' if active else 'inactive'}"
                for component, active in updates.items()
            )
            self._log("SIM", changed)

        if command_applied:
            self._state.update(updates)
            self._state_changed(self.state)

    def stop_all(self, reason: str = "Operator STOP ALL") -> None:
        self._log("SAFETY", reason)
        self.set_components({component: False for component in Component})

    def mark_all_unknown(self, reason: str) -> None:
        self._state = {component: None for component in Component}
        self._state_changed(self.state)
        self._log("SAFETY", reason)

    def request_recovery_inactive(self) -> None:
        """Prioritized inactive request after link restoration.

        This never activates an output. It intentionally bypasses the normal
        operation-readiness check so a recovery reset can run while the safety
        monitor is holding all new operations behind recovery_required.
        """
        updates = {component: False for component in Component}
        if self._safety.mode is OperatingMode.SIMULATION:
            self._state.update(updates)
            self._state_changed(self.state)
            self._log("SIM", "Connection recovery: all actuators set inactive in simulation")
            return
        if not self._safety.link_connected:
            raise RuntimeError("Cannot send recovery reset without a current MAVLink heartbeat")
        values = {
            self._definitions[component].slot: self._definitions[component].inactive
            for component in Component
        }
        self._transport.send_actuator_command(build_actuator_parameters(values))
        self._state.update(updates)
        self._state_changed(self.state)
        self._log(
            "SAFETY",
            "Connection recovery: requested inactive values for all actuators; "
            "physical state is not verified",
        )
