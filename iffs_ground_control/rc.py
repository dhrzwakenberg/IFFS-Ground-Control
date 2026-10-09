from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RCAction(str, Enum):
    WATER = "water"
    AIR = "air"
    FILL_ALL = "fill_all"
    SHOOT = "shoot"
    STOP_ALL = "stop_all"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class RCStatus(str, Enum):
    NO_DATA = "NO DATA"
    CONNECTED = "CONNECTED"
    MISSING = "MISSING CHANNELS"
    INVALID = "INVALID VALUES"
    STALE = "SIGNAL STALE"


class RCSwitchState(str, Enum):
    WAITING_FOR_LOW = "WAITING FOR LOW"
    READY = "READY"
    TRIGGERED = "TRIGGERED"
    WAITING_FOR_RESET = "WAITING FOR RESET"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class RCInputSnapshot:
    status: RCStatus
    values: dict[RCAction, int | None]
    age_seconds: float | None

    @property
    def usable(self) -> bool:
        return self.status is RCStatus.CONNECTED


@dataclass(frozen=True)
class RCTriggerResult:
    action: RCAction
    success: bool
    reason: str


def decode_rc_channels(message: Any) -> dict[int, int]:
    """Decode available RC_CHANNELS fields into one-based channel numbers."""
    channel_count = max(0, min(18, int(getattr(message, "chancount", 0))))
    channels: dict[int, int] = {}
    for channel in range(1, channel_count + 1):
        value = int(getattr(message, f"chan{channel}_raw", 0xFFFF))
        if value != 0xFFFF:
            channels[channel] = value
    return channels


class RCInputMonitor:
    """Track freshness and validity of the configured RC channels."""

    def __init__(
        self,
        channels: Mapping[str | RCAction, int],
        *,
        timeout_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels = {RCAction(action): int(channel) for action, channel in channels.items()}
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._last_values: dict[int, int] = {}
        self._last_update: float | None = None

    def ingest(self, channels: Mapping[int, int], *, now: float | None = None) -> None:
        self._last_values = {int(channel): int(value) for channel, value in channels.items()}
        self._last_update = self._clock() if now is None else now

    def invalidate(self) -> None:
        self._last_values.clear()
        self._last_update = None

    def snapshot(self, *, now: float | None = None) -> RCInputSnapshot:
        timestamp = self._clock() if now is None else now
        values = {
            action: self._last_values.get(channel)
            for action, channel in self.channels.items()
        }
        if self._last_update is None:
            return RCInputSnapshot(RCStatus.NO_DATA, values, None)
        age = max(0.0, timestamp - self._last_update)
        if age > self.timeout_seconds:
            return RCInputSnapshot(RCStatus.STALE, values, age)
        if any(value is None for value in values.values()):
            return RCInputSnapshot(RCStatus.MISSING, values, age)
        if any(value is not None and not 800 <= value <= 2200 for value in values.values()):
            return RCInputSnapshot(RCStatus.INVALID, values, age)
        return RCInputSnapshot(RCStatus.CONNECTED, values, age)


class RCTriggerController:
    """Convert hysteretic RC switch edges into single operation requests."""

    def __init__(
        self,
        dispatch: Callable[[RCAction], tuple[bool, str]],
        *,
        low_threshold: int = 1250,
        high_threshold: int = 1750,
    ) -> None:
        self._dispatch = dispatch
        self.low_threshold = low_threshold
        self.high_threshold = high_threshold
        self.enabled = False
        self.states = {
            action: RCSwitchState.WAITING_FOR_LOW for action in RCAction
        }

    def enable(self) -> None:
        self.enabled = True
        self.require_low()

    def disable(self) -> None:
        self.enabled = False
        self.require_low()

    def require_low(self, action: RCAction | None = None) -> None:
        actions = RCAction if action is None else (action,)
        for item in actions:
            self.states[item] = RCSwitchState.WAITING_FOR_LOW

    def process(self, values: Mapping[RCAction, int | None]) -> list[RCTriggerResult]:
        if not self.enabled:
            return []

        candidates: list[RCAction] = []
        for action in RCAction:
            value = values.get(action)
            if value is None:
                continue
            state = self.states[action]
            if value < self.low_threshold:
                self.states[action] = RCSwitchState.READY
            elif value > self.high_threshold:
                if state is RCSwitchState.READY:
                    candidates.append(action)
            elif state is RCSwitchState.TRIGGERED:
                self.states[action] = RCSwitchState.WAITING_FOR_RESET

        if not candidates:
            return []

        # STOP always wins a simultaneous edge. Other high switches are consumed
        # and must return low before they may request an operation.
        if RCAction.STOP_ALL in candidates:
            suppressed = [action for action in candidates if action is not RCAction.STOP_ALL]
            for action in suppressed:
                self.states[action] = RCSwitchState.BLOCKED
            candidates = [RCAction.STOP_ALL]

        results: list[RCTriggerResult] = []
        for action in candidates:
            success, reason = self._dispatch(action)
            self.states[action] = (
                RCSwitchState.TRIGGERED if success else RCSwitchState.BLOCKED
            )
            results.append(RCTriggerResult(action, success, reason))
        return results
