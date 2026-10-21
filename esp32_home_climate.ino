/*
 * ELDRIS 3.0 — home climate firmware (ESP32 + DHT22)
 * ----------------------------------------------------
 * ONE of these covers the whole home. It reads temperature and humidity every
 * ten seconds and posts them to the Raspberry Pi with POST /api/readings. The
 * dashboard shows a single home climate card; there is no per-room sensing.
 *
 * Wiring (ESP32 DevKit v1):
 *   DHT22 VCC  -> 3V3
 *   DHT22 GND  -> GND
 *   DHT22 DATA -> GPIO4 (10k pull-up from DATA to 3V3)
 *
 * Libraries: DHT sensor library (Adafruit) + Adafruit Unified Sensor.
 *
 * Set WIFI_SSID, WIFI_PASSWORD, API_URL and DEVICE_API_KEY below. API_URL is
 * the full endpoint, so it works against a Pi on the LAN
 * (http://192.168.1.40:5000/api/readings) and against a published host
 * (https://your-address/api/readings) — see docs/DEPLOY-FREE.md.
 * A partial packet is fine: the backend keeps the previous value for any field
 * you do not send.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <DHT.h>

// ---------------------------------------------------------------- settings --
static const char *WIFI_SSID     = "your-wifi";
static const char *WIFI_PASSWORD = "your-password";
static const char *API_URL       = "http://192.168.1.40:5000/api/readings";
static const char *DEVICE_API_KEY = "change-me-device-key";

static const uint8_t  DHT_PIN   = 4;
static const uint8_t  DHT_TYPE  = DHT22;
static const uint32_t REPORT_MS = 10000;   // upload cadence

// ------------------------------------------------------------------- state --
static DHT dht(DHT_PIN, DHT_TYPE);
static uint32_t lastReport = 0;
static uint32_t lastWifiTry = 0;
static uint8_t  failures = 0;
static WiFiClient plainClient;
static WiFiClientSecure tlsClient;

static void postReading() {
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  if (isnan(humidity) || isnan(temperature)) {
    failures++;
    Serial.printf("[home] DHT22 read failed (%u)\n", failures);
    if (failures > 20) {
      failures = 0;
      dht.begin();                       // DHT22 sometimes needs a re-init
    }
    return;
  }
  failures = 0;

  String body = "{\"temperature_c\":" + String(temperature, 1);
  body += ",\"humidity_pct\":" + String(humidity, 1);
  body += "}";

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[home] no Wi-Fi, skipping this report");
    return;
  }

  HTTPClient http;
  const bool secure = String(API_URL).startsWith("https://");
  // setInsecure() skips certificate validation so no root CA has to be baked
  // in; this sketch only publishes climate numbers, never credentials.
  if (secure) tlsClient.setInsecure();
  http.begin(secure ? (WiFiClient &)tlsClient : (WiFiClient &)plainClient, API_URL);
  http.setTimeout(secure ? 8000 : 4000);
  http.addHeader("Content-Type", "application/json");
  if (strlen(DEVICE_API_KEY) > 0) http.addHeader("X-Device-Key", DEVICE_API_KEY);
  int code = http.POST(body);
  Serial.printf("[home] %4.1f C  %4.1f %%  -> HTTP %d\n", temperature, humidity, code);
  http.end();
}

void setup() {
  Serial.begin(115200);
  dht.begin();
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiTry > 5000) {
    lastWifiTry = millis();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
  if (millis() - lastReport >= REPORT_MS) {
    lastReport = millis();
    postReading();
  }
  delay(50);
}
