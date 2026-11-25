//
// SPDX-FileCopyrightText: Copyright 2023 Arm Limited and/or its affiliates <open-source-office@arm.com>
// SPDX-License-Identifier: MIT
//

#ifndef _PICO_HM01B0_H_
#define _PICO_HM01B0_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "hardware/i2c.h"
#include "hardware/pio.h"

struct hm01b0_config {
    i2c_inst_t* i2c;
    uint sda_pin;
    uint scl_pin;

    uint vsync_pin;
    uint hsync_pin;
    uint pclk_pin;

    uint data_pin_base;
    uint data_bits;
    PIO pio;
    uint pio_sm;

    int reset_pin;
    int mclk_pin;

    uint width;
    uint height;

    /* Optional TCA9548A in front of the sensor; set mux_addr to 0 to disable. */
    uint8_t mux_addr;
    uint8_t mux_channel;

    /* 10-instruction capture program without border trimming. Needed when two
       cameras share one PIO block (18 + 18 > 32 instructions). */
    bool compact_program;
};

/*
 * One handle per camera. All the state that used to live in the library's
 * single global instance lives here instead, so several HM01B0 sensors on
 * different PIO blocks / mux channels can be used at the same time.
 */
struct hm01b0_handle {
    struct hm01b0_config config;
    uint pio_program_offset;
    uint pio_program_length;
    pio_sm_config pio_sm_config;
    uint num_pclk_per_px;
    bool ready;
    bool last_capture_ok;   /* false when the DMA wait timed out */
    uint32_t captures;
    uint32_t timeouts;
};

int hm01b0_init(struct hm01b0_handle* handle, const struct hm01b0_config* config);
void hm01b0_deinit(struct hm01b0_handle* handle);

void hm01b0_read_frame(struct hm01b0_handle* handle, uint8_t* buffer, size_t length);

void hm01b0_set_coarse_integration(struct hm01b0_handle* handle, unsigned int lines);

#endif
