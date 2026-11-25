# Camera firmware build log

## 2026-11-10 - Phase 1 toolchain proof

Status: complete. Nothing flashed.

### Versions

| Component | Version / commit |
|---|---|
| Host | Linux x86_64, Ubuntu 24.04 |
| CMake | 4.4.4 (wheel in `.venv`) |
| ARM GNU toolchain | 12.2.rel1 (Build arm-12.24), 12.2.1 20221205 |
| Pico SDK | 1.5.1, commit `6a7db34ff63345a7badec79ebea3aaef1712f374` |
| TinyUSB | `5ce60c5d207b9e7de419be6956164aebde14c3f2` |
| hm01b0-library-for-pico | `f834850419392a817306b618bfb62c10f95d4b7d` |
| Board target | `pico` (RP2040) |

### Paths

- Toolchain: `thirdparty/toolchain/arm-gnu-toolchain-12.2.rel1-x86_64-arm-none-eabi`
- Pico SDK: `thirdparty/pico-sdk-1.5.1`
- Upstream library: `camera_firmware/vendor/hm01b0-library-for-pico`
- Build directory: `/tmp/opencode/hm01b0-build` (outside the repo)

### Commands

```bash
export PICO_SDK_PATH=$PWD/thirdparty/pico-sdk-1.5.1
export PATH=$PWD/thirdparty/toolchain/arm-gnu-toolchain-12.2.rel1-x86_64-arm-none-eabi/bin:$PATH

.venv/bin/cmake -S camera_firmware/vendor/hm01b0-library-for-pico \
  -B /tmp/opencode/hm01b0-build \
  -DPICO_BOARD=pico -DPICO_SDK_PATH="$PICO_SDK_PATH"

.venv/bin/cmake --build /tmp/opencode/hm01b0-build -j$(nproc)
```

### Result

- Built `usb_camera` and `serial_terminal`.
- `usb_camera.uf2`: 55,296 bytes (110 UF2 blocks).
- SHA-256: `49ccf3d7b1394e8bfdc45163d528abb920a7cf104353e431b067a5cba0bd12fb`
- `usb_camera.elf` sections: text 27,960; data 0; bss 60,480.

### Notes

- The build used the upstream single-camera `usb_camera` example unchanged.
- This is a toolchain proof only. It has not been flashed, and it uses the
  upstream 160x120 single-camera pin map and UVC descriptor.
- The existing partial `thirdparty/pico-sdk` was not used or modified.
- Next milestone: Phase 1b, a minimal 480x120 UVC + CDC test-pattern firmware
  with no camera access, to validate the custom descriptor and the `BOOTSEL`
  recovery path before adding PIO/DMA.

## 2026-11-10 - Phase 1b minimal UVC + CDC test pattern

Status: build complete. Nothing flashed.

### Target

- `camera_firmware/src/main.c`
- `camera_firmware/src/usb_descriptors.c`
- `camera_firmware/src/usb_descriptors.h`
- `camera_firmware/src/tusb_config.h`

### Build

```bash
export PICO_SDK_PATH=$PWD/thirdparty/pico-sdk-1.5.1
export PATH=$PWD/thirdparty/toolchain/arm-gnu-toolchain-12.2.rel1-x86_64-arm-none-eabi/bin:$PATH

.venv/bin/cmake -S camera_firmware -B /tmp/opencode/himx-cam-uvc-build \
  -DPICO_BOARD=pico -DPICO_SDK_PATH="$PICO_SDK_PATH"
.venv/bin/cmake --build /tmp/opencode/himx-cam-uvc-build -j$(nproc)
```

### Result

- `himx_cam_uvc.uf2`: 73,216 bytes (143 UF2 blocks).
- SHA-256: `b1914812db8634c55edb47d4d7e112dcab1203a30f1e37ac69ac2f11c3e95377`
- `.elf` sections: text 36,560; data 0; bss 118,996.
- Descriptor check: configuration total length `0x00E9`, 4 interfaces,
  frame descriptor `480x120`, frame interval `1,250,000` (125 ms = 8 fps).
- CDC symbols present (`cdcd_init`, `cdcd_xfer_cb`); strings include
  `HimxCam 3-cam UVC`, `STATUS`, `PING`, `BOOTSEL`.
- Test pattern: three tiles separated by white borders; camera 1 horizontal
  ramp, camera 2 vertical ramp, camera 3 checkerboard.

### Not yet done

- The UF2 has not been flashed.
- No camera/PIO/DMA code is in this build.
- Next action: flash this minimal build, confirm the host sees the 480x120 UVC
  device and the CDC control port, then verify `STATUS` and `BOOTSEL`.

## 2026-11-10 - Phase 1b flashed and verified

Status: complete. The minimal UVC + CDC test firmware is running on the camera
Pico. No camera/PIO/DMA code yet.

### Flash path

1. MicroPython raw REPL: `machine.bootloader()`.
2. `RPI-RP2` mass-storage appeared as `/dev/sda1`.
3. Mounted through the existing user session:
   `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gio mount -d /dev/sda1`.
4. Copied `himx_cam_uvc.uf2` to `/media/commongdc/RPI-RP2`, then `sync`.

### Verification

- USB device: `cafe:4021 HimxCam HimxCam 3-cam UVC test`.
- Video nodes: `/dev/video0`, `/dev/video1`.
- CDC port: `/dev/serial/by-id/usb-HimxCam_HimxCam_3-cam_UVC_test_000001-if02`
  -> `/dev/ttyACM1`.
- CDC `PING` -> `PONG`.
- CDC `STATUS` -> `HimxCam test firmware: frames=... streaming=1 interval_ms=125`.
- OpenCV: opened `/dev/video0`, reported 480x120, read a `(120, 480, 3)` frame.
- Tiles: c1/c2/c3 each 160x120 with the expected ramps/checkerboard; saved to
  `/tmp/opencode/uvc-test.png`.
- Measured capture rate: 31 frames in 5.15 s = 6.02 fps (descriptor advertises
  8 fps; actual is lower and will be re-measured with the camera pipeline).
- Recovery test: CDC `BOOTSEL` returned the board to `RPI-RP2`; the same UF2
  was copied again; UVC and CDC both re-enumerated successfully.

### Notes

- The first custom flash and the `BOOTSEL` recovery cycle both worked without
  physical access.
- The test pattern is static; no camera is being read.
- Next milestone: Phase 2, single-camera capture on Camera 1, then Phase 3
  three-camera tiling. Wiring must match the target pin map before Phase 2.

## 2026-11-10 - LINES wiring diagnostic

Status: complete. Camera sensors remain in standby; no camera output has been
enabled by this firmware.

### Method

Added a CDC `LINES` command. For each GPIO it:

1. pulls the pin down, then up, and measures the relative charge count;
2. pulls the pin down and measures the relative discharge count;
3. reports whether the pin was externally held low or high.

Larger charge counts mean more capacitance. Unconnected pins are the baseline.
The command does not configure or stream the cameras.

### Results

Run-to-run stable results:

| Pins | Charge count | Interpretation |
|---|---:|---|
| GP0, GP1 | 0 (held high) | I2C SDA/SCL with external 4.7 kOhm pull-ups |
| GP6/GP7, GP8/GP9, GP10/GP11 | ~11-12 each | one camera per D0/PCLK pair |
| **GP15, GP16** | **~33-42** | approximately three camera loads: shared HREF/VSYNC |
| GP12-GP14, GP17-GP22 | ~4 | unconnected baseline |
| GP26-GP28 | ~5-6 | unconnected baseline |

Conclusion: the harness is wired as the PDF describes for data/clock
(`GP6/7`, `GP8/9`, `GP10/11`) and for shared sync (`GP15` HREF, `GP16`
VSYNC). I2C is on `GP0/GP1`, not the PDF's `GP4/GP5`, and works.

### Consequence

Shared HREF/VSYNC is the blocker. Three sensor outputs are electrically tied
together; there is no firmware fix for that. The minimal correction is to cut
the shared nets and give each camera its own HREF/VSYNC while keeping the
existing D0/PCLK pairs:

| Camera | D0 | PCLK | HREF | VSYNC |
|---|---|---|---|---|
| 1 | GP6 | GP7 | GP12 | GP13 |
| 2 | GP8 | GP9 | GP14 | GP15 |
| 3 | GP10 | GP11 | GP16 | GP17 |

This preserves the D0/PCLK wiring and changes only the six HREF/VSYNC wires.
The I2C pair stays on GP0/GP1.

## 2026-11-12 - TCA9548A mux and per-camera signal map

Status: mux and all three cameras verified on I2C. Video wiring is partially
complete.

### Mux check

`MUX` scans 0x70-0x77 on GP0/GP1, selects each channel, and probes `0x24`.

```
MUX: found 0x70
MUX 0x70 ch0: camera 0x24
MUX 0x70 ch1: camera 0x24
MUX 0x70 ch2: camera 0x24
MUX 0x70 ch3-7: no camera
```

This is the preferred architecture: the TCA9548A gives each camera its own
I2C channel, so all three can be configured individually despite the fixed
0x24 address.

### As-built signal map

| Camera | D0 | PCLK | HREF | VSYNC | Status |
|---|---|---|---|---|---|
| 1 | GP4 | GP5 | GP6 | GP7 | PCLK GP5 missing |
| 2 | GP8 | GP9 | GP10 | GP11 | D0 GP8 missing |
| 3 | GP12 | GP13 | GP14 | GP15 | fully wired |

LINES results match: GP4, GP6, GP7, GP9-GP15 show camera-like capacitance;
GP5, GP8 and GP16+ show unconnected baseline. Camera 3 is the first candidate
for a live capture test because all four of its signal wires are present.

### Next

- Connect the two missing signal wires: Cam1 PCLK -> GP5, Cam2 D0 -> GP8.
- Then build the three-camera capture path: mux channel select before each
  camera's configuration, PIO/DMA per camera, tile into one 480x120 UVC frame.
- No physical access is needed for the firmware work; only those two wires
  remain on the harness.





## 2026-11-12 — Camera 3 recovered; all three channels identify

After a clean **USB replug** (which is the only thing that actually power-cycles
the TCA9548A and the camera modules), the hardware came back healthy:

```
LINES: GP0 charge=0 discharge=200000  -> I2C pull-ups present, bus idle
MUX 0x70 ch0: camera 0x24
MUX 0x70 ch1: camera 0x24
MUX 0x70 ch2: camera 0x24      <- previously hung here
MUX 0x70 ch3-7: no camera
```

Per-channel probe (`MUXCH<ch>`, new command) reads the model-id register:

```
MUXCH0 select=1 ack=1 reg=2 id=2:01B0
MUXCH1 select=1 ack=1 reg=2 id=2:01B0
MUXCH2 select=1 ack=1 reg=2 id=2:01B0
```

All three are genuine HM01B0 sensors. **The earlier "channel 2 hangs" behaviour
was not a faulty camera**: it was a stale mux selection / bus state left over
from a firmware reset, which a real power cycle clears.

### Camera 3 live capture

`CAM3` now completes:

```
CAM3: selecting mux channel 2
CAM3: mux selected; probing 0x24
CAM3: probe rc=1
CAM3: calling hm01b0_init
CAM3: camera ready
STATUS: frames=8 streaming=1 cam3=1
```

The Camera 3 tile streams real pixel data, but the picture is **black**
(Y ~= 16-24 over the whole tile). Exposure changes the values, so the
sensor/PIO/DMA path is live; the scene itself is dark, the lens is covered,
or gain is still at minimum.

Root cause of the earlier init hangs, in order of discovery:

1. `i2c_read_blocking()` on a wedged bus — no timeout.
2. `stdio_flush()` inside `hm01b0_init()` deadlocked: `tud_task()` is not
   being pumped during a CDC command, so a full FIFO never drains.
3. The library re-initalises I2C (`i2c_init`) and loses the mux selection;
   fixed by re-selecting `HM01B0_MUX_CHANNEL` right after that call.
4. Bus speed: 100 kHz was marginal on the long breadboard wiring; 10 kHz on
   both the diagnostic path and inside the library fixed the write stalls.

### Wiring snapshot (2026-11-12, after replug)

| Camera | D0 | PCLK | HREF | VSYNC | Status |
|---|---|---|---|---|---|
| 1 | GP4 ok | GP5 ok | **GP6 missing** | GP7 ok | HREF wire missing |
| 2 | GP8 ok | GP9 ok | GP10 ok | GP11 ok | complete |
| 3 | GP12 ok | GP13 ok | GP14 ok | GP15 ok | complete |

GP6 now reads at unconnected baseline (charge~4), while GP5 and GP9 that were
missing in the previous snapshot now read as connected. The wiring moved
between checks; always re-run `LINES` before trusting an image.

### Next

- Connect Cam1 HREF -> GP6 (last missing signal wire).
- Point Camera 3 at a lit scene to confirm a real (non-black) image.
- Then generalise `CAM3` into `CAM<ch>` and build the mux-switched,
  three-camera capture path.

### Two-camera capture (CAM2 + CAM3) — 2026-11-12

The hm01b0 library was refactored to be multi-instance
(`struct hm01b0_handle`, `hm01b0_init(handle, config)`), and the TCA9548A
channel moved into `struct hm01b0_config` (`mux_addr`, `mux_channel`) so every
register access re-selects its own camera. Camera 2 uses PIO0 (GP8-GP11),
Camera 3 uses PIO1 (GP12-GP15); each PIO has its own 32-instruction space, so
both 18-instruction capture programs fit. The DMA wait in `hm01b0_read_frame`
is now bounded (500 ms) so a dead camera cannot wedge the main loop.

New CDC commands: `CAM2`, `CAM3`, `CAMEXP <ch> <lines>`, `PULSE <ch>`,
`SCAN <ch>`.

Results:

```
cam23_first.png
  tile1 (cam2): mean=0.0   -> no pixel data
  tile2 (cam3): mean=61.4  -> live image (lamp highlight + scene structure)
```

Camera 2 isolation:

```
PULSE2: mode_select=3 vsync=43 hsync=5185 pclk=325654 d0=0 d0_high=0/473451 float=1
PULSE3: mode_select=3 vsync=43 hsync=5180 pclk=325904 d0=62874 d0_high=66277/473463 float=0
```

* Camera 2 **is streaming** — VSYNC, HSYNC and PCLK all arrive.
* Camera 2's **D0 (GP8) never toggles** and floats when weakly pulled up.
* `SCAN1` (Camera 2 streaming, all spare pins watched for 300 ms):

```
SCAN2: GP9=58608 GP10=2928 GP11=24        <- PCLK, HREF, VSYNC only
SCAN3: GP12=9772 GP13=58617 GP14=2928 GP15=24
```

No other GPIO on the header shows the missing data signal, so the Camera 2
D0 wire is not connected to the Pico at all — most likely the wrong pin at the
camera-module end (e.g. landed on D1-D7, which are high-Z in 1-bit mode), or a
broken wire. `LINES` still reports GP8 as "connected" because it measures wire
capacitance, so it cannot detect this class of fault; `PULSE`/`SCAN` can.

### Commands worth remembering

| Command | Purpose |
|---|---|
| `MUXCH<0-7>` | select one mux channel, read ACK + model id (0x01B0) |
| `PULSE<1-2>` | stream one sensor for 500 ms, count VSYNC/HSYNC/PCLK/D0 edges |
| `SCAN<1-2>` | stream one sensor, report which header pins toggle |
| `CAM2` / `CAM3` | full sensor init (mux ch1 / ch2) |
| `CAMEXP<1-2> <lines>` | manual coarse integration |
| `LINES` | pin capacitance survey (wiring sanity, cannot see dead signal paths) |

### Camera 3 reaches 3D Slicer — 2026-11-13

`tools/camera_stream.py` gained `--device-tiled DEVICE`: one capture on
`/dev/video0`, split into the three tiles, each tile a normal `FrameSource`
(`frame_sources.TiledDevice` / `TiledSource`). The sender still speaks
OpenIGTLink to the Targeting module's server on 18944, so nothing on the
Slicer side changed.

Verified headless against the real module (Slicer 5.12.3, Xvfb):

```
connect: waiting for c1, c2, c3 on port 18944
Connected: localhost:18944
status: connected · uncalibrated (display only)
views: {'Red': 'c1', 'Green': 'c2', 'Yellow': 'c3'}
c1: 320x320x1  c2: 320x320x1  c3: 320x320x1
c3 changed over 3 s: mean abs diff = 0.530      <- live, not a frozen frame
```

Screenshot: `/tmp/opencode/slicer_cam3.png` (Camera 3 in the Yellow view).

Notes:
- c1 and c2 currently show the firmware test patterns (camera 2 still has no
  D0 connection, camera 1 is out of scope).
- No calibration file is published, so the module reports "uncalibrated
  (display only)" and refuses click solving; video display works regardless.
- The 160x120 tile is resized to the module's 320x320 optics, so the picture is
  stretched vertically until a calibration for the real sensor is written.
- Correct run command: `uv run tools/camera_stream.py --device-tiled
  /dev/video0 --resize --fps 5`

### "Connect real cameras" button in Slicer — 2026-11-15

One button now does the whole job: start the Pico sender if it is not already
running, then connect the three views.

- Module panel: **Connect real cameras** / **Stop real cameras** plus a
  `camera device:` field (default `/dev/video0`).
- `logic.start_real_cameras()` reuses a running sender instead of starting a
  second one; `onStopRealCameras` stops the sender and disconnects the views.
- `targeting/video_launcher.build_device_command()` builds the sender command
  (`tools/camera_stream.py --device-tiled <dev> --resize --fps 5 --port N`).

Missing cameras show black, at two levels:

1. **Firmware**: a tile whose camera is not streaming is filled with video
   black (Y=16) instead of a test pattern; `PATTERN 1` restores the ramps and
   checkerboard for bring-up. Cameras 2 and 3 are now initialised at boot, so
   the stream is usable without a CDC session.
2. **Sender**: if the device cannot be opened at all, `camera_stream.py
   --device-tiled` falls back to `BlankSource` and keeps sending black so
   Slicer still binds c1/c2/c3.

Verified headless (Slicer 5.12.3, Xvfb), clicking the real Qt buttons:

```
buttons found: ['Connect real cameras', 'Stop real cameras']
connect  -> status connected, views {'Red': 'c1', 'Green': 'c2', 'Yellow': 'c3'}
            c1/c2/c3 = 320x320x1, video: running (pid ...)
stop     -> video: stopped, views {}
missing device (/dev/definitely-not-a-camera):
            three views still bound, every pixel 0  (pure black)
```

Screenshots: `/tmp/opencode/slicer_real_button.png`,
`/tmp/opencode/slicer_real_black.png`.

Note: with the boot auto-init, Camera 2 also produced live pixel data
(mean 33, std 1.5) — its D0 connection appears to have come good, worth a
`SCAN2` re-check if it ever goes flat again.

### Exposure/gain investigation — 2026-11-16 (read this before touching sensor registers)

Goal was to brighten the near-black c2/c3 tiles. Findings, in order:

1. Zephyr's HM01B0 driver documents the register map: `0x2100` = AE_CTRL
   (bit 0 = AE on), `0x2101` = AE target mean, `0x2000` = AE statistics
   enable, `0x2105` = AE max integration, `0x0205` = analog gain,
   `0x020E` = digital gain, `0x3067` = analog register 17 (internal osc).
2. Writing the vendor default table **killed the video bus**: `0x3067=0x00`
   disables the internal oscillator, and this module has **no external MCLK**
   (`mclk_pin = -1`). Never write 0x3067.
3. Enabling the library's **software reset** (0x0103=0x01) recovers a sensor
   from bad registers, but it also **wipes the module's factory tuning**:
   afterwards the sensor streams a flat frame (every pixel identical, e.g.
   Y=19) and firmware changes no longer help.
4. Only a **power cycle** restores the factory tuning: unplug the camera Pico,
   wait ~5 s, replug. A re-flash does not.
5. Continuous streaming (MODE_SELECT left at 1) breaks the PIO capture: the
   tiles freeze at a constant value even though DMA completes with no
   timeouts. Keep the per-frame MODE_SELECT toggle in `hm01b0_read_frame`.
6. AE cannot converge with per-frame capture (it needs consecutive frames), so
   automatic exposure is not a usable lever in this design yet.

Current firmware state: the original known-good path (skip software reset,
per-frame MODE_SELECT, no vendor register table) plus capture/timeout counters
printed by `STATUS`.

Rule for next time: do not write ACES/analog/oscillator registers to these
modules, and if the image goes flat, ask for a USB power cycle before changing
firmware again.

### After the power cycle (2026-11-16)

Registers came back as factory defaults (`0x2101=0x3C`, `0x0205=0x00`,
`0x2100=0x01`), confirming a real replug rather than another re-flash.

```
c2: mean=0.0   min=0   max=0   std=0.0        <- no pixel data
c3: mean=27.9  min=16  max=236 std=4.3        <- live, structure + lamp highlight
c3 first-vs-last diff = 4.9 over 12 frames
```

Camera 3 is fully healthy again. Camera 2 answers I²C normally but delivers no
pixels, exactly the November 12 failure: its D0 (GP8) carries nothing. That is
a wiring fault, not firmware.

### Stream configuration + mock UI removal — 2026-11-16

**Frame rate / resolution are build options** (they change the USB descriptor,
so they cannot be switched at runtime without re-enumerating the device):

```
cmake -S camera_firmware -B <build> -DPICO_BOARD=pico \
      -DPICO_SDK_PATH=... \
      -DCAM_WIDTH=160 -DCAM_HEIGHT=120 -DCAM_FPS=8
```

* `CAM_FPS` sets the descriptor rate; the host may still ask for a slower
  interval and `tud_video_commit_cb` honours that.
* Only `160x120` tiles are accepted. Three tiles are packed into one YUYV
  frame, so 320x240 per camera would need a 960x240 frame = 460,800 bytes,
  more than the RP2040's 264 KB. The configure step refuses it with that
  explanation instead of failing later at runtime.

**Slicer panel** (mock video removed):

* Gone: Start video…, Stop video, the three clip pickers, Calibration, Truth,
  the Downloaded/Synthetic/Beef presets, and the startup preset calls. Those
  handlers are commented out, not deleted, in `Targeting.py`.
* Kept, now under "Camera settings": frame rate (drives the sender's `--fps`)
  and stream port (now passed to `connect_camera_stream`).
* The real-camera buttons, device field, pause/step/resume and status line are
  unchanged.

Verified headless in Slicer 5.12.3:

```
mock widgets present: []
real-camera buttons: ['Connect real cameras', 'Stop real cameras']
fps spin: 5.0  device: /dev/video0
connect (port 18950, 18944 held by the operator's Slicer)
  -> connected, views {Red: c1, Green: c2, Yellow: c3}
c3 shows the live scene; c2 is black (its D0 wire is still dead)
```

Screenshot: `/tmp/opencode/ui_stream.png`.

### Fresh wiring survey after `STOPCAMS` — 2026-11-16

New `STOPCAMS` command: stops both cameras, frees their PIO programs and hands
GP4–GP15 back to plain GPIO so `LINES` / `PULSE` / `SCAN` can run. `CAM2` and
`CAM3` start them again afterwards (verified).

`LINES` (connected ≈ charge 10–12, unconnected ≈ 4):

```
GP4 12/8  GP5 10/7  GP6 10/7  GP7 11/8     Camera 1: all four wires present
GP8  4/4  GP9 11/8  GP10 11/8 GP11 12/8    Camera 2: D0 (GP8) MISSING
GP12 12/8 GP13 11/8 GP14 10/7 GP15 12/8    Camera 3: all four wires present
```

Camera 1 now reads model ID `0x01B0` over mux channel 0 (`CAMREGR 1 0x0000` =
0x01, `0x0001` = 0xB0), so the sensor itself is healthy — only firmware keeps
it dark.

Summary of blockers:
* Camera 2 — one wire: D0 into GP8.
* Camera 1 — no wiring fault; needs firmware support, and the PIO budget has
  to be solved first (18 + 18 = 36 > 32 instructions per PIO block).

### Camera 1 enabled — 2026-11-16

Camera 1 (mux channel 0, GP4–GP7) was never initialised: boot only called
`cam_init(1)` and `cam_init(2)`. Enabling it needed the PIO budget solved.

What changed:

1. **Compact capture program** (10 instructions instead of 18): drops the
   border-pixel trimming loops. Camera 1 runs it on pio0/sm1 next to
   Camera 2's 18-instruction program: 10 + 18 = 28 of 32 instructions.
   Camera 3 keeps pio1/sm0 and its original program, untouched.
2. **jmp rebasing fix** (latent bug): PIO `jmp` operands are absolute, and the
   library encoded them as if every program started at address 0. That was
   true only for the first program in a block. `hm01b0_init` now records the
   jmp sites, gets the offset from `pio_add_program`, and rewrites those
   instruction words in `instr_mem` with `offset + target`.
3. `cam_init(0)` at boot, `CAM1` CDC command, third capture buffer and tile
   write, `cam1=` and per-camera counters in `STATUS`.

Result:

```
STATUS: cam1=1 cam2=1 cam3=1
c1: mean=25.7  max=28  std=3.0   <- live (dim scene)
c2: mean=0.0                     <- still no D0 wire
c3: mean=18.2  max=21  std=1.0   <- live
```

Camera 1 in isolation (`STOPCAMS` then `CAM1`) fills tile 0 and leaves tiles
1/2 black, so the tile really is its own data.

Cost: captures are serial, ~150–200 ms per camera, so three cameras give
roughly **2 fps** on the UVC stream even though the interval is 125 ms.

### "Pure noise" on c1 and c3 — exposure, not a fault (2026-11-16)

Both tiles are live (`captures` increasing, `timeouts=0`) but sit at the sensor
noise floor: c1 Y≈25, c3 Y≈19–32, with the black level at 16. Register state on
Camera 3: `0x2100=0x01` (AE on), `0x0203=0xF0` (integration 240 lines),
`0x0205=0x00` (analog gain 1x).

Tried analog gain as a lever (`0x0205=0x30`, i.e. 8x, then group hold 0x0104):
the frame went perfectly flat (mean 28, std 0) rather than brighter, so the
gain stage clamps instead of amplifying on this module. Reverting to `0x00`
restored the noisy-but-live frame.

Conclusion: the cameras are working; the scene is too dark for this sensor at
1x gain and 240-line integration. The remaining lever is more light (or a much
longer integration via a larger frame length).

### All three cameras live — 2026-11-17

Camera 2's D0 connection came good (wire reseated), so all three sensors now
deliver pixels through the 480x120 tiled UVC stream:

```
STATUS: cam1=1 cam2=1 cam3=1, captures=14 each, timeouts=0

c1: mean=28.0 std=3.3 max=31  temporal_diff=0.51
c2: mean=27.8 std=2.8 max=45  temporal_diff=0.76
c3: mean=27.9 std=4.2 max=201 temporal_diff=0.56
```

Every tile has a non-zero frame-to-frame difference, so all three are live
video, not frozen buffers. The saved frame shows real structure in each third
(a lit rod in c2, a lamp highlight in c3, a surface edge in c1).

Ceiling worth remembering: captures are serial, so three cameras give roughly
2–3 fps; and the scene is dim (means ~28 against a black level of 16), so the
picture only looks like an image when something bright is in view.

### Camera 1 looked "brighter" — border pixels, not exposure — 2026-11-17

All three sensors had identical exposure registers (`0x0203=0xF0` integration,
`0x0205=0x00` gain, AE on) and identical medians (29). The difference was in
the tile's minimum: Camera 1's compact program does not skip the sensor's
overscan, so its first two columns read 0.

```
before: c1 zeros=1.2%  min=0   max=31   -> auto-window stretches 0..31
        c2 zeros=0     min=21  max=44
        c3 zeros=0     min=20  max=208
after (crop 2 px in cam_tile):
        c1 min=24 mean=28.6 median=29 zeros=0
        c2 min=20 mean=28.7 median=29 zeros=0
        c3 min=19 mean=27.8 median=29 zeros=0
```

`cam_tile()` now takes a border offset; Camera 1 uses 2, the others 0. Slicer
auto-windows each volume independently, so a single band of zeros was enough to
make the whole tile look washed out. Camera 3 still looks darker in an
auto-windowed view because it genuinely contains a bright highlight (max 200) —
that is scene content, not exposure.

### AE hunting removed — 2026-11-17

The tiles were pulsing because auto-exposure only ever sees one isolated frame
per request, so its loop never converged. Measured across consecutive frames:

```
AE on : c1 mean 24.2 -> 36.8 -> 39.8 -> 25.6 -> 28.3 ...
AE off: c1 mean 28.2, 28.3, 28.3, 28.2, 28.4   (std 1.0, stable)
```

`hm01b0_init` now writes `0x2100=0x00` (AE off) and `0x0205=0x00` (1x gain),
leaving a deterministic exposure. `CAMEXP<1-3>` still adjusts it by hand (the
parser only accepted cameras 2 and 3 before — fixed).

After the change, all three tiles are stable within ~0.5 counts between frames:

```
c1 mean=23.4 std=0.6 max=26   temporal_diff=0.47
c2 mean=23.1 std=1.2 max=30   temporal_diff=0.48
c3 mean=23.7 std=2.1 max=122  temporal_diff=0.54
```

c1 remains flat (peak-to-peak ~6 counts) because its view has almost no
contrast; c3 still shows the lamp highlight. Same exposure settings on all
three, so that difference is the scene, not the camera.

### One display window for all three cameras — 2026-11-17

Slicer was auto-windowing each camera volume from its own min/max. A tile with a
few counts of noise (c1: 24–31) got stretched to full scale and looked like a
snowstorm, while a tile containing a lamp (c3) looked normal — the operator's
screenshot shows exactly that.

`_show_camera_views()` now sets a shared window on every camera volume:

```
CAMERA_WINDOW = 64.0     # 16 = sensor black level, 64 covers a lit scene
CAMERA_LEVEL  = 48.0
```

Result in a headless Slicer run: c1 renders dark and flat (honest — no
contrast in view), c2 shows its curved edge and bright object, c3 shows the lamp
and foreground. No more per-camera auto-stretch.

If a camera needs a brighter rendering later, change those two constants rather
than re-enabling auto window/level per camera.
