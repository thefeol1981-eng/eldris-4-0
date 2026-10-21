/*
 * ELDRIS 3.0 — wrist band firmware
 * --------------------------------
 * This is the only band, and it is worn on the LEFT wrist. It senses:
 *   - MPU6050       hand movement, tremor and falls (the Pi runs detection)
 *   - MAX30102      heart rate and blood oxygen (SpO2)
 *   - MLX90614      body temperature (non-contact infrared, aimed at the skin)
 * and posts a batch of IMU samples plus the latest vitals to the Raspberry Pi
 * every 250 ms with POST /api/motion.
 *
 * The wrist is part of the build, not a firmware setting: the dashboard, the
 * detection code and the alert text all say the left wrist. This firmware
 * reports DEVICE_ID "body-band", the only wearable the server accepts.
 *
 * Wiring (ESP32 DevKit v1, one shared I2C bus):
 *   All modules VCC -> 3V3, GND -> GND
 *   SDA -> GPIO21, SCL -> GPIO22
 *   MPU6050 AD0 -> GND (address 0x68)
 *   MLX90614 is fixed at address 0x5A
 *   Status LED -> GPIO2 (onboard LED)
 *
 * Libraries: ESP32 core (WiFi, HTTPClient, Wire) plus:
 *   - "SparkFun MAX3010x Pulse and Proximity Sensor" (MAX30102)
 *   - "Adafruit MLX90614" is NOT required; the temperature is read over raw I2C.
 * The MPU6050 is also driven over raw I2C on purpose: no extra library and
 * nothing to break when a library changes its API.
 *
 * Before flashing, set WIFI_SSID, WIFI_PASSWORD, API_URL and DEVICE_API_KEY.
 * API_URL is the FULL endpoint, so the same sketch works against a Pi on the
 * LAN (http://192.168.1.40:5000/api/motion) and against a public HTTPS host
 * (https://your-site.example.com/api/motion).
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <MAX30105.h>

// ---------------------------------------------------------------- settings --
static const char *WIFI_SSID     = "your-wifi";
static const char *WIFI_PASSWORD = "your-password";

// Full URL of the ELDRIS API. Use the LAN address of the machine running the
// app, or the public address if the app is published for free (see
// docs/DEPLOY-FREE.md). Never use localhost.
static const char *API_URL = "http://192.168.1.40:5000/api/motion";

// Must match DEVICE_API_KEY in backend/.env. Leave empty only if the API runs
// with no device key configured (then only a browser session can post).
static const char *DEVICE_API_KEY = "change-me-device-key";

// Must match the single band in backend/motion.py.
static const char *DEVICE_ID = "body-band";

static const uint8_t  MPU_ADDR     = 0x68;   // 0x69 when AD0 is tied to 3V3
static const uint8_t  MLX_ADDR     = 0x5A;   // MLX90614 default
static const uint16_t SAMPLE_HZ   = 50;      // 20-50 Hz is plenty for tremor
static const uint16_t POST_MS     = 250;     // upload cadence
static const uint8_t  BATCH       = 24;      // samples held between uploads
static const uint16_t VITALS_MS   = 1000;    // pulse/oximetry poll cadence

// MPU6050 registers we touch (raw I2C keeps this dependency-free).
static const uint8_t REG_PWR_MGMT_1 = 0x6B;
static const uint8_t REG_GYRO_CFG   = 0x1B;
static const uint8_t REG_ACCEL_CFG  = 0x1C;
static const uint8_t REG_ACCEL_XOUT = 0x3B;

static const float ACCEL_SCALE = 16384.0f;   // +/-2 g  -> LSB per g
static const float GYRO_SCALE  = 131.0f;     // +/-250 deg/s -> LSB per deg/s

// ------------------------------------------------------------------- state --
struct Sample {
  float ax, ay, az;   // in g
  float gx, gy, gz;   // in deg/s
};

static Sample buffer[BATCH];
static uint8_t buffered = 0;
static uint32_t lastSampleAt = 0;
static uint32_t lastPostAt = 0;
static uint32_t lastVitalsAt = 0;
static uint32_t lastWifiTry = 0;
static bool mpuReady = false;
static bool pulseReady = false;
static bool apiReached = false;

// One plain socket and one TLS socket; postBatch() picks by the URL scheme.
static WiFiClient plainClient;
static WiFiClientSecure tlsClient;

// Latest valid vitals, or NAN when the sensor has not produced one yet.
static float heartRate = NAN;
static float spo2 = NAN;
static float bodyTempC = NAN;

static MAX30105 pulse;

static void statusLed(bool on) { digitalWrite(2, on ? HIGH : LOW); }

// ---------------------------------------------------------------- MPU6050 ---
static bool mpuWrite(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool mpuBegin() {
  if (!mpuWrite(REG_PWR_MGMT_1, 0x00)) return false;   // wake up
  delay(50);
  mpuWrite(REG_PWR_MGMT_1, 0x01);                      // PLL with X gyro
  mpuWrite(REG_GYRO_CFG, 0x00);                        // +/-250 deg/s
  mpuWrite(REG_ACCEL_CFG, 0x00);                       // +/-2 g
  Wire.beginTransmission(MPU_ADDR);
  return Wire.endTransmission() == 0;
}

static bool mpuRead(Sample &out) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)14) != 14) return false;

  int16_t raw[7];
  for (uint8_t i = 0; i < 7; i++) {
    raw[i] = (int16_t)((Wire.read() << 8) | Wire.read());
  }
  out.ax = raw[0] / ACCEL_SCALE;
  out.ay = raw[1] / ACCEL_SCALE;
  out.az = raw[2] / ACCEL_SCALE;
  // raw[3] is the die temperature, not the body temperature.
  out.gx = raw[4] / GYRO_SCALE;
  out.gy = raw[5] / GYRO_SCALE;
  out.gz = raw[6] / GYRO_SCALE;
  return true;
}

// ----------------------------------------------------------- MAX30102 (HR) --
static void pulseBegin() {
  // The MAX30102 sits at 0x57 on the same bus as the MPU6050.
  pulseReady = pulse.begin(Wire, I2C_SPEED_FAST);
  if (!pulseReady) {
    Serial.println("[band] MAX30102 not found. Heart rate and SpO2 stay off.");
    return;
  }
  // ledMode 2 = red + IR, 4-sample average, 411 us pulse width, 100 Hz.
  pulse.setup(0x1F, 4, 2, 100, 411, 4096);
  pulse.setPulseAmplitudeRed(0x24);
  pulse.setPulseAmplitudeIR(0x24);
  pulse.setPulseAmplitudeGreen(0);
}

/** Poll the oximeter. Only believable values reach the Pi. */
static void pulsePoll() {
  if (!pulseReady) return;
  long ir = pulse.getIR();
  if (ir < 50000) return;                 // not touching the skin yet
  if (!pulse.checkForBeat(ir)) return;    // waits for the next beat
  float bpm = pulse.getHeartRate();
  float ox = pulse.getSpO2();
  if (bpm > 25 && bpm < 250) heartRate = bpm;
  if (ox >= 50 && ox <= 100) spo2 = ox;
}

// -------------------------------------------------------- MLX90614 (body) ---
/** Read the object temperature register over SMBus. Returns false if implausible. */
static bool readBodyTemperature(float &celsius) {
  Wire.beginTransmission(MLX_ADDR);
  Wire.write(0x07);                       // RAM address of object temperature 1
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MLX_ADDR, (uint8_t)3) != 3) return false;
  uint8_t lo = Wire.read();
  uint8_t hi = Wire.read();
  (void)Wire.read();                      // packet error code, ignored
  uint16_t raw = ((uint16_t)hi << 8) | lo;
  float kelvin = raw * 0.02f;             // MLX90614 resolution is 0.02 K
  float value = kelvin - 273.15f;
  if (value < 30.0f || value > 43.0f) return false;   // not aimed at a body
  celsius = value;
  return true;
}

// -------------------------------------------------------------------- WiFi --
static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiTry < 5000) return;
  lastWifiTry = millis();
  Serial.println("[band] connecting to Wi-Fi…");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// ------------------------------------------------------------------- upload --
static void appendOptional(String &body, const char *key, float value, uint8_t digits) {
  if (isnan(value)) return;
  body += ",\"";
  body += key;
  body += "\":";
  body += String(value, digits);
}

static void postBatch() {
  if (buffered == 0) return;
  if (WiFi.status() != WL_CONNECTED) {
    buffered = 0;                       // drop rather than grow without bound
    return;
  }

  // Vitals ride along with the samples. The only required fields are the IMU
  // axes; anything absent keeps its previous value on the Pi.
  String body = "{\"device\":\"";
  body += DEVICE_ID;
  body += "\"";
  appendOptional(body, "heart_rate_bpm", heartRate, 0);
  appendOptional(body, "spo2_pct", spo2, 1);
  appendOptional(body, "body_temperature_c", bodyTempC, 1);
  body += ",\"samples\":[";
  for (uint8_t i = 0; i < buffered; i++) {
    if (i) body += ',';
    body += "{\"ax\":" + String(buffer[i].ax, 4);
    body += ",\"ay\":" + String(buffer[i].ay, 4);
    body += ",\"az\":" + String(buffer[i].az, 4);
    body += ",\"gx\":" + String(buffer[i].gx, 2);
    body += ",\"gy\":" + String(buffer[i].gy, 2);
    body += ",\"gz\":" + String(buffer[i].gz, 2);
    body += "}";
  }
  body += "]}";

  HTTPClient http;
  const bool secure = String(API_URL).startsWith("https://");
  // Public hosts use a normal CA-signed certificate. setInsecure() skips
  // validation so no root CA has to be baked into the sketch; the payload is
  // health telemetry, not a secret, but do not reuse this for credentials.
  if (secure) tlsClient.setInsecure();
  http.begin(secure ? (WiFiClient &)tlsClient : (WiFiClient &)plainClient, API_URL);
  http.setTimeout(secure ? 8000 : 4000);
  http.setReuse(true);
  http.addHeader("Content-Type", "application/json");
  if (strlen(DEVICE_API_KEY) > 0) {
    http.addHeader("X-Device-Key", DEVICE_API_KEY);
  }
  int code = http.POST(body);
  if (code < 200 || code >= 300) {
    Serial.printf("[band] upload failed, HTTP %d -> %s\n", code, API_URL);
    apiReached = false;
    // Keep the samples: the next tick retries with the same buffer.
  } else {
    if (!apiReached) {
      Serial.printf("[band] connected to %s\n", API_URL);
      apiReached = true;
    }
    buffered = 0;
  }
  http.end();
}

// --------------------------------------------------------------------- setup --
void setup() {
  Serial.begin(115200);
  pinMode(2, OUTPUT);
  statusLed(false);

  Wire.begin(21, 22);
  Wire.setClock(400000);

  mpuReady = mpuBegin();
  if (!mpuReady) {
    Serial.println("[band] MPU6050 not found. Check SDA/SCL and AD0.");
  } else {
    Serial.printf("[band] %s sampling at %u Hz\n", DEVICE_ID, SAMPLE_HZ);
  }
  pulseBegin();

  WiFi.setHostname("eldris-band");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[band] posting to %s every %u ms\n", API_URL, POST_MS);
}

void loop() {
  uint32_t now = millis();
  ensureWifi();

  // Sample at a steady rate, independently of the network.
  if (now - lastSampleAt >= (1000 / SAMPLE_HZ)) {
    lastSampleAt = now;
    Sample sample;
    if (mpuRead(sample)) {
      mpuReady = true;
      if (buffered < BATCH) buffer[buffered++] = sample;
    } else {
      mpuReady = false;
    }
    pulsePoll();
  }

  // Heart rate and oxygen need their own continuous poll to catch beats.
  if (now - lastVitalsAt >= VITALS_MS) {
    lastVitalsAt = now;
    float temp;
    if (readBodyTemperature(temp)) bodyTempC = temp;
  }

  // Upload whatever has accumulated. A failed upload keeps the buffer so the
  // next tick retries; a longer outage drops it rather than growing forever.
  if (now - lastPostAt >= POST_MS) {
    lastPostAt = now;
    if (buffered == BATCH || (buffered > 0 && WiFi.status() == WL_CONNECTED)) {
      postBatch();
    }
    statusLed(WiFi.status() != WL_CONNECTED || !mpuReady);
  }
}
