# Drive ESP32 — HTTP protocol

The robot chassis carries **one** ESP32 (device id `drive-esp`, named
*ESP32 #2 · Drive controller* in the Devices page). It joins Wi-Fi and runs a
small web server, so the web app drives it over the network. There is no USB
serial link and no Arduino in the current build.

Firmware: [`hardware/esp32_drive/esp32_drive.ino`](../hardware/esp32_drive/esp32_drive.ino)

## Endpoints on the board

Base URL: `http://<drive-esp-ip>` (port 80).

| Method | Path | Body | Reply |
|---|---|---|---|
| GET | `/api/status` | — | `{"movement":"idle","speed_pct":0,"rssi":-58,"estop":false}` |
| POST | `/api/command` | `{"command":"forward"}`, optional `{"speed":70}` | `{"ok":true,"movement":"forward"}` |
| POST | `/api/stop` | — | `{"ok":true}` |

Allowed commands: `forward`, `reverse`, `left`, `right`, `stop`. Anything else
stops the motors and returns HTTP 409. `movement` is `idle` when the board is
not driving.

## Endpoint on the app

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/drive/telemetry` | The board pushes the same status JSON here, with `X-Device-Key` |

Pushing outbound means a server that is not on the robot's LAN — a published
site, for example — still shows live robot state. The app polls
`GET /api/status` on the board when it can reach `DRIVE_ESP_IP` directly and
accepts pushed telemetry either way; the newer of the two wins.

## Safety rules, on both sides

- The **board** stops the motors when no command has refreshed them within 2 s,
  when the command word is unknown, or when the physical e-stop contact is open.
  Those checks stay in firmware so a dropped browser or Wi-Fi link cannot leave
  the motors running.
- The **app** refuses movement while the board is not answering, and sends
  `/api/stop` whenever a command is not confirmed.
- The software stop is not a safety device. Wire a normally-closed switch that
  interrupts motor power, and test it with the wheels lifted.

## Check it

```bash
# from the machine running the app, with the board powered
curl -s http://192.168.1.40/api/status
curl -s -X POST http://192.168.1.40/api/command \
  -H 'Content-Type: application/json' -d '{"command":"forward"}'
curl -s -X POST http://192.168.1.40/api/stop
```

Then open **Robot Control**: the status tag reads *Control ready*, each click
returns a confirmed message, and the telemetry panel shows the movement state.

## Configuration

```bash
# backend/.env
DRIVE_ESP_IP=192.168.1.40
DRIVE_TIMEOUT_SECONDS=2.5
DRIVE_POLL_SECONDS=2.0
COMMAND_TIMEOUT_SECONDS=2.0
```

`DRIVE_ESP_URL` overrides `DRIVE_ESP_IP` when the board is behind a reverse
proxy or a tunnel (`https://…`).
