# ELDRIS 3.0 — connecting the hardware to the web dashboard

Everything below feeds the same Flask API and appears in the dashboard without
touching the frontend. Each section ends with the page that shows the data and a
one-line check you can run before you believe it.

```
  ESP32 #1  left wrist band (MPU6050 + MAX30102 + MLX90614)
        --POST /api/motion--------------------------------------------┐
  ESP32 #2  drive board (motors)   <--HTTP /api/command--------------┤
  ESP32 #3  home climate (DHT22)   --POST /api/readings--------------┤
  ESP32 #4  chatbot (INMP441 mic, MAX98357A amp, speaker)
        --POST /api/voice/audio--------------------------------------┤
  ESP32-CAM front + rear body cameras  --HTTP MJPEG-----------------┤
                                                                     v
                                                     Raspberry Pi 4: Flask + SQLite
                                                          │  Socket.IO + REST
                                                          v
                                        React dashboard (browser on the LAN)
```

Two rules the code keeps everywhere:

1. **Nothing is invented.** Every number comes from a board that reported it.
   When a board is quiet the dashboard says *offline* or *no report*.
2. **Nothing leaves the local network.** Video is relayed, never stored; the
   cloud language model is optional and off by default.

---

## 0. One-time Pi setup

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip alsa-utils espeak-ng ffmpeg
cd ~/eldris/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env (see section 7)
python app.py                 # check http://<pi-ip>:5000/api/health
```

Give the ESP32 nodes a way in. The web dashboard uses a caregiver session; the
hardware cannot, so it presents a shared key instead:

```bash
# backend/.env
DEVICE_API_KEY=put-a-long-random-string-here
```

Generate one with `python3 -c "import secrets;print(secrets.token_hex(24))"`.
Without `DEVICE_API_KEY` the ingest endpoints stay session-only and the nodes
get HTTP 401 — that is the safe default.

## 1. One wrist band — heart rate, oxygen, temperature, tremor and falls

**One band, on the left wrist.** There is no second band and no side setting:
oxygen, body temperature, heart rate, tremor and falls all come from ESP32 #1.
It carries three sensors on the same I2C bus:

| Sensor | Measures |
|---|---|
| MPU6050 | Acceleration and rotation: tremor, falls, hand movement |
| MAX30102 | Heart rate and blood oxygen (SpO2) |
| MLX90614 | Body temperature (non-contact IR, aimed at the skin) |

**Wiring** (ESP32 DevKit v1, shared I2C bus):

| Signal | ESP32 |
|---|---|
| All modules VCC | 3V3 |
| All modules GND | GND |
| SDA (MPU6050, MAX30102, MLX90614) | GPIO21 |
| SCL (MPU6050, MAX30102, MLX90614) | GPIO22 |
| MPU6050 AD0 | GND (address 0x68) or 3V3 (0x69) |

Mount the MPU6050 flat against the wrist so the Z axis points along the arm:
a sudden Y change is then a realistic sideways fall signature, and the small
up/down wobble is the tremor signature. The MAX30102 needs firm skin contact
behind the watch body, and the MLX90614 must point at the skin rather than the
sleeve.

The left wrist is part of the build, not a setting: the detection code, the
alert text and the dashboard all name the same band. Only its battery label is
configurable:

```bash
# backend/.env
BAND_BATTERY_PCT=88   # shown in the device list until the band reports its own
```

**Firmware:** [`hardware/esp32_hand_band/esp32_hand_band.ino`](../hardware/esp32_hand_band/esp32_hand_band.ino)

Set `WIFI_SSID`, `WIFI_PASSWORD`, `API_URL` (the full `…/api/motion` URL) and `DEVICE_API_KEY`, then flash. `API_URL` may be a LAN address or a published HTTPS host — see [`DEPLOY-FREE.md`](DEPLOY-FREE.md).
`DEVICE_ID` is fixed at `body-band`. The sketch needs the SparkFun MAX3010x
library; the MPU6050 and MLX90614 are read over raw I2C. It samples at 50 Hz,
polls the oximeter continuously, reads body temperature every second, and posts
a batch every 250 ms.

**What the Pi does with it** — [`backend/motion.py`](../backend/motion.py)

| Detection | How it is decided |
|---|---|
| Tremor | The gravity axis of the hand has its slow average removed. If what is left oscillates in **4–12 Hz** above **0.02 g** and stays in that band for **2 s**, it is an episode. |
| Fall | A change of **≥ 0.8 g on the Y axis** inside 120 ms opens a confirmation window. It becomes a fall when the hand then changes orientation by **≥ 45°** or stays still; if the hand starts moving again it is downgraded to a knock. |
| Movement | Per-axis deviation from the local mean, in g. Hand rotation keeps the vector length near 1 g, so a magnitude-only measure would call a shaking hand "still". |

The band going quiet for more than `MOTION_OFFLINE_SECONDS` (15 s) is shown as
offline. Nothing is generated in its place.

**Check it**

```bash
curl -s -X POST http://127.0.0.1:5000/api/motion \
  -H 'Content-Type: application/json' -H "X-Device-Key: $DEVICE_API_KEY" \
  -d '{"device":"body-band","heart_rate_bpm":72,"spo2_pct":97.4,"body_temperature_c":36.7,
       "samples":[{"ax":0.02,"ay":0.01,"az":-1.0}]}' | head -c 400
```

Then open **Body & Motion** in the dashboard. The three rings should read those
values and the axes should move when the hand moves. A tremor episode or a fall
raises a caregiver alert and paints the dashboard red until it is acknowledged.

## 2. Home climate — temperature and humidity

One sensor covers the whole home. There is no per-room detection.

**Hardware:** DHT22 (or DHT11) data pin to GPIO4 with a 10 kΩ pull-up to 3V3.

**Firmware:** [`hardware/esp32_home_climate/esp32_home_climate.ino`](../hardware/esp32_home_climate/esp32_home_climate.ino)
(DHT sensor library by Adafruit + Adafruit Unified Sensor). It posts every 10 s.
Partial packets are fine — a field you do not send keeps its previous value.

**Check it**

```bash
curl -s -X POST http://127.0.0.1:5000/api/readings \
  -H 'Content-Type: application/json' -H "X-Device-Key: $DEVICE_API_KEY" \
  -d '{"temperature_c":23.4,"humidity_pct":48.1}'
```

Open **Dashboard**: the home climate card switches to *Live* and shows the board name.
The assistant will now quote those numbers — ask it "what is the temperature?"
or "how humid is it?".

## 3. Drive ESP32 — steering the robot over Wi-Fi

The chassis carries its own ESP32, `drive-esp`, shown as *ESP32 #2 · Drive
controller*. It owns the motors; the web app sends one command per click over
HTTP. The full protocol is in [`DRIVE_PROTOCOL.md`](DRIVE_PROTOCOL.md).

**Hardware:** one ESP32 dev board and an L298N (or equivalent) dual H-bridge.
IN1–IN4 to GPIO25, 26, 27, 14; ENA/ENB to GPIO32/33; a normally-closed
e-stop switch between GPIO34 and GND.

**Firmware:** [`hardware/esp32_drive/esp32_drive.ino`](../hardware/esp32_drive/esp32_drive.ino)

Set `WIFI_SSID`, `WIFI_PASSWORD`, `DEVICE_API_KEY` and (optionally)
`SERVER_URL` so the board also pushes its state outbound. Then flash, open the
serial monitor at 115200 baud, and note the IP it prints.

```bash
# backend/.env — then restart the API
DRIVE_ESP_IP=192.168.1.40            # the address the board printed
```

**Check it**

```bash
curl -s http://192.168.1.40/api/status
curl -s -X POST http://192.168.1.40/api/command \
  -H 'Content-Type: application/json' -d '{"command":"forward"}'
curl -s -X POST http://192.168.1.40/api/stop
```

Open **Robot Control**: the tag reads *Control ready*, each click is confirmed
by the board, and the telemetry panel shows the movement state and signal
strength. With the board offline the page says so and the pad stays disabled.

Give the board a DHCP reservation on the router. If its address changes, the
app cannot find it until `DRIVE_ESP_IP` is updated.

## 4. Cameras — live video on the Camera page

There are exactly two cameras, and both are mounted on the robot body so they
move with it: `cam-robot-front` and `cam-robot-rear`. Each is an **ESP32-CAM**
board with its own Wi-Fi connection, serving `/stream` (MJPEG) and `/snapshot`
(JPEG) on port 81. The server relays the picture to the browser, the camera
addresses never reach the page, and no frame is written to disk.

Flash both boards with an ESP32-CAM MJPEG server sketch (the ESP32 core's
`CameraWebServer` example, or your own) on 2.4 GHz Wi-Fi, then note the two
addresses and put them in `backend/.env`:

```bash
CAMERA_ESP_FRONT_IP=192.168.1.51
CAMERA_ESP_REAR_IP=192.168.1.52
CAMERA_ESP_PORT=81                  # default; change only if the sketch uses another
CAMERA_ESP_STREAM_PATH=/stream
CAMERA_ESP_SNAPSHOT_PATH=/snapshot
```

Both cameras travel with the robot, so there is nothing to add when it moves.
Give each board a DHCP reservation, exactly like the drive board.

**Check it:** open **Cameras** — the picture appears with a LIVE tag and the
segmented control switches between the front and rear body camera. In **Robot
Control** the selected camera sits above the directional pad. A failing camera
shows a "Stream unavailable" card instead of a blank box:

```bash
curl -s -o /tmp/frame.jpg -w '%{http_code} %{content_type}\n' \
  http://127.0.0.1:5000/api/camera/cam-robot-front/snapshot    # needs a session cookie
```

## 5. ESP32 #4 — the chatbot (microphone, amplifier, speaker) and the five help phrases

The room's voice is its own ESP32, shown as *ESP32 #4 · Chatbot*. It carries an
INMP441 I2S microphone, a MAX98357A I2S amplifier and a small speaker, and talks
to the Pi over Wi-Fi. The Pi does the listening and the thinking; the board is
the ears and the mouth.

**Wiring**

| Signal | ESP32 |
|---|---|
| INMP441 SCK / WS / SD | GPIO14 / GPIO15 / GPIO32 |
| INMP441 VDD / GND / L/R | 3V3 / GND / GND |
| MAX98357A BCLK / LRC / DIN | GPIO26 / GPIO27 / GPIO25 |
| MAX98357A VIN / GND | 5 V / GND |
| Speaker | MAX98357A + and − terminals |

**Firmware:** [`hardware/esp32_voice/esp32_voice.ino`](../hardware/esp32_voice/esp32_voice.ino)

Set `WIFI_SSID`, `WIFI_PASSWORD`, `AUDIO_URL` (`…/api/voice/audio`) and
`DEVICE_API_KEY`, then flash. The board waits for a loud block, records three
seconds, posts the PCM to the Pi, and plays the WAV that comes back. Its serial
log prints every transcript and every help phrase it hears.

**On the Pi** — speech-to-text and the spoken answer:

```bash
sudo install -y espeak-ng
pip3 install vosk
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip -d backend/
# backend/.env
VOSK_MODEL=vosk-model-small-en-us-0.15
```

`POST /api/voice/audio` wraps the raw PCM in a WAV header, transcribes it with
Vosk, runs the same assistant as the dashboard, and returns
`{"text":…, "reply":…, "help":…, "audio_wav":…}`. The reply audio is rendered
with `espeak-ng -w`; without espeak the text still comes back and the board
stays silent instead of playing noise. With no Vosk model the endpoint returns
HTTP 503 naming the missing piece — it never guesses at what was said.

### The five help phrases

Five phrases are watched for, and the longest match wins:

| # | Phrase |
|---|---|
| 1 | `help` |
| 2 | `help me` |
| 3 | `please help` |
| 4 | `i need help` |
| 5 | `i have fallen` |

They are set by `HELP_KEYWORDS` and matched on the Pi, offline, so no cloud
service has to hear the person. Hearing any one of them does three things:

1. **Alert.** A `help_request` alert at critical severity is written, so the
   dashboard paints red, the banner reads *Help requested*, and the caregiver
   can see it from anywhere on the LAN.
2. **Record.** A `HELP_RECORD_SECONDS` window opens on the board. The server
   keeps the transcript and the time; the audio never leaves the device.
3. **Move.** The robot is sent to `VOICE_PATIENT_ROOM` and creeps forward for
   at most `HELP_APPROACH_SECONDS` (4 s), then stops. The drive board also stops
   itself after 2 s without a new command. Set `HELP_APPROACH_SECONDS=0` to label
   the room without moving.

A repeat inside `HELP_COOLDOWN_SECONDS` is logged but not escalated twice.

**Check the whole chain without a microphone**

```bash
curl -s -X POST http://127.0.0.1:5000/api/voice/listen \
  -H 'Content-Type: application/json' -H "X-Device-Key: $DEVICE_API_KEY" \
  -d '{"text":"please, I need help","room":"bedroom"}'
```

`GET /api/voice/help` then shows `last_trigger.keyword` and the recording
window, and the Assistant page lists the five phrases under *Help words*.

### The Pi speaker still works too

`espeak-ng`, `espeak` and `say` also serve `POST /api/speak`, which the
Assistant page's *Speak the last reply* button calls. For another engine set
`TTS_COMMAND`, which must contain `{text}`. Text is capped by `TTS_MAX_CHARS`,
and no engine returns HTTP 503 with the exact fix rather than failing silently.

## 6. Raspberry Pi voice loop (optional)


The web Assistant page already offers browser speech input when the browser
supports it. A Pi microphone plus `hardware/pi_voice_loop.py` gives the same
conversation without a browser open:

```bash
sudo apt install -y alsa-utils espeak-ng
pip3 install vosk
arecord -D default -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav && aplay /tmp/test.wav   # mic check
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && unzip vosk-model-small-en-us-0.15.zip
ELDRIS_DEVICE_KEY=$DEVICE_API_KEY ELDRIS_VOSK_MODEL=vosk-model-small-en-us-0.15 python3 hardware/pi_voice_loop.py
```

The loop listens in four-second chunks, waits for the wake word ("ELDRIS"),
records the question, posts it to `POST /api/voice`, and the Pi speaks the
answer through the speaker. Every question is also written to the activity log,
so the caregiver can see what was asked on **Activity History**.

## 7. Environment reference (`backend/.env`)

```bash
# device ingress
DEVICE_API_KEY=                     # empty = nodes cannot post (session-only)
MOTION_SAMPLE_HZ=20                 # rate the band is assumed to send
BAND_BATTERY_PCT=88                 # shown until the band reports its own

# drive ESP32 #2
DRIVE_ESP_IP=192.168.1.40           # address the board printed on boot
DRIVE_TIMEOUT_SECONDS=2.5
DRIVE_POLL_SECONDS=2.0

# ESP32 #4 chatbot (microphone + amplifier + speaker)
VOICE_ESP_IP=192.168.1.54          # label only; the board posts outbound
HELP_KEYWORDS=help,help me,please help,i need help,i have fallen
HELP_RECORD_SECONDS=30             # how long the board keeps the audio locally
HELP_COOLDOWN_SECONDS=45           # stop one sentence raising five alerts
HELP_APPROACH_SECONDS=4            # 0 = label the room without moving
VOICE_PATIENT_ROOM=bedroom
VOICE_OFFLINE_SECONDS=120
VOICE_AUDIO_MAX_BYTES=900000
VOSK_MODEL=vosk-model-small-en-us-0.15

# ESP32-CAM boards (front and rear body cameras)
CAMERA_ESP_FRONT_IP=192.168.1.51
CAMERA_ESP_REAR_IP=192.168.1.52
CAMERA_ESP_PORT=81
CAMERA_ESP_STREAM_PATH=/stream
CAMERA_ESP_SNAPSHOT_PATH=/snapshot
CAMERA_TIMEOUT_SECONDS=5

# ESP32 #3 home climate: it posts outbound, so only the label uses the address
ESP32_HOME_IP=192.168.1.53

# speaker
TTS_COMMAND=                        # e.g. espeak-ng -v en -s 140 {text}
TTS_MAX_CHARS=400
TTS_TIMEOUT_SECONDS=15

# tremor (tune these to the person, not to the demo)
TREMOR_LOW_HZ=4.0
TREMOR_HIGH_HZ=12.0
TREMOR_AMPLITUDE_G=0.02
TREMOR_SUSTAIN_S=2.0
TREMOR_RELEASE_S=1.5
TREMOR_HIGH_G=0.10                  # RMS amplitude that raises a caregiver alert
TREMOR_MEDIUM_G=0.05

# fall
FALL_IMPACT_G=0.8                   # sudden Y change
FALL_FREEFALL_G=0.45
FALL_TILT_DEG=45
FALL_QUIET_G=0.06
FALL_CONFIRM_S=2.5
FALL_COOLDOWN_S=10
MOTION_OFFLINE_SECONDS=15
```

## 8. What each page shows

| Page | Data source |
|---|---|
| Dashboard | `GET /api/motion` (body vitals) and `GET /api/readings` (home climate) |
| Body & Motion | `GET /api/motion` — the one left-wrist band, live axes, tremor, fall events, vitals |
| Robot Control | `GET /api/camera` above the pad, plus `POST /api/movement` |
| Cameras | `GET /api/camera` + `/api/camera/<id>/stream` (two body cameras) |
| Assistant | `POST /api/chat`, `GET /api/voice/help` (the five phrases), optional `POST /api/speak`, optional microphone |
| Emergency state | `GET /api/emergency` — paints the whole dashboard red |
| Alerts | Falls and strong tremors are written here as caregiver alerts |
| Devices | `GET /api/devices` — every named board: left wrist band, drive controller, home climate, chatbot ESP32, both ESP32-CAMs |

## 9. When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Node gets HTTP 401 | No `DEVICE_API_KEY`, or the header is missing | Set the key in `backend/.env` and in the sketch, then reflash |
| Band shows *No report* | Wi-Fi, wrong `API_URL`, or a sensor does not answer | Serial monitor at 115200: look for `MPU6050 not found`, `MAX30102 not found` or the HTTP code (401 = device key mismatch, 404 = bad URL) |
| Heart rate and oxygen read `—` | MAX30102 is not touching the skin | Press it firmly behind the watch, wait a few seconds, and check `GET /api/motion` for a non-null `vitals.heart_rate_bpm` |
| Body temperature reads `—` | MLX90614 is aimed at a sleeve or the air | Point it at bare skin within a few centimetres; implausible values are rejected on the Pi |
| Band posts but never detects | The sample rate is too low or the band is not on the wrist | `GET /api/motion` shows `sample_rate_hz`; 0 Hz means no live packet reached the server |
| Falls detected constantly | Thresholds too tight for the mount | Raise `FALL_IMPACT_G`, or raise `FALL_TILT_DEG` so only a real orientation change confirms |
| Battery bumps flagged as falls | Same | `FALL_CONFIRM_S` and the knock downgrade path handle most of it — check the event list for `impact` (a knock) versus `fall` |
| Camera card says *no address yet* | `CAMERA_ESP_FRONT_IP` / `CAMERA_ESP_REAR_IP` are empty | Set them, restart the API, reload the page |
| Camera card says *Stream unavailable* | The ESP32-CAM is off, on the wrong network, or the address changed | `curl -I http://<cam-ip>:81/stream` from the server; check its 5 V supply |
| Drive pad is disabled | No `DRIVE_ESP_IP`, or the board is not answering | `curl http://<drive-ip>/api/status`; check Wi-Fi and the DHCP reservation |
| Speaker returns 503 | No engine on the Pi | `sudo apt install espeak-ng`, or set `TTS_COMMAND` |
| Voice loop hears nothing | Wrong ALSA device | `arecord -l`, then `ELDRIS_MIC_DEVICE=plughw:1,0` |
| Chatbot shows *No report* | The board has not posted since the API started | Check its serial log for the Wi-Fi status and the HTTP code; 401 means the device key differs |
| `POST /api/voice/audio` returns 503 | Vosk or the model is missing | `pip3 install vosk`, unzip a model into `backend/`, set `VOSK_MODEL`, restart |
| The board hears nothing back | espeak is not installed on the Pi | `sudo apt install espeak-ng`; `audio_wav` is null without it |
| A help phrase did nothing | It arrived inside `HELP_COOLDOWN_SECONDS` | Wait out the cooldown, or check `GET /api/voice/help` for `last_trigger` |

## Safety limits, stated plainly

- A fall or tremor notification **alerts a caregiver**. It is not a diagnosis,
  not a medical device, and not a guaranteed emergency call-out.
- The band only reports while it is worn, charged and in Wi-Fi range. No
  report is not the same as no fall.
- Heart rate, SpO2 and body temperature from a wrist-worn prototype are
  wellness cues, not clinical measurements. Do not use them to make a medical
  decision.
- Detection thresholds shipped here are starting points. Tune them with the
  person who will wear the band, and re-check them after changing the mount.
- A help phrase is not a phone call to an ambulance. It alerts the caregiver
  on this dashboard and moves the robot; it does not contact emergency
  services, and it cannot hear someone who cannot speak.
- The robot's approach after a help phrase is deliberately short and bounded,
  but it is still motion without a person at the controls. Keep a physical
  e-stop on the chassis, clear the path, and set `HELP_APPROACH_SECONDS=0` if
  the room is not safe for that.
- Keep motor control on a trusted private network with a physical emergency
  stop; this API's stop command is software.
