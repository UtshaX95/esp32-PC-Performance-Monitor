// ============================================================================
//  PERFORMANCE MONITOR  -  ESP32 + 2.8" SPI TFT (ILI9341, 240x320, landscape)
// ============================================================================
//  Real-time PC dashboard: CPU / GPU (with FPS) / Memory / Network,
//  each with a live scrolling graph, 12-hour clock with small AM/PM.
//
//  DATA SOURCE : pc_agent/monitor_agent.py running on your PC,
//                streamed over the USB cable as JSON lines @ 115200 baud.
//
//  LIBRARIES   : TFT_eSPI      (display driver - configure with the provided
//                               User_Setup.h, see README section 3.3)
//                ArduinoJson   (JSON parser, by Benoit Blanchon)
//  FONTS       : none to download - built into TFT_eSPI (GLCD font 1, font 2)
//
//  MEMORY      : this version draws DIRECTLY to the display - it needs NO
//                frame buffer, so it runs on every ESP32 board even with
//                very little free RAM (no more "OUT OF MEMORY").
//
//  WIRING      :  TFT SCK  -> GPIO18     TFT MOSI/SDI -> GPIO23
//                 TFT MISO -> GPIO19 (optional)       TFT CS   -> GPIO22
//                 TFT DC   -> GPIO21     TFT RST      -> GPIO27
//                 TFT VCC  -> VIN(5V)*  TFT GND -> GND   TFT LED -> 3V3
//                 (* use 3V3 instead of VIN only if your display has NO
//                    onboard voltage regulator)
// ============================================================================

#include <TFT_eSPI.h>        // display driver
#include <ArduinoJson.h>     // JSON parser
#include <math.h>

TFT_eSPI tft = TFT_eSPI();   // draws straight to the display (no sprite)

// ------------------------- screen geometry ---------------------------------
#define SCREEN_W   320
#define SCREEN_H   240
#define HEADER_H   26

// quadrant boxes (landscape):  TL=CPU  TR=GPU  BL=MEMORY  BR=NETWORK
#define QX_L       0
#define QX_R       162
#define QW         158
#define QY_T       28
#define QH_T       104          // top quadrants   :  28 .. 132
#define QY_B       136
#define QH_B       102          // bottom quadrants: 136 .. 238

// per-quadrant inner layout (relative to quadrant origin)
#define LBL_X      2            // section label  y offset
#define RULE_Y     20           // coloured rule under the label row
#define ROW1_Y     26           // first value row   (CPU / GPU)
#define ROW2_Y     44           // second value row
#define ROW3_Y     62           // third value row (fan RPM, no label - like ref)
#define GAUGE_CX   36
#define GAUGE_CY   52
#define GAUGE_R    20
#define GAUGE_TH   4
#define LBLVAL_X   70           // "Temp"/"Volt"/"VRAM" labels x offset
#define MEM_USE_Y  22           // memory quadrant rows
#define MEM_BAR_Y  40
#define MEM_FRQ_Y  46
#define MEM_VLT_Y  62
#define NET_ROW1   30
#define NET_ROW2   54
#define GRAPH_GX   10           // graph box (inside quadrant)
#define GRAPH_GY   80
#define GRAPH_GW   138
#define GRAPH_GH   20

#define HIST_N     70           // graph history: 70 samples x 2 px = 140 px
#define SAMPLE_PX  2

// ------------------------- palette (RGB565) --------------------------------
#define C_BG       0x0862       // rgb(10,12,18)   near-black blue
#define C_PANEL    0x0883       // rgb(14,18,26)   header strip
#define C_CPU      0x073F       // rgb(0,229,255)  aqua
#define C_GPU      0xFBC0       // rgb(255,122,0)  orange
#define C_MEM      0xFE80       // rgb(255,210,0)  yellow
#define C_NET      0x2F35       // rgb(46,230,168) green
#define C_TEXT     0xADB7       // rgb(170,180,190) grey labels
#define C_WHITE    0xFFFF
#define C_DIM      0x3A09       // rgb(60,66,78)   rules / track
#define C_TITLE    0x7F3E       // rgb(120,230,240) title
#define C_BARBG    0x18E4       // rgb(24,28,36)   progress-bar track

#define DEG2RAD    0.017453293f

// ============================================================================
//  state filled from the PC agent
// ============================================================================
struct State {
  float cpu  = 0, cpuTemp = -1, cpuVolt = -1;  int   cpuFan = -1;
  float gpu  = 0, gpuTemp = -1, vram    = -1;  int   gpuFan = -1, fps = 0;
  int   gpuFanPct = 0;                        // 1 = gpuFan is percent, 0 = RPM
  float mem  = 0, memFreq = -1, memVolt = -1;
  float down = 0, up = 0;
  int   hh   = 0, mm = 0, ap = 0;             // h 1-12, m 0-59, ap 0=AM 1=PM
} st;

bool hasData = false;
bool dirty   = true;

// graph history: 0=cpu 1=gpu 2=mem 3=net(total KB/s)
float hist[4][HIST_N];
int   histHead[4]  = {0, 0, 0, 0};
int   histCount[4] = {0, 0, 0, 0};

char lineBuf[300];
int  linePos = 0;

// ============================================================================
//  low-level helpers
// ============================================================================
void pushHist(int idx, float v) {
  hist[idx][histHead[idx]] = v;
  histHead[idx] = (histHead[idx] + 1) % HIST_N;
  if (histCount[idx] < HIST_N) histCount[idx]++;
}

// ring-segment arc. angle: 0 deg = 12 o'clock, positive = clockwise.
void drawArcRing(int cx, int cy, int r, int thick, float startDeg, float sweepDeg, uint16_t col) {
  if (sweepDeg <= 0.01f) return;
  int steps = (int)(fabsf(sweepDeg) / 2.2f) + 1;
  float px = 0, py = 0;
  bool first = true;
  for (int i = 0; i <= steps; i++) {
    float a  = (startDeg + sweepDeg * ((float)i / steps)) * DEG2RAD;
    float sn = sinf(a), cs = cosf(a);
    float x  = cx + r * sn, y = cy - r * cs;
    float x2 = cx + (r - thick) * sn, y2 = cy - (r - thick) * cs;
    if (!first) {
      tft.drawLine((int)px, (int)py, (int)x, (int)y, col);          // outer edge
      tft.drawLine((int)x2, (int)y2, (int)x, (int)y, col);          // radial fill
    }
    px = x; py = y; first = false;
  }
}

// ============================================================================
//  dynamic rows - each call clears its own area first, then draws, so
//  nothing ever smears or overlaps (no frame buffer needed)
// ============================================================================
// full row: grey label on the left + white value right-aligned
void drawRow(int qx, int y, const char* label, const char* val, uint16_t valCol) {
  int x0 = qx + 8, rightX = qx + QW - 8;
  tft.fillRect(x0, y - 1, rightX - x0, 19, C_BG);
  tft.setTextFont(2);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(C_TEXT, C_BG);
  tft.drawString(label, x0, y);
  int labelEnd = x0 + tft.textWidth(label) + 2;
  if (rightX - tft.textWidth(val) >= labelEnd) {   // fits at full size
    tft.setTextFont(2);
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(valCol, C_BG);
    tft.drawString(val, rightX, y);
  } else {
    tft.setTextFont(1);                             // shrunk value, no overlap
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(valCol, C_BG);
    tft.drawString(val, rightX, y + 5);
  }
}

// row with its label in the inner label column ("Temp" / "Volt" / "VRAM")
// wide values (e.g. "1318 RPM") auto-shrink to the small font so they can
// never collide with the label - layout of short values is unchanged
void drawRowVal(int qx, int y, const char* label, const char* val) {
  int x0 = qx + LBLVAL_X, rightX = qx + QW - 8;
  tft.fillRect(x0, y - 1, rightX - x0, 19, C_BG);
  tft.setTextFont(2);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(C_TEXT, C_BG);
  tft.drawString(label, x0, y);
  int labelEnd = x0 + tft.textWidth(label) + 2;
  if (rightX - tft.textWidth(val) >= labelEnd) {   // fits at full size
    tft.setTextFont(2);
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(C_WHITE, C_BG);
    tft.drawString(val, rightX, y);
  } else {
    tft.setTextFont(1);                             // shrunk value, no overlap
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(C_WHITE, C_BG);
    tft.drawString(val, rightX, y + 5);
  }
}

// temperature row:  "40 °C"  (degree drawn as a small ring)
void drawRowTemp(int qx, int y, const char* label, float t) {
  int x0 = qx + LBLVAL_X, rightX = qx + QW - 8;
  tft.fillRect(x0, y - 1, rightX - x0, 19, C_BG);
  tft.setTextFont(2);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(C_TEXT, C_BG);
  tft.drawString(label, x0, y);
  if (t < 0) {
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(C_WHITE, C_BG);
    tft.drawString("--", rightX, y);
    return;
  }
  char buf[8];
  snprintf(buf, sizeof(buf), "%d", (int)lroundf(t));
  int wNum = tft.textWidth(buf);
  int wC   = tft.textWidth("C");
  int x    = rightX - (wNum + 10 + wC);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(C_WHITE, C_BG);
  tft.drawString(buf, x, y);
  tft.drawCircle(x + wNum + 6, y + 5, 2, C_WHITE);   // degree ring
  tft.drawString("C", x + wNum + 10, y);
}

// ============================================================================
//  gauge (ring with percentage in the middle)
// ============================================================================
void drawGauge(int qx, int qy, float pct, uint16_t col) {
  int cx = qx + GAUGE_CX, cy = qy + GAUGE_CY;
  tft.fillCircle(cx, cy, GAUGE_R + 3, C_BG);                 // clear its area
  drawArcRing(cx, cy, GAUGE_R, GAUGE_TH, -135, 270, C_DIM);  // track
  if (pct >= 0)
    drawArcRing(cx, cy, GAUGE_R, GAUGE_TH, -135, 270.0f * pct / 100.0f, col);
  char buf[24];
  if (pct >= 0) snprintf(buf, sizeof(buf), "%d %%", (int)lroundf(pct));
  else          snprintf(buf, sizeof(buf), "-- %%");
  tft.setTextFont(2);
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(C_WHITE, C_BG);
  tft.drawString(buf, cx, cy);
}

// ============================================================================
//  network speed formatting
// ============================================================================
void fmtNet(char* buf, size_t n, float kbs) {
  if (kbs < 0 || kbs > 9999999.0f) snprintf(buf, n, "--");
  else if (kbs >= 1024.0f)      snprintf(buf, n, "%.1f MB/s", kbs / 1024.0f);
  else if (kbs >= 10.0f)        snprintf(buf, n, "%.0f KB/s", kbs);
  else if (kbs >= 0.05f)        snprintf(buf, n, "%.1f KB/s", kbs);
  else                          snprintf(buf, n, "0 KB/s");
}

// ============================================================================
//  graph
// ============================================================================
void drawGraph(int idx, int gx, int gy, int gw, int gh, uint16_t col, bool autoScale) {
  tft.fillRect(gx, gy, gw, gh, C_BG);            // clear the strip
  tft.drawFastHLine(gx, gy + gh - 1, gw, C_DIM); // baseline
  int n = histCount[idx];
  if (n < 2) return;

  float maxV = 100.0f;
  if (autoScale) {
    maxV = 5.0f;
    int tail = (histHead[idx] - n + HIST_N * 2) % HIST_N;
    for (int i = 0; i < n; i++) {
      float v = hist[idx][(tail + i) % HIST_N];
      if (v > maxV) maxV = v;
    }
    maxV *= 1.15f;
  }

  int drawN = n; if (drawN > (gw / SAMPLE_PX)) drawN = gw / SAMPLE_PX;
  int tail  = (histHead[idx] - drawN + HIST_N * 2) % HIST_N;
  int xEnd  = gx + gw - 1;
  int prevX = 0, prevY = 0;
  for (int i = 0; i < drawN; i++) {
    float v = hist[idx][(tail + i) % HIST_N];
    if (v < 0) v = 0; if (v > maxV) v = maxV;
    int x = xEnd - (drawN - 1 - i) * SAMPLE_PX;
    int y = gy + gh - 2 - (int)((v / maxV) * (gh - 3));
    if (i > 0) tft.drawLine(prevX, prevY, x, y, col);
    prevX = x; prevY = y;
  }
}

// ============================================================================
//  tiny section icons (12x12 line icons, like the reference photo)
// ============================================================================
void iconCPU(int x, int y, uint16_t c) {
  tft.drawRect(x + 1, y + 1, 10, 10, c);
  tft.fillRect(x + 4, y + 4, 4, 4, c);
  for (int k = 0; k < 3; k++) {
    tft.drawFastVLine(x + 3 + 3 * k, y - 1, 3, c);
    tft.drawFastVLine(x + 3 + 3 * k, y + 10, 3, c);
    tft.drawFastHLine(x - 1, y + 3 + 3 * k, 3, c);
    tft.drawFastHLine(x + 10, y + 3 + 3 * k, 3, c);
  }
}
void iconGPU(int x, int y, uint16_t c) {
  tft.drawRect(x, y + 1, 12, 10, c);
  tft.drawCircle(x + 4, y + 6, 2, c);
  tft.drawFastHLine(x + 8, y + 3, 3, c);
  tft.drawFastHLine(x + 8, y + 6, 3, c);
  tft.drawFastHLine(x + 8, y + 9, 3, c);
}
void iconMEM(int x, int y, uint16_t c) {
  tft.drawRect(x, y + 1, 12, 10, c);
  tft.drawFastVLine(x + 4, y + 3, 6, c);
  tft.drawFastVLine(x + 8, y + 3, 6, c);
  tft.drawFastHLine(x + 2, y + 12, 8, c);
}
void iconNET(int x, int y, uint16_t c) {
  tft.drawCircle(x + 6, y + 6, 5, c);
  tft.drawFastVLine(x + 6, y + 1, 10, c);
  tft.drawFastHLine(x + 1, y + 6, 10, c);
  tft.drawCircle(x + 6, y + 6, 1, c);
}

// ============================================================================
//  static chrome - drawn ONCE at boot (never cleared again)
// ============================================================================
void drawHeaderStatic() {
  tft.fillRect(0, 0, SCREEN_W, HEADER_H, C_PANEL);
  tft.drawFastHLine(0, HEADER_H - 1, SCREEN_W, C_DIM);

  // title (shrinks to the small font if it would collide with the clock)
  const char* title = "PERFORMANCE MONITOR";
  int timeW = tft.textWidth("10:51");     // estimate for the fit check
  int apW   = tft.textWidth("PM");
  int avail = (SCREEN_W - 12 - apW - 4 - timeW) - 8 - 12;
  tft.setTextFont(2);
  if (tft.textWidth(title) > avail) tft.setTextFont(1);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(C_TITLE);
  tft.drawString(title, 8, 5);
}

void drawChrome() {
  // separators
  tft.drawFastVLine(160, HEADER_H, SCREEN_H - HEADER_H, C_DIM);
  tft.drawFastHLine(0, 134, SCREEN_W, C_DIM);

  // ---- CPU ----
  iconCPU(QX_L + 8, QY_T + 3, C_CPU);
  tft.setTextFont(2); tft.setTextDatum(TL_DATUM); tft.setTextColor(C_CPU);
  tft.drawString("CPU", QX_L + 26, QY_T + LBL_X);
  tft.fillRect(QX_L + 8, QY_T + RULE_Y, QW - 16, 2, C_CPU);
  tft.fillRect(QX_L + 8, QY_T + QH_T - 3, QW - 16, 2, C_CPU);

  // ---- GPU ----
  iconGPU(QX_R + 8, QY_T + 3, C_GPU);
  tft.setTextFont(2); tft.setTextDatum(TL_DATUM); tft.setTextColor(C_GPU);
  tft.drawString("GPU", QX_R + 26, QY_T + LBL_X);
  tft.fillRect(QX_R + 8, QY_T + RULE_Y, QW - 16, 2, C_GPU);
  tft.fillRect(QX_R + 8, QY_T + QH_T - 3, QW - 16, 2, C_GPU);

  // ---- MEMORY ----
  iconMEM(QX_L + 8, QY_B + 3, C_MEM);
  tft.setTextFont(2); tft.setTextDatum(TL_DATUM); tft.setTextColor(C_MEM);
  tft.drawString("MEMORY", QX_L + 26, QY_B + LBL_X);
  tft.fillRect(QX_L + 8, QY_B + RULE_Y, QW - 16, 2, C_MEM);
  tft.fillRect(QX_L + 8, QY_B + QH_B - 3, QW - 16, 2, C_MEM);

  // ---- NETWORK ----
  iconNET(QX_R + 8, QY_B + 3, C_NET);
  tft.setTextFont(2); tft.setTextDatum(TL_DATUM); tft.setTextColor(C_NET);
  tft.drawString("NETWORK", QX_R + 26, QY_B + LBL_X);
  tft.fillRect(QX_R + 8, QY_B + RULE_Y, QW - 16, 2, C_NET);
  tft.fillRect(QX_R + 8, QY_B + QH_B - 3, QW - 16, 2, C_NET);
}

// ============================================================================
//  dynamic parts
// ============================================================================
void drawClock() {
  char timeStr[8], apStr[4];
  if (hasData) snprintf(timeStr, sizeof(timeStr), "%d:%02d", st.hh, st.mm);
  else         snprintf(timeStr, sizeof(timeStr), "--:--");
  snprintf(apStr, sizeof(apStr), "%s", (st.ap == 1) ? "PM" : "AM");

  tft.setTextFont(2);
  int timeW = tft.textWidth(timeStr);
  tft.setTextFont(1);
  int apW   = tft.textWidth(apStr);
  int timeX = SCREEN_W - 12 - apW - 4 - timeW;

  tft.fillRect(timeX - 6, 2, SCREEN_W - (timeX - 6), 22, C_BG);  // clear slot

  tft.setTextFont(2);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(C_WHITE, C_BG);
  tft.drawString(timeStr, timeX, 5);
  tft.setTextFont(1);
  tft.setTextColor(C_TEXT, C_BG);
  tft.drawString(apStr, SCREEN_W - 12 - apW, 12);
}

void drawCPU() {
  char buf[24];
  drawGauge(QX_L, QY_T, hasData ? st.cpu : -1, C_CPU);

  if (hasData && st.cpuTemp >= 0) drawRowTemp(QX_L, QY_T + ROW1_Y, "Temp", st.cpuTemp);
  else                            drawRowVal(QX_L, QY_T + ROW1_Y, "Temp", "--");

  if (hasData && st.cpuVolt >= 0) { snprintf(buf, sizeof(buf), "%.2f V", st.cpuVolt); drawRowVal(QX_L, QY_T + ROW2_Y, "Volt", buf); }
  else                            drawRowVal(QX_L, QY_T + ROW2_Y, "Volt", "--");

  if (hasData && st.cpuFan >= 0) { snprintf(buf, sizeof(buf), "%d RPM", st.cpuFan); drawRowVal(QX_L, QY_T + ROW3_Y, "Fan", buf); }
  else                           drawRowVal(QX_L, QY_T + ROW3_Y, "Fan", "--");
}

void drawGPU() {
  char buf[24];

  // FPS top-right of the GPU quadrant (live game FPS from the PC agent)
  if (hasData) snprintf(buf, sizeof(buf), "%d FPS", st.fps);
  else         snprintf(buf, sizeof(buf), "-- FPS");
  int rightX = QX_R + QW - 8, x0 = QX_R + 88;
  tft.fillRect(x0, QY_T + LBL_X - 1, rightX - x0, 19, C_BG);
  tft.setTextFont(2); tft.setTextDatum(TR_DATUM); tft.setTextColor(C_GPU, C_BG);
  tft.drawString(buf, rightX, QY_T + LBL_X);

  drawGauge(QX_R, QY_T, hasData ? st.gpu : -1, C_GPU);

  if (hasData && st.gpuTemp >= 0) drawRowTemp(QX_R, QY_T + ROW1_Y, "Temp", st.gpuTemp);
  else                            drawRowVal(QX_R, QY_T + ROW1_Y, "Temp", "--");

  if (hasData && st.vram >= 0) { snprintf(buf, sizeof(buf), "%d %%", (int)lroundf(st.vram)); drawRowVal(QX_R, QY_T + ROW2_Y, "VRAM", buf); }
  else                         drawRowVal(QX_R, QY_T + ROW2_Y, "VRAM", "--");

  if (hasData && st.gpuFan >= 0) {
    if (st.gpuFanPct) snprintf(buf, sizeof(buf), "%d %%", st.gpuFan);
    else              snprintf(buf, sizeof(buf), "%d RPM", st.gpuFan);
    drawRowVal(QX_R, QY_T + ROW3_Y, "Fan", buf);
  }
  else drawRowVal(QX_R, QY_T + ROW3_Y, "Fan", "--");
}

void drawMEM() {
  char buf[24];
  int rx = QX_L + QW - 8;

  if (hasData && st.mem >= 0) { snprintf(buf, sizeof(buf), "%d %%", (int)lroundf(st.mem)); drawRow(QX_L, QY_B + MEM_USE_Y, "Usage", buf, C_WHITE); }
  else                        drawRow(QX_L, QY_B + MEM_USE_Y, "Usage", "--", C_WHITE);

  // usage progress bar
  int bw = QW - 16;
  tft.fillRect(QX_L + 8, QY_B + MEM_BAR_Y, bw, 5, C_BARBG);
  int fill = (hasData && st.mem >= 0) ? (int)(bw * st.mem / 100.0f) : 0;
  if (fill > 0) tft.fillRect(QX_L + 8, QY_B + MEM_BAR_Y, fill, 5, C_MEM);

  if (hasData && st.memFreq >= 0) { snprintf(buf, sizeof(buf), "%d MHz", (int)lroundf(st.memFreq)); drawRow(QX_L, QY_B + MEM_FRQ_Y, "Freq", buf, C_WHITE); }
  else                            drawRow(QX_L, QY_B + MEM_FRQ_Y, "Freq", "--", C_WHITE);

  if (hasData && st.memVolt >= 0) { snprintf(buf, sizeof(buf), "%.2f V", st.memVolt); drawRow(QX_L, QY_B + MEM_VLT_Y, "Volt", buf, C_WHITE); }
  else                            drawRow(QX_L, QY_B + MEM_VLT_Y, "Volt", "--", C_WHITE);
  (void)rx;
}

void drawNET() {
  char buf[24];
  if (hasData) { fmtNet(buf, sizeof(buf), st.down); drawRow(QX_R, QY_B + NET_ROW1, "Down", buf, C_WHITE); }
  else         drawRow(QX_R, QY_B + NET_ROW1, "Down", "--", C_WHITE);
  if (hasData) { fmtNet(buf, sizeof(buf), st.up);   drawRow(QX_R, QY_B + NET_ROW2, "Upload", buf, C_WHITE); }
  else         drawRow(QX_R, QY_B + NET_ROW2, "Upload", "--", C_WHITE);
}

// ============================================================================
//  render one update (each area clears itself before drawing -> no smear)
// ============================================================================
void render() {
  drawClock();
  drawCPU();
  drawGPU();
  drawMEM();
  drawNET();
  if (hasData) {
    drawGraph(0, QX_L + GRAPH_GX, QY_T + GRAPH_GY, GRAPH_GW, GRAPH_GH, C_CPU, false);
    drawGraph(1, QX_R + GRAPH_GX, QY_T + GRAPH_GY, GRAPH_GW, GRAPH_GH, C_GPU, false);
    drawGraph(2, QX_L + GRAPH_GX, QY_B + GRAPH_GY, GRAPH_GW, GRAPH_GH, C_MEM, false);
    drawGraph(3, QX_R + GRAPH_GX, QY_B + GRAPH_GY, GRAPH_GW, GRAPH_GH, C_NET, true);
  }
}

// ============================================================================
//  serial protocol: one JSON object per line, keys are documented in README.md
//  (-1 on a sensor value = "not available", shown as "--")
// ============================================================================
void parseLine(char* line) {
  while (line[0] == '\r' || line[0] == ' ') line++;
#if defined(ARDUINOJSON_VERSION_MAJOR) && ARDUINOJSON_VERSION_MAJOR >= 7
  JsonDocument doc;
#else
  DynamicJsonDocument doc(1024);
#endif
  DeserializationError err = deserializeJson(doc, line);
  if (err) return;                       // ignore malformed lines

  st.cpu     = doc["cpu"]  | 0.0f;
  st.cpuTemp = doc["ct"]   | -1.0f;      // -1 = sensor not available
  st.cpuVolt = doc["cv"]   | -1.0f;
  st.cpuFan  = doc["cf"]   | -1;
  st.gpu     = doc["gpu"]  | 0.0f;
  st.fps     = doc["fps"]  | 0;
  st.gpuTemp = doc["gt"]   | -1.0f;
  st.vram    = doc["vram"] | -1.0f;
  st.gpuFan  = doc["gf"]   | -1;
  st.gpuFanPct = doc["gfu"] | 0;
  st.mem     = doc["mem"]  | 0.0f;
  st.memFreq = doc["mf"]   | -1.0f;
  st.memVolt = doc["mv"]   | -1.0f;
  st.down    = doc["down"] | 0.0f;
  st.up      = doc["up"]   | 0.0f;
  st.hh      = doc["h"]    | 0;
  st.mm      = doc["m"]    | 0;
  st.ap      = doc["ap"]   | 0;

  pushHist(0, st.cpu);
  pushHist(1, st.gpu);
  pushHist(2, st.mem);
  pushHist(3, st.down + st.up);

  hasData = true;
  dirty   = true;
}

// ============================================================================
void setup() {
  Serial.begin(115200);

  tft.init();
  tft.setRotation(1);                     // landscape 320x240
  tft.fillScreen(C_BG);

  drawHeaderStatic();                     // strip + title (once)
  drawChrome();                           // icons, labels, rules (once)
  render();                               // values in "--" state
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      lineBuf[linePos] = 0;
      if (linePos > 2) parseLine(lineBuf);
      linePos = 0;
    } else if (c != '\r' && linePos < (int)sizeof(lineBuf) - 1) {
      lineBuf[linePos++] = c;
    }
  }

  if (dirty) {
    render();
    dirty = false;
  }
}
