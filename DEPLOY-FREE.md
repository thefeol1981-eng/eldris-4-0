# Publish ELDRIS 3.0 for free

There are three free ways to put this dashboard on the internet. They differ in
what they can do, so pick by what you need:

| Option | Cost | Public URL | Needs an account | Your PC must stay on | Camera + robot control |
|---|---|---|---|---|---|
| **A. Cloudflare quick tunnel** | free | new address each run | no | yes | yes, while your PC runs |
| **B. Render free web service** | free | permanent | yes (GitHub + Render) | no | no (cameras are on your LAN) |
| **C. Static preview (Sites)** | free | permanent | no | no | no, and no Python backend |

Option A is the fastest and needs no sign-up. Option B is the one to send to a
teacher or a friend if you want a link that keeps working after you close the
laptop.

---

## A. Cloudflare quick tunnel — one command, no account

Cloudflare's free `cloudflared` tunnel gives this app a real HTTPS address
without port forwarding, DNS or a paid host. Everything stays on your computer;
Cloudflare only forwards requests.

### Windows

Double-click **`publish-free.cmd`** in the project folder, or run:

```bat
python tools\publish_free.py
```

### macOS / Linux

```bash
./publish-free.sh
# or: python3 tools/publish_free.py
```

What the script does:

1. creates `backend/.venv` and installs `backend/requirements.txt` (first run only);
2. creates `backend/.env` with a generated `SESSION_SECRET` and `DEVICE_API_KEY`;
3. starts the Flask app and waits for `/api/health` to answer;
4. downloads `cloudflared` into `tools/` (first run only) and opens the tunnel;
5. prints your public address and the exact lines to paste into the sketch.

You will see something like:

```
 Dashboard   https://three-words-here.trycloudflare.com
 Band URL    https://three-words-here.trycloudflare.com/api/motion
```

Open the dashboard address on any phone or laptop. Keep the terminal window
open — closing it stops publishing. Press `Ctrl+C` to stop.

> The `trycloudflare.com` address is different every run. That is the trade-off
> for not having an account. The dashboard itself does not care, but the ESP32
> sketch holds the old address, so re-flash it (or change `API_URL`) after a
> restart. Use option A2 below if you want one address forever.

### A2. Permanent address with the same tunnel (free account)

1. Create a free Cloudflare account and add a domain (a cheap one, or a free
   subdomain from a provider that supports custom domains).
2. Install `cloudflared`, then:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create eldris
   cloudflared tunnel route dns eldris eldris.example.com
   cloudflared tunnel run --url http://127.0.0.1:5000 eldris
   ```
3. Put `https://eldris.example.com` in the sketch. This address is stable, and
   `cloudflared` can be installed as a service so it starts with the machine.

---

## B. Render free web service — permanent URL

The repository already contains a Dockerfile and a Render Blueprint. The
Blueprint is set to the **free** plan.

1. Put this folder in a GitHub repository (Render deploys from Git):
   ```bash
   git init
   git add .
   git commit -m "ELDRIS 3.0"
   git branch -M main
   git remote add origin https://github.com/<you>/eldris.git
   git push -u origin main
   ```
2. Sign in at <https://render.com> with GitHub.
3. **New → Blueprint**, pick the repository. Render reads `render.yaml`.
   It asks for `DEVICE_API_KEY`; type any long random string and keep it — the
   band sketch must use the same value.
4. Wait for the build (Docker: Node builds the frontend, then Python runs it).
   The service is live at `https://<name>.onrender.com`.

**Free-plan facts to know**

- It sleeps after 15 minutes with no traffic and takes ~50 seconds to wake.
  The wrist band posting every 250 ms keeps it awake while it is powered on.
- There is **no persistent disk** on the free plan, so SQLite lives in `/tmp`
  and accounts, reminders and alert history reset when the service restarts or
  redeploys. Vitals and motion are in-memory anyway, so the dashboard is
  unaffected. Move to a paid plan if you need history to survive.
- Set `DEVICE_API_KEY` and `DRIVE_ESP_IP` in the Render dashboard. There is no
  simulation: a board that has never reported stays *offline* until it posts.

To connect the band, use `https://<name>.onrender.com/api/motion` as `API_URL`.
Free Render serves HTTPS only, and the sketch handles that (see below).

---

## C. Static-only preview

`frontend/dist` is a complete static build. Dropping it on any static host
(GitHub Pages, Netlify drop, Cloudflare Pages, the Freebuff Sites panel) gives
you the interface, but every `/api/*` call fails, so the dashboard shows no
data. Use it for a visual preview only; use A or B for a working demo.

---

## Connecting the wrist band to whichever host you chose

**You do not need the ESP32's IP address.** The band opens an outbound HTTPS
connection to the server; nothing connects *into* the band. What the sketch
needs is the address of the app you just published.

In `hardware/esp32_hand_band/esp32_hand_band.ino`:

```cpp
static const char *WIFI_SSID     = "your-wifi";
static const char *WIFI_PASSWORD = "your-password";

// LAN (Raspberry Pi or this computer):
//   "http://192.168.1.40:5000/api/motion"
// Public host (tunnel or Render):
static const char *API_URL = "https://your-address/api/motion";

// Must equal DEVICE_API_KEY in backend/.env, or the key Render asked for.
static const char *DEVICE_API_KEY = "the-same-long-string";
```

Then flash the board and open the serial monitor at 115200 baud. A healthy boot
prints:

```
[band] body-band sampling at 50 Hz
[band] posting to https://your-address/api/motion every 250 ms
[band] connected to https://your-address/api/motion
```

The sketch uses `WiFiClientSecure` automatically when `API_URL` starts with
`https://`, so the same file works on the LAN and on a public host.

### What arrives on the dashboard

One ESP32 carries all four signals:

| Signal | Sensor | Where it appears |
|---|---|---|
| Tremor | MPU6050 (batched IMU samples) | tremor badge, frequency and amplitude, Body & Motion |
| Fall | MPU6050 | red emergency state, caregiver alert, motion history |
| Heart rate | MAX30102 | heart-rate ring and body card |
| Blood oxygen | MAX30102 | SpO₂ ring and body card |
| Body temperature | MLX90614 (raw I2C) | temperature ring and body card |

Detection runs on the server, not the ESP32: the band sends raw samples and the
backend decides. That keeps thresholds editable in `backend/.env` without
re-flashing, and the dashboard can show *why* an event fired.

**The ESP32 IP is only useful if you want to flash or debug the board** (for
example `esptool.py --port COM5` or opening its serial monitor). It plays no
part in the dashboard connection.

---

## The chatbot board on a public host

ESP32 #4 opens the same kind of outbound connection, but to
`/api/voice/audio` instead of `/api/motion`:

```cpp
static const char *AUDIO_URL      = "https://your-address/api/voice/audio";
static const char *DEVICE_API_KEY = "the-same-long-string";
```

Speech-to-text still runs on the machine serving the app: `pip3 install vosk`,
unzip a model into `backend/` and set `VOSK_MODEL` in `backend/.env`. A three
second recording is about 96 kB, which the free tunnel and the free Render plan
both accept (`VOICE_AUDIO_MAX_BYTES` caps it at 900 kB).

A help phrase still raises the alert on a public host, so a caregiver watching
from elsewhere sees *Help requested*. The robot cannot move there: the drive
board is on your LAN, and the drive address is not reachable from the internet.
That is deliberate — run motor control on the LAN.

---

## Checklist before you call it published

1. `https://<your-address>/api/health` returns `{"ok":true,...}`.
2. The dashboard opens without a login and shows the guest session.
3. The band's serial monitor prints `connected to ...` within a few seconds.
4. Body & Motion shows `Live` and a sample rate around 20–50 Hz.
5. Cover the oximeter with a finger: heart rate and SpO₂ fill in.
6. Tap the band sharply: a knock is logged, not a fall. Shake it at ~6 Hz for
   three seconds: a tremor episode appears.
7. Press `Ctrl+C` when finished. The tunnel closes and the public address stops
   answering immediately.

## Troubleshooting

- **`upload failed, HTTP 401`** — `DEVICE_API_KEY` in the sketch does not match
  the server. Re-copy it.
- **`upload failed, HTTP 404`** — `API_URL` is missing `/api/motion` or the
  smoke test URL is wrong.
- **TLS handshake fails** — the board's core is old. Update the ESP32 core in
  Arduino IDE; the sketch calls `setInsecure()`, so no CA bundle is required.
- **Render shows "Application failed to respond"** — the free instance was
  asleep; load the URL once and wait a minute.
- **Camera shows "Stream unavailable" on a public host** — expected: the
  ESP32-CAM boards live on your LAN. Run the app on the same network as the
  cameras, or use option A so the app and the boards share a machine.
