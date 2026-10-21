# ELDRIS 3.0
**Smart Assistance. Safer Everyday Care.**

A caregiver dashboard prototype for a semi-autonomous elderly-care companion robot. Every reading comes from a named ESP32 board; the build contains no simulation, and the interface says *offline* rather than inventing a number. The Flask backend exposes REST endpoints and Socket.IO updates, with SQLite persistence.

## Features
- React + Vite + TypeScript + Tailwind + React Router + Lucide
- Dashboard, Body & Motion, Robot Control, Cameras, Assistant, Reminders, Alerts, Devices, Activity History, Settings
- Flask REST API, Flask-SocketIO, SQLite persistence
- Zero-friction entry: the dashboard opens in a local guest session, with an optional caregiver Sign Up → Sign In flow, hashed passwords, HttpOnly session cookies and protected API/socket access
- No simulation: every reading comes from a named ESP32, and the dashboard says so when a board is offline
- Command timeout and stop-on-unconfirmed-command behavior for the drive ESP32
- One home climate sensor (ESP32 + DHT22) for temperature and humidity — no room-by-room tracking
- **One wrist band, on the left wrist**, with on-Pi **tremor** and **fall** detection plus **heart rate, blood oxygen and body temperature** — oxygen, temperature, heart rate, tremor and falls all come from this one ESP32
- A whole-dashboard **red emergency state** that appears while a fall or a strong tremor is unresolved
- **Two ESP32-CAM boards** on the robot body, streamed through the Pi (no frames stored), with the live view sitting above the driving pad
- A **chatbot ESP32** (#4) with a microphone, amplifier and speaker that runs the ChatGPT-style conversation, plus an optional Pi speaker and voice loop
- **Five help phrases** (`help`, `help me`, `please help`, `i need help`, `i have fallen`): hearing one raises a critical alert, opens a recording window and sends the robot to the patient
- Four ESP32 sketches (left wrist band, drive board, home climate, chatbot) plus two ESP32-CAM boards, the drive HTTP protocol, and a hardware wiring guide in `docs/HARDWARE.md`
## Interface theme

The caregiver UI uses an **Apple-inspired Liquid Glass** theme: a white canvas with a soft aurora backdrop, frosted translucent surfaces with specular edges, garnet and champagne accents, serif display type, springy page transitions, animated ring gauges for the body vitals, and a segmented camera control.

- All styling lives in `frontend/src/styles.css`; all markup lives in `frontend/src/App.tsx`.
- The API and payloads for this build are documented in `docs/API.md`.
- Motion is disabled automatically when the operating system requests `prefers-reduced-motion`.
- While an emergency is active the palette switches to alarm red through a single `data-emergency` attribute on `<html>`: every surface keeps its blur, hairline and specular edge, so the layout is identical and only the mood changes.

## Requirements
- Node.js 20+
- Python 3.10+
- Optional hardware: five ESP32 boards (left wrist band, drive controller, home climate, chatbot with mic and speaker) and two ESP32-CAM boards

## Run in VS Code

### 1. Backend
Run these commands in a terminal from the project root. For **Windows Command Prompt (CMD)**:
```cmd
cd backend
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
python app.py
```
For **Windows PowerShell**, activate with `.venv\Scripts\Activate.ps1` and copy the config with `Copy-Item .env.example .env`. For **macOS/Linux**, activate with `source .venv/bin/activate` and copy with `cp .env.example .env`.

Backend defaults to `http://127.0.0.1:5000`. It runs without any board attached; every device simply reports offline until its address is set and it posts.

### 2. Frontend
Open a second terminal:
```bash
cd frontend
npm install
npm run dev
```
Open the URL Vite prints (usually `http://localhost:5173`).

The Vite development proxy forwards `/api` and `/socket.io` to Flask. If the backend runs on another host, set `VITE_API_BASE_URL` in `frontend/.env.local` and configure the backend CORS origin accordingly.

## Publish it for free

**`docs/DEPLOY-FREE.md`** has the full guide. Two paths work today:

1. **Cloudflare quick tunnel — no account, one command.** Run `publish-free.cmd` (Windows) or `./publish-free.sh` (macOS/Linux). It installs the backend, starts Flask, and prints a public `https://….trycloudflare.com` address plus the exact firmware lines to paste. Your computer must stay on and the address changes each run.
2. **Render free web service — permanent URL.** Push this repository to GitHub, then create a Blueprint from `render.yaml` (already set to `plan: free`). Render builds the Dockerfile and serves the frontend and API from one HTTPS origin. The free plan sleeps after 15 minutes of idle traffic and has no disk, so SQLite data resets on restart.

In both cases the ESP32 band connects **outbound** to the published address; you never need to know or expose its IP. Set `DEVICE_API_KEY` on the host and use the same value in the sketch.

Do not enable robot motor control on an internet-facing service; deploy hardware control only on a trusted private network with independent physical safety controls. Camera streams also stay on the LAN — the server must be able to reach each ESP32-CAM address.

## Configuration
See `backend/.env.example` for every setting. Put each board's address there (`DRIVE_ESP_IP`, `CAMERA_ESP_FRONT_IP`, `CAMERA_ESP_REAR_IP`), keep `DEVICE_API_KEY` identical to the sketches, and set a strong unique `SESSION_SECRET`. Set `SESSION_COOKIE_SECURE=true` when serving over HTTPS. Keep `CORS_ORIGINS` restricted to trusted frontend origins.

## Safety notes
- Missing readings are shown as offline, never filled in with a plausible number.
- Movement is refused while the drive ESP32 is offline, and an unconfirmed command stops the motors.
- The software stop command is not a replacement for a physical emergency-stop switch.
- Motor safety checks also run on the drive ESP32 (unknown command, 2 s timeout, e-stop contact). Use a physical switch that independently interrupts motor power.
- This prototype is not a medical device and does not guarantee emergency response.
- A fall or tremor notification alerts a caregiver. It is not a diagnosis, and a band only reports while it is worn, charged and in range — no report is not the same as no fall.
- Camera video is relayed through the Pi and never written to disk; presence detection reports a face-presence flag only. No identity or emotion recognition is implemented.

## Authentication
- First visit opens the dashboard in a **local guest session**, so nobody has to sign in to watch the home. `POST /api/auth/guest` creates it.
- The sign-in page is still available (sidebar or top bar → **Sign in**). Accounts are optional and are used for a named caregiver session.
- Passwords are hashed with Werkzeug scrypt before storage; plaintext passwords are not stored.
- Session cookies are HttpOnly, SameSite=Lax, and expire after eight hours. Sign Out returns to the guest session.
- `GET /api/auth/me`, `POST /api/auth/signup`, `POST /api/auth/signin`, `POST /api/auth/guest`, and `POST /api/auth/logout` are authentication endpoints. Health check is public; caregiver/robot APIs and Socket.IO connections require the guest or a signed-in session.
- Local prototype note: the guest session has the same access as an account, which suits a trusted private network. Restrict network access (or disable the guest endpoint) before exposing the app publicly. Use HTTPS and a strong `SESSION_SECRET` for real deployment.

## API overview
- `GET /api/health`
- `GET /api/status`
- `POST /api/movement` body: `{"command":"forward|reverse|left|right|stop"}`
- `GET|POST /api/reminders`
- `PATCH|DELETE /api/reminders/<id>`
- `GET|POST /api/alerts`
- `PATCH /api/alerts/<id>` body: `{"status":"acknowledged|resolved"}`
- `GET|POST /api/readings` (home climate), `GET /api/devices`, `GET|POST /api/robot/location`, `GET /api/robot/rooms`
- `GET|POST /api/motion` (the one left-wrist band and its vitals), `GET /api/motion/events`
- `GET /api/emergency`, `POST /api/emergency/acknowledge`
- `GET /api/activity?search=&category=`
- `GET /api/camera`, `GET /api/camera/<id>/stream`, `GET /api/camera/<id>/snapshot`
- `POST /api/speak`, `POST /api/voice`, `GET /api/voice/help`, `POST /api/voice/listen`, `POST /api/voice/audio`, `POST /api/chat`

See `docs/API.md`, `docs/DRIVE_PROTOCOL.md` and `docs/HARDWARE.md`.

## One band: motion, vitals, falls and the emergency state

One ESP32 band is worn on the **left wrist**, and there is no second band. It carries an MPU6050 for movement, a MAX30102 for heart rate and SpO2, and an MLX90614 for body temperature, and posts samples plus the latest vitals to `POST /api/motion`. The Pi decides:

- **Tremor** — small up-and-down oscillation of the hand held inside 4–12 Hz for more than two seconds. An ongoing strong episode escalates immediately; episodes are stored with their duration, mean frequency and peak amplitude.
- **Fall** — a sudden change on the Y axis, optionally preceded by free-fall, confirmed when the hand then changes orientation or stays still. A hand that starts moving again is recorded as a knock instead, which is what keeps bumps out of the alarm list.
- Either a fall or a high-severity tremor writes a caregiver alert, a row in the motion history and an `emergency_update` socket event. While the emergency is active the whole dashboard turns red with a persistent banner and an **Acknowledge** action.

The dashboard shows the three vitals as ring gauges and repeats them on the **Body & Motion** page. Wrist-worn vitals are wellness cues, not clinical measurements.

Thresholds live in `backend/.env` and are documented in `docs/HARDWARE.md`; they are starting points and should be tuned with the person wearing the band.

## Connecting hardware

`docs/HARDWARE.md` walks through every path end to end: the left-wrist band (MPU6050 + MAX30102 + MLX90614) wiring and firmware, the drive ESP32 and its HTTP protocol, the single DHT22 home climate sensor, the two ESP32-CAM boards, the chatbot ESP32 with its microphone, amplifier and speaker, the five help phrases and what they trigger, the optional Pi voice loop, the `DEVICE_API_KEY` that lets a node post without a browser session, and a curl check for each one.

## Raspberry Pi deployment outline
1. Install Python 3, pip, the camera dependencies and `espeak-ng` for the speaker.
2. Copy `backend/` to the Pi and create a virtual environment.
3. Install `backend/requirements.txt` and fill `.env`: `DEVICE_API_KEY`, `DRIVE_ESP_IP`, `CAMERA_ESP_FRONT_IP`, `CAMERA_ESP_REAR_IP`, plus a restricted host and network.
4. Confirm the Pi can reach each board (`curl http://<esp-ip>/api/status`) before enabling motors.
5. Run `python app.py` for bench testing. For deployment, use a process manager such as systemd and a production WSGI/Socket.IO setup; do not expose the development server publicly. Camera streaming holds a long-lived response open, so give the WSGI setup enough threads.
5b. Flash the left-wrist band, the drive board, the home climate board, the chatbot board and both ESP32-CAMs, install `espeak-ng` and Vosk, and set `DEVICE_API_KEY` plus the board addresses — see `docs/HARDWARE.md` for the exact commands and a check for each path.
6. Connect the frontend to the Pi's private LAN address. Add caregiver authentication, HTTPS where appropriate, firewall rules, and operational monitoring before use outside a controlled prototype.
7. Test the physical emergency stop, command timeouts, Wi-Fi drops and power loss with wheels lifted and the area clear.

## Troubleshooting
- **Drive board offline:** the dashboard shows *Drive ESP32 is not answering*. Check the board is powered, on the same Wi-Fi, and that `DRIVE_ESP_IP` is its current address (the router may hand out a new one).
- **No sensor reading:** confirm `DEVICE_API_KEY` matches the sketch and that the board prints an HTTP 201 in its serial monitor. Missing readings are shown as offline, never as zero.
- **Frontend cannot reach API:** make sure Flask is running on port 5000 and Vite proxy is active; check browser console and backend logs.
- **Camera unavailable:** set `CAMERA_ESP_FRONT_IP` and `CAMERA_ESP_REAR_IP` in `backend/.env`, restart the API, and check `curl http://<cam-ip>:81/stream` from the server. The Camera page shows a *Stream unavailable* card when the server cannot reach the board.
- **Speaker returns 503:** no speech engine is installed; run `sudo apt install espeak-ng` or set `TTS_COMMAND`.
- **`/api/voice/audio` returns 503:** Vosk or its model is missing. `pip3 install vosk`, unzip a model into `backend/`, set `VOSK_MODEL`, restart the API.
- **Wrist band shows no report:** check `DEVICE_API_KEY` (matching the sketch), Wi-Fi, and the ESP32 serial monitor for `MPU6050 not found` or `MAX30102 not found`.
- **Chatbot shows no report:** the board has not posted since the API started. Check its serial log for the Wi-Fi status and HTTP code; 401 means the device key differs.
- **Heart rate or oxygen shows `—`:** the MAX30102 needs firm skin contact; wait a few seconds after putting the band on.
- **Body temperature shows `—`:** point the MLX90614 at bare skin within a few centimetres. Implausible values are rejected on the Pi.
- **Falls detected too readily:** raise `FALL_IMPACT_G` or `FALL_TILT_DEG` in `backend/.env`; events recorded as `impact` are knocks, not falls.
- **Socket updates missing:** verify `/socket.io` proxy and allowed origins. REST polling still updates the UI periodically.
