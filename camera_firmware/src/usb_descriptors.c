/*
 * The MIT License (MIT)
 *
 * Based on TinyUSB's UVC device example and the
 * ArmDeveloperEcosystem/hm01b0-library-for-pico usb_camera example.
 */

#include <string.h>

#include "tusb.h"
#include "usb_descriptors.h"

#define _PID_MAP(itf, n) ((CFG_TUD_##itf) << (n))
#define USB_PID (0x4000 | _PID_MAP(CDC, 0) | _PID_MAP(VIDEO, 5))

tusb_desc_device_t const desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0xCafe,
    .idProduct = USB_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = 0x01,
    .iProduct = 0x02,
    .iSerialNumber = 0x03,
    .bNumConfigurations = 0x01,
};

uint8_t const *tud_descriptor_device_cb(void) {
  return (uint8_t const *)&desc_device;
}

#if LIB_PICO_STDIO_USB == 1
#define CONFIG_TOTAL_LEN \
  (TUD_CONFIG_DESC_LEN + TUD_VIDEO_CAPTURE_DESC_UNCOMPR_BULK_LEN + TUD_CDC_DESC_LEN)
#else
#define CONFIG_TOTAL_LEN \
  (TUD_CONFIG_DESC_LEN + TUD_VIDEO_CAPTURE_DESC_UNCOMPR_BULK_LEN)
#endif

#define EPNUM_VIDEO_IN 0x81

#if LIB_PICO_STDIO_USB == 1
#define USBD_CDC_EP_CMD 0x82
#define USBD_CDC_EP_OUT 0x03
#define USBD_CDC_EP_IN 0x83
#define USBD_CDC_CMD_MAX_SIZE 8
#define USBD_CDC_IN_OUT_MAX_SIZE 64
#define USBD_STR_CDC 0x04
#endif

uint8_t const desc_fs_configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0, 500),

    TUD_VIDEO_CAPTURE_DESCRIPTOR_UNCOMPR_BULK(4, EPNUM_VIDEO_IN,
                                              FRAME_WIDTH, FRAME_HEIGHT,
                                              FRAME_RATE, 64),
#if LIB_PICO_STDIO_USB == 1
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, USBD_STR_CDC, USBD_CDC_EP_CMD,
                       USBD_CDC_CMD_MAX_SIZE, USBD_CDC_EP_OUT,
                       USBD_CDC_EP_IN, USBD_CDC_IN_OUT_MAX_SIZE),
#endif
};

uint8_t const *tud_descriptor_configuration_cb(uint8_t index) {
  (void)index;
  return desc_fs_configuration;
}

char const *string_desc_arr[] = {
    (const char[]){0x09, 0x04}, /* English (0x0409) */
    "HimxCam",
    "HimxCam 3-cam UVC test",
    "000001",
    "HimxCam UVC",
#if LIB_PICO_STDIO_USB == 1
    "HimxCam control",
#endif
};

static uint16_t _desc_str[32];

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
  (void)langid;

  uint8_t chr_count;
  if (index == 0) {
    memcpy(&_desc_str[1], string_desc_arr[0], 2);
    chr_count = 1;
  } else {
    if (index >= sizeof(string_desc_arr) / sizeof(string_desc_arr[0])) {
      return NULL;
    }
    const char *str = string_desc_arr[index];
    chr_count = (uint8_t)strlen(str);
    if (chr_count > 31) {
      chr_count = 31;
    }
    for (uint8_t i = 0; i < chr_count; i++) {
      _desc_str[1 + i] = str[i];
    }
  }

  _desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
  return _desc_str;
}
