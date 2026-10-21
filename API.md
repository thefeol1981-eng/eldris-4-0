# ELDRIS 3.0 REST API

Base URL: `http://127.0.0.1:5000`. JSON request/response bodies. Error format: `{"error":"..."}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Public API health check |
| POST | `/api/auth/signup` | Create caregiver account (name, email, password) |
| POST | `/api/auth/signin` | Sign in and create session cookie |
| POST | `/api/auth/guest` | Start the local guest session the dashboard opens with |
| GET | `/api/auth/me` | Check current session (account or guest) |
| POST | `/api/auth/logout` | Clear current session |
| GET | `/api/status` | Drive ESP32, camera, movement and mode state |
| POST | `/api/movement` | Send one validated movement command |
| GET / POST | `/api/reminders` | List or create reminders |
| PATCH / DELETE | `/api/reminders/<id>` | Update or delete reminder |
| GET / POST | `/api/alerts` | List or create caregiver alert |
| PATCH | `/api/alerts/<id>` | Acknowledge or resolve alert |
| GET | `/api/activity` | Latest 250 activity events; optional `search` and `category` |
| GET | `/api/camera` | Camera availability, sources and privacy metadata |
| GET | `/api/camera/<id>/stream` | Relayed MJPEG video for one camera |
| GET | `/api/camera/<id>/snapshot` | Single JPEG frame from one camera |
| GET / POST | `/api/readings` | The single home temperature and humidity reading |
| GET | `/api/devices` | Full hardware inventory, including the wrist band |
| GET / POST | `/api/robot/location` | Read or move the robot (both body cameras follow it) |
| GET | `/api/robot/rooms` | Navigation zones the robot can be sent to (no sensors) |
| GET / POST | `/api/motion` | The one left-wrist band: vitals and IMU state, or push a batch of samples |
| GET | `/api/motion/events` | Stored falls, knocks and tremor episodes |
| GET | `/api/emergency` | One merged verdict used to paint the dashboard red |
| POST | `/api/emergency/acknowledge` | Acknowledge every open emergency alert |
| POST | `/api/speak` | Speak text through the Raspberry Pi speaker |
| POST | `/api/voice` | Voice loop: ask the assistant and speak the answer |
| GET | `/api/voice/help` | The five help phrases, the last one heard and the recording window |
| POST | `/api/voice/listen` | One transcript from the chatbot ESP32; escalates on a help phrase |
| POST | `/api/voice/audio` | Recorded audio from ESP32 #4: transcribe, answer, return WAV |
| POST | `/api/chat` | Ask the caregiver assistant |

Movement request: `{"command":"forward"}`. Allowed commands: `forward`, `reverse`, `left`, `right`, `stop`. Commands are rejected with HTTP 409 if the drive ESP32 has no address configured or is not answering; an unconfirmed command stops the motors.

Reminder request: `{"title":"Drink water","category":"Hydration","scheduled_at":"2026-10-12T08:00:00+05:30","enabled":true}`. Allowed categories: Medication, Hydration, Daily activity, Check-in, Appointment, Other.

Alert request: `{"type":"help_request","severity":"high","description":"Help button pressed","simulated":false}`. Severity: low, medium, high, critical. Alert status updates accept `acknowledged` or `resolved`.

## Authentication and device access

The dashboard opens without asking for credentials: on first load it calls `POST /api/auth/guest`, which creates a local guest session so the home can be watched immediately. `GET /api/auth/me` reports `{"authenticated":true,"guest":true}` for that session. A named caregiver account is optional and is used only when someone chooses to sign in through the sign-in page.

All robot, reminder, alert, camera, motion, audio and activity endpoints and Socket.IO connections require either a guest session or a signed-in caregiver session, except `/api/health` and the authentication endpoints. Session cookies are HttpOnly and SameSite=Lax, with an eight-hour lifetime. Passwords are stored as Werkzeug scrypt hashes, not plaintext.

A guest session has the same access as a caregiver account, which is appropriate for a local prototype but should be disabled or restricted before the API is exposed beyond a trusted network.

Hardware nodes cannot hold a browser session, so they present a shared key in the `X-Device-Key` header instead. This is accepted **only** on `/api/readings`, `/api/motion`, `/api/voice`, `/api/voice/listen`, `/api/voice/audio` and `/api/drive/telemetry`, **only** when `DEVICE_API_KEY` is set in `backend/.env`, and the comparison is constant-time. With no key configured those endpoints remain session-only and a node receives HTTP 401.

## Home climate

There is one climate sensor for the whole home, not one per room. `POST /api/readings` accepts partial packets; a missing field keeps its previous value:

```json
{"temperature_c":23.4,"humidity_pct":48.1,"battery_pct":76}
```

Values are validated into plausible bands (temperature −40…80 °C, humidity 0…100 %) and out-of-range fields are ignored rather than stored. An older node that still sends `room` is accepted, but its values land in the single home reading. `GET /api/readings` returns:

```json
{"home":{"temperature_c":23.4,"humidity_pct":48.1,"comfort":"comfortable",
  "humidity_state":"comfortable","online":true,"source":"live","updated_at":"…"},
 "summary":{"temperature_c":23.4,"humidity_pct":48.1,"comfort":"comfortable"},
 "simulated":false,"updated_at":"…"}
```

The sensor is marked offline when it has not reported within `ROOM_ONLINE_TIMEOUT_SECONDS`. `GET /api/robot/rooms` returns the navigation zones (`living`, `bedroom`, `kitchen`, `bathroom`, `hallway`); those are destinations for the robot, not sensor locations.

## Wrist band: motion, falls, tremor and vitals

There is one band and it is worn on the **left wrist**. It is the only source of heart rate, blood oxygen, body temperature, tremor and falls in the system; no second wearable exists and there is no side setting. `POST /api/motion` accepts one sample or a batch of up to 400, plus the latest vitals:

```json
{"device":"body-band","heart_rate_bpm":72,"spo2_pct":97.4,"body_temperature_c":36.7,
 "samples":[{"ax":0.02,"ay":0.01,"az":-1.0,"gx":3.1,"gy":0.4,"gz":0.2}]}
```

`ax/ay/az` are in g (±16 g accepted), `gx/gy/gz` in °/s, `t`/`ts` optional. Vitals may also be nested under `vitals`, and may appear on a sample. Heart rate is validated to 25…250 bpm, SpO₂ to 50…100 %, body temperature to 30…43 °C; invalid values are ignored. Device: `body-band`. A rejection returns HTTP 400 with the reason. A successful post returns HTTP 201 with the updated band snapshot (including `vitals`), any events it raised, and the current emergency verdict.

Detection runs on the Pi, not in the firmware, so thresholds are tunable through the environment:

- **Tremor** — the hand's gravity axis has its slow average removed; a dominant frequency inside `TREMOR_LOW_HZ`…`TREMOR_HIGH_HZ` above `TREMOR_AMPLITUDE_G`, held for `TREMOR_SUSTAIN_S`, is an episode. At that point an event is raised (so a strong tremor escalates immediately) and a second summary event is stored when it ends.
- **Fall** — a change of at least `FALL_IMPACT_G` on the Y axis within 120 ms opens a confirmation window. It becomes a `fall` when the hand then changes orientation by `FALL_TILT_DEG` or stays still; if the hand resumes moving first it is stored as an `impact` (a knock) at low severity instead.
- Events are stored in `motion_events` and returned by `GET /api/motion/events?kind=fall&limit=50` (`kind` may be `all`, `fall`, `impact` or `tremor`).
- A fall, or a tremor at high severity, also creates a caregiver alert (`fall_detected` / `tremor_detected`), rate-limited per device by `MOTION_ALERT_COOLDOWN_SECONDS`.
- A band that has not reported within `MOTION_OFFLINE_SECONDS` is marked offline (`online: false`, `source: "none"`). No sample data or vitals are generated in its place.

## Emergency state

`GET /api/emergency` merges open caregiver alerts with live hand motion into one verdict:

```json
{"active":true,"level":"critical","headline":"Fall detected",
 "detail":"Fall detected on the left hand: a sudden 2.6 g change on the y axis, then the hand stayed still",
 "since":"2026-10-12T07:31:02+00:00",
 "sources":[{"kind":"motion","type":"fall_detected","severity":"critical","at":"…","detail":"…"}],
 "checked_at":"…"}
```

`active` is true while an alert of type `fall_detected`, `tremor_detected`, `help_request`, `sos`, `panic_button` or `no_movement` is **open** with severity `high`/`critical`, or while the wrist band reports an **ongoing tremor**. The dashboard paints its whole palette red while this is true.

A fall therefore stays red until its alert is acknowledged or resolved — an unanswered fall keeps the alarm up, and acknowledging it clears the dashboard. An ongoing tremor keeps the dashboard red even after an acknowledgement, because it is still happening.

`POST /api/emergency/acknowledge` acknowledges every open emergency alert and returns the new verdict. Socket.IO emits `emergency_update` whenever the verdict changes.

## Cameras

`GET /api/camera` lists every camera with `stream_configured` and `snapshot_configured` flags. `GET /api/camera/<id>/stream` relays the camera's own MJPEG stream byte for byte, using the upstream content type, with `Cache-Control: no-store`; the browser only ever sees this same-origin URL. `GET /api/camera/<id>/snapshot` fetches a single frame (capped by `CAMERA_SNAPSHOT_MAX_BYTES`). Both return HTTP 404 when no source is configured and HTTP 503 when the camera is unreachable. No frame is ever written to disk.

Exactly two cameras exist, and both are mounted on the robot body so they move with it: `cam-robot-front` and `cam-robot-rear`. `GET /api/camera` reports `follows_robot: true` for both. Sources are configured in `backend/.env`:

```
CAMERA_ESP_FRONT_IP=192.168.1.51   # ESP32-CAM serving /stream and /snapshot on port 81
CAMERA_ESP_REAR_IP=192.168.1.52
CAMERA_ESP_PORT=81
CAMERA_ESP_STREAM_PATH=/stream
CAMERA_ESP_SNAPSHOT_PATH=/snapshot
```

## Speaker and voice

`POST /api/speak` with `{"text":"Time for your medication"}` speaks through the Pi. The engine is auto-detected (`espeak-ng`, `espeak`, `say`) or set explicitly with `TTS_COMMAND`, which must contain `{text}`. Text is capped by `TTS_MAX_CHARS`. HTTP 503 is returned with the exact remedy when no engine is installed or the engine fails.

`POST /api/voice` is the microphone loop: `{"message":"any falls today","speak":true}` runs the same assistant as the dashboard, speaks the reply, logs the question to the activity feed, and returns `{"reply":…,"source":…,"spoken":…,"help":null}`. If the message contains a help phrase, `help` carries what was heard and the reply is the escalation confirmation instead.

## Chatbot ESP32 and the five help phrases

ESP32 #4 is the voice node: an INMP441 microphone, a MAX98357A amplifier and a speaker. Two endpoints accept what it heard.

`POST /api/voice/listen` takes a transcript:

```json
{"text":"please, I need help","room":"bedroom","device":"voice-esp"}
```

It returns the match and the escalation state:

```json
{"result":{"heard":"please, I need help","keyword":"please help","escalated":true,
  "cooldown_seconds_left":45,
  "trigger":{"keyword":"please help","heard":"please, I need help","at":"…","device":"voice-esp"},
  "recording":{"active":true,"seconds_left":30,"window_seconds":30,
               "stores_audio_on_server":false}},
 "emergency":{"active":true,"level":"critical","headline":"Help requested","…":"…"}}
```

Hearing a phrase does three things, in this order:

1. a `help_request` alert at `critical` severity is written (so `GET /api/emergency` goes red with the headline *Help requested*);
2. a `HELP_RECORD_SECONDS` recording window opens on the board — the server stores the transcript and the time, never the audio;
3. the robot is sent to `VOICE_PATIENT_ROOM` and creeps forward for at most `HELP_APPROACH_SECONDS`, then stops. If the drive board is not answering, the alert still stands and nothing moves.

A repeat inside `HELP_COOLDOWN_SECONDS` returns `"escalated": false` so one sentence cannot raise five alerts.

`POST /api/voice/audio` takes the recording itself as the request body — 16-bit mono PCM or a WAV file, up to `VOICE_AUDIO_MAX_BYTES`, sample rate in `X-Sample-Rate` (default 16000). It transcribes with Vosk, answers with the same assistant, and returns:

```json
{"text":"what is my heart rate","reply":"From the left wrist band: heart rate 72 bpm, …",
 "source":"llm:openai","help":null,"spoken":null,"audio_wav":"UklGR…"}
```

`audio_wav` is base64 WAV rendered by `espeak-ng -w`, or `null` when no espeak engine is installed. With no Vosk model the endpoint returns HTTP 503 naming the missing piece rather than guessing at the words.

`GET /api/voice/help` returns the five phrases (`HELP_KEYWORDS`), whether the board has reported recently, the last trigger and the recording window:

```json
{"device":{"id":"voice-esp","name":"ESP32 #4 · Chatbot","online":true,"last_seen_at":"…"},
 "keywords":["help","help me","please help","i need help","i have fallen"],
 "last_trigger":{"keyword":"help me","heard":"someone help me","at":"…"},
 "recording":{"active":false,"seconds_left":0,"window_seconds":30},
 "approach":{"room":"bedroom","approach_seconds":4,"enabled":true},
 "patient_room":"bedroom"}
```

## Socket.IO

Emits `status_update`, `activity_update`, `reading_update` (home climate), `motion_update`, `voice_update`, and `emergency_update`. The frontend also polls REST endpoints every five seconds as a fallback, so a dropped socket degrades to slightly slower updates rather than a dead page.

## Deployment notes

Restrict local-network access and configure HTTPS plus a strong unique `SESSION_SECRET` before deployment. `GET /api/camera/<id>/stream` holds a connection open for as long as the browser watches, so put the API behind a process manager with enough worker threads rather than a single-threaded dev server.
