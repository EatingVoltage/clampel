// CLAMPEL — Claude status lamp. ESP32-C3 SuperMini + one WS2812D-F5 on GPIO4.
//
// The host bridge (host/clampel.py) owns all the logic: which session counts,
// when "done" decays to "idle". This firmware only renders a mode and keeps
// itself safe:
//   - no line from the host for 5 s → dark (bridge died, PC asleep).
//   - an image delivered by `clampel flash` must hear `?` from the bridge
//     within 30 s, or the bootloader rolls back to the previous one.
//
// Serial protocol: README.md, "Talking to the lamp".

#include <Arduino.h>
#include <Preferences.h>
#include <Update.h>
#include <esp_mac.h>
#include <esp_ota_ops.h>

#ifndef CLAMPEL_VERSION
#define CLAMPEL_VERSION "dev"
#endif

namespace {

#ifdef CLAMPEL_ROLLBACK_TEST
constexpr const char* kVersion = "rollback-test";
#else
constexpr const char* kVersion = CLAMPEL_VERSION;
#endif

constexpr uint8_t  kLedPin          = 4;      // WS2812D-F5 DIN
constexpr uint8_t  kOnboardLedPin   = 8;      // blue, active low — kept dark
// The datasheet says GRB, but this lamp's LED reads RGB: with GRB, "think" pulsed
// green (seen 2026-10-12). neopixelWrite() always sends GRB, so swap in show().
constexpr bool     kLedRgbOrder     = true;
constexpr uint32_t kHostTimeoutMs   = 5000;
constexpr uint32_t kVerifyTimeoutMs = 30000;
constexpr uint32_t kUpdateIdleMs    = 5000;
constexpr uint32_t kUpdateChunk     = 1024;   // host waits for an ACK per chunk

enum class Mode : uint8_t { Off, Idle, Done, Think, Ask, Credits };

Mode        mode          = Mode::Off;
uint8_t     brightness    = 60;               // percent, perceptual
bool        hostSeen      = false;
uint32_t    lastRxMs      = 0;
bool        pendingVerify = false;
bool        pinTest       = false;            // `P` diagnostic: LED pin is a plain GPIO until reboot
char        mac[18];
Preferences prefs;

char   line[96];
size_t lineLen = 0;

// ── update (binary) mode ─────────────────────────────────────────────────────
bool     updating  = false;
uint32_t updSize   = 0;
uint32_t updGot    = 0;
uint32_t updAcked  = 0;
uint32_t updLastMs = 0;

// ── LED ──────────────────────────────────────────────────────────────────────

// Perceptual 0..1 → output byte, master brightness first, then gamma 2.2.
uint8_t level(float v) {
    if (v <= 0.0f) return 0;
    if (v > 1.0f) v = 1.0f;
    const float x = v * brightness / 100.0f;
    return (uint8_t)lroundf(powf(x, 2.2f) * 255.0f);
}

void show(float r, float g, float b) {
    static int16_t lr = -1, lg = -1, lb = -1;
    const uint8_t R = level(r), G = level(g), B = level(b);
    if (R == lr && G == lg && B == lb) return;
    lr = R; lg = G; lb = B;
    if (kLedRgbOrder) neopixelWrite(kLedPin, G, R, B);   // core sends its 2nd arg first
    else              neopixelWrite(kLedPin, R, G, B);
}

// 0..1 raised-cosine wave, period in ms.
float wave(uint32_t now, uint32_t periodMs) {
    const float ph = (float)(now % periodMs) / periodMs;
    return 0.5f - 0.5f * cosf(ph * 2.0f * PI);
}

void render(uint32_t now) {
    if (pinTest) return;
    if (updating) {                               // fast cyan breathing — blue is reserved for credits
        const float v = 0.2f + 0.6f * wave(now, 600);
        show(0, v * 0.6f, v);
        return;
    }
    const bool hostAlive = hostSeen && now - lastRxMs < kHostTimeoutMs;
    switch (hostAlive ? mode : Mode::Off) {
        case Mode::Off:   show(0, 0, 0); break;
        case Mode::Idle:  show(0, 0.30f, 0); break;
        case Mode::Done:  show(0, 1.0f, 0); break;
        case Mode::Think: show(0.25f + 0.75f * wave(now, 2400), 0, 0); break;
        case Mode::Ask: {
            const bool on = (now % 800) < 400;
            show(on ? 1.0f : 0, on ? 0.5f : 0, 0);
            break;
        }
        case Mode::Credits:                       // slow "sleeping" breath, ~12 per minute
            show(0, 0, 0.08f + 0.72f * wave(now, 5000));
            break;
    }
}

// ── protocol ─────────────────────────────────────────────────────────────────

void reply(const char* s) {
    Serial.print(s);
    Serial.print('\n');
}

void updateFail(const char* why) {
    Update.abort();
    updating = false;
    Serial.printf("ERR update %s\n", why);
}

void beginUpdate(char* args) {
    char* md5 = nullptr;
    const uint32_t size = strtoul(args, &md5, 10);
    while (md5 && *md5 == ' ') md5++;
    if (size == 0 || !md5 || strlen(md5) != 32) { reply("ERR usage: U <size> <md5>"); return; }
    if (!Update.begin(size))   { Serial.printf("ERR update %s\n", Update.errorString()); return; }
    if (!Update.setMD5(md5))   { updateFail("md5"); return; }
    updating  = true;
    updSize   = size;
    updGot    = 0;
    updAcked  = 0;
    updLastMs = millis();
    reply("READY");
}

// Binary phase: the host sends kUpdateChunk bytes, then waits for "ACK <total>".
void pumpUpdate(uint32_t now) {
    uint8_t buf[256];
    while (updGot < updSize && Serial.available()) {
        const size_t want = min((uint32_t)sizeof(buf), updSize - updGot);
        const size_t n    = Serial.read(buf, want);
        if (n == 0) break;
        if (Update.write(buf, n) != n) { updateFail(Update.errorString()); return; }
        updGot   += n;
        updLastMs = now;
    }
    if (updGot - updAcked >= kUpdateChunk || (updGot == updSize && updAcked != updSize)) {
        updAcked = updGot;
        Serial.printf("ACK %u\n", (unsigned)updGot);
    }
    if (updGot == updSize) {
        if (!Update.end()) { updateFail(Update.errorString()); return; }
        reply("DONE");
        Serial.flush();
        delay(200);
        ESP.restart();
    }
    if (now - updLastMs > kUpdateIdleMs) updateFail("timeout");
}

void handleLine(char* s) {
    switch (s[0]) {
        case '?':
            // The bridge is talking to us: a freshly updated image is good.
            // (The rollback-test build never confirms itself, to prove the net.)
#ifndef CLAMPEL_ROLLBACK_TEST
            if (pendingVerify) {
                esp_ota_mark_app_valid_cancel_rollback();
                pendingVerify = false;
            }
#endif
            Serial.printf("CLAMPEL %s %s %s\n", kVersion, mac, pendingVerify ? "pending" : "valid");
            break;
        case 'S': {
            const char* m = s + 1;
            while (*m == ' ') m++;
            if      (!strcmp(m, "off"))   mode = Mode::Off;
            else if (!strcmp(m, "idle"))  mode = Mode::Idle;
            else if (!strcmp(m, "done"))  mode = Mode::Done;
            else if (!strcmp(m, "think")) mode = Mode::Think;
            else if (!strcmp(m, "ask"))   mode = Mode::Ask;
            else if (!strcmp(m, "credits")) mode = Mode::Credits;
            else { reply("ERR mode"); break; }
            reply("OK");
            break;
        }
        case 'B': {
            const long v = strtol(s + 1, nullptr, 10);
            if (v < 1 || v > 100) { reply("ERR 1..100"); break; }
            brightness = (uint8_t)v;
            prefs.putUChar("bri", brightness);
            reply("OK");
            break;
        }
        case 'H':                                  // heartbeat, no reply
            break;
        case 'P': {
            // Diagnostic: take the LED pin away from RMT and drive it as a plain GPIO
            // (h = high, l = low, anything else = high-Z). A WS2812 keeps its last
            // colour whatever the pin does; a plain RGB LED follows it.
            const char* a = s + 1;
            while (*a == ' ') a++;
            pinTest = true;
            pinMatrixOutDetach(kLedPin, false, false);
            if (*a == 'h' || *a == 'l') {
                pinMode(kLedPin, OUTPUT);
                digitalWrite(kLedPin, *a == 'h' ? HIGH : LOW);
            } else {
                pinMode(kLedPin, INPUT);
            }
            reply("OK");
            break;
        }
        case 'U':
            beginUpdate(s + 1);
            break;
        case 'R':
            reply("OK");
            Serial.flush();
            delay(100);
            ESP.restart();
            break;
        default:
            reply("ERR unknown");
    }
}

void pumpLines(uint32_t now) {
    while (!updating && Serial.available()) {
        const int c = Serial.read();
        if (c < 0) break;
        if (c == '\r') continue;
        if (c == '\n') {
            line[lineLen] = 0;
            lineLen = 0;
            if (line[0]) {
                hostSeen = true;
                lastRxMs = now;
                handleLine(line);
            }
        } else if (lineLen < sizeof(line) - 1) {
            line[lineLen++] = (char)c;
        }
    }
}

} // namespace

// Arduino's init would mark a fresh OTA image valid on its own. Returning true
// keeps it PENDING_VERIFY until the bridge says hello (`?`), so an image that
// can't talk to the host gets rolled back.
extern "C" bool verifyRollbackLater() { return true; }

void setup() {
    pinMode(kOnboardLedPin, OUTPUT);
    digitalWrite(kOnboardLedPin, HIGH);

    Serial.setRxBufferSize(4096);
    Serial.begin(115200);

    prefs.begin("clampel", false);
    brightness = prefs.getUChar("bri", brightness);

    uint8_t m[6];
    esp_efuse_mac_get_default(m);
    snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);

    esp_ota_img_states_t st;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK &&
        st == ESP_OTA_IMG_PENDING_VERIFY) {
        pendingVerify = true;
    }

    // Boot blip (dim white), so a restart is visible.
    show(0.4f, 0.4f, 0.4f);
    delay(250);
    show(0, 0, 0);
}

void loop() {
    const uint32_t now = millis();

    if (updating) pumpUpdate(now);
    else          pumpLines(now);

    if (pendingVerify && now > kVerifyTimeoutMs) {
        esp_ota_mark_app_invalid_rollback_and_reboot();
        // Only returns if there is nothing to roll back to — keep running.
        esp_ota_mark_app_valid_cancel_rollback();
        pendingVerify = false;
    }

    render(now);
    delay(10);
}
