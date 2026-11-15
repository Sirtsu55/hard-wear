// Preset 13x8 3-bit grayscale (0..7) procedural & frame animations for Fashion Wearable Idle Mode.
#pragma once
#include <Arduino.h>

static const int ANIM_COUNT = 5;
static const char* const ANIM_NAMES[ANIM_COUNT] = {
  "Pulse Heart",
  "Cyber Eyes",
  "Synth Wave",
  "Starfall",
  "HardWear Tag"
};

// Helper pixel writer into a 13x8 grayscale buffer
static inline void animPx(uint8_t* f, int x, int y, uint8_t v) {
  if (x >= 0 && x < 13 && y >= 0 && y < 8) {
    f[y * 13 + x] = v;
  }
}

static inline void animClear(uint8_t* f) {
  memset(f, 0, 13 * 8);
}

// 0: Pulsing grayscale heart with subtle sparkle
static void renderHeartAnim(uint8_t* f, unsigned long ms) {
  static const uint16_t rows[8] = {
    0b0001100011000,
    0b0011110111100,
    0b0111111111110,
    0b0111111111110,
    0b0011111111100,
    0b0001111111000,
    0b0000111110000,
    0b0000011100000
  };
  static const uint16_t core[8] = {
    0b0000000000000,
    0b0001100011000,
    0b0011110111100,
    0b0011111111100,
    0b0001111111000,
    0b0000111110000,
    0b0000011100000,
    0b0000000000000
  };
  animClear(f);
  // Lub-dub heartbeat cycle every 1200 ms
  unsigned long phase = ms % 1200;
  uint8_t outerV = 3;
  uint8_t innerV = 5;
  if (phase < 120) {
    outerV = 7; innerV = 7;
  } else if (phase >= 220 && phase < 360) {
    outerV = 6; innerV = 7;
  } else if (phase < 700) {
    outerV = 4; innerV = 6;
  } else {
    outerV = 2; innerV = 4;
  }

  for (int y = 0; y < 8; y++) {
    for (int x = 0; x < 13; x++) {
      bool c = (core[y] >> (12 - x)) & 1;
      bool r = (rows[y] >> (12 - x)) & 1;
      if (c) animPx(f, x, y, innerV);
      else if (r) animPx(f, x, y, outerV);
    }
  }
  // Corner sparkles
  int sp = (ms / 300) % 4;
  const int sx[4] = {0, 12, 1, 11};
  const int sy[4] = {1, 6, 6, 1};
  animPx(f, sx[sp], sy[sp], (ms / 150) % 2 ? 5 : 2);
}

// 1: Expressive Cyber Buddy Eyes (looks left/right, blinks, happy squint)
static void renderCyberEyesAnim(uint8_t* f, unsigned long ms) {
  animClear(f);
  unsigned long cycle = ms % 6000;
  int lookX = 0;
  int lookY = 0;
  bool blink = false;
  bool happy = false;

  if (cycle > 1200 && cycle < 2200) lookX = -1;
  else if (cycle >= 2600 && cycle < 3600) lookX = 1;
  else if (cycle >= 4400 && cycle < 5400) happy = true;

  if ((cycle > 950 && cycle < 1100) || (cycle > 3800 && cycle < 3950)) {
    blink = true;
  }

  // Two eyes centered around x=2..5 and x=7..10, y=1..6
  const int ex[2] = {2, 7};
  for (int e = 0; e < 2; e++) {
    int x0 = ex[e];
    if (blink) {
      for (int dx = 0; dx < 4; dx++) animPx(f, x0 + dx, 4, 6);
    } else if (happy) {
      // ^ ^ happy arcs
      animPx(f, x0, 4, 6);
      animPx(f, x0 + 1, 2, 7);
      animPx(f, x0 + 2, 2, 7);
      animPx(f, x0 + 3, 4, 6);
      animPx(f, x0 + 1, 3, 4);
      animPx(f, x0 + 2, 3, 4);
    } else {
      // 4x5 rounded eye outline + bright pupil
      for (int y = 1; y <= 6; y++) {
        for (int x = 0; x < 4; x++) {
          bool corner = ((y == 1 || y == 6) && (x == 0 || x == 3));
          if (!corner) animPx(f, x0 + x, y, 2);
        }
      }
      int px0 = x0 + 1 + lookX;
      int py0 = 3 + lookY;
      animPx(f, px0, py0, 7);
      animPx(f, px0 + 1, py0, 7);
      animPx(f, px0, py0 + 1, 7);
      animPx(f, px0 + 1, py0 + 1, 5);
    }
  }
}

// 2: Traveling Synth Wave / Equalizer
static void renderSynthWaveAnim(uint8_t* f, unsigned long ms) {
  animClear(f);
  static const int8_t sinLut[16] = {3, 5, 6, 7, 7, 6, 5, 3, 2, 1, 0, 0, 0, 1, 2, 3};
  int t = (int)(ms / 70);
  for (int x = 0; x < 13; x++) {
    int h1 = sinLut[(x * 2 + t) & 15];
    int h2 = sinLut[(x + 16 - (t & 15)) & 15];
    int peak = (h1 + h2) / 2;  // 0..7
    for (int y = 7; y >= 7 - peak; y--) {
      uint8_t v = (y == 7 - peak) ? 7 : (uint8_t)(2 + (7 - y) / 2);
      animPx(f, x, y, v);
    }
  }
}

// 3: Diagonal Starfall / Matrix Rain with grayscale trails
static void renderStarfallAnim(uint8_t* f, unsigned long ms) {
  animClear(f);
  int step = (int)(ms / 85);
  for (int x = 0; x < 13; x++) {
    // Deterministic pseudo-random speed & offset per column
    int speed = 1 + (x % 2);
    int offset = (x * 7 + 3) % 13;
    int headY = ((step * speed) + offset) % 12 - 2;
    for (int tail = 0; tail < 4; tail++) {
      int y = headY - tail;
      const uint8_t levels[4] = {7, 4, 2, 1};
      animPx(f, x, y, levels[tail]);
    }
  }
}

// 4: Scrolling "HARD WEAR *" fashion ticker on the 13x8 matrix using font5x7
static void renderTickerAnim(uint8_t* f, unsigned long ms, const uint8_t font[95][5]) {
  static const char* msg = "HARD WEAR * ";
  const int msgLen = 12;
  const int totalCols = msgLen * 6;  // 5 cols + 1 spacing
  animClear(f);

  int scroll = (int)(ms / 75) % totalCols;
  for (int x = 0; x < 13; x++) {
    int col = (scroll + x) % totalCols;
    int chIdx = col / 6;
    int glyphCol = col % 6;
    if (glyphCol < 5) {
      char c = msg[chIdx];
      if (c >= 0x20 && c <= 0x7E) {
        uint8_t bits = font[c - 0x20][glyphCol];
        for (int y = 0; y < 7; y++) {
          if ((bits >> y) & 1) {
            // Subtle edge fade at x=0 and x=12
            uint8_t v = (x == 0 || x == 12) ? 3 : 7;
            animPx(f, x, y + 1, v);
          }
        }
      }
    }
  }
}

static inline void renderIdleAnim(int animIdx, uint8_t* f, unsigned long ms, const uint8_t font[95][5]) {
  switch (animIdx % ANIM_COUNT) {
    case 0: renderHeartAnim(f, ms); break;
    case 1: renderCyberEyesAnim(f, ms); break;
    case 2: renderSynthWaveAnim(f, ms); break;
    case 3: renderStarfallAnim(f, ms); break;
    default: renderTickerAnim(f, ms, font); break;
  }
}
