from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .models import Component


class AutoReloadState(str, Enum):
    IDLE = "IDLE"
    SHOOTING = "SHOOTING"
    WAITING = "WAITING TO RELOAD"
    RELOADING = "RELOADING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAULT = "FAULT"


@dataclass(frozen=True)
class AutoReloadSnapshot:
    enabled: bool
    state: AutoReloadState
    delay_remaining: float
    detail: str


class AutoReloadController:
    """Sequence Shoot completion into the existing Fill All operation."""

    _ACTIVE_STATES = {
        AutoReloadState.SHOOTING,
        AutoReloadState.WAITING,
        AutoReloadState.RELOADING,
    }

    def __init__(
        self,
        start_fill_all: Callable[[], None],
        *,
        delay_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("Reload delay cannot be negative")
        self.enabled = False
        self.state = AutoReloadState.IDLE
        self.delay_seconds = delay_seconds
        self.detail = "Automatic reload is off"
        self._start_fill_all = start_fill_all
        self._clock = clock
        self._deadline: float | None = None
        self._terminal_deadline: float | None = None

    @property
    def sequence_active(self) -> bool:
        return self.state in self._ACTIVE_STATES

    def set_enabled(self, enabled: bool) -> None:
        if not enabled and self.state in {
            AutoReloadState.SHOOTING,
            AutoReloadState.WAITING,
        }:
            self.cancel("Automatic reload disabled before reload started")
        self.enabled = enabled
        if enabled and not self.sequence_active:
            self.state = AutoReloadState.IDLE
            self.detail = "Ready for the next successful Shoot"
            self._terminal_deadline = None
        elif not enabled and self.state is AutoReloadState.IDLE:
            self.detail = "Automatic reload is off"

    def set_delay(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Reload delay cannot be negative")
        self.delay_seconds = seconds

    def shoot_started(self) -> None:
        if not self.enabled:
            return
        if self.sequence_active:
            raise RuntimeError("An automatic reload sequence is already active")
        self.state = AutoReloadState.SHOOTING
        self.detail = "Waiting for the Shoot timer to complete"
        self._deadline = None
        self._terminal_deadline = None

    def tick(
        self,
        completed: set[Component],
        active: set[Component],
    ) -> None:
        now = self._clock()
        if self.state is AutoReloadState.SHOOTING and Component.SHOOT in completed:
            if not self.enabled:
                self.cancel("Automatic reload was disabled")
                return
            self.state = AutoReloadState.WAITING
            self._deadline = now + self.delay_seconds
            self.detail = "Shoot inactive requested; waiting before Fill All"

        if self.state is AutoReloadState.WAITING:
            if not self.enabled:
                self.cancel("Automatic reload was disabled")
                return
            if self._deadline is not None and now >= self._deadline:
                try:
                    self._start_fill_all()
                except (RuntimeError, ValueError, OSError) as exc:
                    self.fail(f"Fill All could not start: {exc}")
                    return
                self.state = AutoReloadState.RELOADING
                self._deadline = None
                self.detail = "Fill All active; software timers do not confirm pressure"
                return

        if self.state is AutoReloadState.RELOADING and not active.intersection(
            {Component.WATER, Component.AIR}
        ):
            self.state = AutoReloadState.COMPLETE
            self.detail = "Reload timers completed; physical charge state is not verified"
            self._terminal_deadline = now + 1.0

        if (
            self.state in {AutoReloadState.COMPLETE, AutoReloadState.CANCELLED}
            and self._terminal_deadline is not None
            and now >= self._terminal_deadline
        ):
            self.state = AutoReloadState.IDLE
            self.detail = (
                "Ready for the next successful Shoot"
                if self.enabled
                else "Automatic reload is off"
            )
            self._terminal_deadline = None

    def cancel(self, reason: str) -> None:
        if not self.sequence_active:
            return
        self.state = AutoReloadState.CANCELLED
        self.detail = reason
        self._deadline = None
        self._terminal_deadline = self._clock() + 1.0

    def fail(self, reason: str) -> None:
        if not self.sequence_active:
            return
        self.state = AutoReloadState.FAULT
        self.detail = reason
        self._deadline = None
        self._terminal_deadline = None

    def snapshot(self) -> AutoReloadSnapshot:
        remaining = 0.0
        if self.state is AutoReloadState.WAITING and self._deadline is not None:
            remaining = max(0.0, self._deadline - self._clock())
        return AutoReloadSnapshot(self.enabled, self.state, remaining, self.detail)
