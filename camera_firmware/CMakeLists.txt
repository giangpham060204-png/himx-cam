cmake_minimum_required(VERSION 3.13)

# Reuse the upstream import helper; PICO_SDK_PATH must point at the pinned SDK.
include(${CMAKE_CURRENT_LIST_DIR}/vendor/hm01b0-library-for-pico/pico_sdk_import.cmake)

project(himx_cam_uvc C CXX ASM)

pico_sdk_init()

add_executable(himx_cam_uvc
    src/main.c
    src/usb_descriptors.c
    vendor/hm01b0-library-for-pico/src/hm01b0.c
)

target_include_directories(himx_cam_uvc PRIVATE
    src
    vendor/hm01b0-library-for-pico/src/include
)

# --- stream configuration -------------------------------------------------
# One UVC frame carries three tiles side by side, so the frame is 3x wide.
# The whole YUYV frame must fit in the RP2040's 264 KB of SRAM:
#   480x120 YUYV = 115,200 B  (160x120 per tile)   <- the only size that fits
#   960x240 YUYV = 460,800 B  (320x240 per tile)   <- does not fit
set(CAM_WIDTH 160 CACHE STRING "Pixels per camera tile, horizontally")
set(CAM_HEIGHT 120 CACHE STRING "Pixels per camera tile, vertically")
set(CAM_FPS 8 CACHE STRING "UVC frame rate (the host may ask for slower)")

if(NOT CAM_WIDTH STREQUAL "160" OR NOT CAM_HEIGHT STREQUAL "120")
    message(FATAL_ERROR
        "Only 160x120 tiles fit in SRAM with three tiles per frame "
        "(a 320x240 tile needs a 960x240 YUYV frame = 460,800 bytes). "
        "Got CAM_WIDTH=${CAM_WIDTH} CAM_HEIGHT=${CAM_HEIGHT}.")
endif()
math(EXPR FRAME_WIDTH "${CAM_WIDTH} * 3")

target_compile_definitions(himx_cam_uvc PRIVATE
    CAM_WIDTH=${CAM_WIDTH}
    CAM_HEIGHT=${CAM_HEIGHT}
    FRAME_WIDTH=${FRAME_WIDTH}
    FRAME_HEIGHT=${CAM_HEIGHT}
    FRAME_RATE=${CAM_FPS}
    # Skip the library's software reset: it wipes the module's factory tuning
    # and leaves an untuned, flat sensor. The cameras are power-cycled by
    # unplugging the Pico, which is the reset that actually helps.
    HM01B0_SKIP_SOFTWARE_RESET=1
)

target_link_libraries(himx_cam_uvc PRIVATE
    tinyusb_device
    tinyusb_board
    pico_stdlib
    hardware_i2c
    hardware_watchdog
    hardware_dma
    hardware_pio
    hardware_pwm
    cmsis_core
)

# Composite UVC + CDC: CDC is the recovery/diagnostic channel.
pico_enable_stdio_usb(himx_cam_uvc 1)
pico_enable_stdio_uart(himx_cam_uvc 0)

pico_add_extra_outputs(himx_cam_uvc)
