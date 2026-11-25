# Custom three-camera HM01B0 UVC firmware - implementation plan

Status: Phase 1 and Phase 1b complete and verified on hardware; no camera capture yet. See `BUILD_LOG.md`.
Date: 2026-11-10
Target board: camera Pico, MicroPython unique id `e665485457276c2f`
Host: this Linux machine, arm Pico (`E66164084383A533`) must remain untouched

## 1. Goal

Make one RP2040 (the camera Pico) enumerate as a **single UVC camera** whose
frames contain three HM01B0 images side by side, so the existing host path can
split them back into `c1`, `c2`, `c3` and keep using the `FrameSource` seam.

Success means:

- one USB video device, one `/dev/videoN`, no custom kernel driver;
- a 480x120 YUYV frame containing three 160x120 monochrome views;
- about 6-8 frames per second, stable for at least 10 minutes;
- the host can capture a frame, split it at x=160 and x=320, and obtain three
  non-black images;
- `tools/camera_stream.py` can stream those three views to Slicer.

## 2. Non-goals

- No per-camera exposure or gain. All three sensors share I2C address `0x24`;
  they receive identical register writes by design.
- No hardware frame synchronisation. Each sensor free-runs on its own internal
  oscillator. For a static target, or while the stream is paused, that is fine.
- No 320x320 stream from all three cameras. USB Full Speed cannot carry it.
- No change to the arm/Klipper Pico, its firmware, or its serial port.
- No system-wide package installation. Everything stays under the repo or
  `/tmp/opencode`.

## 3. What is already proven

Measured on 2026-11-10 with `tools/camera_probe.py`:

- The camera Pico is running MicroPython 1.29.0 and is reachable over USB CDC.
- An HM01B0 answers at I2C address `0x24` on **SDA GP0 / SCL GP1**.
- The 16-bit model-ID register `0x0000` reads `0x01B0`, which is the HM01B0
  model ID.
- The address sweep finds no device on GP4/GP5. The brief's GP4/GP5 wiring is
  therefore not what the current harness is doing.
- All three cameras share the same I2C address, so the scan proves at least one
  camera is present; it cannot count them.
- No UVC firmware is flashed and no `/dev/video*` device exists today.

Earlier brief assumptions, now checked:

- Camera 1 data/clock are on GP6/GP7, as in the client brief.
- Cameras 2 and 3 data/clock are on GP8/GP9 and GP10/GP11, as in the brief.
- HREF and VSYNC are currently shared on GP15/GP16, as in the brief. That
  sharing is unsafe for three active sensors and must be removed before the
  three-camera firmware can work.

Confirmed by the 2026-11-10 `LINES` diagnostic:

- GP6/GP7, GP8/GP9, GP10/GP11 each look like one camera load, matching the
  brief's D0/PCLK pairs.
- GP15 and GP16 look like approximately three camera loads each: the shared
  HREF and VSYNC nets are real.
- I2C is on GP0/GP1; GP4/GP5 is unconnected.
- GP12-GP14 and GP17-GP22 are unconnected and available for the per-camera
  HREF/VSYNC fix.

## 4. Design decisions

### D1 - I2C is configuration only; video is separate

The shared I2C bus configures all sensors. It is not used to carry pixels.
Each sensor's D0/PCLK/HREF/VSYNC are separate wires, captured independently by
PIO and DMA. This is why three streams are possible despite one bus and one
address.

### D2 - Use the as-built I2C pins GP0/GP1

The camera is already answering on GP0/GP1. Keeping that pair avoids another
physical change and is electrically fine (I2C0). The firmware pin map therefore
uses:

- SDA = GP0
- SCL = GP1

The client brief and the Arducam example use GP4/GP5. That is a documentation
difference, not a requirement. If the harness is later rewired to GP4/GP5,
change one header and rebuild.

### D3 - Reject shared HREF and VSYNC

HREF and VSYNC are sensor outputs. Three outputs tied together contend, and
three free-running sensors do not have aligned edges. Every camera gets its own
HREF and VSYNC. This supersedes the client brief's "shared HREF/VSYNC" section.

### D4 - One tiled UVC stream, not three USB cameras

Three separate 160x120 YUYV streams would not fit in USB Full Speed. One
480x120 YUYV frame is 115,200 bytes. At 8 fps that is 921,600 B/s, inside the
practical FS bulk range (~1.0-1.1 MB/s) but with little margin.

The host splits the frame at x=160 and x=320. Tile order is left to right:
camera 1, camera 2, camera 3.

### D5 - Uniform per-camera pin groups for one shared PIO program

Each camera's four signal pins are a contiguous group:

- offset 0 = D0
- offset 1 = PCLK
- offset 2 = HREF
- offset 3 = VSYNC

The RP2040 PIO `WAIT PIN n` instruction resolves `n` against the executing
state machine's input pin mapping. Load one 1-bit capture program once, then
give each of the three state machines its own `in_base` set to that camera's
D0. The program uses offsets 1/2/3 for PCLK/HREF/VSYNC, so all three SMs can
share it.

This must be verified with a known input pattern. If the relative-pin behaviour
does not hold on the pinned SDK, fall back to separate compact programs in
PIO0/PIO1 or absolute `WAIT GPIO` instructions. Do not silently assume it.

### D6 - Capture at the display rate, not the sensor rate

The sensors run at their configured internal rate (about 30 fps in QQVGA).
The firmware samples one frame from each camera per output cycle. All three
PIO state machines are armed together, so the three captures happen
concurrently: total capture time is one sensor frame period, not three.
Frames that arrive between output cycles are ignored.

This avoids spending CPU and USB bandwidth on frames nobody sees.

### D7 - Single UVC frame buffer, pipelined capture

The three monochrome buffers are separate from the YUYV tile buffer. While USB
is transmitting the previous tiled frame, the PIO/DMA path can capture the next
three monochrome frames. Compose into the YUYV buffer only after the previous
transfer completes.

Memory budget:

| Buffer | Bytes |
|---|---:|
| 3 x monochrome 160x120 | 57,600 |
| 1 x tiled YUYV 480x120 | 115,200 |
| total | 172,800 |

RP2040 has 264 KB SRAM, so this leaves about 91 KB for stack, TinyUSB, and
code. No second tiled buffer is needed initially.

### D8 - Keep a CDC control channel for remote recovery

The first custom flash replaces MicroPython. After that, the raw-REPL
`machine.bootloader()` route no longer exists. If the custom firmware's USB
stack works but the UVC descriptor does not, the board could otherwise require
a physical BOOTSEL press for every retry.

The firmware therefore keeps a USB CDC interface alongside UVC (the upstream
example already supports this composite mode) and accepts a small command:

- `BOOTSEL` -> `reset_usb_boot(0, 0)`, which reboots into the ROM mass-storage
  bootloader;
- `STATUS` -> one line with camera model ID, frame counter, and per-camera
  health.

This is for recovery and diagnostics, not for streaming. If the whole USB
stack fails, physical BOOTSEL remains the fallback.

## 5. Pin map

### 5.1 Current harness (measured / assumed)

| Signal | Camera 1 | Camera 2 | Camera 3 | Status |
|---|---|---|---|---|
| VCC | 3V3 | 3V3 | 3V3 | shared, verify |
| GND | GND | GND | GND | shared, verify |
| SDA | GP0 | GP0 | GP0 | measured working |
| SCL | GP1 | GP1 | GP1 | measured working |
| D0 | GP6 | GP8 | GP10 | from brief, verify |
| PCLK | GP7 | GP9 | GP11 | from brief, verify |
| HREF | GP15 | GP15 | GP15 | shared - **must change** |
| VSYNC | GP16 | GP16 | GP16 | shared - **must change** |

### 5.2 Target harness for three cameras

| Signal | Camera 1 | Camera 2 | Camera 3 |
|---|---|---|---|
| D0 | GP6 | GP10 | GP14 |
| PCLK | GP7 | GP11 | GP15 |
| HREF | GP8 | GP12 | GP16 |
| VSYNC | GP9 | GP13 | GP17 |
| VCC | 3V3 | 3V3 | 3V3 |
| GND | GND | GND | GND |
| SDA | GP0 | GP0 | GP0 |
| SCL | GP1 | GP1 | GP1 |

That is **16 distinct pins**: 4 shared (3V3, GND, SDA, SCL) + 4 per camera.
The brief's "14 pins" count omitted the separated HREF/VSYNC.

Each camera group is contiguous and has the same relative offsets:

| Camera | D0 base | PCLK | HREF | VSYNC |
|---|---:|---:|---:|---:|
| 1 | GP6 | GP7 | GP8 | GP9 |
| 2 | GP10 | GP11 | GP12 | GP13 |
| 3 | GP14 | GP15 | GP16 | GP17 |

Pins GP2-GP5, GP18-GP22, and GP26-GP28 remain free for UART, debug, or a
status output.

### 5.2.1 Alternative: keep the brief's D0/PCLK pairs

If the client already wired the brief's D0/PCLK pairs, those wires can stay.
Only HREF/VSYNC need to become per-camera:

| Signal | Camera 1 | Camera 2 | Camera 3 |
|---|---|---|---|
| D0 | GP6 | GP8 | GP10 |
| PCLK | GP7 | GP9 | GP11 |
| HREF | GP12 | GP14 | GP16 |
| VSYNC | GP13 | GP15 | GP17 |

The offsets from each D0 base are identical for all three cameras:
PCLK +1, HREF +6, VSYNC +7. One shared PIO program still works.

This is the smallest wiring change if the brief's data/clock pairs are already
in place. The shared HREF/VSYNC nets on GP15/GP16 must still be cut apart.

### 5.3 Wiring migration

Do not rewire all three cameras at once while debugging. Power off before any
rewiring.

1. Leave the working I2C pair on GP0/GP1.
2. Rewire Camera 1 to GP6/GP7/GP8/GP9 and test it alone.
3. Add Camera 2 on GP10/GP11/GP12/GP13 and test two tiles.
4. Add Camera 3 on GP14/GP15/GP16/GP17 and test three tiles.

A camera's four signal wires are outputs from the module, so never connect two
cameras' HREF or VSYNC together.

### 5.4 Hardware safety

- The UC-805 module takes **3.3 V** on VCC. Do not apply 5 V.
- All modules must share a common ground with the Pico.
- Power off before moving any wire.
- The module has no LED. UVC enumeration and the three image tiles are the
  status indication; there is no light to watch at the camera.

## 6. Firmware architecture

### 6.1 Source layout

```
camera_firmware/
  CMakeLists.txt
  src/
    main.c                 scheduler, USB task, frame pacing
    camera_pins.h          compile-time pin map
    sensor_bus.c/.h        I2C init, broadcast register writes, model-ID read
    sensor_config.c/.h     HM01B0 QQVGA register sequence
    capture.c/.h           PIO program, 3 SMs, 3 DMA channels
    tile.c/.h              mono -> YUYV composition
    uvc_device.c/.h        TinyUSB UVC descriptors and callbacks
    control.c/.h           CDC STATUS/BOOTSEL command parser
  vendor/
    hm01b0-library-for-pico/   pinned upstream source, MIT
  build/                   generated, gitignored
```

Start from the upstream `examples/usb_camera` files (`main.c`,
`usb_descriptors.c`, `usb_descriptors.h`) and replace the single-camera
capture path. Keep the upstream license headers.

### 6.2 Initialisation order

1. `board_init()`, disable stdio USB if UVC owns the USB stack.
2. Initialise I2C0 on GP0/GP1 at 100 kHz.
3. Reset/configure the HM01B0 register set once. All cameras receive the same
   writes.
4. Read model ID `0x0000` best-effort; expect `0x01B0`. A failed read does not
   prevent UVC enumeration, but it means the bus or at least one camera is not
   healthy.
5. Initialise the three PIO state machines and three DMA channels.
6. Initialise TinyUSB with the composite UVC + CDC configuration.
7. Start the frame scheduler and the CDC command loop.

### 6.3 I2C rules

- Writes are broadcast-safe: every camera at `0x24` receives the same bytes.
- Reads are diagnostic only. Multiple devices driving the same read bits can
  corrupt the value if they disagree; identical sensors normally agree, but do
  not build control logic on readback.
- Do not call the upstream `hm01b0_init()` unchanged in the multi-camera build:
  it aborts if the model-ID read is not exactly `0x01B0`. Split it into a
  broadcast configuration path that always writes the register set, and a
  separate best-effort model-ID check.
- Use 100 kHz for bring-up. Move to 400 kHz only after the harness is stable.
- Every I2C write uses a finite timeout; never block USB enumeration on a stuck
  bus.
- The modules have no external pull-ups; the RP2040 internal pull-ups are used
  in the current probe. If the bus becomes unreliable with three modules, add
  external 2.2-4.7 kOhm pull-ups to 3V3 (hardware change, not firmware).

### 6.4 Capture path

For each camera `i`:

1. Configure SM `i` with `in_base = D0_i`, same program offset.
2. Set `sm_config_set_in_shift(true, true, 8)`: shift right, autopush every
   8 bits, 8-bit transfers.
3. Set `sm_config_set_in_pins(config, D0_i)`.
4. Route the four pins to PIO with `pio_gpio_init`.
5. Configure DMA channel `i` from SM `i` RX FIFO to `mono[i]`, 8-bit, read
   fixed, write increment.
6. Arm all three DMA channels and enable all three SMs first.
7. Write `MODE_SELECT = 1` once; all sensors start and each SM waits for its
   own VSYNC.
8. Wait for each DMA completion with a timeout (for example 250 ms). On
   timeout, fill that tile black and mark the camera unhealthy; do not block
   the other two.
9. Disable SM and DMA before the next cycle.

The PIO program should be the upstream 1-bit program with the wait
instructions encoded as `pio_encode_wait_pin(polarity, offset)` rather than
absolute GPIO numbers. Offsets are 1=PCLK, 2=HREF, 3=VSYNC for every camera in
the target harness.

### 6.5 Composition

For each row `y` in 0..119 and each camera `c` in 0..2:

- source row `mono[c][y * 160 .. y * 160 + 159]`
- destination starts at `yuyv[y * 480 * 2 + c * 160 * 2]`
- write 80 16-bit words per tile row:
  - low byte = Y0
  - high byte = 128 (U)
  - next word: low byte = Y1, high byte = 128 (V)

Equivalently, each 32-bit group is `Y0 | (128 << 8) | (Y1 << 16) | (128 << 24)`
on a little-endian core. A simple loop is fast enough: 57,600 pixels per
frame at 8 fps is about 461k pixels/s.

Tile boundaries are even (160 pixels), so a YUYV pair never crosses cameras.

### 6.6 Frame scheduler

Target interval: 125 ms (8 fps). If USB is not keeping up, fall back to
143 ms (7 fps) or 167 ms (6 fps). Measure before choosing the final descriptor
rate.

Pseudo-loop:

```
if UVC streaming:
    capture three mono frames (with timeout per camera)
    if previous UVC transfer is complete:
        compose mono -> yuyv
        tud_video_n_frame_xfer(...)
    else:
        keep the newest mono frames for the next opportunity
tud_task()
```

The UVC completion callback clears the busy flag. The next composition waits
for it; never overwrite the buffer while TinyUSB is reading it.

### 6.7 UVC descriptors

Start from the upstream bulk UVC descriptor:

- format: YUYV (uncompressed)
- width: 480
- height: 120
- frame rate: 8 fps
- endpoint: bulk IN `0x81`, 64-byte packets
- one streaming interface, one frame descriptor
- one CDC interface for `STATUS` / `BOOTSEL`

Keep the example's `0xCafe` VID/PID for bring-up; assign a stable product
string such as "HimxCam 3-cam UVC". Do not change VID/PID between test flashes
until the host driver has settled.

The descriptor's `dwFrameInterval` must match the delivered rate. If the final
rate is 7 fps, set the descriptor to 7; do not advertise 30 fps and deliver 7.

Windows and macOS are secondary. The target host is Linux/V4L2/OpenCV.

## 7. Host-side changes

### 7.1 `tools/frame_sources.py`

Add a tiled source group:

- one `cv2.VideoCapture` for the 480x120 UVC device;
- `read()` once per output cycle, split at x=160 and x=320;
- expose three `FrameSource` objects, one per camera, in c1/c2/c3 order;
- convert BGR to grayscale with `cv2.COLOR_BGR2GRAY`;
- verify the incoming frame is 480x120 before splitting; refuse other sizes
  with a clear error.

The split must be exact, not resized: resizing would change the calibration.

### 7.2 `tools/camera_stream.py`

Add a mutually exclusive option:

```
--device-tiled /dev/video0
```

When present, open one device and create the three sources from the tiled
group. Keep `--devices` (three separate devices) and the video-file paths
unchanged.

### 7.3 `targeting/optics.py`

Do not guess a 160x120 optics entry. After the first frames, verify the
sensor's 160x120 window/binning and compute the effective field of view.
Then add an entry such as `arducam-hm01b0-160x120` with measured values.

## 8. Build and toolchain

These are approval gates, not actions to run yet. Nothing is installed
system-wide.

### 8.1 Required versions

- Pico SDK `1.5.1`
- TinyUSB commit `5ce60c5d207b9e7de419be6956164aebde14c3f2`
- `ArmDeveloperEcosystem/hm01b0-library-for-pico` commit
  `f834850419392a817306b618bfb62c10f95d4b7d`
- ARM GNU toolchain `12.2.rel1` (x86_64 host)
- CMake from a wheel in the existing `.venv` (or an existing user-space cmake)

The existing `thirdparty/pico-sdk` is a partial 689 MB clone of unknown state.
Do not build against it. Create a clean `thirdparty/pico-sdk-1.5.1` and leave
the old directory untouched until its owner confirms it can be removed.

### 8.2 Proposed commands

```
mkdir -p thirdparty/toolchain
ARM_URL=https://developer.arm.com/-/media/Files/downloads/gnu/12.2.rel1/binrel/arm-gnu-toolchain-12.2.rel1-x86_64-arm-none-eabi.tar.xz
curl -fL "$ARM_URL" -o thirdparty/toolchain/arm-gnu-12.2.rel1.tar.xz
tar -C thirdparty/toolchain -xf thirdparty/toolchain/arm-gnu-12.2.rel1.tar.xz

git clone --branch 1.5.1 --depth 1 \
  https://github.com/raspberrypi/pico-sdk.git \
  thirdparty/pico-sdk-1.5.1
cd thirdparty/pico-sdk-1.5.1
git submodule update --init lib/tinyusb
cd lib/tinyusb
git fetch --depth 1 origin 5ce60c5d207b9e7de419be6956164aebde14c3f2
git checkout 5ce60c5d207b9e7de419be6956164aebde14c3f2

cd <repo-root>
git clone https://github.com/ArmDeveloperEcosystem/hm01b0-library-for-pico.git \
  camera_firmware/vendor/hm01b0-library-for-pico
cd camera_firmware/vendor/hm01b0-library-for-pico
git checkout f834850419392a817306b618bfb62c10f95d4b7d
```

Then, after approval:

```
.venv/bin/python -m pip install --no-cache-dir cmake
```

Build:

```
export PICO_SDK_PATH=$PWD/thirdparty/pico-sdk-1.5.1
export PATH=$PWD/thirdparty/toolchain/arm-gnu-toolchain-12.2.rel1-x86_64-arm-none-eabi/bin:$PATH
cmake -S camera_firmware -B camera_firmware/build \
  -DPICO_BOARD=pico -DPICO_SDK_PATH="$PICO_SDK_PATH"
cmake --build camera_firmware/build -j
```

Expected artifact: `camera_firmware/build/usb_camera_multi.uf2`.

### 8.3 Flashing and recovery

Add `tools/flash_camera_firmware.py` with two entry paths:

1. **MicroPython present** (before the first custom flash): discover only the
   camera Pico by id (`e665485457276c2f`), refuse the arm Pico
   (`E66164084383A533`) or an unknown serial, open the raw REPL, send
   `machine.bootloader()`, wait for `RPI-RP2`, copy the UF2.
2. **Custom firmware present** (after the first flash): open the CDC control
   port, send `BOOTSEL\n`, wait for `RPI-RP2`, copy the UF2.

Both paths finish by waiting for re-enumeration and reporting either the UVC
video node or a clear timeout.

**First custom flash is the recovery risk.** Once MicroPython is gone, a
firmware that fails before USB enumeration cannot be reflashed remotely. Keep
the known-good MicroPython UF2 and the custom UF2 in a local recovery folder.
If the board does not enumerate after a custom flash, the client must hold
BOOTSEL while plugging it in once; after that, the ROM mass-storage device is
available and flashing can continue remotely.

To reduce that risk, the first custom milestone is a **minimal UVC + CDC
enumerator** with a fixed test pattern and no camera access. Do not add PIO,
DMA, or sensor code until the host has opened that device successfully.

Manual fallback: hold BOOTSEL, plug in, copy the UF2. This remains remote-work
friendly because the board is reachable over USB.

## 9. Implementation phases

### Phase 0 - Freeze the wiring

Deliverable: agreed pin map, physically verified.

- Decide I2C: keep GP0/GP1 (recommended, already working) or move to GP4/GP5.
- Rewire Camera 1 to GP6/GP7/GP8/GP9, one camera only.
- Run the I2C probe; confirm model ID `0x01B0`.
- Add Cameras 2 and 3 only after Camera 1 video works.

Acceptance: one camera answers on the chosen I2C pair; no shared HREF/VSYNC;
no shorts between 3V3 and GND.

### Phase 1 - Toolchain proof

Deliverable: unmodified upstream `usb_camera` UF2 builds.

- Download toolchain/SDK/TinyUSB after approval.
- Build the upstream example for `pico`.
- Record compiler, SDK, and TinyUSB versions in the build log.

Acceptance: UF2 produced; no system-wide installs.

### Phase 1b - Minimal UVC + CDC enumerator

Deliverable: a safe first custom flash.

- Build a UVC device at 480x120 with a fixed grey/colour test pattern, no PIO,
  no DMA, no I2C.
- Include the CDC `STATUS` / `BOOTSEL` command channel.
- Flash it, verify `/dev/video*`, capture the test pattern, and verify the CDC
  port answers `STATUS`.
- Only then add camera code.

Acceptance: host opens the test-pattern UVC device; `BOOTSEL` command returns
the board to the ROM bootloader. If this fails, the client has to hold BOOTSEL
once for recovery.

### Phase 2 - Single-camera UVC first light

Deliverable: one camera visible on the host.

- Configure Camera 1 on its final group.
- Keep 160x120 YUYV at 30 fps initially.
- Flash; verify `/dev/video*`; capture and save one frame.

Acceptance: a 160x120 image with scene content, opened by OpenCV; CDC `STATUS`
reports model ID `0x01B0`; no DMA timeout.

### Phase 3 - Three-camera capture and tiling

Deliverable: 480x120 tiled UVC stream.

- Add three PIO SMs + three DMA channels.
- Compose the tiled YUYV frame.
- Add per-camera timeout and black-tile fallback.
- Set the descriptor to 480x120 and the measured frame rate.

Acceptance: captured 480x120 frame splits into three 160x120 images; each tile
has non-zero variance and shows the expected view; at least 6 fps; 10 minutes
without a lockup or USB error.

### Phase 4 - Host integration

Deliverable: `camera_stream.py --device-tiled` works.

- Add `TiledGroupSource` and the CLI option.
- Add a synthetic tiled-frame unit test that proves the split order and shapes.
- Send c1/c2/c3 to Slicer and exercise a click set.

Acceptance: three views in Slicer; click solve still produces the same
behaviour as the video-file path.

### Phase 5 - Optics and calibration

Deliverable: a real 160x120 camera model in `targeting/optics.py`.

- Verify the sensor's 160x120 window/binning and effective FOV.
- Capture calibration frames; compute intrinsics.
- Write the camera-rig JSON and record the reprojection error.

Acceptance: a click set solved with the real calibration, marked as measured,
not DEMO.

### Phase 6 - Robustness

Deliverable: predictable behaviour with missing or flaky cameras.

- One camera unplugged: its tile is black, the other two keep streaming.
- USB unplug/replug: device re-enumerates without reflashing.
- I2C write failure: UVC still enumerates; status is visible in the logs.
- 30-minute run: no memory corruption, no DMA hang, no host disconnect.

Acceptance: failure modes documented in the firmware README.

## 10. Verification commands

After flashing, on this host:

```
lsusb | grep -i -E 'cafe|uvc|video'
ls -l /dev/video*
python3 - <<'PY'
import cv2
cap = cv2.VideoCapture('/dev/video0')
print('opened', cap.isOpened())
print('size', cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
print('fps', cap.get(cv2.CAP_PROP_FPS))
ok, frame = cap.read()
print('read', ok, None if frame is None else frame.shape)
if ok:
    cv2.imwrite('/tmp/opencode/uvc-tiled.png', frame)
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    for i, name in enumerate(('c1', 'c2', 'c3')):
        tile = grey[:, i * 160:(i + 1) * 160]
        cv2.imwrite(f'/tmp/opencode/{name}.png', tile)
        print(name, tile.shape, 'mean', float(tile.mean()), 'std', float(tile.std()))
cap.release()
PY
```

A tile with `mean < 2` and `std < 2` is effectively black and indicates that
camera's data path is missing or not capturing.

## 11. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| USB FS bandwidth too tight at 8 fps | dropped frames, host timeouts | start at 6-7 fps, measure; Y800/GREY as phase-2 speedup |
| Breadboard signal integrity at PCLK | bit errors, noisy tiles | short wires, common ground, test one camera first, lower PCLK if needed |
| Shared I2C reads contend | wrong readback | use writes for control; reads only diagnostic |
| Shared HREF/VSYNC remains wired | bus contention, no capture | enforce per-camera HREF/VSYNC in the target map |
| PIO `WAIT PIN` offset assumption | shared program fails | verify with a known pattern; fallback to separate programs or absolute waits |
| Non-standard 480x120 UVC mode | host rejects format | test OpenCV/V4L2 early; fall back to 480x160 or lower fps |
| Memory pressure | stack/USB corruption | 172.8 KB buffers leaves ~91 KB; keep code paths lean; measure high-water mark |
| Toolchain/TinyUSB drift | build failures | pin SDK 1.5.1 and TinyUSB `5ce60c5`; record versions |
| Camera free-run phase | tiles from slightly different times | document; pause stream for moving targets |
| First custom flash loses remote recovery | board needs physical BOOTSEL | flash a minimal UVC+CDC test-pattern build first; keep MicroPython UF2; client holds BOOTSEL only if enumeration fails |
| Clone board / no LED | weak status feedback | UVC enumeration and per-tile images are the status signal |

## 12. Open decisions

1. **I2C pins:** keep GP0/GP1 (already working, recommended) or move to the
   brief's GP4/GP5. Pick one before flashing; the firmware header must match.
2. **Final frame rate:** 6, 7, or 8 fps. Decide from measured USB throughput.
3. **Format:** YUYV first. Consider Y800/GREY only after YUYV works; it halves
   bandwidth but needs host support.
4. **VID/PID/product string:** keep the upstream example values for bring-up,
   then assign a stable identity.
5. **Calibration method:** checkerboard vs marker-based; depends on what the
   client can print and hold still.

## 13. Approval needed before implementation

- Download ARM GNU 12.2.rel1 (~155 MB) and the CMake wheel into user-space.
- Clone Pico SDK 1.5.1 and TinyUSB at the pinned commit.
- Flash the camera Pico with custom firmware.
- Ask the client to separate HREF/VSYNC and rewire the three camera groups.

No `apt`, no `sudo`, no system-wide toolchain, and no changes to the arm Pico
or the pinned Klipper/Moonraker/Mainsail trees.
