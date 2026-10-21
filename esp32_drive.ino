/*
 * ELDRIS 3.0 — drive ESP32 firmware
 * ---------------------------------
 * ESP32 #2 of the build: one board on the robot chassis that owns the motors.
 * It joins Wi-Fi and runs a small HTTP server, so the web app drives it over
 * the network instead of a USB cable:

 *   GET  http://<drive-ip>/api/status   -> {"movement":"idle","speed_pct":0,"rssi":-58}
 *   POST http://<drive-ip>/api/command  {"command":"forward"}
 *   POST http://<drive-ip>/api/stop
 *
 * The same board can also push its own telemetry outbound, which is what makes
 * the dashboard work when the server is not on this LAN (a published host, for
 * example). Set SERVER_URL to the app address; the board then posts to
 * SERVER_URL + /api/drive/telemetry every TELEMETRY_MS with X-Device-Key.
 *
 * Wiring (L298N or a comparable dual H-bridge):
 *   IN1 -> GPIO25, IN2 -> GPIO26   (left motor)
 *   IN3 -> GPIO27, IN4 -> GPIO14   (right motor)
 *   ENA -> GPIO32, ENB -> GPIO33   (PWM speed)
 *   Emergency stop -> GPIO34, wired to GND through a normally-closed switch.
 *                     Open switch = motors cut. Do this in hardware as well;
 *                     the software stop is not a safety device.
 *
 * Libraries: none beyond the ESP32 core (WiFi, WebServer, HTTPClient,
 * WiFiClientSecure).
 *
 * Before flashing set WIFI_SSID, WIFI_PASSWORD, DEVICE_API_KEY and, if you want
 * outbound telemetry, SERVER_URL (http://192.168.1.40:5000 or a public HTTPS
 * address; leave empty to disable).
 */

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

// ---------------------------------------------------------------- settings --
static const char *WIFI_SSID     = "your-wifi";
static const char *WIFI_PASSWORD = "your-password";

// The ELDRIS app, without a trailing slash. Empty disables outbound telemetry.
static const char *SERVER_URL    = "http://192.168.1.40:5000";

// Must match DEVICE_API_KEY in backend/.env.
static const char *DEVICE_API_KEY = "change-me-device-key";

static const uint16_t HTTP_PORT      = 80;
static const uint16_t TELEMETRY_MS   = 2000;   // outbound push cadence
static const uint16_t COMMAND_MS     = 2000;   // stop if nothing refreshes it
static const uint8_t  DEFAULT_SPEED  = 70;     // percent

// ------------------------------------------------------------------- pins ---
static const uint8_t IN1 = 25, IN2 = 26, IN3 = 27, IN4 = 14;
static const uint8_t ENA = 32, ENB = 33;
static const uint8_t ESTOP_PIN = 34;           // normally closed to GND

// ------------------------------------------------------------------- state --
static String movement = "idle";
static uint8_t speedPct = DEFAULT_SPEED;
static uint32_t lastCommandAt = 0;
static uint32_t lastTelemetryAt = 0;
static bool estopLatched = false;

static WebServer server(HTTP_PORT);

// ------------------------------------------------------------------ motors --
static void writeMotors(int leftPct, int rightPct) {
  leftPct  = constrain(leftPct,  -100, 100);
  rightPct = constrain(rightPct, -100, 100);

  if (estopLatched || millis() - lastCommandAt > COMMAND_MS) leftPct = rightPct = 0;

  digitalWrite(IN1, leftPct >= 0 ? HIGH : LOW);
  digitalWrite(IN2, leftPct >= 0 ? LOW : HIGH);
  digitalWrite(IN3, rightPct >= 0 ? HIGH : LOW);
  digitalWrite(IN4, rightPct >= 0 ? LOW : HIGH);

  ledcWrite(ENA, map(abs(leftPct),  0, 100, 0, 255));
  ledcWrite(ENB, map(abs(rightPct), 0, 100, 0, 255));

  if (leftPct == 0 && rightPct == 0) movement = "idle";
}

static void stopMotors() {
  movement = "idle";
  speedPct = 0;   // cleared speed reads as "stopped" in the dashboard
  writeMotors(0, 0);
  speedPct = DEFAULT_SPEED;   // keep the driver setting for the next command
}

// One command -> one movement. Anything unknown stops the board.
static bool applyCommand(const String &raw) {
  String cmd = raw;
  cmd.trim();
  cmd.toLowerCase();

  if (estopLatched) return false;

  if (cmd == "stop") { stopMotors(); lastCommandAt = millis(); return true; }

  int left = 0, right = 0;
  if      (cmd == "forward") { movement = "forward"; left =  speedPct; right =  speedPct; }
  else if (cmd == "reverse") { movement = "reverse"; left = -speedPct; right = -speedPct; }
  else if (cmd == "left")    { movement = "left";    left = -speedPct; right =  speedPct; }
  else if (cmd == "right")   { movement = "right";   left =  speedPct; right = -speedPct; }
  else { stopMotors(); return false; }

  lastCommandAt = millis();
  writeMotors(left, right);
  return true;
}

// ---------------------------------------------------------------- tiny JSON --
// No ArduinoJson dependency: the only body we accept is {"command":"forward"}
// with an optional {"speed":70}.
static String jsonString(const String &body, const char *key) {
  String needle = String("\"") + key + "\"";
  int k = body.indexOf(needle);
  if (k < 0) return "";
  int colon = body.indexOf(':', k + needle.length());
  if (colon < 0) return "";
  int firstQuote = body.indexOf('"', colon + 1);
  int secondQuote = firstQuote < 0 ? -1 : body.indexOf('"', firstQuote + 1);
  if (firstQuote < 0 || secondQuote < 0) return "";
  return body.substring(firstQuote + 1, secondQuote);
}

static long jsonNumber(const String &body, const char *key, long fallback) {
  String needle = String("\"") + key + "\"";
  int k = body.indexOf(needle);
  if (k < 0) return fallback;
  int colon = body.indexOf(':', k + needle.length());
  if (colon < 0) return fallback;
  return body.substring(colon + 1).toInt();
}

static String statusJson() {
  String out = "{\"movement\":\"";
  out += movement;
  out += "\",\"speed_pct\":";
  out += String(movement == "idle" ? 0 : speedPct);
  out += ",\"rssi\":";
  out += String(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
  out += ",\"estop\":";
  out += (estopLatched ? "true" : "false");
  out += "}";
  return out;
}

// ------------------------------------------------------------- web handlers --
static void handleStatus() {
  server.send(200, "application/json", statusJson());
}

static void handleCommand() {
  String body = server.arg("plain");
  String cmd = jsonString(body, "command");
  long requestedSpeed = jsonNumber(body, "speed", speedPct);

  if (requestedSpeed >= 20 && requestedSpeed <= 100) speedPct = (uint8_t)requestedSpeed;
  if (cmd.length() == 0) {
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"Send {\\\"command\\\":\\\"forward\\\"}.\"}");
    return;
  }
  // The board keeps its own interlock: an unknown word or an open e-stop stops it.
  if (!applyCommand(cmd)) {
    stopMotors();
    server.send(409, "application/json", estopLatched
      ? "{\"ok\":false,\"error\":\"Emergency stop is engaged.\"}"
      : "{\"ok\":false,\"error\":\"Unknown command; motors stopped.\"}");
    return;
  }
  String out = "{\"ok\":true,\"movement\":\"";
  out += movement;
  out += "\"}";
  server.send(200, "application/json", out);
}

static void handleStop() {
  stopMotors();
  lastCommandAt = millis();
  server.send(200, "application/json", "{\"ok\":true}");
}

static void handleNotFound() {
  server.send(404, "application/json", "{\"error\":\"Not found.\"}");
}

// -------------------------------------------------------------- networking --
static void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Wi-Fi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500);
    Serial.print('.');
  }
  Serial.println(WiFi.status() == WL_CONNECTED
    ? " connected: " + WiFi.localIP().toString()
    : " not connected — the server reports this board offline");
}

// Push status outbound so a remote server can show the robot without reaching
// into the LAN. Safe to fail: the board keeps driving locally either way.
static void pushTelemetry() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (String(SERVER_URL).length() == 0) return;

  String url = String(SERVER_URL) + "/api/drive/telemetry";
  HTTPClient http;
  bool tls = url.startsWith("https://");
  WiFiClient plain;
  WiFiClientSecure secure;

  if (tls) {
    secure.setInsecure();          // prototype: no clock to validate the chain
    http.begin(secure, url);
  } else {
    http.begin(plain, url);
  }
  http.addHeader("Content-Type", "application/json");
  if (strlen(DEVICE_API_KEY)) http.addHeader("X-Device-Key", DEVICE_API_KEY);
  http.setTimeout(4000);
  int code = http.POST(statusJson());
  if (code <= 0) Serial.printf("telemetry failed: %s\n", http.errorToString(code).c_str());
  http.end();
}

// --------------------------------------------------------------------- main --
void setup() {
  Serial.begin(115200);
  Serial.println("\nELDRIS drive ESP32 starting");

  pinMode(IN1, OUTPUT); pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT); pinMode(IN4, OUTPUT);
  pinMode(ESTOP_PIN, INPUT_PULLUP);

  ledcSetup(0, 20000, 8);   // 20 kHz keeps the motors quiet
  ledcSetup(1, 20000, 8);
  ledcAttachPin(ENA, 0);
  ledcAttachPin(ENB, 1);

  stopMotors();
  connectWifi();

  server.on("/api/status",  HTTP_GET,  handleStatus);
  server.on("/api/command", HTTP_POST, handleCommand);
  server.on("/api/stop",    HTTP_POST, handleStop);
  server.on("/",            HTTP_GET,  handleStatus);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.printf("HTTP server on port %u\n", HTTP_PORT);
}

void loop() {
  server.handleClient();
  if (WiFi.status() != WL_CONNECTED && millis() % 5000 < 20) connectWifi();

  // Physical emergency stop: open contact latches the board off until it closes.
  if (digitalRead(ESTOP_PIN) == HIGH) {
    if (!estopLatched) {
      estopLatched = true;
      stopMotors();
      Serial.println("Emergency stop engaged");
    }
  } else if (estopLatched) {
    estopLatched = false;
    Serial.println("Emergency stop released");
  }

  // Local command timeout: the motors never keep running without fresh input.
  if (!estopLatched && movement != "idle" && millis() - lastCommandAt > COMMAND_MS) {
    stopMotors();
  }

  if (millis() - lastTelemetryAt > TELEMETRY_MS) {
    lastTelemetryAt = millis();
    pushTelemetry();
  }
}
