/*
 * ELDRIS 3.0 — ESP32 #4, the chatbot node (microphone + amplifier + speaker)
 * ---------------------------------------------------------------------------
 * This board is the room's voice. It listens on an INMP441 I2S microphone,
 * notices when somebody speaks, records that sentence, and posts the recording
 * to the Raspberry Pi. The Pi transcribes it with Vosk, answers from the live
 * dashboard data (or the optional ChatGPT-class model), and — the important
 * part — checks it against the five help phrases:
 *
 *     help · help me · please help · i need help · i have fallen
 *
 * Hearing one of them raises a critical alert, opens a recording window and
 * sends the robot to the patient. The server replies with an audio clip, which
 * this board plays through the MAX98357A amplifier and speaker, so the person
 * hears "Help is on the way" from the robot itself.
 *
 * It is deliberately a thin client: no API key is stored in the sketch beyond
 * the shared device key, and no audio is kept on the board once it is sent.
 *
 * Wiring
 *   INMP441  VDD 3V3   GND GND   L/R GND        SCK GPIO14  WS GPIO15  SD GPIO32
 *   MAX98357A VIN 5V   GND GND   SD (channel)   BCLK GPIO26 LRC GPIO27 DIN GPIO25
 *   Speaker   + / - across the MAX98357A screw terminals
 *
 * Edit the four values below, then flash. Open the Serial Monitor at 115200.
 * Built against the ESP32 Arduino core 2.0.x (legacy driver/i2s.h API).
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <driver/i2s.h>

/* --------------------------------------------------------------- settings */
static const char *WIFI_SSID = "your-wifi";
static const char *WIFI_PASSWORD = "your-password";
/* Full URL of the API on the Pi, no trailing slash, e.g.
   http://192.168.1.50:5000/api/voice/audio  — or an HTTPS host. */
static const char *AUDIO_URL = "http://192.168.1.50:5000/api/voice/audio";
static const char *DEVICE_API_KEY = "same-key-as-backend-env";
static const char *DEVICE_ID = "voice-esp";

/* ---------------------------------------------------------------- pin map */
#define I2S_MIC_PORT I2S_NUM_0
#define MIC_SCK 14
#define MIC_WS  15
#define MIC_SD  32

#define I2S_SPK_PORT I2S_NUM_1
#define SPK_BCLK 26
#define SPK_LRC  27
#define SPK_DIN  25

#define SAMPLE_RATE 16000
#define RECORD_SECONDS 3
#define RECORD_SAMPLES (SAMPLE_RATE * RECORD_SECONDS)
#define READ_BLOCK 256                 /* int32 samples per I2S read */
#define SPEECH_RMS 350                 /* raise if the room is noisy */
#define SPEAKER_RATE 22050             /* espeak-ng default; parsed from the WAV */

static int16_t *recordBuffer = nullptr;
static bool micReady = false;
static bool ampReady = false;

/* ------------------------------------------------------------------ audio */
static void startMic() {
  i2s_config_t config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = READ_BLOCK,
      .use_apll = false,
      .tx_desc_auto_clear = false,
      .fixed_multi_bank = false,
  };
  if (i2s_driver_install(I2S_MIC_PORT, &config, 0, NULL) != ESP_OK) {
    Serial.println("[voice] microphone driver failed");
    return;
  }
  i2s_pin_config_t pins = {
      .mck_io_num = I2S_PIN_NO_CHANGE,
      .bck_io_num = MIC_SCK,
      .ws_io_num = MIC_WS,
      .data_out_num = I2S_PIN_NO_CHANGE,
      .data_in_num = MIC_SD,
  };
  i2s_set_pin(I2S_MIC_PORT, &pins);
  micReady = true;
}

static void startAmp() {
  i2s_config_t config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = SPEAKER_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 256,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_multi_bank = false,
  };
  if (i2s_driver_install(I2S_SPK_PORT, &config, 0, NULL) != ESP_OK) {
    Serial.println("[voice] amplifier driver failed");
    return;
  }
  i2s_pin_config_t pins = {
      .mck_io_num = I2S_PIN_NO_CHANGE,
      .bck_io_num = SPK_BCLK,
      .ws_io_num = SPK_LRC,
      .data_out_num = SPK_DIN,
      .data_in_num = I2S_PIN_NO_CHANGE,
  };
  i2s_set_pin(I2S_SPK_PORT, &pins);
  i2s_set_clk(I2S_SPK_PORT, SPEAKER_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_STEREO);
  ampReady = true;
}

/* Read one block and return its loudness. Fills `buffer` when not null. */
static float readBlock(int16_t *buffer, size_t want) {
  int32_t raw[READ_BLOCK];
  size_t bytes = 0;
  if (i2s_read(I2S_MIC_PORT, raw, sizeof(raw), &bytes, portMAX_DELAY) != ESP_OK) {
    return 0.0f;
  }
  size_t got = bytes / sizeof(int32_t);
  double sum = 0;
  for (size_t i = 0; i < got && i < want; i++) {
    /* INMP441 is 24-bit data left-justified in a 32-bit slot. */
    int16_t sample = (int16_t)(raw[i] >> 14);
    if (buffer) buffer[i] = sample;
    sum += (double)sample * (double)sample;
  }
  if (!got) return 0.0f;
  return (float)sqrt(sum / got);
}

static void recordUtterance() {
  size_t written = 0;
  while (written < RECORD_SAMPLES) {
    size_t want = min((size_t)READ_BLOCK, (size_t)(RECORD_SAMPLES - written));
    readBlock(recordBuffer + written, want);
    written += want;
  }
}

/* --- minimal WAV reader: play the data chunk of whatever the Pi returns --- */
static uint32_t le32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint16_t le16(const uint8_t *p) {
  return (uint16_t)(p[0] | (p[1] << 8));
}

static bool playWav(const uint8_t *wav, size_t length) {
  if (!ampReady || length < 44 || memcmp(wav, "RIFF", 4) != 0) return false;
  uint32_t rate = 0, dataOffset = 0, dataLength = 0;
  size_t at = 12;
  while (at + 8 <= length) {
    uint32_t size = le32(wav + at + 4);
    if (memcmp(wav + at, "fmt ", 4) == 0 && at + 20 <= length) {
      rate = le32(wav + at + 12);
    } else if (memcmp(wav + at, "data", 4) == 0) {
      dataOffset = at + 8;
      dataLength = min((size_t)size, (size_t)(length - dataOffset));
      break;
    }
    at += 8 + size + (size & 1);
  }
  if (!dataOffset || !dataLength) return false;
  if (rate < 8000 || rate > 48000) rate = SPEAKER_RATE;
  i2s_set_clk(I2S_SPK_PORT, rate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_STEREO);

  const int16_t *pcm = (const int16_t *)(wav + dataOffset);
  size_t samples = dataLength / sizeof(int16_t);
  int16_t stereo[512];
  size_t index = 0;
  while (index < samples) {
    size_t batch = min((size_t)(sizeof(stereo) / sizeof(stereo[0]) / 2), samples - index);
    for (size_t i = 0; i < batch; i++) {
      stereo[i * 2] = pcm[index + i];
      stereo[i * 2 + 1] = pcm[index + i];
    }
    size_t bytes = batch * 2 * sizeof(int16_t), sent = 0;
    i2s_write(I2S_SPK_PORT, stereo, bytes, &sent, portMAX_DELAY);
    index += batch;
  }
  i2s_zero_dma_buffer(I2S_SPK_PORT);
  return true;
}

/* --------------------------------------------------------------- base64 */
static int b64Value(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

static size_t decodeBase64(const String &in, uint8_t *out, size_t capacity) {
  size_t written = 0;
  uint32_t buffer = 0;
  int bits = 0;
  for (size_t i = 0; i < (size_t)in.length(); i++) {
    int value = b64Value(in[i]);
    if (value < 0) continue;
    buffer = (buffer << 6) | (uint32_t)value;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (written < capacity) out[written++] = (uint8_t)((buffer >> bits) & 0xFF);
    }
  }
  return written;
}

/* ----------------------------------------------------------- tiny JSON --- */
static String jsonString(const String &source, const char *key) {
  String needle = String("\"") + key + "\":\"";
  int at = source.indexOf(needle);
  if (at < 0) return "";
  at += needle.length();
  String out;
  bool escaped = false;
  for (; at < (int)source.length(); at++) {
    char c = source[at];
    if (escaped) {
      out += (c == 'n') ? '\n' : c;
      escaped = false;
      continue;
    }
    if (c == '\\') { escaped = true; continue; }
    if (c == '"') break;
    out += c;
  }
  return out;
}

/* ------------------------------------------------------------- the reply */
static void sendToPi() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[voice] no Wi-Fi, dropping the recording");
    return;
  }
  HTTPClient http;
  http.setTimeout(20000);
  if (!http.begin(AUDIO_URL)) {
    Serial.println("[voice] could not open the API URL");
    return;
  }
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("X-Device-Key", DEVICE_API_KEY);
  http.addHeader("X-Sample-Rate", String(SAMPLE_RATE));
  int code = http.POST((uint8_t *)recordBuffer, RECORD_SAMPLES * sizeof(int16_t));
  String body = http.getString();
  http.end();
  if (code != 200) {
    Serial.printf("[voice] API said %d: %s\n", code, body.substring(0, 160).c_str());
    return;
  }
  String heard = jsonString(body, "text");
  String reply = jsonString(body, "reply");
  String keyword = jsonString(body, "keyword");
  Serial.printf("[voice] heard \"%s\"\n", heard.c_str());
  if (heard.isEmpty()) return;
  Serial.printf("[voice] reply \"%s\"\n", reply.c_str());
  if (!keyword.isEmpty()) Serial.printf("[voice] HELP PHRASE: %s\n", keyword.c_str());

  String wav = jsonString(body, "audio_wav");
  if (wav.isEmpty()) {
    Serial.println("[voice] no audio in the reply; install espeak-ng on the Pi to hear it");
    return;
  }
  uint8_t *audio = (uint8_t *)malloc(wav.length());
  if (!audio) {
    Serial.println("[voice] not enough memory for the reply audio");
    return;
  }
  size_t length = decodeBase64(wav, audio, wav.length());
  playWav(audio, length);
  free(audio);
}

/* ------------------------------------------------------------------ main */
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[voice] ELDRIS chatbot node starting");

  recordBuffer = (int16_t *)malloc(RECORD_SAMPLES * sizeof(int16_t));
  if (!recordBuffer) {
    Serial.println("[voice] could not allocate the recording buffer");
  }
  startMic();
  startAmp();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[voice] joining Wi-Fi");
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 20000) {
    delay(400);
    Serial.print('.');
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " connected" : " FAILED — will retry in the loop");
  Serial.printf("[voice] %s at %s\n", DEVICE_ID, AUDIO_URL);
}

/* Wait for speech: one loud block opens a 3 s recording, then the Pi answers. */
void loop() {
  if (!micReady || !recordBuffer) {
    delay(1000);
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(1000);
    return;
  }
  static unsigned long cooldownUntil = 0;
  if (millis() < cooldownUntil) {
    delay(50);
    return;
  }
  float level = readBlock(nullptr, READ_BLOCK);
  if (level < SPEECH_RMS) {
    delay(5);
    return;
  }
  Serial.printf("[voice] speech detected (%.0f)\n", level);
  recordUtterance();
  sendToPi();
  /* Let the speaker settle and the room go quiet before listening again. */
  cooldownUntil = millis() + 1200;
}
