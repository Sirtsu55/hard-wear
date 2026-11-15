#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <Arduino_Modulino.h>
#include <Wire.h>
#include "font5x7.h"
#include "idle_anims.h"

// ============================================================================
// Hard Wear — Milestone 1: OLED Text Engine, Menu UI, Buzzer SFX & Wearable Idle
//   - Knob Turn:        Navigate OLED menu / cycle Idle animations / move cursor / scroll text
//   - Knob Short Press: Select menu item / game move / wake from Idle / trigger AI or Mic
//   - Knob Long Press:  Return to Main Menu (1 s)
//   - Inactivity (25s): Auto-enters Fashion Wearable Idle animations from Menu
// ============================================================================

// ---------- Zero-constructor I2C 128x32 SSD1306 OLED ----------
#define OLED_W 128
#define OLED_H 32

HardwareI2C* oledWire = nullptr;
uint8_t oledAddr = 0x3C;
bool oledOk = false;
uint8_t oledBuf[OLED_W * OLED_H / 8];
const char* oledStatus = "OLED uninitialized";

bool oledCmd(uint8_t c) {
  if (!oledWire) return false;
  oledWire->beginTransmission(oledAddr);
  oledWire->write((uint8_t)0x00);
  oledWire->write(c);
  return oledWire->endTransmission() == 0;
}

void oledClear() { memset(oledBuf, 0, sizeof(oledBuf)); }

void oledPx(int x, int y, bool on = true) {
  if (x < 0 || x >= OLED_W || y < 0 || y >= OLED_H) return;
  uint16_t idx = (y >> 3) * OLED_W + x;
  uint8_t mask = (uint8_t)(1U << (y & 7));
  if (on) oledBuf[idx] |= mask;
  else oledBuf[idx] &= ~mask;
}

void oledFillRect(int x0, int y0, int w, int h, bool on = true) {
  for (int y = y0; y < y0 + h; y++) {
    for (int x = x0; x < x0 + w; x++) oledPx(x, y, on);
  }
}

void oledHLine(int x0, int x1, int y, bool on = true) {
  for (int x = x0; x <= x1; x++) oledPx(x, y, on);
}

void oledReinit() {
  if (!oledWire) return;
  // 0xA0 + 0xC0 flips SSD1306 180 degrees relative to 0xA1 + 0xC8
  const uint8_t initSeq[] = {
    0xAE, 0xD5, 0x80, 0xA8, 0x1F, 0xD3, 0x00, 0x40,
    0x8D, 0x14, 0x20, 0x00, 0xA0, 0xC0, 0xDA, 0x02,
    0x81, 0x8F, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6,
    0x2E, 0xAF
  };
  for (uint8_t c : initSeq) oledCmd(c);
}

void oledFlush() {
  if (!oledOk || !oledWire) return;
  if (!oledCmd(0x21)) {
    // I2C NACK: bus glitched (e.g. wire wiggle while pressing knob) -> mark for auto-reinit
    oledOk = false;
    return;
  }
  oledCmd(0); oledCmd(OLED_W - 1);
  oledCmd(0x22); oledCmd(0); oledCmd((OLED_H / 8) - 1);
  for (size_t i = 0; i < sizeof(oledBuf); i += 16) {
    oledWire->beginTransmission(oledAddr);
    oledWire->write((uint8_t)0x40);
    oledWire->write(oledBuf + i, 16);
    if (oledWire->endTransmission() != 0) {
      oledOk = false;
      return;
    }
  }
}

bool tryOledBus(HardwareI2C& bus, const char* busName, bool initBus = true) {
  if (initBus) {
    bus.begin();
    bus.setClock(100000);  // 100 kHz Standard-Mode I2C for reliable SSD1306 detection
    delay(20);
  }
  const uint8_t addrs[2] = {0x3C, 0x3D};
  for (int attempt = 0; attempt < 4; attempt++) {
    for (uint8_t a : addrs) {
      bus.beginTransmission(a);
      if (bus.endTransmission() == 0) {
        oledWire = &bus;
        oledAddr = a;
        oledOk = true;
        oledReinit();
        oledClear();
        oledFlush();
        oledStatus = busName;
        Monitor.println(oledStatus);
        return true;
      }
    }
    delay(25);
  }
  return false;
}

// ---------- 5x7 Font Drawing & Word-Wrapped Auto-Scroller ----------
void oledDrawChar(int x, int y, char c, bool fg = true, int scale = 1) {
  if (c < 0x20 || c > 0x7E) c = '?';
  const uint8_t* glyph = FONT5X7[c - 0x20];
  for (int col = 0; col < 5; col++) {
    uint8_t bits = glyph[col];
    for (int row = 0; row < 7; row++) {
      if ((bits >> row) & 1) {
        if (scale <= 1) {
          oledPx(x + col, y + row, fg);
        } else {
          oledFillRect(x + col * scale, y + row * scale, scale, scale, fg);
        }
      }
    }
  }
}

void oledPrint(int x, int y, const char* s, bool fg = true, int scale = 1) {
  int step = 6 * scale;
  while (*s) {
    if (x + 5 * scale > 0 && x < OLED_W) {
      oledDrawChar(x, y, *s, fg, scale);
    }
    x += step;
    s++;
  }
}

// Word-wrapped scrolling text engine (20 chars/line, up to 20 lines = ~400 chars)
const int WRAP_COLS = 20;
const int MAX_WRAP_LINES = 20;
const int SCROLL_LINE_H = 9;

char scrollTitle[22] = "";
char wrapLines[MAX_WRAP_LINES][WRAP_COLS + 1];
int wrapLineCount = 0;
int scrollPxY = 0;
int scrollMaxPxY = 0;
unsigned long scrollPauseUntil = 0;
unsigned long lastScrollStepMs = 0;
bool scrollActive = false;

void oledSetScrollText(const char* title, const char* body) {
  strncpy(scrollTitle, title ? title : "", 21);
  scrollTitle[21] = '\0';

  wrapLineCount = 0;
  const char* p = body ? body : "";
  while (*p && wrapLineCount < MAX_WRAP_LINES) {
    while (*p == ' ') p++;
    if (!*p) break;
    if (*p == '\n') {
      wrapLines[wrapLineCount][0] = '\0';
      wrapLineCount++;
      p++;
      continue;
    }

    int len = 0;
    int lastSpace = -1;
    while (p[len] && p[len] != '\n' && len < WRAP_COLS) {
      if (p[len] == ' ') lastSpace = len;
      len++;
    }

    int take = len;
    if (p[len] && p[len] != ' ' && p[len] != '\n' && lastSpace > 0) {
      take = lastSpace;
    }

    memcpy(wrapLines[wrapLineCount], p, take);
    wrapLines[wrapLineCount][take] = '\0';
    wrapLineCount++;
    p += take;
  }

  if (wrapLineCount == 0) {
    strcpy(wrapLines[0], "...");
    wrapLineCount = 1;
  }

  int viewportH = (scrollTitle[0] != '\0') ? 22 : 32;
  int totalH = wrapLineCount * SCROLL_LINE_H;
  scrollMaxPxY = (totalH > viewportH) ? (totalH - viewportH + 2) : 0;
  scrollPxY = 0;
  scrollPauseUntil = millis() + 1400;  // pause at top so first lines read cleanly
  lastScrollStepMs = millis();
  scrollActive = true;
}

void oledRenderScrollView() {
  if (!oledOk) return;
  oledClear();

  int bodyTop = 0;
  if (scrollTitle[0] != '\0') {
    oledFillRect(0, 0, OLED_W, 9, true);
    oledPrint(2, 1, scrollTitle, false, 1);  // inverted header bar
    bodyTop = 10;
  }

  for (int i = 0; i < wrapLineCount; i++) {
    int y = bodyTop + i * SCROLL_LINE_H - scrollPxY;
    if (y + 7 < bodyTop || y >= OLED_H) continue;
    // Render clipped to bodyTop..OLED_H-1
    const char* line = wrapLines[i];
    int x = 2;
    while (*line && x < OLED_W - 4) {
      char c = *line++;
      if (c < 0x20 || c > 0x7E) c = '?';
      const uint8_t* glyph = FONT5X7[c - 0x20];
      for (int col = 0; col < 5; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < 7; row++) {
          int py = y + row;
          if (py >= bodyTop && py < OLED_H && ((bits >> row) & 1)) {
            oledPx(x + col, py, true);
          }
        }
      }
      x += 6;
    }
  }

  // Right-edge scrollbar indicator when content overflows
  if (scrollMaxPxY > 0) {
    int trackTop = bodyTop + 1;
    int trackH = OLED_H - trackTop - 1;
    for (int y = trackTop; y < trackTop + trackH; y += 2) oledPx(OLED_W - 1, y, true);
    int barH = 5;
    int barY = trackTop + ((trackH - barH) * scrollPxY) / scrollMaxPxY;
    oledFillRect(OLED_W - 2, barY, 2, barH, true);
  }

  oledFlush();
}

void stepOledScroller(unsigned long now) {
  if (!scrollActive || scrollMaxPxY <= 0) return;
  if ((long)(now - scrollPauseUntil) < 0) return;

  if (now - lastScrollStepMs >= 55) {  // ~18 px/sec smooth readable auto-scroll
    lastScrollStepMs = now;
    scrollPxY++;
    if (scrollPxY > scrollMaxPxY) {
      scrollPxY = 0;
      scrollPauseUntil = now + 1400;
    } else if (scrollPxY == scrollMaxPxY) {
      scrollPauseUntil = now + 1600;  // pause at bottom before wrapping to top
    }
    oledRenderScrollView();
  }
}

// ---------- Non-blocking Modulino Buzzer Sequencer ----------
ModulinoBuzzer buzzer;

uint16_t sfxFreqs[6];
uint16_t sfxDurs[6];
int sfxLen = 0;
int sfxIdx = 0;
unsigned long sfxNextMs = 0;

void playSfx(const uint16_t* freqs, const uint16_t* durs, int count) {
  if (count > 6) count = 6;
  for (int i = 0; i < count; i++) {
    sfxFreqs[i] = freqs[i];
    sfxDurs[i] = durs[i];
  }
  sfxLen = count;
  sfxIdx = 0;
  sfxNextMs = 0;
}

void stepSfx(unsigned long now) {
  if (sfxIdx >= sfxLen) return;
  if (sfxNextMs == 0 || now >= sfxNextMs) {
    uint16_t f = sfxFreqs[sfxIdx];
    uint16_t d = sfxDurs[sfxIdx];
    sfxIdx++;
    if (f > 0) buzzer.tone(f, d);
    sfxNextMs = now + d + 12;
  }
}

void sfxClick(bool up) {
  const uint16_t f[1] = { (uint16_t)(up ? 1320 : 1046) };
  const uint16_t d[1] = { 14 };
  playSfx(f, d, 1);
}
void sfxSelect() {
  const uint16_t f[2] = { 880, 1320 };
  const uint16_t d[2] = { 35, 55 };
  playSfx(f, d, 2);
}
void sfxBack() {
  const uint16_t f[2] = { 880, 587 };
  const uint16_t d[2] = { 35, 65 };
  playSfx(f, d, 2);
}
void sfxReplyReady() {
  const uint16_t f[3] = { 784, 988, 1319 };
  const uint16_t d[3] = { 45, 45, 80 };
  playSfx(f, d, 3);
}
void sfxBoot() {
  const uint16_t f[3] = { 523, 659, 1046 };
  const uint16_t d[3] = { 50, 50, 90 };
  playSfx(f, d, 3);
}
void sfxInvite() {
  const uint16_t f[4] = { 988, 1319, 988, 1568 };
  const uint16_t d[4] = { 55, 55, 55, 110 };
  playSfx(f, d, 4);
}

// ---------- LED Matrix & Modes ----------
Arduino_LED_Matrix matrix;
ModulinoKnob knob;

enum Mode { IDLE_WEARABLE, MENU, AI_BUDDY, GAME_PICKER, GAME, PEERS, INVITE_POPUP, MIC_TEST };
Mode mode = IDLE_WEARABLE;
Mode preAiMode = MENU;  // Tracks where Hold-to-Talk was launched from so 2xClick cancels back cleanly
int blePeerCount = 0;
char bleMenuLabel[20] = "BLE Peers (0)";
int inviteSel = 0;  // 0 = Yes, 1 = No
char invitePeerName[18] = "";
char inviteGameTitle[18] = "";
char pickerHdr[22] = "MINI GAMES";
char pickerTitle[22] = "1v1 Pong";
int pickerIdx = 0;

const int MENU_COUNT = 4;
const char* const MENU_ITEMS[MENU_COUNT] = {
  "Ask EdgeAI",
  "Mini Games",
  "BLE Peers (0)",
  "Mic Waveform"
};
int menuSel = 0;
int idleAnimIdx = 0;
unsigned long lastActivityMs = 0;
unsigned long modeEnterMs = 0;
unsigned long lastIdleFrameMs = 0;
unsigned long idleOverlayUntil = 0;
const unsigned long IDLE_TIMEOUT_MS = 10000;  // 10s in Menu -> auto-sleep to Idle

bool clickPending = false;
unsigned long firstClickReleaseMs = 0;
const unsigned long DBL_CLICK_MS = 300;

bool debouncedPressed = false;
bool rawLastPressed = false;
unsigned long rawEdgeMs = 0;
bool longDone = false;
unsigned long pressStart = 0;
bool lastBlink = true;

const int W = 13, H = 8;
uint8_t frame[W * H];
uint8_t rotFrame[W * H];

void clearFrame() { memset(frame, 0, sizeof(frame)); }
void px(int x, int y, uint8_t v = 7) {
  if (x >= 0 && x < W && y >= 0 && y < H) frame[y * W + x] = v;
}
void show() {
  // Flip 13x8 LED matrix 180 degrees so physical upside-down mount reads right-side up
  for (int i = 0; i < W * H; i++) {
    rotFrame[i] = frame[(W * H - 1) - i];
  }
  matrix.draw(rotFrame);
}

// ---------- Menu Icons (13x8 Matrix) & OLED Menu Renderer ----------
void drawMenuMatrixIcon(int sel) {
  clearFrame();
  switch (sel) {
    case 0: {  // Ask AI Buddy: chat bubble + sparkle
      for (int x = 2; x <= 10; x++) { px(x, 1, 6); px(x, 5, 6); }
      for (int y = 2; y <= 4; y++) { px(2, y, 6); px(10, y, 6); }
      px(4, 6, 6); px(3, 7, 6);
      px(4, 3, 7); px(6, 3, 7); px(8, 3, 7);
      break;
    }
    case 1: {  // Mini Games: 3x3 grid icon
      for (int y = 0; y < 8; y++) { px(4, y, 5); px(8, y, 5); }
      for (int x = 1; x <= 11; x++) { px(x, 2, 5); px(x, 5, 5); }
      px(2, 0, 7); px(6, 3, 7); px(10, 6, 7);
      break;
    }
    case 2: {  // BLE Peers: radar waves
      px(6, 4, 7);
      px(4, 3, 5); px(4, 5, 5); px(8, 3, 5); px(8, 5, 5);
      px(2, 2, 3); px(2, 4, 3); px(2, 6, 3);
      px(10, 2, 3); px(10, 4, 3); px(10, 6, 3);
      break;
    }
    case 3: {  // Mic Waveform: equalizer bars
      const int hs[7] = {2, 5, 7, 4, 8, 5, 3};
      for (int i = 0; i < 7; i++) {
        int x = 3 + i;
        for (int k = 0; k < hs[i]; k++) px(x, 7 - k, k == hs[i] - 1 ? 7 : 4);
      }
      break;
    }
  }
  show();
}

void drawOledMenu() {
  if (!oledOk) return;
  scrollActive = false;
  oledClear();

  // Top compact header (8 px tall)
  oledPrint(2, 0, "HARD WEAR", true, 1);
  char idxBuf[8];
  snprintf(idxBuf, sizeof(idxBuf), "%d/%d", menuSel + 1, MENU_COUNT);
  oledPrint(OLED_W - 20, 0, idxBuf, true, 1);
  oledHLine(0, OLED_W - 1, 8, true);

  // Show 2 visible rows in y = 10..31 (11 px each) with inverted highlight pill on active item
  int first = (menuSel == 0) ? 0 : (menuSel - 1);
  if (first > MENU_COUNT - 2) first = MENU_COUNT - 2;

  for (int row = 0; row < 2; row++) {
    int idx = first + row;
    int y = 10 + row * 11;
    bool selected = (idx == menuSel);
    const char* label = (idx == 2) ? bleMenuLabel : MENU_ITEMS[idx];
    if (selected) {
      oledFillRect(0, y, OLED_W - 4, 10, true);
      oledPrint(3, y + 1, ">", false, 1);
      oledPrint(11, y + 1, label, false, 1);
    } else {
      oledPrint(11, y + 1, label, true, 1);
    }
  }

  // Mini scrollbar on right
  int dotY = 10 + (menuSel * 18) / (MENU_COUNT - 1);
  oledFillRect(OLED_W - 2, dotY, 2, 4, true);

  oledFlush();
}

void drawOledIdleBadge(bool showHint) {
  if (!oledOk) return;
  scrollActive = false;
  oledClear();
  if (showHint) {
    char hdr[22];
    snprintf(hdr, sizeof(hdr), "STYLE %d/%d", idleAnimIdx + 1, ANIM_COUNT);
    oledFillRect(0, 0, OLED_W, 9, true);
    oledPrint(2, 1, hdr, false, 1);
    oledPrint(2, 13, ANIM_NAMES[idleAnimIdx], true, 1);
    oledPrint(2, 24, "Click: Open Menu", true, 1);
  } else {
    // Clean minimal fashion tag while worn on bag/belt
    oledHLine(8, OLED_W - 9, 3, true);
    oledPrint(19, 9, "HARD WEAR", true, 2);
    oledHLine(8, OLED_W - 9, 27, true);
  }
  oledFlush();
}

// ---------- Multi-Game Rendering (Pong, Snake, Tic-Tac-Toe) ----------
char lastBoard[136] = {0};

void drawGamePreviewIcon(int gIdx) {
  clearFrame();
  if (gIdx == 0) {
    // Pong preview: two paddles + ball + center net
    for (int y = 2; y < 5; y++) px(0, y, 7);
    for (int y = 3; y < 6; y++) px(12, y, 7);
    for (int y = 0; y < H; y += 2) px(6, y, 2);
    px(4, 3, 7);
  } else if (gIdx == 1) {
    // Snake preview: coiled snake + food pellet
    px(2, 5, 3); px(3, 5, 4); px(4, 5, 5); px(4, 4, 5); px(4, 3, 6); px(5, 3, 6); px(6, 3, 7);
    px(10, 3, 7);
  } else {
    // Tic-Tac-Toe grid preview
    for (int y = 0; y < 8; y++) { px(4, y, 4); px(8, y, 4); }
    for (int x = 1; x <= 11; x++) { px(x, 2, 4); px(x, 5, 4); }
    px(2, 0, 7); px(6, 3, 7); px(10, 6, 7);
  }
  show();
}

void drawOledGamePicker() {
  if (!oledOk) return;
  scrollActive = false;
  oledClear();
  oledFillRect(0, 0, OLED_W, 9, true);
  oledPrint(2, 1, pickerHdr, false, 1);

  char mid[22];
  snprintf(mid, sizeof(mid), "< %s >", pickerTitle);
  oledFillRect(0, 11, OLED_W, 10, true);
  oledPrint(4, 12, mid, false, 1);
  oledPrint(2, 24, "Click:Play  2x:Back", true, 1);
  oledFlush();
}

void drawMark(char m, int x0, int y0, uint8_t v = 7) {
  if (m == 'X') {
    px(x0, y0, v); px(x0 + 2, y0, v); px(x0 + 1, y0 + 1, v);
  } else if (m == 'O') {
    px(x0, y0, v); px(x0 + 1, y0, v); px(x0 + 2, y0, v);
    px(x0, y0 + 1, v); px(x0 + 2, y0 + 1, v);
  }
}

void renderBoard(const char* s, bool blinkOn) {
  if (mode != GAME || s[0] == '\0') return;
  clearFrame();

  // 1. PONG: "P,<my_y>,<opp_y>,<bx>,<by>,<s1>,<s2>,<state>"
  if (s[0] == 'P' && s[1] == ',') {
    int myY = 2, oppY = 2, bx = 6, by = 3, s1 = 0, s2 = 0;
    char st = 'p';
    sscanf(s + 2, "%d,%d,%d,%d,%d,%d,%c", &myY, &oppY, &bx, &by, &s1, &s2, &st);
    for (int y = 0; y < H; y += 2) px(6, y, 1);  // faint center net
    for (int dy = 0; dy < 3; dy++) {
      px(0, myY + dy, 7);
      px(W - 1, oppY + dy, 5);
    }
    if (st == 'p') {
      px(bx, by, 7);
    } else if (st == 'w') {
      for (int y = 0; y < H; y++) { px(0, y, 7); px(1, y, 4); }
    } else if (st == 'l') {
      for (int y = 0; y < H; y++) { px(W - 1, y, 7); }
    }
    show();
    return;
  }

  // 2. SNAKE: "S,<state>,<l1>,<l2>,<104-char grid>"
  if (s[0] == 'S' && s[1] == ',') {
    char st = 'p';
    int l1 = 3, l2 = 3;
    const char* p = s + 2;
    sscanf(p, "%c,%d,%d", &st, &l1, &l2);
    // Advance to 4th comma (start of 104-char grid)
    int commas = 0;
    const char* g = s;
    while (*g && commas < 4) {
      if (*g == ',') commas++;
      g++;
    }
    if (commas == 4 && strlen(g) >= 104) {
      for (int i = 0; i < 104; i++) {
        int x = i % W, y = i / W;
        char c = g[i];
        if (c == 'M') px(x, y, 7);
        else if (c == 'm') px(x, y, 4);
        else if (c == 'E') px(x, y, 6);
        else if (c == 'e') px(x, y, 2);
        else if (c == 'F') px(x, y, blinkOn ? 7 : 3);
      }
    }
    show();
    return;
  }

  // 3. TIC-TAC-TOE: 12-char snapshot
  if (strlen(s) < 12) return;
  const int bx = 1;
  for (int y = 0; y < H; y++) { px(bx + 3, y, 2); px(bx + 7, y, 2); }
  for (int x = bx; x < bx + 11; x++) { px(x, 2, 2); px(x, 5, 2); }

  int cursor = s[9] - '0';
  char state = s[10];
  for (int c = 0; c < 9; c++) {
    int x0 = bx + (c % 3) * 4, y0 = (c / 3) * 3;
    char m = s[c];
    bool isCur = (state == 't' && c == cursor);
    if (m == 'X' || m == 'O') {
      drawMark(m, x0, y0, (isCur && !blinkOn) ? 2 : 7);
    } else if (isCur) {
      uint8_t cv = blinkOn ? 7 : 3;
      px(x0 + 1, y0, cv);
      px(x0 + 1, y0 + 1, cv);
    }
  }
  if (state == 'w') {
    for (int y = 0; y < H; y++) { px(0, y, 7); px(W - 1, y, 7); }
  } else if (state == 'd' || state == 'q') {
    for (int y = 1; y < H; y += 2) { px(0, y, 4); px(W - 1, y, 4); }
  }
  show();
}

char lastGameEndChime = ' ';

void drawOledGameStatus(const char* s) {
  if (!oledOk || mode != GAME || s[0] == '\0') return;
  scrollActive = false;
  oledClear();

  if (s[0] == 'P' && s[1] == ',') {
    int myY = 2, oppY = 2, bx = 6, by = 3, s1 = 0, s2 = 0;
    char st = 'p';
    sscanf(s + 2, "%d,%d,%d,%d,%d,%d,%c", &myY, &oppY, &bx, &by, &s1, &s2, &st);
    oledFillRect(0, 0, OLED_W, 9, true);
    oledPrint(2, 1, "1v1 PONG (FIRST TO 5)", false, 1);

    char line1[22];
    snprintf(line1, sizeof(line1), "YOU %d  -  %d FOE", s1, s2);
    oledPrint(14, 12, line1, true, 1);

    if (st == 'p') {
      lastGameEndChime = ' ';
      oledPrint(2, 23, "Turn:Paddle  2x:Exit", true, 1);
    } else if (st == 'w') {
      oledPrint(2, 23, "YOU WIN! Click:Again", true, 1);
      if (lastGameEndChime != 'w') { lastGameEndChime = 'w'; sfxReplyReady(); }
    } else if (st == 'l') {
      oledPrint(2, 23, "FOE WINS! Click:Again", true, 1);
      if (lastGameEndChime != 'l') { lastGameEndChime = 'l'; sfxBack(); }
    } else {
      oledPrint(2, 23, "Match Ended (2x:Menu)", true, 1);
    }
    oledFlush();
    return;
  }

  if (s[0] == 'S' && s[1] == ',') {
    char st = 'p';
    int l1 = 3, l2 = 3;
    sscanf(s + 2, "%c,%d,%d", &st, &l1, &l2);
    oledFillRect(0, 0, OLED_W, 9, true);
    oledPrint(2, 1, "1v1 SNAKE DUEL", false, 1);

    char line1[22];
    snprintf(line1, sizeof(line1), "Len:%d  vs  Foe:%d", l1, l2);
    oledPrint(8, 12, line1, true, 1);

    if (st == 'p') {
      lastGameEndChime = ' ';
      oledPrint(2, 23, "Turn:Steer   2x:Exit", true, 1);
    } else if (st == 'w') {
      oledPrint(2, 23, "YOU WIN! Click:Again", true, 1);
      if (lastGameEndChime != 'w') { lastGameEndChime = 'w'; sfxReplyReady(); }
    } else if (st == 'l') {
      oledPrint(2, 23, "CRASHED! Click:Again", true, 1);
      if (lastGameEndChime != 'l') { lastGameEndChime = 'l'; sfxBack(); }
    } else {
      oledPrint(2, 23, "DRAW! Click:Again", true, 1);
    }
    oledFlush();
    return;
  }

  if (strlen(s) < 12) return;
  oledFillRect(0, 0, OLED_W, 9, true);
  oledPrint(2, 1, "TIC-TAC-TOE", false, 1);

  char state = s[10];
  char mark = s[11];
  char line1[22] = "";
  const char* line2 = "2xClick: Back";

  if (state == 't') {
    lastGameEndChime = ' ';
    snprintf(line1, sizeof(line1), "Turn: Player %c", mark);
    line2 = "Click:Mark 2x:Back";
  } else if (state == 'w') {
    snprintf(line1, sizeof(line1), "Player %c Wins!", mark);
    line2 = "Click: Play Again";
    if (lastGameEndChime != 'w') { lastGameEndChime = 'w'; sfxReplyReady(); }
  } else if (state == 'd') {
    strcpy(line1, "It's a Draw!");
    line2 = "Click: Play Again";
  } else {
    strcpy(line1, "Game Ended");
  }

  oledPrint(2, 12, line1, true, 1);
  oledPrint(2, 23, line2, true, 1);
  oledFlush();
}

volatile bool hasPendingBoard = false;

void drawBoard(String s) {
  if (s.length() < 4) return;
  strncpy(lastBoard, s.c_str(), sizeof(lastBoard) - 1);
  lastBoard[sizeof(lastBoard) - 1] = '\0';
  hasPendingBoard = true;
}

// ---------- SPH0645LM4H I2S mic (DOUT=D11/PB15, BCLK=D13/PB13, LRCL=A5/PC0) ----------
const int PIN_DOUT = D11;
const int PIN_BCLK = D13;
const int PIN_LRCL = A5;

#define GPIOB_IDR   (*(volatile uint32_t*)(0x42020400UL + 0x10UL))
#define GPIOB_BSRR  (*(volatile uint32_t*)(0x42020400UL + 0x18UL))
#define GPIOC_BSRR  (*(volatile uint32_t*)(0x42020800UL + 0x18UL))
#define GPIOF_MODER (*(volatile uint32_t*)(0x42021400UL + 0x00UL))

#define BCLK_HIGH() (GPIOB_BSRR = (1UL << 13))
#define BCLK_LOW()  (GPIOB_BSRR = (1UL << (13 + 16)))
#define LRCL_HIGH() (GPIOC_BSRR = (1UL << 0))
#define LRCL_LOW()  (GPIOC_BSRR = (1UL << (0 + 16)))
#define READ_DOUT() ((GPIOB_IDR >> 15) & 1UL)

bool matrixRunning = false;
unsigned long lastMicMs = 0;

void stopMatrix() {
  if (!matrixRunning) return;
  clearFrame();
  show();
  delay(5);
  matrix.end();
  GPIOF_MODER &= 0xF0000000UL;  // float PF0..PF13 so all matrix LEDs are completely off
  matrixRunning = false;
}

void startMatrix() {
  if (matrixRunning) return;
  matrix.begin();
  matrix.setGrayscaleBits(3);
  matrixRunning = true;
}

void initMicPins() {
  pinMode(PIN_BCLK, OUTPUT);
  pinMode(PIN_LRCL, OUTPUT);
  pinMode(PIN_DOUT, INPUT);
  BCLK_LOW();
  LRCL_LOW();
}

inline int32_t readI2SFrameRaw() {
  uint32_t left = 0;
  LRCL_LOW();
  for (int i = 0; i < 32; i++) {
    BCLK_LOW();
    __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP();
    BCLK_HIGH();
    __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP();
    left = (left << 1) | READ_DOUT();
  }
  LRCL_HIGH();
  for (int i = 0; i < 32; i++) {
    BCLK_LOW();
    __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP();
    BCLK_HIGH();
    __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP(); __NOP();
  }
  return ((int32_t)(left << 1)) >> 14;
}

inline int32_t readI2SSample() {
  unsigned int key = irq_lock();
  int32_t s = readI2SFrameRaw();
  irq_unlock(key);
  return s;
}

int32_t micDcEst = 0;
uint8_t oledWave[OLED_W] = {0};

void sampleMicWave() {
  for (int i = 0; i < 300; i++) {
    int32_t s = readI2SSample();
    micDcEst = (micDcEst * 31 + s) >> 5;
  }
  const int N = 128;
  int64_t sumAbs = 0;
  for (int i = 0; i < N; i++) {
    int32_t s = readI2SSample();
    micDcEst = (micDcEst * 31 + s) >> 5;
    int32_t diff = s - micDcEst;
    int32_t ac = diff < 0 ? -diff : diff;
    sumAbs += ac;
  }
  int32_t avg = (int32_t)(sumAbs / N);
  int oledH = (avg * (OLED_H - 10)) / 2500;
  if (oledH > OLED_H - 10) oledH = OLED_H - 10;
  memmove(oledWave, oledWave + 1, OLED_W - 1);
  oledWave[OLED_W - 1] = (uint8_t)oledH;
}

void drawOledWave() {
  if (!oledOk) return;
  scrollActive = false;
  oledClear();
  oledFillRect(0, 0, OLED_W, 9, true);
  oledPrint(2, 1, "MIC TEST (CLICK:3s)", false, 1);

  const int cy = 10 + (OLED_H - 10) / 2;
  for (int x = 0; x < OLED_W; x++) {
    int half = oledWave[x] / 2;
    if (half == 0) {
      oledPx(x, cy);
    } else {
      int y0 = cy - half;
      int y1 = cy + half;
      if (y0 < 10) y0 = 10;
      if (y1 >= OLED_H) y1 = OLED_H - 1;
      for (int y = y0; y <= y1; y++) oledPx(x, y);
    }
  }
  oledFlush();
}

const int REC_RATE = 8000;
const int REC_MAX_SECONDS = 5;
const int REC_MAX_SAMPLES = REC_RATE * REC_MAX_SECONDS;  // 40,000 samples (80 KB RAM)
const int SLICE_SAMPLES = 800;                            // 100 ms gapless capture slice
const int MIN_PTT_SAMPLES = 2800;                         // >= 350 ms valid speech clip
const int CHUNK_SAMPLES = 256;                            // 1 KB hex chunks (proven safe for Bridge MsgPack RPC)
int16_t recBuf[REC_MAX_SAMPLES];

bool streamingAudio = false;
bool streamForAi = false;
bool aiThinking = false;
unsigned long lastThinkFrameMs = 0;
int recordedSamples = 0;
int streamOffset = 0;
int streamChunkIdx = 0;

void renderAiThinkingMatrix(unsigned long ms) {
  clearFrame();
  // Orbiting 3-dot comet around the center of the 13x8 matrix
  static const int8_t ox[12] = {3, 4, 5, 6, 7, 8, 9, 8, 7, 6, 5, 4};
  static const int8_t oy[12] = {2, 1, 1, 1, 1, 1, 2, 5, 6, 6, 6, 5};
  int head = (ms / 75) % 12;
  for (int t = 0; t < 5; t++) {
    int idx = (head + 12 - t) % 12;
    const uint8_t lv[5] = {7, 5, 3, 2, 1};
    px(ox[idx], oy[idx], lv[t]);
  }
  // Center gentle pulse
  uint8_t cv = ((ms / 250) % 2) ? 6 : 3;
  px(6, 3, cv);
  px(6, 4, cv);
  show();
}

void drawOledHoldRec() {
  if (!oledOk) return;
  scrollActive = false;
  oledClear();
  oledFillRect(0, 0, OLED_W, 9, true);
  oledPrint(2, 1, "LISTENING (MTX OFF)", false, 1);
  for (int dy = -5; dy <= 5; dy++) {
    for (int dx = -5; dx <= 5; dx++) {
      if (dx * dx + dy * dy <= 25) oledPx(14 + dx, 20 + dy);
    }
  }
  oledPrint(26, 13, "Speak now...", true, 1);
  oledPrint(26, 23, "Release to send", true, 1);
  oledFlush();
}

void calibrateMicDwt(uint32_t& cyclesPerSample) {
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CYCCNT = 0;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
  unsigned long t0 = micros();
  uint32_t c0 = DWT->CYCCNT;
  while ((unsigned long)(micros() - t0) < 1000UL) {}
  cyclesPerSample = (DWT->CYCCNT - c0) / (REC_RATE / 1000);
  if (cyclesPerSample < 2000) cyclesPerSample = 20000;
}

int aiReqId = 0;
int activeAiReqId = 0;
int pendingReplyId = 0;
String pendingReply = "";
volatile bool hasPendingReply = false;
String pendingStatusTitle = "";
String pendingStatusBody = "";
volatile bool hasPendingStatus = false;

void cancelActiveAiRequest() {
  aiReqId++;
  activeAiReqId = 0;
  streamingAudio = false;
  streamForAi = false;
  aiThinking = false;
  hasPendingReply = false;
  Bridge.notify("cancel_ai", aiReqId);
}

void startMicCapture() {
  drawOledHoldRec();
  stopMatrix();
  digitalWrite(LED_BUILTIN, LOW);

  uint32_t cyclesPerSample = 20000;
  calibrateMicDwt(cyclesPerSample);
  const int fixedSamples = REC_RATE * 3;

  unsigned int key = irq_lock();
  for (int i = 0; i < 2500; i++) {
    int32_t s = readI2SFrameRaw();
    micDcEst = (micDcEst * 31 + s) >> 5;
  }

  uint32_t nextCyc = DWT->CYCCNT + cyclesPerSample;
  for (int i = 0; i < fixedSamples; i++) {
    int64_t acc = 0;
    int subCount = 0;
    do {
      int32_t s = readI2SFrameRaw();
      micDcEst = (micDcEst * 255 + s) >> 8;
      acc += (s - micDcEst);
      subCount++;
    } while ((int32_t)(DWT->CYCCNT - nextCyc) < 0);
    nextCyc += cyclesPerSample;

    int32_t ac = (int32_t)((acc / subCount) >> 1);
    if (ac > 32767) ac = 32767;
    if (ac < -32768) ac = -32768;
    recBuf[i] = (int16_t)ac;
  }

  BCLK_LOW();
  LRCL_LOW();
  irq_unlock(key);

  digitalWrite(LED_BUILTIN, HIGH);
  if (mode == MIC_TEST) drawOledWave();

  buzzer.tone(880, 50);

  recordedSamples = fixedSamples;
  streamForAi = false;
  bool ok = false;
  Bridge.call("audio_start").result(ok);
  streamingAudio = true;
  streamOffset = 0;
  streamChunkIdx = 0;
}

void enterMenu();

void enterIdleWearable(bool showHint);

// Hold-to-Talk variable-length capture (up to 5s, stops immediately on knob release)
void runHoldToTalkCapture() {
  Mode originMode = mode;
  if (streamingAudio || aiThinking) {
    // Allow starting a fresh voice recording immediately, cancelling the stalled one
    cancelActiveAiRequest();
  }

  // 1. Audible cue BEFORE mic opens so buzzer never bleeds into recording
  buzzer.tone(784, 45);
  delay(60);

  // Verify user is still holding the knob after the start chirp
  if (!knob.isPressed()) return;

  // 2. Kill LED Matrix EMI completely & show static OLED listening prompt
  stopMatrix();
  drawOledHoldRec();
  digitalWrite(LED_BUILTIN, LOW);

  uint32_t cyclesPerSample = 20000;
  calibrateMicDwt(cyclesPerSample);

  // Warm up SPH0645 I2S modulator (~60 ms)
  {
    unsigned int key = irq_lock();
    for (int i = 0; i < 2000; i++) {
      int32_t s = readI2SFrameRaw();
      micDcEst = (micDcEst * 31 + s) >> 5;
    }
    irq_unlock(key);
  }

  // 3. Capture 100 ms (800-sample) gapless DWT-paced slices, polling knob release between slices
  int total = 0;
  while (total < REC_MAX_SAMPLES) {
    int limit = total + SLICE_SAMPLES;
    if (limit > REC_MAX_SAMPLES) limit = REC_MAX_SAMPLES;

    unsigned int key = irq_lock();
    // 16-frame quick settle after the ~0.3 ms Wire1 I2C poll
    for (int w = 0; w < 16; w++) {
      int32_t s = readI2SFrameRaw();
      micDcEst = (micDcEst * 63 + s) >> 6;
    }
    uint32_t nextCyc = DWT->CYCCNT + cyclesPerSample;
    for (int i = total; i < limit; i++) {
      int64_t acc = 0;
      int subCount = 0;
      do {
        int32_t s = readI2SFrameRaw();
        micDcEst = (micDcEst * 255 + s) >> 8;
        acc += (s - micDcEst);
        subCount++;
      } while ((int32_t)(DWT->CYCCNT - nextCyc) < 0);
      nextCyc += cyclesPerSample;

      int32_t ac = (int32_t)((acc / subCount) >> 1);
      if (ac > 32767) ac = 32767;
      if (ac < -32768) ac = -32768;
      recBuf[i] = (int16_t)ac;
    }
    BCLK_LOW();
    LRCL_LOW();
    irq_unlock(key);

    total = limit;
    if (!knob.isPressed()) {
      break;  // User released knob!
    }
  }

  digitalWrite(LED_BUILTIN, HIGH);

  // Sync debounce state cleanly after capture
  bool cur = knob.isPressed();
  rawLastPressed = cur;
  debouncedPressed = cur;
  longDone = true;
  clickPending = false;
  rawEdgeMs = millis();
  lastActivityMs = rawEdgeMs;
  modeEnterMs = rawEdgeMs;

  if (total < MIN_PTT_SAMPLES) {
    sfxBack();
    if (originMode == IDLE_WEARABLE) enterIdleWearable(false);
    else enterMenu();
    return;
  }

  // 4. Mic capture is done -> safe to turn LED matrix back on with AI Thinking animation!
  buzzer.tone(1046, 50);
  if (originMode == IDLE_WEARABLE || originMode == MENU) {
    preAiMode = originMode;
  }
  mode = AI_BUDDY;
  aiThinking = true;
  activeAiReqId = ++aiReqId;
  startMatrix();
  renderAiThinkingMatrix(millis());
  oledSetScrollText("Processing recording...", "(2xClick: Cancel)");
  oledRenderScrollView();

  recordedSamples = total;
  streamForAi = true;
  bool ok = false;
  Bridge.call("audio_start").result(ok);
  streamingAudio = true;
  streamOffset = 0;
  streamChunkIdx = 0;
}

void stepAudioStream() {
  static const char HEX_CHARS[] = "0123456789abcdef";
  char hexBuf[CHUNK_SAMPLES * 4 + 1];

  if (streamOffset >= recordedSamples) {
    streamingAudio = false;
    bool ok = false;
    if (streamForAi) {
      oledSetScrollText("Transcribing...", "(2xClick: Cancel)");
      oledRenderScrollView();
      Bridge.call("audio_ask_end", recordedSamples, activeAiReqId).result(ok);
    } else {
      Bridge.call("audio_end", recordedSamples).result(ok);
      sfxReplyReady();
    }
    return;
  }

  int count = recordedSamples - streamOffset;
  if (count > CHUNK_SAMPLES) count = CHUNK_SAMPLES;

  for (int i = 0; i < count; i++) {
    uint16_t u = (uint16_t)recBuf[streamOffset + i];
    uint8_t lo = u & 0xFF;
    uint8_t hi = (u >> 8) & 0xFF;
    hexBuf[i * 4 + 0] = HEX_CHARS[lo >> 4];
    hexBuf[i * 4 + 1] = HEX_CHARS[lo & 0x0F];
    hexBuf[i * 4 + 2] = HEX_CHARS[hi >> 4];
    hexBuf[i * 4 + 3] = HEX_CHARS[hi & 0x0F];
  }
  hexBuf[count * 4] = '\0';
  bool ok = false;
  Bridge.call("audio_chunk", streamChunkIdx++, String(hexBuf)).result(ok);
  streamOffset += count;
}

volatile bool hasPendingPeerCount = false;
int pendingPeerCountVal = 0;
volatile bool hasPendingInvite = false;
String pendingInvPeer = "";
String pendingInvGame = "";
volatile bool hasPendingStartGame = false;
String pendingStartGameName = "";
volatile bool hasPendingPicker = false;
String pendingPickerHdr = "";
String pendingPickerTitle = "";
int pendingPickerIdx = 0;
volatile bool hasPendingBleCancelled = false;
String pendingBleCancelMsg = "";

void onBlePeerCount(int count) {
  pendingPeerCountVal = count;
  hasPendingPeerCount = true;
}

void onBleInvite(String peerName, String gameTitle) {
  pendingInvPeer = peerName;
  pendingInvGame = gameTitle;
  hasPendingInvite = true;
}

void onBleStartGame(String gameName) {
  pendingStartGameName = gameName;
  hasPendingStartGame = true;
}

void onShowPicker(String hdr, String title, int idx) {
  pendingPickerHdr = hdr;
  pendingPickerTitle = title;
  pendingPickerIdx = idx;
  hasPendingPicker = true;
}

void onBleCancelled(String reason) {
  pendingBleCancelMsg = reason;
  hasPendingBleCancelled = true;
}

void drawOledInvitePopup() {
  if (!oledOk) return;
  scrollActive = false;
  oledClear();
  oledFillRect(0, 0, OLED_W, 9, true);
  char hdr[22];
  snprintf(hdr, sizeof(hdr), "PEER: %s", invitePeerName);
  oledPrint(2, 1, hdr, false, 1);

  oledPrint(2, 11, inviteGameTitle, true, 1);

  // Two selectable pill buttons on bottom row: [>Yes]  [No]
  if (inviteSel == 0) {
    oledFillRect(6, 21, 50, 10, true);
    oledPrint(14, 22, "> Yes", false, 1);
    oledPrint(76, 22, "  No", true, 1);
  } else {
    oledPrint(14, 22, "  Yes", true, 1);
    oledFillRect(70, 21, 48, 10, true);
    oledPrint(76, 22, "> No", false, 1);
  }
  oledFlush();
}

void renderInviteMatrix(unsigned long ms) {
  clearFrame();
  // Flashing exclamation + radar rings on the 13x8 matrix
  bool on = ((ms / 220) % 2) == 0;
  uint8_t outer = on ? 7 : 2;
  uint8_t inner = on ? 3 : 7;
  for (int x = 0; x < W; x++) { px(x, 0, outer); px(x, H - 1, outer); }
  for (int y = 0; y < H; y++) { px(0, y, outer); px(W - 1, y, outer); }
  px(6, 1, inner); px(6, 2, inner); px(6, 3, inner); px(6, 4, inner);
  px(6, 6, inner);
  show();
}

// ---------- AI Reply & Status Handlers (Deferred to main loop for safe I2C) ----------
void showReply(int reqId, String text) {
  if (reqId != activeAiReqId || activeAiReqId == 0) return;  // Ignore cancelled/stale replies
  pendingReplyId = reqId;
  pendingReply = text;
  hasPendingReply = true;
}

void showStatus(String title, String body) {
  pendingStatusTitle = title;
  pendingStatusBody = body;
  hasPendingStatus = true;
}

// ---------- Mode Transitions ----------
void mcuLog(const char* s) {
  Bridge.notify("mcu_log", s);
}

void enterMenu() {
  if (mode == GAME) {
    Bridge.notify("game_input", "quit");
    lastBoard[0] = '\0';
  } else if (mode == GAME_PICKER) {
    Bridge.notify("picker_ui", "cancel");
  }
  aiThinking = false;
  mode = MENU;
  lastActivityMs = millis();
  modeEnterMs = lastActivityMs;
  clickPending = false;
  startMatrix();
  drawMenuMatrixIcon(menuSel);
  drawOledMenu();
}

void enterIdleWearable(bool showHint) {
  if (mode == GAME) {
    Bridge.notify("game_input", "quit");
    lastBoard[0] = '\0';
  } else if (mode == GAME_PICKER) {
    Bridge.notify("picker_ui", "cancel");
  }
  aiThinking = false;
  mode = IDLE_WEARABLE;
  modeEnterMs = millis();
  clickPending = false;
  startMatrix();
  idleOverlayUntil = showHint ? (millis() + 1800) : 0;
  drawOledIdleBadge(showHint);
  renderIdleAnim(idleAnimIdx, frame, millis(), FONT5X7);
  show();
}

void enterSelectedMode(int sel) {
  sfxSelect();
  modeEnterMs = millis();
  clickPending = false;
  aiThinking = false;
  switch (sel) {
    case 0:  // Ask AI Buddy
      preAiMode = MENU;
      mode = AI_BUDDY;
      startMatrix();
      drawMenuMatrixIcon(0);
      oledSetScrollText("EDGE AI", "Hold knob to talk! (Or click for quick prompt. 2xClick: Menu)");
      oledRenderScrollView();
      break;
    case 1:  // Mini Games (Solo Game Picker: Pong, Snake, Tic-Tac-Toe)
      mode = GAME_PICKER;
      startMatrix();
      Bridge.notify("picker_ui", "open_solo");
      break;
    case 2:  // BLE Peers Discovery & 1v1 Invites
      mode = PEERS;
      startMatrix();
      drawMenuMatrixIcon(2);
      oledSetScrollText(bleMenuLabel, "Scanning nearby Hard Wear badges...");
      oledRenderScrollView();
      Bridge.notify("ble_ui", "open");
      break;
    case 3:  // Mic Waveform
      mode = MIC_TEST;
      stopMatrix();
      memset(oledWave, 0, sizeof(oledWave));
      drawOledWave();
      break;
  }
}

// ---------- Knob Input Handlers ----------

void onTurn(int8_t d) {
  lastActivityMs = millis();
  if (mode == IDLE_WEARABLE) {
    // Simple scroll in Idle smoothly switches the active animation/icon
    idleAnimIdx = (idleAnimIdx + (d > 0 ? 1 : ANIM_COUNT - 1)) % ANIM_COUNT;
    sfxClick(d > 0);
    idleOverlayUntil = millis() + 1600;
    drawOledIdleBadge(true);
    renderIdleAnim(idleAnimIdx, frame, millis(), FONT5X7);
    show();
  } else if (mode == MENU) {
    menuSel = (menuSel + (d > 0 ? 1 : MENU_COUNT - 1)) % MENU_COUNT;
    sfxClick(d > 0);
    drawMenuMatrixIcon(menuSel);
    drawOledMenu();
  } else if (mode == GAME_PICKER) {
    sfxClick(d > 0);
    Bridge.notify("picker_ui", d > 0 ? "next" : "prev");
  } else if (mode == GAME) {
    sfxClick(d > 0);
    Bridge.notify("game_input", d > 0 ? "next" : "prev");
  } else if (mode == PEERS) {
    sfxClick(d > 0);
    Bridge.notify("ble_ui", d > 0 ? "next" : "prev");
  } else if (mode == INVITE_POPUP) {
    inviteSel = (inviteSel + 1) % 2;
    sfxClick(d > 0);
    drawOledInvitePopup();
  } else if (scrollActive && scrollMaxPxY > 0) {
    // Allow manual scroll nudge with the knob when reading long answers!
    scrollPxY += (d > 0 ? 6 : -6);
    if (scrollPxY < 0) scrollPxY = 0;
    if (scrollPxY > scrollMaxPxY) scrollPxY = scrollMaxPxY;
    scrollPauseUntil = millis() + 2000;
    oledRenderScrollView();
  }
}

void onShortPress() {
  lastActivityMs = millis();
  if (mode == MENU) {
    enterSelectedMode(menuSel);
  } else if (mode == IDLE_WEARABLE) {
    sfxSelect();
    enterMenu();
  } else if (mode == AI_BUDDY) {
    if (!streamingAudio && !aiThinking) {
      sfxSelect();
      aiThinking = true;
      activeAiReqId = ++aiReqId;
      renderAiThinkingMatrix(millis());
      oledSetScrollText("EdgeAI Thinking...", "(2xClick: Cancel)");
      oledRenderScrollView();
      Bridge.notify("ask_request", activeAiReqId);
    }
  } else if (mode == GAME_PICKER) {
    sfxSelect();
    Bridge.notify("picker_ui", "select");
  } else if (mode == GAME) {
    sfxSelect();
    Bridge.notify("game_input", "press");
  } else if (mode == PEERS) {
    sfxSelect();
    Bridge.notify("ble_ui", "select");
  } else if (mode == INVITE_POPUP) {
    if (inviteSel == 0) {
      sfxSelect();
      Bridge.notify("ble_ui", "accept");
    } else {
      sfxBack();
      Bridge.notify("ble_ui", "decline");
      enterMenu();
    }
  } else if (mode == MIC_TEST) {
    if (!streamingAudio) {
      buzzer.tone(220, 70);
      delay(90);
      startMicCapture();
      bool cur = knob.isPressed();
      rawLastPressed = cur;
      debouncedPressed = cur;
      longDone = cur;
      rawEdgeMs = millis();
      modeEnterMs = rawEdgeMs;
    }
  }
}

void onPingStatus() {
  Bridge.notify("mcu_log", oledStatus);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);

  startMatrix();
  initMicPins();

  Bridge.begin();
  Bridge.provide("draw_board", drawBoard);
  Bridge.provide("show_reply", showReply);
  Bridge.provide("show_status", showStatus);
  Bridge.provide("ping_status", onPingStatus);
  Bridge.provide("ble_peer_count", onBlePeerCount);
  Bridge.provide("ble_invite", onBleInvite);
  Bridge.provide("ble_start_game", onBleStartGame);
  Bridge.provide("show_picker", onShowPicker);
  Bridge.provide("ble_cancelled", onBleCancelled);

  Monitor.begin();
  Modulino.begin(Wire1);
  knob.begin();
  buzzer.begin();

  if (!tryOledBus(Wire, "Wire", true) && !tryOledBus(Wire1, "Wire1", false)) {
    oledStatus = "OLED not found on Wire or Wire1";
  }

  sfxBoot();
  enterIdleWearable(false);
}

void onDoubleClick() {
  lastActivityMs = millis();
  if (streamingAudio || aiThinking) {
    // Immediately abort any uploading / thinking / stalled AI request!
    cancelActiveAiRequest();
    sfxBack();
    if (preAiMode == IDLE_WEARABLE) enterIdleWearable(false);
    else enterMenu();
    return;
  }
  if (mode == INVITE_POPUP) {
    sfxBack();
    Bridge.notify("ble_ui", "decline");
    enterIdleWearable(false);
    return;
  }
  if (mode == MENU) {
    sfxBack();
    enterIdleWearable(false);
  } else if (mode != IDLE_WEARABLE) {
    sfxBack();
    enterMenu();
  }
}

unsigned long lastOledRetryMs = 0;

void loop() {
  unsigned long now = millis();
  if (!oledOk && (now - lastOledRetryMs >= 800)) {
    lastOledRetryMs = now;
    if (tryOledBus(Wire, "Wire", true) || tryOledBus(Wire1, "Wire1", false)) {
      if (mode == IDLE_WEARABLE) drawOledIdleBadge(idleOverlayUntil != 0);
      else if (mode == MENU) drawOledMenu();
      else if (mode == GAME_PICKER) drawOledGamePicker();
      else if (mode == INVITE_POPUP) drawOledInvitePopup();
      else if (mode == GAME && lastBoard[0] != '\0') drawOledGameStatus(lastBoard);
      else if (scrollActive) oledRenderScrollView();
    }
  }

  // Process deferred Bridge notifications safely on the main loop thread (never collide I2C!)
  if (hasPendingReply) {
    hasPendingReply = false;
    if (pendingReplyId == activeAiReqId && activeAiReqId != 0) {
      String reply = pendingReply;
      activeAiReqId = 0;
      Monitor.println(reply);
      aiThinking = false;
      mode = AI_BUDDY;
      modeEnterMs = millis();
      lastActivityMs = modeEnterMs;
      startMatrix();
      drawMenuMatrixIcon(0);
      oledSetScrollText("EDGE AI", reply.c_str());
      oledRenderScrollView();
      sfxReplyReady();
      now = millis();
    }
  }
  if (hasPendingStatus) {
    hasPendingStatus = false;
    oledSetScrollText(pendingStatusTitle.c_str(), pendingStatusBody.c_str());
    oledRenderScrollView();
    now = millis();
  }
  if (hasPendingBoard) {
    hasPendingBoard = false;
    renderBoard(lastBoard, true);
    drawOledGameStatus(lastBoard);
    now = millis();
  }
  if (hasPendingPeerCount) {
    hasPendingPeerCount = false;
    blePeerCount = pendingPeerCountVal;
    snprintf(bleMenuLabel, sizeof(bleMenuLabel), "BLE Peers (%d)", blePeerCount);
    if (mode == MENU) {
      drawOledMenu();
      now = millis();
    }
  }
  if (hasPendingInvite) {
    hasPendingInvite = false;
    // Don't interrupt an active mic recording; otherwise auto-wake and chime!
    if (!streamingAudio) {
      strncpy(invitePeerName, pendingInvPeer.c_str(), sizeof(invitePeerName) - 1);
      invitePeerName[sizeof(invitePeerName) - 1] = '\0';
      strncpy(inviteGameTitle, pendingInvGame.c_str(), sizeof(inviteGameTitle) - 1);
      inviteGameTitle[sizeof(inviteGameTitle) - 1] = '\0';
      inviteSel = 0;
      aiThinking = false;
      mode = INVITE_POPUP;
      modeEnterMs = millis();
      lastActivityMs = modeEnterMs;
      clickPending = false;
      startMatrix();
      renderInviteMatrix(modeEnterMs);
      drawOledInvitePopup();
      sfxInvite();
      now = millis();
    }
  }
  if (hasPendingStartGame) {
    hasPendingStartGame = false;
    aiThinking = false;
    mode = GAME;
    modeEnterMs = millis();
    lastActivityMs = modeEnterMs;
    clickPending = false;
    lastBoard[0] = '\0';
    lastGameEndChime = ' ';
    startMatrix();
    oledSetScrollText("MATCH STARTING", "Get ready!");
    oledRenderScrollView();
    sfxSelect();
    now = millis();
  }
  if (hasPendingPicker) {
    hasPendingPicker = false;
    aiThinking = false;
    mode = GAME_PICKER;
    modeEnterMs = millis();
    lastActivityMs = modeEnterMs;
    clickPending = false;
    strncpy(pickerHdr, pendingPickerHdr.c_str(), sizeof(pickerHdr) - 1);
    pickerHdr[sizeof(pickerHdr) - 1] = '\0';
    strncpy(pickerTitle, pendingPickerTitle.c_str(), sizeof(pickerTitle) - 1);
    pickerTitle[sizeof(pickerTitle) - 1] = '\0';
    pickerIdx = pendingPickerIdx;
    startMatrix();
    drawGamePreviewIcon(pickerIdx);
    drawOledGamePicker();
    now = millis();
  }
  if (hasPendingBleCancelled) {
    hasPendingBleCancelled = false;
    aiThinking = false;
    sfxBack();
    enterIdleWearable(false);
    now = millis();
  }

  stepSfx(now);

  int8_t d = knob.getDirection();
  d = -d;  // knob is mounted reversed: swap turn direction in software
  if (d != 0) {
    if (clickPending) {
      clickPending = false;
      onShortPress();
    }
    onTurn(d);
  }

  // 35 ms symmetrical hardware debounce on Modulino Knob switch
  bool rawP = knob.isPressed();
  if (rawP != rawLastPressed) {
    rawLastPressed = rawP;
    rawEdgeMs = now;
  }

  if ((now - rawEdgeMs >= 35) && (rawP != debouncedPressed)) {
    debouncedPressed = rawP;
    lastActivityMs = now;

    if (debouncedPressed) {
      // Button stably pressed
      pressStart = now;
      longDone = false;
    } else {
      // Button stably released
      // Ignore any bounce/click within 200 ms of waking into a new mode
      if (!longDone && (now - modeEnterMs >= 200)) {
        if (mode == IDLE_WEARABLE) {
          clickPending = false;
          onShortPress();
        } else if (clickPending && (now - firstClickReleaseMs <= DBL_CLICK_MS)) {
          clickPending = false;
          onDoubleClick();
        } else {
          clickPending = true;
          firstClickReleaseMs = now;
        }
      }
    }
  }

  // Hold-to-Talk (> 350 ms hold) from Idle, Menu, or AI Buddy
  if (debouncedPressed && !longDone && (now - pressStart >= 350)) {
    longDone = true;
    clickPending = false;
    lastActivityMs = now;
    if (mode == IDLE_WEARABLE || mode == MENU || mode == AI_BUDDY) {
      runHoldToTalkCapture();
    }
  }

  if (clickPending && !debouncedPressed && (now - firstClickReleaseMs > DBL_CLICK_MS)) {
    clickPending = false;
    onShortPress();
  }

  if (streamingAudio) {
    stepAudioStream();
  }

  // Auto-idle after 10s of inactivity in Menu
  if (mode == MENU && ((unsigned long)(millis() - lastActivityMs) >= IDLE_TIMEOUT_MS)) {
    enterIdleWearable(false);
  }

  // Mode-specific frame/display updates
  if (aiThinking && mode == AI_BUDDY) {
    if (now - lastThinkFrameMs >= 65) {
      lastThinkFrameMs = now;
      renderAiThinkingMatrix(now);
    }
    if (scrollActive) stepOledScroller(now);
  } else if (mode == INVITE_POPUP) {
    if (now - lastIdleFrameMs >= 90) {
      lastIdleFrameMs = now;
      renderInviteMatrix(now);
    }
  } else if (mode == IDLE_WEARABLE) {
    if (idleOverlayUntil != 0 && (long)(now - idleOverlayUntil) >= 0) {
      idleOverlayUntil = 0;
      drawOledIdleBadge(false);
    }
    if (now - lastIdleFrameMs >= 45) {
      lastIdleFrameMs = now;
      renderIdleAnim(idleAnimIdx, frame, now, FONT5X7);
      show();
    }
  } else if (mode == GAME && lastBoard[0] != '\0') {
    bool isSnakeFood = (lastBoard[0] == 'S' && lastBoard[2] == 'p');
    bool isTttTurn = (lastBoard[0] != 'P' && lastBoard[0] != 'S' && strlen(lastBoard) >= 12 && lastBoard[10] == 't');
    if (isSnakeFood || isTttTurn) {
      bool blinkOn = ((now / 260) % 2) == 0;
      if (blinkOn != lastBlink) {
        lastBlink = blinkOn;
        renderBoard(lastBoard, blinkOn);
      }
    }
  } else if (mode == MIC_TEST && !streamingAudio && (now - lastMicMs >= 50)) {
    lastMicMs = now;
    sampleMicWave();
    drawOledWave();
  } else if (scrollActive) {
    stepOledScroller(now);
  }

  delay(2);
}
