/*
 * UVC test pattern plus an optional CAM3 live-capture path.
 *
 * The base build proves the 480x120 YUYV UVC descriptor, the composite CDC
 * control channel, and the BOOTSEL recovery command. The CAM3 command uses
 * mux channel 2 (GP12-GP15) and places the real Camera 3 frame in the
 * right-hand tile; the other tiles stay synthetic.
 *
 * MIT licensed. Based on the TinyUSB UVC device example and the
 * ArmDeveloperEcosystem/hm01b0-library-for-pico usb_camera example.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bsp/board.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "pico/hm01b0.h"
#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "hardware/watchdog.h"
#include "tusb.h"

#include "usb_descriptors.h"

#define FRAME_BYTES (FRAME_WIDTH * FRAME_HEIGHT * 2)
#define TILE_WIDTH (FRAME_WIDTH / 3)
#define LINES_LIMIT 200000u
#ifndef CAM_WIDTH
#define CAM_WIDTH 160
#endif
#ifndef CAM_HEIGHT
#define CAM_HEIGHT 120
#endif
#if FRAME_WIDTH != 3 * CAM_WIDTH
#error "FRAME_WIDTH must be three camera tiles wide"
#endif
#if FRAME_HEIGHT != CAM_HEIGHT
#error "FRAME_HEIGHT must equal the camera tile height"
#endif
#define MUX_ADDR 0x70
#define CAM3_MUX_CHANNEL 2

static uint8_t frame_buffer[FRAME_BYTES] __attribute__((aligned(4)));
static uint8_t cam1_buffer[CAM_WIDTH * CAM_HEIGHT] __attribute__((aligned(4)));
static uint8_t cam2_buffer[CAM_WIDTH * CAM_HEIGHT] __attribute__((aligned(4)));
static uint8_t cam3_buffer[CAM_WIDTH * CAM_HEIGHT] __attribute__((aligned(4)));
static struct hm01b0_handle cam1_handle;
static struct hm01b0_handle cam2_handle;
static struct hm01b0_handle cam3_handle;
static volatile bool tx_busy;
static bool cam1_ready;
static bool cam2_ready;
static bool cam3_ready;
static uint32_t interval_ms = 1000 / FRAME_RATE;
static uint32_t last_frame_ms;
static uint32_t frame_count;

/*
 * Tiles whose camera is not streaming are filled with video black (Y=16), so
 * Slicer shows a blank panel rather than a pattern that looks like an image.
 * ``PATTERN 1`` restores the diagnostic ramps/checkerboard.
 */
static bool pattern_enabled;

static bool tile_has_camera(uint8_t tile) {
  if (tile == 0) {
    return cam1_ready;
  }
  if (tile == 1) {
    return cam2_ready;
  }
  if (tile == 2) {
    return cam3_ready;
  }
  return false;
}

static void fill_test_pattern(void) {
  for (int y = 0; y < FRAME_HEIGHT; y++) {
    for (int x = 0; x < FRAME_WIDTH; x++) {
      uint8_t tile = (uint8_t)(x / TILE_WIDTH);
      uint8_t value = 16; /* video black */

      if (tile_has_camera(tile)) {
        value = 16;
      } else if (pattern_enabled) {
        if (tile == 0) {
          value = (uint8_t)(16 + (x * 219) / (TILE_WIDTH - 1));
        } else if (tile == 1) {
          value = (uint8_t)(16 + (y * 219) / (FRAME_HEIGHT - 1));
        } else {
          value = (((x / 8) + (y / 8)) & 1) ? 235 : 16;
        }
      }

      /* White border around each tile makes the split obvious. */
      if (pattern_enabled &&
          (x == 0 || x == TILE_WIDTH - 1 ||
           x == TILE_WIDTH || x == 2 * TILE_WIDTH - 1 ||
           x == 2 * TILE_WIDTH || x == FRAME_WIDTH - 1 ||
           y == 0 || y == FRAME_HEIGHT - 1)) {
        value = 255;
      }

      size_t index = ((size_t)y * FRAME_WIDTH + x) * 2;
      frame_buffer[index] = value;
      frame_buffer[index + 1] = 128; /* U/V = 128 gives a grey pixel. */
    }
  }
}

static bool mux_select(uint8_t channel) {
  gpio_init(0);
  gpio_init(1);
  gpio_set_function(0, GPIO_FUNC_I2C);
  gpio_set_function(1, GPIO_FUNC_I2C);
  gpio_pull_up(0);
  gpio_pull_up(1);
  i2c_init(i2c0, 10000);
  sleep_ms(2);
  uint8_t mask = (uint8_t)(1u << channel);
  return i2c_write_timeout_per_char_us(i2c0, MUX_ADDR, &mask, 1, false, 1000) == 1;
}

/*
 * Cameras 2 and 3 live behind the TCA9548A. Their signal pins are grouped in
 * blocks of four: ch1 -> GP8..GP11, ch2 -> GP12..GP15. Each camera gets its
 * own PIO block so both capture paths can coexist (18 instructions each,
 * 32 available per PIO).
 */
static void cam_build_config(uint8_t ch, struct hm01b0_config *cfg) {
  memset(cfg, 0, sizeof(*cfg));
  cfg->i2c = i2c0;
  cfg->sda_pin = 0;
  cfg->scl_pin = 1;
  cfg->data_pin_base = (uint)(4 * ch + 4);  /* ch1 -> 8,  ch2 -> 12 */
  cfg->pclk_pin = (uint)(4 * ch + 5);       /* ch1 -> 9,  ch2 -> 13 */
  cfg->hsync_pin = (uint)(4 * ch + 6);      /* ch1 -> 10, ch2 -> 14 */
  cfg->vsync_pin = (uint)(4 * ch + 7);      /* ch1 -> 11, ch2 -> 15 */
  cfg->data_bits = 1;
  if (ch == 2) {
    cfg->pio = pio1;
    cfg->pio_sm = 0;
  } else {
    /* Camera 1 uses a 10-instruction program so it fits beside Camera 2's
       18-instruction one in a 32-instruction PIO block. */
    cfg->pio = pio0;
    cfg->pio_sm = (ch == 0) ? 1 : 0;
    cfg->compact_program = (ch == 0);
  }
  cfg->reset_pin = -1;
  cfg->mclk_pin = -1;
  cfg->width = CAM_WIDTH;
  cfg->height = CAM_HEIGHT;
  cfg->mux_addr = MUX_ADDR;
  cfg->mux_channel = ch;
}

static bool cam_init(uint8_t ch) {
  /* Mux channel 1 is Camera 2, channel 2 is Camera 3. */
  uint8_t cam = (uint8_t)(ch + 1);

  if (ch > 2) {
    printf("CAM: only mux channels 0, 1 and 2 are supported\n");
    return false;
  }
  if ((ch == 0 && cam1_ready) || (ch == 1 && cam2_ready) || (ch == 2 && cam3_ready)) {
    printf("CAM%u: already active\n", cam);
    return true;
  }

  printf("CAM%u: selecting mux channel %u\n", cam, ch);
  if (!mux_select(ch)) {
    printf("CAM%u: mux select failed\n", cam);
    return false;
  }

  uint8_t probe;
  int prc = i2c_read_timeout_per_char_us(i2c0, 0x24, &probe, 1, false, 1000);
  printf("CAM%u: probe rc=%d\n", cam, prc);
  if (prc != 1) {
    printf("CAM%u: no camera on mux channel %u\n", cam, ch);
    return false;
  }

  struct hm01b0_config config;
  cam_build_config(ch, &config);

  struct hm01b0_handle *handle = (ch == 0) ? &cam1_handle
                                 : (ch == 1) ? &cam2_handle : &cam3_handle;
  if (hm01b0_init(handle, &config) != 0) {
    printf("CAM%u: camera init failed\n", cam);
    return false;
  }

  if (ch == 0) {
    cam1_ready = true;
  } else if (ch == 1) {
    cam2_ready = true;
  } else {
    cam3_ready = true;
  }
  printf("CAM%u: camera ready\n", cam);
  return true;
}

/*
 * ``border`` skips the sensor's overscan pixels. The compact camera-1 program
 * does not trim them, and they read as 0, which drags the image's minimum to 0
 * and makes any auto-windowed viewer stretch the whole tile.
 */
static void cam_tile(const uint8_t *buffer, uint8_t tile, uint8_t border) {
  for (int y = 0; y < CAM_HEIGHT; y++) {
    for (int x = 0; x < CAM_WIDTH; x++) {
      int sx = x + border;
      int sy = y + border;
      if (sx >= CAM_WIDTH) {
        sx = CAM_WIDTH - 1;
      }
      if (sy >= CAM_HEIGHT) {
        sy = CAM_HEIGHT - 1;
      }
      uint8_t value = buffer[sy * CAM_WIDTH + sx];
      size_t index = ((size_t)y * FRAME_WIDTH + (tile * TILE_WIDTH + x)) * 2;
      frame_buffer[index] = value;
      frame_buffer[index + 1] = 128;
    }
  }
}

static void video_task(void) {
  if (!tud_video_n_streaming(0, 0)) {
    tx_busy = false;
    last_frame_ms = board_millis();
    return;
  }

  if (tx_busy) {
    return;
  }

  uint32_t now = board_millis();
  if (now - last_frame_ms < interval_ms) {
    return;
  }

  last_frame_ms = now;
  tx_busy = true;
  /* Redraw the black/pattern background so missing cameras stay black and
     PATTERN takes effect without a reboot. */
  fill_test_pattern();
  if (cam1_ready) {
    hm01b0_read_frame(&cam1_handle, cam1_buffer, sizeof(cam1_buffer));
    cam_tile(cam1_buffer, 0, 2);
  }
  if (cam2_ready) {
    hm01b0_read_frame(&cam2_handle, cam2_buffer, sizeof(cam2_buffer));
    cam_tile(cam2_buffer, 1, 0);
  }
  if (cam3_ready) {
    hm01b0_read_frame(&cam3_handle, cam3_buffer, sizeof(cam3_buffer));
    cam_tile(cam3_buffer, 2, 0);
  }
  if (!tud_video_n_frame_xfer(0, 0, frame_buffer, sizeof(frame_buffer))) {
    tx_busy = false;
  }
}

static void lines_one(uint pin) {
  gpio_init(pin);
  gpio_set_dir(pin, GPIO_IN);

  gpio_pull_down(pin);
  sleep_us(200);
  bool low_ok = !gpio_get(pin);

  gpio_pull_up(pin);
  uint32_t charge = 0;
  while (!gpio_get(pin) && charge < LINES_LIMIT) {
    charge++;
  }
  bool high_ok = gpio_get(pin);

  gpio_pull_down(pin);
  uint32_t discharge = 0;
  while (gpio_get(pin) && discharge < LINES_LIMIT) {
    discharge++;
  }
  bool low_again = !gpio_get(pin);

  gpio_disable_pulls(pin);

  printf("GP%u charge=%lu discharge=%lu low=%d high=%d low2=%d\n",
         pin, (unsigned long)charge, (unsigned long)discharge,
         low_ok ? 1 : 0, high_ok ? 1 : 0, low_again ? 1 : 0);
}

static void lines_report(void) {
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("LINES: refused, a camera is active\n");
    return;
  }
  printf("LINES: relative charge/discharge counts; camera sensors must be in standby\n");
  printf("LINES: larger counts mean more capacitance; low/high=0 means externally held\n");
  for (uint pin = 0; pin <= 22; pin++) {
    lines_one(pin);
  }
  for (uint pin = 26; pin <= 28; pin++) {
    lines_one(pin);
  }
  printf("LINES: done\n");
}

static void i2c_report_one(i2c_inst_t *inst, uint sda, uint scl, const char *label) {
  gpio_init(sda);
  gpio_init(scl);
  gpio_set_function(sda, GPIO_FUNC_I2C);
  gpio_set_function(scl, GPIO_FUNC_I2C);
  gpio_pull_up(sda);
  gpio_pull_up(scl);
  i2c_init(inst, 100000);
  sleep_ms(5);

  bool found = false;
  uint8_t byte;
  int rc = i2c_read_timeout_per_char_us(inst, 0x24, &byte, 1, false, 1000);
  if (rc == 1) {
    printf("I2C %s: 0x24\n", label);
    found = true;
  }
  if (!found) {
    printf("I2C %s: no 0x24\n", label);
  }

  i2c_deinit(inst);
  gpio_init(sda);
  gpio_init(scl);
  gpio_set_dir(sda, GPIO_IN);
  gpio_set_dir(scl, GPIO_IN);
  gpio_disable_pulls(sda);
  gpio_disable_pulls(scl);
}

static void i2c_report(void) {
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("I2C: refused, a camera is active\n");
    return;
  }
  i2c_report_one(i2c0, 0, 1, "GP0/GP1");
  i2c_report_one(i2c0, 4, 5, "GP4/GP5");
  printf("I2C: done\n");
}

static void mux_report(void) {
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("MUX: refused, a camera is active\n");
    return;
  }
  i2c_inst_t *inst = i2c0;
  gpio_init(0);
  gpio_init(1);
  gpio_set_function(0, GPIO_FUNC_I2C);
  gpio_set_function(1, GPIO_FUNC_I2C);
  gpio_pull_up(0);
  gpio_pull_up(1);
  i2c_init(inst, 100000);
  sleep_ms(5);

  printf("MUX: scanning 0x70-0x77 on GP0/GP1\n");
  bool any = false;
  for (uint8_t addr = 0x70; addr <= 0x77; addr++) {
    uint8_t byte;
    int rc = i2c_read_timeout_per_char_us(inst, addr, &byte, 1, false, 1000);
    if (rc != 1) {
      continue;
    }
    any = true;
    printf("MUX: found 0x%02X\n", addr);
    for (uint8_t ch = 0; ch < 8; ch++) {
      uint8_t mask = (uint8_t)(1u << ch);
      int wr = i2c_write_timeout_per_char_us(inst, addr, &mask, 1, false, 1000);
      if (wr != 1) {
        printf("MUX 0x%02X ch%u: select failed\n", addr, ch);
        continue;
      }
      sleep_ms(2);
      int cr = i2c_read_timeout_per_char_us(inst, 0x24, &byte, 1, false, 1000);
      printf("MUX 0x%02X ch%u: %s\n", addr, ch,
             cr == 1 ? "camera 0x24" : "no camera");
    }
    uint8_t zero = 0;
    i2c_write_timeout_per_char_us(inst, addr, &zero, 1, false, 1000);
  }
  if (!any) {
    printf("MUX: no TCA9548A at 0x70-0x77\n");
  }
  printf("MUX: done\n");
}

static void mux01_report(void) {
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("MUX01: refused, a camera is active\n");
    return;
  }
  i2c_init(i2c0, 10000);
  gpio_init(0);
  gpio_init(1);
  gpio_set_function(0, GPIO_FUNC_I2C);
  gpio_set_function(1, GPIO_FUNC_I2C);
  gpio_pull_up(0);
  gpio_pull_up(1);
  sleep_ms(5);

  for (uint8_t ch = 0; ch <= 1; ch++) {
    uint8_t mask = (uint8_t)(1u << ch);
    int wr = i2c_write_timeout_per_char_us(i2c0, MUX_ADDR, &mask, 1, false, 1000);
    if (wr != 1) {
      printf("MUX01 ch%u: select failed\n", ch);
      continue;
    }
    sleep_ms(2);
    uint8_t byte;
    int cr = i2c_read_timeout_per_char_us(i2c0, 0x24, &byte, 1, false, 1000);
    printf("MUX01 ch%u: %s\n", ch, cr == 1 ? "camera 0x24" : "no camera");
  }

  uint8_t zero = 0;
  i2c_write_timeout_per_char_us(i2c0, MUX_ADDR, &zero, 1, false, 1000);
  printf("MUX01: done\n");
}

static void muxch_report(uint8_t ch) {
  if (ch > 7) {
    printf("MUXCH: channel must be 0-7\n");
    return;
  }
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("MUXCH: refused, a camera is active\n");
    return;
  }

  gpio_init(0);
  gpio_init(1);
  gpio_set_function(0, GPIO_FUNC_I2C);
  gpio_set_function(1, GPIO_FUNC_I2C);
  gpio_pull_up(0);
  gpio_pull_up(1);
  i2c_init(i2c0, 10000);
  sleep_ms(3);

  uint8_t mask = (uint8_t)(1u << ch);
  int wr = i2c_write_timeout_per_char_us(i2c0, MUX_ADDR, &mask, 1, false, 1000);
  printf("MUXCH%u select=%d", ch, wr);
  if (wr == 1) {
    sleep_ms(3);
    uint8_t byte = 0;
    int ack = i2c_read_timeout_per_char_us(i2c0, 0x24, &byte, 1, false, 1000);
    printf(" ack=%d", ack);
    if (ack == 1) {
      /* Read the HM01B0 model-id register (0x0000) so we know it is a real sensor. */
      uint8_t addr[2] = {0x00, 0x00};
      int wa = i2c_write_timeout_per_char_us(i2c0, 0x24, addr, 2, false, 1000);
      uint8_t id[2] = {0xEE, 0xEE};
      int rd = i2c_read_timeout_per_char_us(i2c0, 0x24, id, 2, false, 1000);
      printf(" reg=%d id=%d:%02X%02X", wa, rd, id[0], id[1]);
    }
    uint8_t zero = 0;
    i2c_write_timeout_per_char_us(i2c0, MUX_ADDR, &zero, 1, false, 1000);
  }
  printf(" done\n");
}

static void cam_exposure(uint8_t ch, unsigned int lines) {
  if (ch == 1 && cam2_ready) {
    hm01b0_set_coarse_integration(&cam2_handle, lines);
  } else if (ch == 2 && cam3_ready) {
    hm01b0_set_coarse_integration(&cam3_handle, lines);
  } else {
    printf("CAMEXP: camera %u not initialized\n", ch + 1);
    return;
  }
  printf("CAMEXP%u: integration=%u\n", ch + 1, lines);
}

/*
 * Put one sensor into streaming mode and count transitions on its video pins
 * for 500 ms. This separates "sensor does not stream" from "signals never
 * arrive at the Pico".
 */
static void pulse_report(uint8_t ch) {
  if (ch < 1 || ch > 2) {
    printf("PULSE: only mux channels 1 and 2 are supported\n");
    return;
  }
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("PULSE: refused, a camera is active\n");
    return;
  }

  struct hm01b0_config cfg;
  cam_build_config(ch, &cfg);

  mux_select(ch);
  gpio_init(cfg.vsync_pin);
  gpio_init(cfg.hsync_pin);
  gpio_init(cfg.pclk_pin);
  gpio_init(cfg.data_pin_base);
  gpio_set_dir(cfg.vsync_pin, GPIO_IN);
  gpio_set_dir(cfg.hsync_pin, GPIO_IN);
  gpio_set_dir(cfg.pclk_pin, GPIO_IN);
  gpio_set_dir(cfg.data_pin_base, GPIO_IN);

  uint8_t on[3] = {0x01, 0x00, 0x01};
  int w = i2c_write_timeout_per_char_us(i2c0, 0x24, on, 3, false, 1000);
  printf("PULSE%u: mode_select=%d ", (unsigned)(ch + 1), w);

  uint32_t vs = 0, hs = 0, ps = 0, ds = 0, dhigh = 0, dsamples = 0;
  bool lv = gpio_get(cfg.vsync_pin);
  bool lh = gpio_get(cfg.hsync_pin);
  bool lp = gpio_get(cfg.pclk_pin);
  bool ld = gpio_get(cfg.data_pin_base);

  absolute_time_t end = make_timeout_time_ms(500);
  while (!time_reached(end)) {
    bool v = gpio_get(cfg.vsync_pin);
    bool h = gpio_get(cfg.hsync_pin);
    bool p = gpio_get(cfg.pclk_pin);
    bool d = gpio_get(cfg.data_pin_base);
    if (v != lv) { vs++; lv = v; }
    if (h != lh) { hs++; lh = h; }
    if (p != lp) { ps++; lp = p; }
    if (d != ld) { ds++; ld = d; }
    if (d) { dhigh++; }
    dsamples++;
  }

  /* Weak pull-up test: a floating D0 rises, a line held low stays low. */
  gpio_pull_up(cfg.data_pin_base);
  sleep_ms(2);
  bool pulled = gpio_get(cfg.data_pin_base);
  gpio_disable_pulls(cfg.data_pin_base);

  uint8_t off[3] = {0x01, 0x00, 0x00};
  i2c_write_timeout_per_char_us(i2c0, 0x24, off, 3, false, 1000);

  printf("vsync=%lu hsync=%lu pclk=%lu d0=%lu d0_high=%lu/%lu float=%d\n",
         (unsigned long)vs, (unsigned long)hs, (unsigned long)ps,
         (unsigned long)ds, (unsigned long)dhigh, (unsigned long)dsamples,
         pulled ? 1 : 0);
}

/*
 * Stream one sensor and count transitions on every spare GPIO. If a signal
 * wire is plugged into the wrong pin this finds it; if nothing toggles the
 * wire is wrong at the camera end.
 */
static void scan_report(uint8_t ch) {
  if (ch < 1 || ch > 2) {
    printf("SCAN: only mux channels 1 and 2 are supported\n");
    return;
  }
  if (cam1_ready || cam2_ready || cam3_ready) {
    printf("SCAN: refused, a camera is active\n");
    return;
  }

  static const uint pins[] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
                              15, 16, 17, 18, 19, 20, 21, 22, 26, 27, 28};
  enum { NPINS = sizeof(pins) / sizeof(pins[0]) };
  bool last[NPINS];
  uint32_t count[NPINS];

  for (int i = 0; i < NPINS; i++) {
    gpio_init(pins[i]);
    gpio_set_dir(pins[i], GPIO_IN);
    gpio_disable_pulls(pins[i]);
    last[i] = gpio_get(pins[i]);
    count[i] = 0;
  }

  mux_select(ch);
  uint8_t on[3] = {0x01, 0x00, 0x01};
  i2c_write_timeout_per_char_us(i2c0, 0x24, on, 3, false, 1000);

  absolute_time_t end = make_timeout_time_ms(300);
  while (!time_reached(end)) {
    for (int i = 0; i < NPINS; i++) {
      bool v = gpio_get(pins[i]);
      if (v != last[i]) {
        count[i]++;
        last[i] = v;
      }
    }
  }

  uint8_t off[3] = {0x01, 0x00, 0x00};
  i2c_write_timeout_per_char_us(i2c0, 0x24, off, 3, false, 1000);

  printf("SCAN%u:", ch + 1);
  bool any = false;
  for (int i = 0; i < NPINS; i++) {
    if (count[i] > 0) {
      printf(" GP%u=%lu", pins[i], (unsigned long)count[i]);
      any = true;
    }
  }
  if (!any) {
    printf(" no activity on any spare pin");
  }
  printf("\n");
}

static void cam_reg_read(uint8_t cam, uint16_t addr) {
  uint8_t ch = (uint8_t)(cam - 1);
  mux_select(ch);
  uint8_t a[2] = {(uint8_t)(addr >> 8), (uint8_t)(addr & 0xff)};
  int w = i2c_write_timeout_per_char_us(i2c0, 0x24, a, 2, false, 1000);
  uint8_t v = 0;
  int r = i2c_read_timeout_per_char_us(i2c0, 0x24, &v, 1, false, 1000);
  printf("CAMREGR cam%u 0x%04X=0x%02X w=%d r=%d\n", cam, addr, v, w, r);
}

static void cam_reg_write(uint8_t cam, uint16_t addr, uint8_t val) {
  uint8_t ch = (uint8_t)(cam - 1);
  mux_select(ch);
  uint8_t d[3] = {(uint8_t)(addr >> 8), (uint8_t)(addr & 0xff), val};
  int w = i2c_write_timeout_per_char_us(i2c0, 0x24, d, 3, false, 1000);
  printf("CAMREG cam%u 0x%04X<-0x%02X w=%d\n", cam, addr, val, w);
}

/*
 * Stop both cameras and hand their pins back to plain GPIO so LINES / PULSE /
 * SCAN can run. CAM2 / CAM3 start them again (the PIO programs are freed).
 */
static void cam_stop_all(void) {
  if (cam1_ready) {
    hm01b0_deinit(&cam1_handle);
    cam1_ready = false;
  }
  if (cam2_ready) {
    hm01b0_deinit(&cam2_handle);
    cam2_ready = false;
  }
  if (cam3_ready) {
    hm01b0_deinit(&cam3_handle);
    cam3_ready = false;
  }
  for (uint pin = 4; pin <= 15; pin++) {
    gpio_init(pin);
    gpio_set_dir(pin, GPIO_IN);
    gpio_disable_pulls(pin);
  }
  printf("STOPCAMS: cameras stopped; LINES/PULSE/SCAN available, CAM2/CAM3 restart\n");
}

/*
 * Lengthen the sensor's frame so it can integrate for longer than the 128-line
 * default. Integration is clamped to frame_length - 2, so this is the only way
 * to get more light out of a dim scene once gain is exhausted.
 */
static void cam_frame_lines(uint8_t cam, unsigned int lines) {
  uint8_t ch = (uint8_t)(cam - 1);
  if (lines < 128) {
    lines = 128;
  } else if (lines > 4095) {
    lines = 4095;
  }
  mux_select(ch);
  uint8_t frame[4] = {0x03, 0x40, (uint8_t)(lines >> 8), (uint8_t)(lines & 0xff)};
  int w1 = i2c_write_timeout_per_char_us(i2c0, 0x24, frame, 4, false, 1000);
  unsigned integration = lines - 2;
  uint8_t integ[4] = {0x02, 0x02, (uint8_t)(integration >> 8),
                      (uint8_t)(integration & 0xff)};
  int w2 = i2c_write_timeout_per_char_us(i2c0, 0x24, integ, 4, false, 1000);
  uint8_t hold[3] = {0x01, 0x04, 0x01};
  int w3 = i2c_write_timeout_per_char_us(i2c0, 0x24, hold, 3, false, 1000);
  printf("CAMFRAME%u: lines=%u rc=%d/%d/%d\n", cam, lines, w1, w2, w3);
}

static void cdc_task(void) {
  static char command[32];
  static size_t command_len;

  int c;
  while ((c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
    if (c == '\r' || c == '\n') {
      command[command_len] = '\0';
      if (strcmp(command, "BOOTSEL") == 0) {
        printf("rebooting to BOOTSEL\n");
        stdio_flush();
        reset_usb_boot(0, 0);
      } else if (strcmp(command, "STATUS") == 0) {
        printf("HimxCam test firmware: frames=%lu streaming=%d interval_ms=%lu cam1=%d cam2=%d cam3=%d\n",
               (unsigned long)frame_count,
               tud_video_n_streaming(0, 0) ? 1 : 0,
               (unsigned long)interval_ms,
               cam1_ready ? 1 : 0,
               cam2_ready ? 1 : 0,
               cam3_ready ? 1 : 0);
        if (cam1_ready) {
          printf("cam1: captures=%lu timeouts=%lu\n",
                 (unsigned long)cam1_handle.captures,
                 (unsigned long)cam1_handle.timeouts);
        }
        if (cam2_ready) {
          printf("cam2: captures=%lu timeouts=%lu\n",
                 (unsigned long)cam2_handle.captures,
                 (unsigned long)cam2_handle.timeouts);
        }
        if (cam3_ready) {
          printf("cam3: captures=%lu timeouts=%lu\n",
                 (unsigned long)cam3_handle.captures,
                 (unsigned long)cam3_handle.timeouts);
        }
      } else if (strcmp(command, "STOPCAMS") == 0) {
        cam_stop_all();
      } else if (strcmp(command, "PING") == 0) {
        printf("PONG\n");
      } else if (strcmp(command, "CAM1") == 0) {
        cam_init(0);
      } else if (strcmp(command, "CAM2") == 0) {
        cam_init(1);
      } else if (strcmp(command, "CAM3") == 0) {
        cam_init(2);
      } else if (strcmp(command, "LINES") == 0) {
        lines_report();
      } else if (strcmp(command, "I2C") == 0) {
        i2c_report();
      } else if (strcmp(command, "MUX") == 0) {
        mux_report();
      } else if (strcmp(command, "MUX01") == 0) {
        mux01_report();
      } else if (strncmp(command, "MUXCH", 5) == 0 && command[5] >= '0' && command[5] <= '7') {
        muxch_report((uint8_t)(command[5] - '0'));
      } else if (strncmp(command, "PATTERN", 7) == 0) {
        pattern_enabled = (strchr(command, '1') != NULL);
        printf("PATTERN: %s\n", pattern_enabled ? "on" : "off");
      } else if (strncmp(command, "CAMFRAME ", 9) == 0) {
        unsigned cam = 0, lines = 0;
        if (sscanf(command + 9, "%u %u", &cam, &lines) == 2 && cam >= 1 && cam <= 3) {
          cam_frame_lines((uint8_t)cam, lines);
        } else {
          printf("usage: CAMFRAME <1|2|3> <lines>\n");
        }
      } else if (strncmp(command, "CAMEXP", 6) == 0 && command[6] >= '1' && command[6] <= '3') {
        char *space = strchr(command, ' ');
        if (space) {
          cam_exposure((uint8_t)(command[6] - '0' - 1), (unsigned int)atoi(space + 1));
        }
      } else if (strncmp(command, "CAMREGR ", 8) == 0) {
        unsigned cam = 0, addr = 0;
        if (sscanf(command + 8, "%u %i", &cam, &addr) == 2 && cam >= 1 && cam <= 3) {
          cam_reg_read((uint8_t)cam, (uint16_t)addr);
        } else {
          printf("usage: CAMREGR <2|3> <addr>\n");
        }
      } else if (strncmp(command, "CAMREG ", 7) == 0) {
        unsigned cam = 0, addr = 0, val = 0;
        if (sscanf(command + 7, "%u %i %i", &cam, &addr, &val) == 3 && cam >= 1 && cam <= 3) {
          cam_reg_write((uint8_t)cam, (uint16_t)addr, (uint8_t)val);
        } else {
          printf("usage: CAMREG <2|3> <addr> <val>\n");
        }
      } else if (strncmp(command, "SCAN", 4) == 0 && command[4] >= '2' && command[4] <= '3') {
        scan_report((uint8_t)(command[4] - '0' - 1));
      } else if (strncmp(command, "PULSE", 5) == 0 && command[5] >= '2' && command[5] <= '3') {
        pulse_report((uint8_t)(command[5] - '0' - 1));
      } else if (command_len > 0) {
        printf("unknown command\n");
      }
      command_len = 0;
    } else if (command_len < sizeof(command) - 1) {
      command[command_len++] = (char)c;
    }
  }
}

void tud_mount_cb(void) {}
void tud_umount_cb(void) {}
void tud_suspend_cb(bool remote_wakeup_en) { (void)remote_wakeup_en; }
void tud_resume_cb(void) {}

void tud_video_frame_xfer_complete_cb(uint_fast8_t ctl_idx,
                                      uint_fast8_t stm_idx) {
  (void)ctl_idx;
  (void)stm_idx;
  tx_busy = false;
  frame_count++;
}

int tud_video_commit_cb(uint_fast8_t ctl_idx, uint_fast8_t stm_idx,
                        video_probe_and_commit_control_t const *parameters) {
  (void)ctl_idx;
  (void)stm_idx;
  uint32_t requested = parameters->dwFrameInterval / 10000;
  /* Never advertise faster than the descriptor's rate. */
  uint32_t minimum = 1000 / FRAME_RATE;
  interval_ms = requested >= minimum ? requested : minimum;
  return VIDEO_ERROR_NONE;
}

int main(void) {
  board_init();

  /* If a diagnostic or I2C operation wedges the main loop, reboot after 5 s. */
  watchdog_enable(5000, 1);

  /* stdio_usb_init() asserts that TinyUSB has already been initialised. */
  tud_init(BOARD_TUD_RHPORT);
  stdio_init_all();

  fill_test_pattern();

  /* Bring cameras 2 and 3 up once, in the loop, so stdout has a live CDC to
     drain into. A camera that is absent or unwired just leaves its tile black;
     the CDC commands still allow a manual retry. */
  bool boot_cameras = true;

  while (true) {
    watchdog_update();
    tud_task();
    if (boot_cameras) {
      boot_cameras = false;
      cam_init(0);
      cam_init(1);
      cam_init(2);
    }
    video_task();
    cdc_task();
  }
}
