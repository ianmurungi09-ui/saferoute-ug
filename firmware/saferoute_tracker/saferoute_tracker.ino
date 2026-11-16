/*
 * SafeRoute UG tracker: reference firmware for ESP32 + MPU6050 + NMEA GPS + GSM/LTE modem.
 *
 * !! NOT COMPILED OR BENCH-TESTED. This was written without an Arduino toolchain or hardware.
 * !! Treat it as a readable design to adapt, then bench-test thresholds on real vehicles and roads
 * !! (a boda-boda frame, a bus floor and a truck cab all vibrate very differently).
 *
 * Libraries: TinyGPSPlus, TinyGSM, ArduinoHttpClient, ArduinoJson (v7).
 * Wiring (change to suit your board):
 *   MPU6050  SDA=21 SCL=22 (I2C)        GPS    UART1 RX=18 TX=19, 9600 baud
 *   Modem    UART2 RX=16 TX=17          Buzzer GPIO25 (active)    Cancel button GPIO27 to GND
 *
 * What it does
 *   telemetry : 1 point / 2 s into a ring buffer, flushed in batches (store-and-forward when offline)
 *   potholes  : |acceleration magnitude - 1 g| above POTHOLE_G while moving -> /hazards/report
 *   crash     : impact above CRASH_G + big speed drop or rollover -> 20 s buzzer + CANCEL button
 *               -> /crash, and if the data call fails, a direct SMS to NEXT_OF_KIN
 * Mounting assumption: sensor X axis points to the vehicle's right. Power it on while stationary:
 * the first 2 s calibrate the "up" reference used for the rollover angle.
 */
#define TINY_GSM_MODEM_SIM800  // use TINY_GSM_MODEM_A7672X / SIM7600 for 4G
#include <Arduino.h>
#include <Wire.h>
#include <TinyGPSPlus.h>
#include <TinyGsmClient.h>
#include <ArduinoHttpClient.h>
#include <ArduinoJson.h>
#include <time.h>

// ------------------------------- configuration -------------------------------
static const char APN[]         = "internet";          // per network
static const char SERVER_HOST[] = "saferoute.example.org";
static const int  SERVER_PORT   = 80;                  // PRODUCTION: use TLS (4G modem with TinyGsmClientSecure, or a TLS gateway)
static const char DEVICE_ID[]   = "bus-001";
static const char DEVICE_KEY[]  = "PASTE_KEY_FROM_/admin/devices";
static const char NEXT_OF_KIN[] = "+256700000000";     // SMS fallback if the data call fails

static const float POTHOLE_G        = 0.6f;   // deviation from 1 g that counts as a hit
static const float POTHOLE_MIN_KMH  = 15.0f;
static const float CRASH_G          = 6.0f;   // peak magnitude that starts crash analysis
static const float CRASH_DV_KMH     = 20.0f;  // speed drop within the observation window
static const float CRASH_TILT_DEG   = 60.0f;  // rollover angle vs boot calibration
static const uint32_t CANCEL_WINDOW_MS = 20000;
static const uint32_t TELEMETRY_EVERY_MS = 2000, FLUSH_EVERY_MS = 30000;

#define PIN_BUZZER 25
#define PIN_CANCEL 27
#define MPU_ADDR 0x68

// ------------------------------- state -------------------------------
TinyGPSPlus gps;
HardwareSerial gpsSerial(1), modemSerial(2);
TinyGsm modem(modemSerial);
TinyGsmClient gsmClient(modem);

struct Pt { uint32_t ts; double lat, lon; float kmh, heading, ay; };
static Pt ring[120]; static int ringN = 0;           // oldest dropped when full
struct Hz { uint32_t ts; double lat, lon; float g, kmh; };
static Hz hazQ[10]; static int hazN = 0;

static float refX = 0, refY = 0, refZ = 1;            // gravity vector at rest
static float ax = 0, ay = 0, az = 1, gmag = 1;        // latest sample, in g
static float speedHist[6]; static uint32_t lastSpeedTick = 0;  // 1 Hz GPS speed history
static uint32_t lastTel = 0, lastFlush = 0, lastHaz = 0, peakWinStart = 0; static float peakDev = 0;

// ------------------------------- helpers -------------------------------
void feedGps() { while (gpsSerial.available()) gps.encode(gpsSerial.read()); }

void waitMs(uint32_t ms) { uint32_t t = millis(); while (millis() - t < ms) { feedGps(); delay(2); } }

uint32_t gpsEpoch() {
  if (!gps.date.isValid() || !gps.time.isValid() || gps.date.year() < 2024) return 0;
  struct tm t = {};
  t.tm_year = gps.date.year() - 1900; t.tm_mon = gps.date.month() - 1; t.tm_mday = gps.date.day();
  t.tm_hour = gps.time.hour(); t.tm_min = gps.time.minute(); t.tm_sec = gps.time.second();
  return (uint32_t)mktime(&t);                        // TZ is set to UTC in setup()
}

bool imuRead() {
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x3B); if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, 6) != 6) return false;
  int16_t rx = (Wire.read() << 8) | Wire.read(), ry = (Wire.read() << 8) | Wire.read(), rz = (Wire.read() << 8) | Wire.read();
  ax = rx / 2048.0f; ay = ry / 2048.0f; az = rz / 2048.0f;   // +/-16 g range: 2048 LSB/g
  gmag = sqrtf(ax * ax + ay * ay + az * az);
  return true;
}

float tiltDeg() {                                     // angle between current and boot-time gravity
  float dot = ax * refX + ay * refY + az * refZ;
  float c = dot / (gmag * sqrtf(refX * refX + refY * refY + refZ * refZ) + 1e-6f);
  return acosf(fmaxf(-1.f, fminf(1.f, c))) * 57.2958f;
}

void beep(int ms) { digitalWrite(PIN_BUZZER, HIGH); waitMs(ms); digitalWrite(PIN_BUZZER, LOW); }

// ------------------------------- network -------------------------------
bool ensureData() {
  if (modem.isGprsConnected()) return true;
  return modem.waitForNetwork(15000) && modem.gprsConnect(APN);
}

bool postJson(const char* path, const String& body, String* resp = nullptr) {
  if (!ensureData()) return false;
  HttpClient http(gsmClient, SERVER_HOST, SERVER_PORT);
  http.setTimeout(15000);
  http.beginRequest(); http.post(path);
  http.sendHeader("Content-Type", "application/json");
  http.sendHeader("Content-Length", body.length());
  http.sendHeader("X-Device-Key", DEVICE_KEY);
  http.beginBody(); http.print(body); http.endRequest();
  int status = http.responseStatusCode();
  String r = http.responseBody();
  if (resp) *resp = r;
  return status >= 200 && status < 300;
}

void flushTelemetry() {
  if (ringN == 0 || !ensureData()) return;
  JsonDocument doc; doc["device_id"] = DEVICE_ID; JsonArray pts = doc["points"].to<JsonArray>();
  for (int i = 0; i < ringN; i++) {
    JsonObject o = pts.add<JsonObject>();
    o["ts"] = ring[i].ts; o["lat"] = ring[i].lat; o["lon"] = ring[i].lon;
    o["speed_kmh"] = ring[i].kmh; o["heading"] = ring[i].heading; o["ay"] = ring[i].ay;
  }
  String body, resp; serializeJson(doc, body);
  if (postJson("/telemetry", body, &resp)) {
    ringN = 0;                                        // delivered
    JsonDocument r; if (!deserializeJson(r, resp) && r["alerts"].size() > 0) { beep(150); waitMs(100); beep(150); }
  }                                                   // on failure the points stay buffered
}

void flushHazards() {
  for (int i = 0; i < hazN; i++) {
    JsonDocument d; d["device_id"] = DEVICE_ID; d["ts"] = hazQ[i].ts; d["lat"] = hazQ[i].lat; d["lon"] = hazQ[i].lon;
    d["peak_g"] = hazQ[i].g; d["speed_kmh"] = hazQ[i].kmh;
    String b; serializeJson(d, b);
    if (!postJson("/hazards/report", b)) { memmove(hazQ, hazQ + i, (hazN - i) * sizeof(Hz)); hazN -= i; return; }
  }
  hazN = 0;
}

// ------------------------------- crash handling -------------------------------
void handleCrash(float peakG, float speedBefore) {
  uint32_t impact = millis();
  int stillSecs = 0; float tilt = 0;
  for (int s = 0; s < 5; s++) {                       // watch the 5 s after impact
    waitMs(1000); imuRead(); tilt = fmaxf(tilt, tiltDeg());
    if (gps.speed.kmph() < 5.0f) stillSecs++;
  }
  float dv = fmaxf(0.0f, speedBefore - (float)gps.speed.kmph());
  if (!(dv >= CRASH_DV_KMH || tilt >= CRASH_TILT_DEG)) return;   // a bump or a dropped device, not a crash

  // Local cancel window: buzzer on, press the button to stop. Prevents false dispatches.
  uint32_t t0 = millis();
  while (millis() - t0 < CANCEL_WINDOW_MS) {
    digitalWrite(PIN_BUZZER, ((millis() / 250) % 2) ? HIGH : LOW);
    if (digitalRead(PIN_CANCEL) == LOW) { digitalWrite(PIN_BUZZER, LOW); return; }
    feedGps(); delay(5);
  }
  digitalWrite(PIN_BUZZER, LOW);

  char eventId[20]; snprintf(eventId, sizeof(eventId), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
  JsonDocument d; d["device_id"] = DEVICE_ID; d["event_id"] = eventId;
  if (gpsEpoch()) d["ts"] = gpsEpoch();
  d["lat"] = gps.location.lat(); d["lon"] = gps.location.lng();
  d["peak_g"] = peakG; d["delta_v_kmh"] = dv; d["tilt_deg"] = tilt;
  d["speed_before_kmh"] = speedBefore; d["stationary_s"] = stillSecs;
  String body; serializeJson(d, body);

  bool ok = false;
  for (int attempt = 0; attempt < 5 && !ok; attempt++) { ok = postJson("/crash", body); if (!ok) waitMs(3000); }
  if (!ok) {                                          // data is down: fall back to a direct SMS
    char sms[160];
    snprintf(sms, sizeof(sms), "SAFEROUTE: %s possible crash. GPS %.5f,%.5f. Call the driver; if no answer call 999.",
             DEVICE_ID, gps.location.lat(), gps.location.lng());
    modem.sendSMS(NEXT_OF_KIN, sms);
  }
  (void)impact;
}

// ------------------------------- arduino entry points -------------------------------
void setup() {
  setenv("TZ", "UTC0", 1); tzset();
  pinMode(PIN_BUZZER, OUTPUT); pinMode(PIN_CANCEL, INPUT_PULLUP);
  Wire.begin(21, 22); Wire.setClock(400000);
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x6B); Wire.write(0); Wire.endTransmission();       // wake
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x1C); Wire.write(0x18); Wire.endTransmission();    // +/-16 g
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x1A); Wire.write(0x03); Wire.endTransmission();    // ~44 Hz low-pass
  gpsSerial.begin(9600, SERIAL_8N1, 18, 19);
  modemSerial.begin(115200, SERIAL_8N1, 16, 17);
  modem.restart();

  float sx = 0, sy = 0, sz = 0; int n = 0;            // calibrate "up" while stationary
  uint32_t t = millis(); while (millis() - t < 2000) { if (imuRead()) { sx += ax; sy += ay; sz += az; n++; } delay(10); }
  if (n) { refX = sx / n; refY = sy / n; refZ = sz / n; }
  beep(200);
}

void loop() {
  feedGps();
  uint32_t now = millis();
  if (imuRead()) {
    float dev = fabsf(gmag - 1.0f);
    if (now - peakWinStart > 200) {                   // 200 ms window for the pothole peak
      if (peakDev > POTHOLE_G && now - lastHaz > 3000 && gps.location.isValid() && gps.speed.kmph() > POTHOLE_MIN_KMH && gpsEpoch()) {
        if (hazN < 10) hazQ[hazN++] = { gpsEpoch(), gps.location.lat(), gps.location.lng(), peakDev, (float)gps.speed.kmph() };
        lastHaz = now;
      }
      peakDev = 0; peakWinStart = now;
    }
    peakDev = fmaxf(peakDev, dev);
    if (gmag > CRASH_G) handleCrash(gmag, speedHist[4]);   // speed ~4-5 s before the impact
  }
  if (now - lastSpeedTick >= 1000) {                  // 1 Hz speed history
    lastSpeedTick = now; for (int i = 0; i < 5; i++) speedHist[i] = speedHist[i + 1]; speedHist[5] = gps.speed.kmph();
  }
  if (now - lastTel >= TELEMETRY_EVERY_MS && gps.location.isValid() && gpsEpoch()) {
    lastTel = now;
    if (ringN == 120) { memmove(ring, ring + 1, 119 * sizeof(Pt)); ringN = 119; }
    ring[ringN++] = { gpsEpoch(), gps.location.lat(), gps.location.lng(), (float)gps.speed.kmph(), (float)gps.course.deg(), (ax - refX) * 9.81f };
  }
  if (now - lastFlush >= FLUSH_EVERY_MS) { lastFlush = now; flushTelemetry(); flushHazards(); }
}
