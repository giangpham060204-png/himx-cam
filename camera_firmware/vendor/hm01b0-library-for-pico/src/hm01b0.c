//
// SPDX-FileCopyrightText: Copyright 2023 Arm Limited and/or its affiliates <open-source-office@arm.com>
// SPDX-License-Identifier: MIT
//

#include <string.h>
#include <stdint.h>
#include <stdio.h>

#include "cmsis_gcc.h"

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/pwm.h"

#include "pico/hm01b0.h"

#define HM01B0_I2C_ADDRESS 0x24
#define HM01B0_I2C_BAUD_HZ (10 * 1000)

static int hm01b0_reset(struct hm01b0_handle* handle);

static uint8_t hm01b0_read_reg8(struct hm01b0_handle* handle, uint16_t address);
static uint16_t hm01b0_read_reg16(struct hm01b0_handle* handle, uint16_t address);
static void hm01b0_write_reg8(struct hm01b0_handle* handle, uint16_t address, uint8_t value);
static void hm01b0_write_reg16(struct hm01b0_handle* handle, uint16_t address, uint16_t value);

/*
 * Every transaction re-selects the camera's mux channel. The mux has a single
 * common bus and the selection persists across Pico resets, so this is the
 * only way to be sure a register access reaches the intended sensor.
 */
static void hm01b0_select_mux(struct hm01b0_handle* handle)
{
    if (handle->config.mux_addr == 0) {
        return;
    }

    uint8_t mask = (uint8_t)(1u << handle->config.mux_channel);
    i2c_write_blocking(handle->config.i2c, handle->config.mux_addr, &mask, 1, false);
}

int hm01b0_init(struct hm01b0_handle* handle, const struct hm01b0_config* config)
{
    memset(handle, 0, sizeof(*handle));
    memcpy(&handle->config, config, sizeof(handle->config));


    uint8_t readout_x_val;           // 0x0383
    uint8_t readout_y_val;           // 0x0387
    uint8_t binning_mode_val;        // 0x0390
    uint8_t qvga_win_en_val;         // 0x3010
    uint16_t frame_length_lines_val; // 0x0340
    uint16_t line_length_pclk_val;   // 0x0342
    uint8_t bit_control_val;         // 0x3059

    uint8_t num_border_px;

    if (config->width == 320 && config->height == 320) {
        readout_x_val          = 0x01;
        readout_y_val          = 0x01;
        binning_mode_val       = 0x00;
        qvga_win_en_val        = 0x00;
        frame_length_lines_val = 0x0158;
        line_length_pclk_val   = 0x0178;

        num_border_px = 2;
    } else if (config->width == 320 && config->height == 240) {
        readout_x_val          = 0x01;
        readout_y_val          = 0x01;
        binning_mode_val       = 0x00;
        qvga_win_en_val        = 0x01;
        frame_length_lines_val = 0x0104;
        line_length_pclk_val   = 0x0178;

        num_border_px = 2;
    } else if (config->width == 160 && config->height == 120) {
        readout_x_val          = 0x03;
        readout_y_val          = 0x03;
        binning_mode_val       = 0x03;
        qvga_win_en_val        = 0x01;
        frame_length_lines_val = 0x0080;
        line_length_pclk_val   = 0x00D7;

        num_border_px = 2;
    } else {
        printf("Invalid resolution!\n");
        return -1;
    }

    if (config->data_bits == 8) {
        bit_control_val = 0x02;
        handle->num_pclk_per_px = 1;
    } else if (config->data_bits == 4) {
        bit_control_val = 0x42;
        handle->num_pclk_per_px = 2;
    } else if (config->data_bits == 1) {
        bit_control_val = 0x22;
        handle->num_pclk_per_px = 8;
    } else {
        printf("Invalid data bits!\n");
        return -1;
    }

    if (config->reset_pin > -1) {
        gpio_init(config->reset_pin);
        gpio_set_dir(config->reset_pin, GPIO_OUT);
        gpio_put(config->reset_pin, 0);
        sleep_ms(100);
        gpio_put(config->reset_pin, 1);
    }

    if (config->mclk_pin > -1) {
        gpio_set_function(config->mclk_pin, GPIO_FUNC_PWM);
        uint mclk_slice_num = pwm_gpio_to_slice_num(config->mclk_pin);
        uint mclk_channel = pwm_gpio_to_channel(config->mclk_pin);

        // PWM @ ~25 MHz, 50% duty cycle
        pwm_set_clkdiv(mclk_slice_num, 1.25);
        pwm_set_wrap(mclk_slice_num, 3);
        pwm_set_chan_level(mclk_slice_num, mclk_channel, 2);
        pwm_set_enabled(mclk_slice_num, true);
    }

    gpio_set_function(config->sda_pin, GPIO_FUNC_I2C);
    gpio_set_function(config->scl_pin, GPIO_FUNC_I2C);
    gpio_pull_up(config->sda_pin);
    gpio_pull_up(config->scl_pin);

    i2c_init(config->i2c, HM01B0_I2C_BAUD_HZ);

    uint16_t model_id = hm01b0_read_reg16(handle, 0x0000);
    if (model_id != 0x01b0) {
        printf("Invalid model id!\n");
        return -1;
    }

#ifdef HM01B0_SKIP_SOFTWARE_RESET
    /* Camera was power-cycled before this init; skip the software reset. */
#else
    if (hm01b0_reset(handle) != 0) {
        printf("Reset failed!\n");
        return -1;
    }
#endif

    hm01b0_write_reg8(handle, 0x3059, bit_control_val);

    hm01b0_write_reg8(handle, 0x0383, readout_x_val);
    hm01b0_write_reg8(handle, 0x0387, readout_y_val);
    hm01b0_write_reg8(handle, 0x0390, binning_mode_val);
    hm01b0_write_reg8(handle, 0x3010, qvga_win_en_val);
    hm01b0_write_reg16(handle, 0x0340, frame_length_lines_val);
    hm01b0_write_reg16(handle, 0x0342, line_length_pclk_val);


    hm01b0_write_reg8(handle, 0x3060, 0x08 | 0); // OSC_CLK_DIV

    hm01b0_write_reg16(handle, 0x0202, line_length_pclk_val / 2); // INTEGRATION_H

    /*
     * Auto-exposure only converges on consecutive frames, and this capture path
     * delivers one isolated frame per request, so AE hunts between captures
     * (means swinging 24 -> 40 -> 28). Freeze it: manual integration, 1x gain.
     */
    hm01b0_write_reg8(handle, 0x2100, 0x00); // AE off
    hm01b0_write_reg8(handle, 0x0205, 0x00); // analog gain 1x

    /* Fixed exposure: the AE loop needs continuous frames, which the capture
       path cannot provide, so drive gain by hand. 0x40 = 4x. */


    hm01b0_write_reg8(handle, 0x0104, 0x01); // GRP_PARAM_HOLD

    /*
     * Build the capture program. jmp targets are recorded, not encoded: a
     * second program on the same PIO does not start at address 0, and PIO jmp
     * operands are absolute, so they are rebased onto the offset that
     * pio_add_program() hands back.
     */
    uint16_t insns[20];
    uint len = 0;
    uint jmp_index[4];
    uint jmp_target[4];
    uint jmp_is_y[4];
    uint njmp = 0;
    uint wrap_target;

    insns[len++] = pio_encode_pull(false, true);
    insns[len++] = pio_encode_wait_gpio(false, config->vsync_pin);
    insns[len++] = pio_encode_wait_gpio(true, config->vsync_pin);

    if (config->compact_program) {
        /* 10 instructions, no border trimming, so two cameras fit one PIO. */
        insns[len++] = pio_encode_mov(pio_x, pio_osr);              /* 3 */
        insns[len++] = pio_encode_wait_gpio(true, config->hsync_pin);   /* 4 */
        insns[len++] = pio_encode_wait_gpio(true, config->pclk_pin);    /* 5 */
        insns[len++] = pio_encode_in(pio_pins, config->data_bits);      /* 6 */
        insns[len++] = pio_encode_wait_gpio(false, config->pclk_pin);   /* 7 */
        jmp_index[njmp] = len;
        jmp_target[njmp] = 5;
        jmp_is_y[njmp] = 0;
        njmp++;
        insns[len++] = pio_encode_nop();                                /* 8 */
        insns[len++] = pio_encode_wait_gpio(false, config->hsync_pin);  /* 9 */
        wrap_target = 3;
    } else {
        insns[len++] = pio_encode_set(pio_y, num_border_px - 1);        /* 3 */
        insns[len++] = pio_encode_wait_gpio(true, config->hsync_pin);   /* 4 */
        insns[len++] = pio_encode_wait_gpio(false, config->hsync_pin);  /* 5 */
        jmp_index[njmp] = len;
        jmp_target[njmp] = 4;
        jmp_is_y[njmp] = 1;
        njmp++;
        insns[len++] = pio_encode_nop();                                /* 6 */
        /* .wrap_target */
        insns[len++] = pio_encode_mov(pio_x, pio_osr);                  /* 7 */
        insns[len++] = pio_encode_wait_gpio(true, config->hsync_pin);   /* 8 */
        insns[len++] = pio_encode_set(pio_y, num_border_px * handle->num_pclk_per_px - 1); /* 9 */
        insns[len++] = pio_encode_wait_gpio(true, config->pclk_pin);    /* 10 */
        insns[len++] = pio_encode_wait_gpio(false, config->pclk_pin);   /* 11 */
        jmp_index[njmp] = len;
        jmp_target[njmp] = 10;
        jmp_is_y[njmp] = 1;
        njmp++;
        insns[len++] = pio_encode_nop();                                /* 12 */
        insns[len++] = pio_encode_wait_gpio(true, config->pclk_pin);    /* 13 */
        insns[len++] = pio_encode_in(pio_pins, config->data_bits);      /* 14 */
        insns[len++] = pio_encode_wait_gpio(false, config->pclk_pin);   /* 15 */
        jmp_index[njmp] = len;
        jmp_target[njmp] = 13;
        jmp_is_y[njmp] = 0;
        njmp++;
        insns[len++] = pio_encode_nop();                                /* 16 */
        insns[len++] = pio_encode_wait_gpio(false, config->hsync_pin);  /* 17 */
        wrap_target = 7;
    }

    pio_program_t pio_program = {
        .instructions = insns,
        .length = (uint8_t)len,
        .origin = -1,
    };

    int offset = pio_add_program(config->pio, &pio_program);
    if (offset < 0) {
        printf("No PIO program space!\n");
        return -1;
    }

    /* Rebase the jmp targets now that the program's address is known. */
    for (uint i = 0; i < njmp; i++) {
        uint target = (uint)offset + jmp_target[i];
        config->pio->instr_mem[offset + jmp_index[i]] =
            jmp_is_y[i] ? pio_encode_jmp_y_dec((uint16_t)target)
                        : pio_encode_jmp_x_dec((uint16_t)target);
    }

    handle->pio_program_offset = (uint)offset;
    handle->pio_program_length = pio_program.length;
    handle->pio_sm_config = pio_get_default_sm_config();

    sm_config_set_in_pins(&handle->pio_sm_config, config->data_pin_base);
    sm_config_set_in_shift(&handle->pio_sm_config, true, true, 8);
    sm_config_set_wrap(
        &handle->pio_sm_config,
        handle->pio_program_offset + wrap_target,
        handle->pio_program_offset + pio_program.length - 1
    );

    pio_gpio_init(config->pio, config->vsync_pin);
    pio_gpio_init(config->pio, config->hsync_pin);
    pio_gpio_init(config->pio, config->pclk_pin);

    handle->ready = true;
    return 0;
}

void hm01b0_deinit(struct hm01b0_handle* handle)
{
    struct hm01b0_config* config = &handle->config;

    if (handle->pio_program_length > 0) {
        /* Free the instruction slots so another camera can be loaded later. */
        pio_program_t program = {0};
        program.length = (uint8_t)handle->pio_program_length;
        pio_remove_program(config->pio, &program, handle->pio_program_offset);
        handle->pio_program_length = 0;
        handle->pio_program_offset = 0;
    }

    i2c_deinit(config->i2c);

    gpio_set_function(config->sda_pin, GPIO_FUNC_NULL);
    gpio_set_function(config->scl_pin, GPIO_FUNC_NULL);

    if (config->mclk_pin > -1) {
        gpio_set_function(config->mclk_pin, GPIO_FUNC_NULL);
    }

    if (config->reset_pin > -1) {
        gpio_set_function(config->reset_pin, GPIO_FUNC_NULL);
    }

    handle->ready = false;
}

void hm01b0_read_frame(struct hm01b0_handle* handle, uint8_t* buffer, size_t length)
{
    struct hm01b0_config* config = &handle->config;

    hm01b0_select_mux(handle);

    pio_sm_init(config->pio, config->pio_sm, handle->pio_program_offset, &handle->pio_sm_config);

    int dma_channel = dma_claim_unused_channel(true);

    dma_channel_config dcc = dma_channel_get_default_config(dma_channel);
    channel_config_set_read_increment(&dcc, false);
    channel_config_set_write_increment(&dcc, true);
    channel_config_set_dreq(&dcc, pio_get_dreq(config->pio, config->pio_sm, false));
    channel_config_set_transfer_data_size(&dcc, DMA_SIZE_8);

    dma_channel_configure(
        dma_channel,
        &dcc,
        buffer,
        ((uint8_t*)&config->pio->rxf[config->pio_sm]) + 3,
        length,
        false
    );

    dma_channel_start(dma_channel);
    pio_sm_set_enabled(config->pio, config->pio_sm, true);
    pio_sm_put_blocking(config->pio, config->pio_sm, config->width * handle->num_pclk_per_px - 1);
    hm01b0_write_reg8(handle, 0x0100, 0x01); // MODE_SELECT

    /*
     * Bounded wait: a camera that is not wired correctly must not wedge the
     * main loop (and with it the watchdog refresh).
     */
    uint32_t start = time_us_32();
    bool timed_out = false;
    while (dma_channel_is_busy(dma_channel)) {
        if (time_us_32() - start > 500000u) {
            timed_out = true;
            break;
        }
        tight_loop_contents();
    }
    handle->last_capture_ok = !timed_out;
    handle->captures++;
    if (timed_out) {
        handle->timeouts++;
    }

    pio_sm_set_enabled(config->pio, config->pio_sm, false);
    dma_channel_abort(dma_channel);
    dma_channel_unclaim(dma_channel);
    hm01b0_write_reg8(handle, 0x0100, 0x00); // MODE_SELECT
}

void hm01b0_set_coarse_integration(struct hm01b0_handle* handle, unsigned int lines)
{
    if (lines < 2) {
        lines = 2;
    } else if (lines > 0xffff) {
        lines = 0xffff;
    }

    lines -= 2;

    hm01b0_write_reg16(handle, 0x0202, lines); // INTEGRATION_H

    hm01b0_write_reg8(handle, 0x0104, 0x01); // GRP_PARAM_HOLD
}

static int hm01b0_reset(struct hm01b0_handle* handle)
{
    hm01b0_write_reg8(handle, 0x0103, 0x01);

    for (int retries = 0; retries < 10; retries++) {
        if (hm01b0_read_reg8(handle, 0x0100) == 0x00) {
            return 0;
        }

        sleep_ms(100);
    }

    return -1;
}

static uint8_t hm01b0_read_reg8(struct hm01b0_handle* handle, uint16_t address)
{
    address = __REV16(address);

    uint8_t result = 0xff;

    hm01b0_select_mux(handle);
    i2c_write_blocking(handle->config.i2c, HM01B0_I2C_ADDRESS, (const uint8_t*)&address, sizeof(address), false);
    i2c_read_timeout_per_char_us(handle->config.i2c, HM01B0_I2C_ADDRESS, (uint8_t*)&result, sizeof(result), false, 1000);

    return result;
}

static uint16_t hm01b0_read_reg16(struct hm01b0_handle* handle, uint16_t address)
{
    address = __REV16(address);

    uint16_t result = 0xffff;

    hm01b0_select_mux(handle);
    i2c_write_blocking(handle->config.i2c, HM01B0_I2C_ADDRESS, (const uint8_t*)&address, sizeof(address), false);
    i2c_read_timeout_per_char_us(handle->config.i2c, HM01B0_I2C_ADDRESS, (uint8_t*)&result, sizeof(result), false, 1000);

    return __REV16(result);
}


static void hm01b0_write_reg8(struct hm01b0_handle* handle, uint16_t address, uint8_t value)
{
    uint8_t data[3];

    *((uint16_t*)data) = __REV16(address);
    data[2] = value;

    hm01b0_select_mux(handle);
    i2c_write_blocking(handle->config.i2c, HM01B0_I2C_ADDRESS, data, sizeof(data), false);
}

static void hm01b0_write_reg16(struct hm01b0_handle* handle, uint16_t address, uint16_t value)
{
    uint8_t data[4];

    *((uint16_t*)data + 0) = __REV16(address);
    *((uint16_t*)data + 1) = __REV16(value);

    hm01b0_select_mux(handle);
    i2c_write_blocking(handle->config.i2c, HM01B0_I2C_ADDRESS, data, sizeof(data), false);
}
