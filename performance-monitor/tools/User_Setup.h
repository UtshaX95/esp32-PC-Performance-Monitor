// ===========================================================================
//  TFT_eSPI  User_Setup.h   -   PERFORMANCE MONITOR project
// ===========================================================================
//  HOW TO USE:
//    1. In Arduino IDE: Sketch -> Include Library -> Manage Libraries ->
//       install "TFT_eSPI" by Bodmer (latest version).
//    2. Close the IDE, then REPLACE THE ENTIRE CONTENTS of this file:
//         Documents\Arduino\libraries\TFT_eSPI\User_Setup.h
//       with the text below, save, and re-open the IDE.
//    3. Make sure User_Setup_Select.h (same folder) still contains the line
//         #include <User_Setup.h>
//       (it is active by default - do not touch it).
//
//  For an ST7789 240x320 display instead of ILI9341, change ONLY the
//  driver line below (see comment at that line).
// ===========================================================================

// ---- ESP32 Arduino core 3.x compatibility -------------------------------
// Fixes: "error: 'VSPI_HOST' was not declared in this scope; did you mean
//        'SPI3_HOST'?"  (Espressif removed the old VSPI/HSPI names in
//        core 3.x / ESP-IDF 5; map them to the new SPI host names.)
#if defined(ESP32) && !defined(VSPI_HOST)
  #define VSPI_HOST SPI3_HOST
#endif
#if defined(ESP32) && !defined(HSPI_HOST)
  #define HSPI_HOST SPI2_HOST
#endif
// -------------------------------------------------------------------------

#define ILI9341_DRIVER       // <-- for ST7789 240x320 use:  #define ST7789_2_DRIVER

#define TFT_WIDTH  240       // Native (portrait) width
#define TFT_HEIGHT 320       // Native (portrait) height

// ------------------- wiring to the ESP32 DevKit (VSPI) --------------------
#define TFT_MISO 19          // TFT SDO  (optional - not read, but wired)
#define TFT_MOSI 23          // TFT SDI / MOSI
#define TFT_SCLK 18          // TFT SCK / CLK
#define TFT_CS   22          // TFT CS
#define TFT_DC   21          // TFT DC  (sometimes labelled RS / A0)
#define TFT_RST  27          // TFT RESET

// ------------------- fonts used by the sketch -----------------------------
#define LOAD_GLCD            // Font 1.  8 pixel high
#define LOAD_FONT2           // Font 2. 16 pixel high
#define LOAD_FONT4           // Font 4. 26 pixel high (spare)
#define LOAD_GFXFF           // FreeFonts (built into the library)
#define SMOOTH_FONT

// ------------------- SPI speed -------------------------------------------
#define SPI_FREQUENCY       40000000   // 40 MHz - if the screen glitches use 27000000
#define SPI_READ_FREQUENCY   20000000
#define SPI_TOUCH_FREQUENCY   2500000
