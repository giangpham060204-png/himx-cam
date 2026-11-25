/*
 * The MIT License (MIT)
 *
 * Based on TinyUSB's UVC device example and the
 * ArmDeveloperEcosystem/hm01b0-library-for-pico usb_camera example.
 */

#ifndef _USB_DESCRIPTORS_H_
#define _USB_DESCRIPTORS_H_

/* Time stamp base clock. Deprecated in newer UVC revisions. */
#define UVC_CLOCK_FREQUENCY 27000000

#define UVC_ENTITY_CAP_INPUT_TERMINAL  0x01
#define UVC_ENTITY_CAP_OUTPUT_TERMINAL 0x02

#ifndef FRAME_WIDTH
#define FRAME_WIDTH 480
#endif

#ifndef FRAME_HEIGHT
#define FRAME_HEIGHT 120
#endif

#ifndef FRAME_RATE
#define FRAME_RATE 8
#endif

enum {
  ITF_NUM_VIDEO_CONTROL,
  ITF_NUM_VIDEO_STREAMING,
#if LIB_PICO_STDIO_USB == 1
  ITF_NUM_CDC,
  ITF_NUM_CDC_DATA,
#endif
  ITF_NUM_TOTAL
};

#define TUD_VIDEO_CAPTURE_DESC_UNCOMPR_BULK_LEN ( \
    TUD_VIDEO_DESC_IAD_LEN \
    + TUD_VIDEO_DESC_STD_VC_LEN \
    + (TUD_VIDEO_DESC_CS_VC_LEN + 1 /* bInCollection */) \
    + TUD_VIDEO_DESC_CAMERA_TERM_LEN \
    + TUD_VIDEO_DESC_OUTPUT_TERM_LEN \
    + TUD_VIDEO_DESC_STD_VS_LEN \
    + (TUD_VIDEO_DESC_CS_VS_IN_LEN + 1 /* bNumFormats x bControlSize */) \
    + TUD_VIDEO_DESC_CS_VS_FMT_UNCOMPR_LEN \
    + TUD_VIDEO_DESC_CS_VS_FRM_UNCOMPR_CONT_LEN \
    + TUD_VIDEO_DESC_CS_VS_COLOR_MATCHING_LEN \
    + 7 /* endpoint */ \
  )

#define TUD_VIDEO_DESC_CS_VS_FMT_YUY2(_fmtidx, _numfmtdesc, _frmidx, _asrx, _asry, _interlace, _cp) \
  TUD_VIDEO_DESC_CS_VS_FMT_UNCOMPR(_fmtidx, _numfmtdesc, TUD_VIDEO_GUID_YUY2, 16, _frmidx, _asrx, _asry, _interlace, _cp)

#define TUD_VIDEO_CAPTURE_DESCRIPTOR_UNCOMPR_BULK(_stridx, _epin, _width, _height, _fps, _epsize) \
  TUD_VIDEO_DESC_IAD(ITF_NUM_VIDEO_CONTROL, 0x02, _stridx), \
  TUD_VIDEO_DESC_STD_VC(ITF_NUM_VIDEO_CONTROL, 0, _stridx), \
    TUD_VIDEO_DESC_CS_VC(0x0150, \
        TUD_VIDEO_DESC_CAMERA_TERM_LEN + TUD_VIDEO_DESC_OUTPUT_TERM_LEN, \
        UVC_CLOCK_FREQUENCY, ITF_NUM_VIDEO_STREAMING), \
      TUD_VIDEO_DESC_CAMERA_TERM(UVC_ENTITY_CAP_INPUT_TERMINAL, 0, 0, 0, 0, 0, 0), \
      TUD_VIDEO_DESC_OUTPUT_TERM(UVC_ENTITY_CAP_OUTPUT_TERMINAL, VIDEO_TT_STREAMING, 0, 1, 0), \
  TUD_VIDEO_DESC_STD_VS(ITF_NUM_VIDEO_STREAMING, 0, 1, _stridx), \
    TUD_VIDEO_DESC_CS_VS_INPUT(1, \
        TUD_VIDEO_DESC_CS_VS_FMT_UNCOMPR_LEN \
        + TUD_VIDEO_DESC_CS_VS_FRM_UNCOMPR_CONT_LEN \
        + TUD_VIDEO_DESC_CS_VS_COLOR_MATCHING_LEN, \
        _epin, 0, UVC_ENTITY_CAP_OUTPUT_TERMINAL, 0, 0, 0, 0), \
      TUD_VIDEO_DESC_CS_VS_FMT_YUY2(1, 1, 1, 0, 0, 0, 0), \
        TUD_VIDEO_DESC_CS_VS_FRM_UNCOMPR_CONT(1, 0, _width, _height, \
            _width * _height * 16, _width * _height * 16 * _fps, \
            _width * _height * 16, \
            (10000000 / _fps), (10000000 / _fps), \
            (10000000 / _fps) * _fps, (10000000 / _fps)), \
        TUD_VIDEO_DESC_CS_VS_COLOR_MATCHING(VIDEO_COLOR_PRIMARIES_BT709, \
            VIDEO_COLOR_XFER_CH_BT709, VIDEO_COLOR_COEF_SMPTE170M), \
        TUD_VIDEO_DESC_EP_BULK(_epin, _epsize, 1)

#endif
