# IFFS Ground Control

IFFS Ground Control is a compact Windows desktop console for laboratory control of the three generic PX4 actuators used by the Impulse Fire Fighting System:

| IFFS component | Existing PX4 assignment | MAVLink command parameter |
|---|---|---|
| Water pump | MAIN 1 — Peripheral via Actuator Set 1 (301) | `param1` |
| Air filling solenoid | MAIN 2 — Peripheral via Actuator Set 2 (302) | `param2` |
| Firing solenoid | MAIN 3 — Peripheral via Actuator Set 3 (303) | `param3` |

The application does not change PX4 parameters. It starts in **Simulation Mode** on every launch. Live output is available only in explicitly authorized **PWM Bench Test Mode**.

> **Safety warning:** This software is not safety-rated. A timer expiry, STOP ALL request, or MAVLink acknowledgement does not prove that a physical output or valve is inactive. Loss of communication can prevent an inactive command from reaching PX4. Before any live operation, validate the physical coil-power interlock and independent electrical and pneumatic maximum-activation-time protection. Keep the real pump and solenoid drivers physically disconnected during PWM bench testing. Never bypass PX4 safety or arming restrictions.

## What is included

- Responsive PySide6 dashboard with Water Fill, Air Fill, Fill All, adjustable-duration Shoot, and persistent STOP ALL controls.
- Independent monotonic countdowns; Water and Air can run concurrently.
- Central actuator command boundary using `MAV_CMD_DO_SET_ACTUATOR` (187), normalized values, and `NaN` for unchanged actuator parameters.
- Compact flight telemetry for PX4 mode, GPS, multiple batteries, relative altitude, ground speed, and heading, with stale-value suppression.
- Threaded heartbeat/acknowledgement handling, system-ID discovery, armed/disarmed display, timeout detection, recovery, and event logging.
- One visible **IFFS ARM / SAFE** firing authorization. It automatically returns to SAFE on link loss and never represents the physical interlock.
- Optional, session-only RC switch control using MAVLink `RC_CHANNELS`, with configurable channel mapping, hysteresis, initial-high protection, STOP priority, and a diagnostic panel.
- Optional **Auto Reload** sequencing that starts the existing Fill All operation after a successfully completed Shoot and a configurable monotonic delay.
- Fail-closed connection recovery: active timers are cancelled, states become UNKNOWN, and an all-output inactive request is prioritized after reconnection.
- Compact actuator rows, collapsible secondary panels, and a fixed STOP ALL bar optimized for 1920×1080 and 1366×768 displays.
- Persistent connection, duration, reload-delay, and actuator-value configuration.
- Pure-Python unit tests for mapping, timers, state transitions, cancellation, and link failure.

## Windows installation

Install 64-bit Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/) and enable **Add Python to PATH** during setup. In PowerShell, from this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, either allow locally created scripts for your user or run the virtual-environment Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m iffs_ground_control
```

With the environment activated, start the app using:

```powershell
python -m iffs_ground_control
```

The application stores its working configuration at `%APPDATA%\IFFS Ground Control\config.json`. The repository's `config.example.json` documents every supported field. Authorization flags and PWM Bench Test Mode are deliberately not persisted.

## RC control

RC control is **disabled on every application start** and is never persisted or enabled automatically. Expand **RC CONTROL** to see the signal state, configured channels, live PWM values, and each switch state. A valid `RC_CHANNELS` stream must be present before the operator can explicitly select **Enable RC control**.

The default mapping is:

| Action | RC channel |
|---|---:|
| Water Fill | 11 |
| Air Fill | 12 |
| Fill All | 13 |
| Shoot | 14 |
| STOP ALL | 15 |

Each action is edge-triggered with hysteresis: below 1250 µs is the ready/reset position, 1250–1750 µs is the hold band, and above 1750 µs requests the action once. A switch must return below 1250 µs before it can trigger again. When RC control is enabled, re-enabled, restored after signal loss, or Shoot changes from IFFS SAFE to IFFS ARMED, a switch already high is ignored until it has first been observed low.

The RC adapter calls the same `OperationScheduler` used by the GUI. It does not send actuator commands directly. The STOP ALL switch is evaluated first and suppresses activation edges received in the same RC frame. Shoot still requires **IFFS ARM** and all existing operation conflicts and safety interlocks remain in force.

If required RC channels are missing, invalid, or older than `rc_timeout_seconds` (1.0 second by default), RC control disables itself and invalidates all switch states. If an RC-initiated operation is active and MAVLink communication is still alive, the existing STOP ALL path cancels all ongoing operations and requests every output inactive. A MAVLink heartbeat loss remains a separate condition and follows the connection-loss recovery process below.

The application normally listens passively for `RC_CHANNELS`. Set `rc_request_rate_hz` to a value from 5 through 10 only if the routed stream is absent and the vehicle should receive a single `MAV_CMD_SET_MESSAGE_INTERVAL` request after connection. The default is `0.0`, which sends no request and avoids changing link bandwidth.

### RC telemetry availability check

1. Connect through the existing MAVProxy endpoint and expand **RC CONTROL**. Do not enable RC control yet.
2. Confirm the status becomes **CONNECTED** and that CH11–CH15 change with the intended transmitter switches. **NO DATA**, **MISSING CHANNELS**, **INVALID VALUES**, and **SIGNAL STALE** remain distinct diagnostics.
3. If no `RC_CHANNELS` stream is present, first confirm message 65 is absent in QGroundControl's MAVLink Inspector on the same routed link. Then set `rc_request_rate_hz` to `5.0`, reconnect once, and check the event log for the one-shot interval request and its acknowledgement.
4. If PX4 still does not provide the message, leave RC control disabled and record the PX4/MAVLink stream configuration for later hardware investigation. This application does not alter PX4 stream profiles or repeatedly request rates.

Without hardware, run the automated suite described below. It constructs `RC_CHANNELS`-compatible messages and exercises the trigger state machines and existing scheduler entirely in Simulation Mode; no Pixhawk connection is required.

## Automatic reload

**Auto Reload is OFF on every application start** and its authorization is never persisted. Its delay is persisted and defaults to 1.0 second. When enabled before a Shoot begins, the sequence is:

1. The existing Shoot operation starts and uses its configured duration.
2. The Shoot timer expires and the existing inactive request is issued.
3. The state changes to **WAITING TO RELOAD** for the configured monotonic delay.
4. The existing Fill All operation starts Water and Air together with their independently configured durations.
5. After both software timers finish, the sequence briefly reports **COMPLETE** and returns to **IDLE**.

The GUI displays `IDLE`, `SHOOTING`, `WAITING TO RELOAD`, `RELOADING`, `COMPLETE`, `CANCELLED`, or `FAULT`, plus the applicable delay or Water/Air countdown. A timer finishing or a MAVLink acknowledgement is never treated as confirmation that the pneumatic system is charged, depressurized, or physically inactive.

GUI- and RC-triggered Shoot operations use the same sequence and the same central `OperationScheduler`. Automatic reload never initiates Shoot. While a sequence is active, conflicting manual and RC starts are blocked. STOP ALL, RC STOP ALL, connection loss, mode changes, command failures, and an aborted Shoot prevent pending reload. Disabling Auto Reload during the delay cancels the pending Fill All. Interrupted sequences are never restored after reconnection.

This feature remains limited to Simulation Mode and isolated PWM bench testing with the real pump and solenoid drivers disconnected. It depends on the ground-station MAVLink path and is not an onboard failsafe.

## Modes and operating sequence

### Simulation Mode

No MAVLink actuator command is transmitted. Connect to telemetry if desired to test the flight dashboard, or use the payload controls without a vehicle. Water, Air, and Shoot durations remain independently adjustable. Shoot requires the visible **IFFS ARM** state; selecting ARM is the explicit action and no additional per-shot dialog is shown.

### PWM Bench Test Mode

1. Physically disconnect the real pump and both solenoid drivers. Connect only a PWM meter or appropriate isolated test equipment.
2. Start the app; it will be in Simulation Mode.
3. Connect to the app's routed UDP endpoint and wait for a green heartbeat indicator.
4. Select PWM Bench Test Mode and accept its warning.
5. Exercise one output at a time and independently verify its inactive and active pulse widths. Selecting PWM Bench Test Mode and accepting its warning is the session authorization for Water/Air bench commands.
6. Select **IFFS ARM** only when firing-output bench testing is required.
7. Use STOP ALL after each test. Treat ACTIVE/INACTIVE as requested states, not physical feedback.

Shoot remains unavailable while **IFFS SAFE**. Its default duration is 1.0 second and its configurable minimum/maximum must be set from validated hardware constraints. Software timing is explicitly not an adequate firing cutoff.

### Connection-loss recovery

If a previously connected link is lost, the application immediately cancels all operation deadlines, clears pending command correlations, returns IFFS authorization to SAFE, and marks every physical actuator state UNKNOWN. No interrupted operation is resumed. Once a valid vehicle heartbeat returns, new operations remain blocked until the application has requested the configured inactive value for all three outputs.

That recovery request is not a hardware failsafe and does not prove that an output changed. During any link outage, use the independent hardware power interlock. The application cannot stop an output while no command path exists.

The example uses normalized `-1.0` for inactive and `+1.0` for active. With an approximately 1000–2000 µs PX4 output range these normally correspond to the endpoints, but the exact output must be verified with a PWM meter against the existing PX4 configuration.

## Running beside QGroundControl with MAVProxy

QGroundControl and this application must not both open the telemetry radio's COM port. Use MAVProxy as the single owner of the physical link and give each application its own bidirectional UDP endpoint.

It is cleanest to install MAVProxy in a separate virtual environment:

```powershell
py -3.11 -m venv "$env:LOCALAPPDATA\IFFS-MAVProxy"
& "$env:LOCALAPPDATA\IFFS-MAVProxy\Scripts\python.exe" -m pip install MAVProxy
```

For a radio on `COM14` at 57600 baud, start the router with:

```powershell
& "$env:LOCALAPPDATA\IFFS-MAVProxy\Scripts\mavproxy.exe" `
  --master=COM14,57600 `
  --out=udp:127.0.0.1:14550 `
  --out=udp:127.0.0.1:14551 `
  --source-system=250
```

Then:

1. Configure QGroundControl to listen on UDP port `14550` (its usual automatic UDP link may already do this).
2. Configure IFFS Ground Control as `udpin:127.0.0.1:14551` and select **Connect**.
3. Leave MAVProxy command forwarding enabled. It is enabled by default; `set mavfwd true` restores it if it was disabled.
4. Confirm that both clients show the same vehicle system ID and current armed state before bench testing.

If your radio uses another port or baud rate, replace `COM14,57600`. Give every local MAVLink client a distinct UDP port. Windows Firewall may need to allow Python/MAVProxy on private networks.

Do not use QGroundControl's one-way forwarding feature as the command path for this app. The two explicit MAVProxy outputs provide the return path needed for commands and acknowledgements.

## Connection strings

Typical pymavlink endpoints include:

- MAVProxy local output: `udpin:127.0.0.1:14551`
- MAVProxy TCP endpoint: `tcp:127.0.0.1:15761`
- Listen on all local interfaces: `udpin:0.0.0.0:14551`
- Direct serial for isolated testing without QGroundControl: `COM14` plus the configured baud field

Direct serial mode is intentionally not the recommended simultaneous-QGroundControl setup.

## Configuration

Copy values from `config.example.json` into the generated user configuration only after checking them. The actuator `slot` values select command parameters 1–6, not physical PWM channel numbers; PX4's existing Actuator configuration performs the mapping to MAIN outputs. `active` and `inactive` are normalized values in the inclusive range −1 to +1.

The Shoot duration defaults to 1.0 second. `shoot_duration_min_seconds` and `shoot_duration_max_seconds` constrain the GUI and must reflect independently validated hardware limits; they are not a safety cutoff.

`reload_delay_seconds` defaults to 1.0 and accepts 0 through 3600 seconds. Only the delay is persisted; the Auto Reload ON/OFF state always starts OFF.

The source system ID should be unique on the MAVLink network. The default component ID `190` identifies a mission-planner-style ground component. The app ignores GCS heartbeats when selecting its target vehicle.

RC channel numbers must be unique integers from 1 through 18. The five required keys are `water`, `air`, `fill_all`, `shoot`, and `stop_all`. Thresholds are microseconds. Configuration controls only how received RC data is interpreted; it does not modify PX4 RC assignments, MAVProxy routing, or actuator mappings.

## Tests

```powershell
python -m pytest -q
```

The tests use a fake transport and never open a MAVLink connection or send a hardware command.

The simulated suite covers RC decoding, missing/invalid/stale data, channel validation, one-shot edge triggering, hysteresis, initial-high protection, re-enable behavior, STOP priority, IFFS SAFE→ARM protection, STOP ALL on RC loss, and the optional one-shot message-rate request. Auto Reload tests cover GUI and RC Shoot, delay timing, independent Water/Air completion, disabled/restart behavior, command faults, conflicts, repeated requests, STOP and connection loss at every stage, reconnection without restart, configuration validation, collapsed panels, and both target display sizes.

## Remaining hardware validation

The following work deliberately remains for a controlled bench session; it was not claimed or simulated as physical validation:

- Confirm that the connected receiver/PX4 combination actually publishes channels 11–15 in `RC_CHANNELS` through the current MAVProxy route.
- Measure the low, center/hold, and high PWM values for every assigned switch and confirm adequate margin from both configured thresholds.
- Verify receiver failsafe behavior and measure how quickly RC loss becomes missing or stale at the application.
- Confirm that a high switch during application start, reconnect, signal restoration, re-enable, and IFFS SAFE→ARM never initiates an output.
- Confirm STOP priority and RC-loss cancellation using a PWM meter with the real pump and solenoid drivers physically disconnected.
- Verify the measured Shoot-inactive-to-Fill-All delay and independent Water/Air output durations with a PWM meter in isolated bench mode.
- Exercise STOP ALL and telemetry loss during Shoot, reload delay, and Fill All; confirm no sequence resumes after reconnection.
- Confirm that hardware pressure and timeout protections operate independently of the Windows application before considering any pressurized-payload use.
- Check routed-link bandwidth before enabling the optional 5–10 Hz message-rate request alongside QGroundControl.
- Revalidate the independent physical power interlock and electrical/pneumatic maximum-time protection before any energized actuator test.

## Verified protocol references

Implementation semantics were checked against the official documentation on 8 October 2026:

- [PX4 1.17 — Generic Actuator Control](https://docs.px4.io/v1.17/en/payloads/generic_actuator_control): command parameters 1–6 map to Peripheral via Actuator Set 1–6.
- [MAVLink Common — MAV_CMD_DO_SET_ACTUATOR](https://mavlink.io/en/messages/common.html#MAV_CMD_DO_SET_ACTUATOR): command ID 187, normalized −1 to +1 values, `NaN` to ignore, and actuator-set index in parameter 7.
- [MAVLink Command Protocol](https://mavlink.io/en/services/command.html): `COMMAND_LONG`/`COMMAND_ACK` behavior and result handling.
- [MAVLink Common — RC_CHANNELS](https://mavlink.io/en/messages/common.html#RC_CHANNELS): message ID 65, channel count, raw microsecond values, and the unavailable-channel marker.
- [MAVLink Common — MAV_CMD_SET_MESSAGE_INTERVAL](https://mavlink.io/en/messages/common.html#MAV_CMD_SET_MESSAGE_INTERVAL): command ID 511 and interval parameters used by the optional one-shot stream request.
- [MAVProxy Telemetry Forwarding](https://ardupilot.org/mavproxy/docs/getting_started/forwarding.html): multiple `--out` endpoints for local ground-station clients.
- [MAVProxy FAQ](https://ardupilot.org/mavproxy/docs/getting_started/faq.html): return-command forwarding is enabled by default and controlled by `mavfwd`.

## Architecture and extension point

`OperationScheduler` owns timing and conflict rules, `SafetyMonitor` owns software authorization/readiness, `ActuatorController` is the only actuator-command boundary, and `MavlinkClient` owns transport and protocol events. `RCInputMonitor` validates signal freshness and channel availability, while `RCTriggerController` owns switch-edge state. `AutoReloadController` reacts only to successful scheduler completion and calls the existing Fill All boundary after its delay. GUI, RC, and automatic requests therefore share one scheduler and one actuator-command path.
