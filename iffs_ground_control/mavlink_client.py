from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .command_mapping import MAV_CMD_DO_SET_ACTUATOR
from .rc import decode_rc_channels
from .telemetry import (
    decode_battery_status,
    decode_global_position_int,
    decode_gps_raw_int,
    decode_px4_flight_mode,
    decode_sys_status,
    decode_vfr_hud,
)

MAV_CMD_SET_MESSAGE_INTERVAL = 511
MAVLINK_MSG_ID_RC_CHANNELS = 65


@dataclass(frozen=True)
class MavlinkEvent:
    kind: str
    data: dict[str, Any]


@dataclass(frozen=True)
class LinkSnapshot:
    running: bool = False
    connected: bool = False
    target_system: int | None = None
    target_component: int | None = None
    armed: bool | None = None


@dataclass(frozen=True)
class PendingCommand:
    token: int
    sent_at: float


class CommandAckTracker:
    """Correlate shared-link ACKs with commands sent by this MAVLink client."""

    def __init__(
        self,
        source_system: int,
        source_component: int,
        *,
        pending_timeout: float = 10.0,
    ) -> None:
        self.source_system = source_system
        self.source_component = source_component
        self.pending_timeout = pending_timeout
        self._pending: dict[int, deque[PendingCommand]] = defaultdict(deque)
        self._next_token = 1
        self._lock = threading.Lock()

    def record(self, command: int, *, now: float | None = None) -> int:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            self._expire(timestamp)
            token = self._next_token
            self._next_token += 1
            self._pending[command].append(PendingCommand(token, timestamp))
            return token

    def cancel(self, command: int, token: int) -> None:
        with self._lock:
            pending = self._pending.get(command)
            if not pending:
                return
            self._pending[command] = deque(item for item in pending if item.token != token)
            if not self._pending[command]:
                self._pending.pop(command, None)

    def owns_ack(
        self,
        command: int,
        result: int,
        *,
        target_system: int = 0,
        target_component: int = 0,
        now: float | None = None,
    ) -> bool:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            self._expire(timestamp)
            # MAVLink 2 ACK extension fields identify the client that sent the
            # command. Non-zero mismatches are authoritative on a shared link.
            if target_system not in (0, self.source_system):
                return False
            if target_component not in (0, self.source_component):
                return False
            pending = self._pending.get(command)
            if not pending:
                return False
            # IN_PROGRESS may be followed by one or more progress/final ACKs.
            if result != 5:
                pending.popleft()
                if not pending:
                    self._pending.pop(command, None)
            return True

    def pending_count(self, command: int) -> int:
        with self._lock:
            return len(self._pending.get(command, ()))

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()

    def _expire(self, now: float) -> None:
        cutoff = now - self.pending_timeout
        for command in list(self._pending):
            pending = self._pending[command]
            while pending and pending[0].sent_at < cutoff:
                pending.popleft()
            if not pending:
                self._pending.pop(command, None)


class MavlinkClient:
    """Threaded pymavlink connection with a queue-based UI boundary."""

    def __init__(self) -> None:
        self.events: queue.Queue[MavlinkEvent] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: Any = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._snapshot = LinkSnapshot()
        self._heartbeat_timeout = 3.0
        self._ack_tracker = CommandAckTracker(252, 190)

    @property
    def snapshot(self) -> LinkSnapshot:
        with self._state_lock:
            return self._snapshot

    def connect(
        self,
        connection: str,
        *,
        baud: int,
        heartbeat_timeout: float,
        source_system: int,
        source_component: int,
    ) -> None:
        if self.snapshot.running:
            raise RuntimeError("MAVLink client is already running")
        self._stop.clear()
        self._heartbeat_timeout = heartbeat_timeout
        self._ack_tracker = CommandAckTracker(source_system, source_component)
        with self._state_lock:
            self._snapshot = LinkSnapshot(running=True)
        self._thread = threading.Thread(
            target=self._run,
            args=(connection, baud, source_system, source_component),
            name="mavlink-reader",
            daemon=True,
        )
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._connection = None
        self._ack_tracker.clear()
        with self._state_lock:
            self._snapshot = LinkSnapshot()
        self.events.put(MavlinkEvent("disconnected", {}))

    def send_actuator_command(self, params: tuple[float, ...]) -> None:
        snapshot = self.snapshot
        if not snapshot.connected or snapshot.target_system is None:
            raise RuntimeError("Cannot send command without a current heartbeat")
        connection = self._connection
        if connection is None:
            raise RuntimeError("MAVLink connection is unavailable")
        with self._send_lock:
            token = self._ack_tracker.record(MAV_CMD_DO_SET_ACTUATOR)
            try:
                connection.mav.command_long_send(
                    snapshot.target_system,
                    snapshot.target_component or 1,
                    MAV_CMD_DO_SET_ACTUATOR,
                    0,
                    *params,
                )
            except Exception:
                self._ack_tracker.cancel(MAV_CMD_DO_SET_ACTUATOR, token)
                raise

    def request_message_interval(self, message_id: int, rate_hz: float) -> None:
        """Make one best-effort MAVLink message-rate request for this connection."""
        if rate_hz <= 0:
            raise ValueError("Message request rate must be positive")
        snapshot = self.snapshot
        if not snapshot.connected or snapshot.target_system is None:
            raise RuntimeError("Cannot request a message interval without a current heartbeat")
        connection = self._connection
        if connection is None:
            raise RuntimeError("MAVLink connection is unavailable")
        interval_microseconds = round(1_000_000 / rate_hz)
        with self._send_lock:
            token = self._ack_tracker.record(MAV_CMD_SET_MESSAGE_INTERVAL)
            try:
                connection.mav.command_long_send(
                    snapshot.target_system,
                    snapshot.target_component or 1,
                    MAV_CMD_SET_MESSAGE_INTERVAL,
                    0,
                    float(message_id),
                    float(interval_microseconds),
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                )
            except Exception:
                self._ack_tracker.cancel(MAV_CMD_SET_MESSAGE_INTERVAL, token)
                raise

    def clear_pending_commands(self) -> None:
        self._ack_tracker.clear()

    def _set_snapshot(self, **changes: Any) -> None:
        with self._state_lock:
            current = self._snapshot.__dict__
            self._snapshot = LinkSnapshot(**(current | changes))

    def _queue_telemetry(
        self, group: str, decoder: Callable[[Any], dict[str, Any]], message: Any
    ) -> None:
        try:
            values = decoder(message)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            self.events.put(
                MavlinkEvent(
                    "telemetry_error",
                    {"group": group, "message": str(exc)},
                )
            )
            return
        self.events.put(MavlinkEvent("telemetry", {"group": group, "values": values}))

    def _run(
        self, connection_string: str, baud: int, source_system: int, source_component: int
    ) -> None:
        try:
            from pymavlink import mavutil

            self.events.put(MavlinkEvent("status", {"message": "Opening MAVLink connection"}))
            connection = mavutil.mavlink_connection(
                connection_string,
                baud=baud,
                source_system=source_system,
                source_component=source_component,
                autoreconnect=True,
            )
            self._connection = connection
            last_heartbeat: float | None = None
            timed_out = False
            opened_at = time.monotonic()

            while not self._stop.is_set():
                message = connection.recv_match(blocking=False)
                now = time.monotonic()
                if message is None:
                    heartbeat_age = (
                        now - last_heartbeat if last_heartbeat is not None else now - opened_at
                    )
                    if not timed_out and heartbeat_age > self._heartbeat_timeout:
                        timed_out = True
                        self._set_snapshot(connected=False, armed=None)
                        self.events.put(MavlinkEvent("timeout", {}))
                    self._stop.wait(0.02)
                    continue

                message_type = message.get_type()
                if message_type == "BAD_DATA":
                    continue
                if message_type == "HEARTBEAT":
                    # MAVLink routers also forward heartbeats from QGC and other
                    # clients. Only an autopilot heartbeat may become our target.
                    if (
                        message.type == mavutil.mavlink.MAV_TYPE_GCS
                        or message.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID
                    ):
                        continue
                    first = last_heartbeat is None or timed_out
                    last_heartbeat = now
                    timed_out = False
                    armed = bool(message.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                    system_id = message.get_srcSystem()
                    component_id = message.get_srcComponent()
                    try:
                        flight_mode = decode_px4_flight_mode(message)
                    except (AttributeError, TypeError, ValueError):
                        flight_mode = "UNKNOWN"
                    self.events.put(
                        MavlinkEvent(
                            "telemetry",
                            {"group": "heartbeat", "values": {"flight_mode": flight_mode}},
                        )
                    )
                    self._set_snapshot(
                        connected=True,
                        target_system=system_id,
                        target_component=component_id,
                        armed=armed,
                    )
                    if first:
                        self.events.put(
                            MavlinkEvent(
                                "heartbeat",
                                {
                                    "system_id": system_id,
                                    "component_id": component_id,
                                    "armed": armed,
                                },
                            )
                        )
                    else:
                        self.events.put(MavlinkEvent("armed", {"armed": armed}))
                elif message_type == "COMMAND_ACK":
                    command = int(message.command)
                    result = int(message.result)
                    target_system = int(getattr(message, "target_system", 0) or 0)
                    target_component = int(getattr(message, "target_component", 0) or 0)
                    owned = self._ack_tracker.owns_ack(
                        command,
                        result,
                        target_system=target_system,
                        target_component=target_component,
                    )
                    try:
                        command_name = mavutil.mavlink.enums["MAV_CMD"][command].name
                    except (KeyError, AttributeError):
                        command_name = f"MAV_CMD_{command}"
                    self.events.put(
                        MavlinkEvent(
                            "ack",
                            {
                                "command": command,
                                "command_name": command_name,
                                "result": result,
                                "owned": owned,
                                "target_system": target_system,
                                "target_component": target_component,
                            },
                        )
                    )
                elif message_type == "GPS_RAW_INT":
                    self._queue_telemetry("gps", decode_gps_raw_int, message)
                elif message_type == "BATTERY_STATUS":
                    self._queue_telemetry("battery", decode_battery_status, message)
                elif message_type == "SYS_STATUS":
                    self._queue_telemetry("battery", decode_sys_status, message)
                elif message_type == "GLOBAL_POSITION_INT":
                    self._queue_telemetry("position", decode_global_position_int, message)
                elif message_type == "VFR_HUD":
                    self._queue_telemetry("vfr", decode_vfr_hud, message)
                elif message_type == "RC_CHANNELS":
                    snapshot = self.snapshot
                    if (
                        not snapshot.connected
                        or message.get_srcSystem() != snapshot.target_system
                    ):
                        continue
                    try:
                        channels = decode_rc_channels(message)
                    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                        self.events.put(
                            MavlinkEvent(
                                "telemetry_error",
                                {"group": "RC_CHANNELS", "message": str(exc)},
                            )
                        )
                    else:
                        self.events.put(MavlinkEvent("rc_channels", {"channels": channels}))
        except Exception as exc:
            if not self._stop.is_set():
                self.events.put(MavlinkEvent("error", {"message": str(exc)}))
        finally:
            connection = self._connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._connection = None
            self._set_snapshot(running=False, connected=False, armed=None)


ACK_RESULT_NAMES = {
    0: "ACCEPTED",
    1: "TEMPORARILY_REJECTED",
    2: "DENIED",
    3: "UNSUPPORTED",
    4: "FAILED",
    5: "IN_PROGRESS",
    6: "CANCELLED",
}
