# himx-cam — touch-guided 3D targeting for 3D Slicer

A research prototype that turns a point selected in a 3D Slicer slice view into
a precise 3D target. A working volume (the "field") is declared in
`frame.json`; every accepted selection is converted from view pixels + slice
depth into millimetre coordinates, displayed as a virtual tip, and appended to
an auditable CSV log.

The coordinate chain is verified against Slicer's own hit-testing at 0.000 mm.
No robot is integrated yet — the current deliverable is the verified
selection → 3D-point workflow on a laptop, plus the module an eventual
touchscreen and robot arm will build on.

Two placeholders are used throughout this document: `<path-to-himx-cam>` for
the location of this project on your machine, and `<slicer>` for the path of
the Slicer executable you installed.

## Status

| | |
|---|---|
| Works today | Pixel + depth → 3D point, verified at 0.000 mm against Slicer's Data Probe; Slicer module with pick mode, virtual tip, labelled targets, daily CSV log; live view mapping (window resizes, zoom, monitor changes need nothing) |
| Tested on | Linux desktop, 3D Slicer 5.12.3, system Python 3.12 (headless) + Slicer's bundled 3.12.10 (module) |
| Not built yet | Patient-specific field shapes; finger-sized depth control (needs the touch device); robot-arm integration (needs the arm) |
| Automated evidence | 123 headless tests; in-Slicer smoke suite 15/15; human acceptance run 6/6 |

---

## Background: the Slicer concepts this project uses

If you have never used 3D Slicer, this section gives you the vocabulary the
rest of the README relies on. None of it needs to be memorised — every step
below says exactly where to click.

**The four-up layout.** Slicer's default screen shows four panes:

```
┌───────────────────┬───────────────────┐
│  RED view         │  3D view          │
│  (axial plane)    │  (the whole scene)│
├───────────────────┼───────────────────┤
│  GREEN view       │  YELLOW view      │
│  (coronal plane)  │  (sagittal plane) │
└───────────────────┴───────────────────┘
```

Each 2D pane has a coloured bar with the pane's name and a **slice slider**.
Red shows the **axial** plane (looking along the patient's superior–inferior
axis), Green the **coronal** plane (front view), Yellow the **sagittal** plane
(side view). These three views are where targets are clicked.

**Slice sliders — the third coordinate.** A slice view is a 2D cut through the
3D scene. The slider in each view's bar moves that cut forwards and backwards,
and its number is the cut's position in millimetres along the direction the
slider label names:

| View | Slider label | Moves the cut along |
|---|---|---|
| Red (axial) | `S:` superior | head ↕ feet |
| Green (coronal) | `A:` anterior | front ↕ back |
| Yellow (sagittal) | `L:` left | left ↕ right |

This is the key idea of the whole project: **clicking gives two coordinates,
and the slider supplies the third.** The value of the slider at the moment you
click is stored as `depth_mm` in the log.

**The mouse: Data Probe and crosshair.** Moving the mouse over a slice view
draws Slicer's own "you are here" marker (the crosshair) and shows the
corresponding position in the **Data Probe** panel (bottom-left of the window),
for example `Red (L 2.7, P 4.6, S 0.0)`. That reading is Slicer's independent
answer to "what 3D point is under the mouse pixel?", and the project's health
check compares itself against it. (`L`/`R` = left/right, `P`/`A` =
posterior/anterior, `I`/`S` = inferior/superior.)

**The scene and its nodes.** Everything shown in Slicer is a *node* in the
current *scene*: slices of a volume, surface models, markups points, and so on.
A freshly started Slicer has an **empty scene**, and the scene is not saved
anywhere until you explicitly use File → Save. Consequence for this project:
the field cube (a node) disappears when Slicer closes, so it must be recreated
at the start of every working session (one console command, step "Daily use").
The CSV log is the only thing that persists automatically.

**The module panel.** The left-hand side of Slicer shows the GUI of whichever
*module* is selected. The dropdown/search box at the top (it says "Modules:")
switches between modules — `Data`, `Models`, `Markups`, and, after
Installation, `Targeting`.

**The Python console.** `View → Python Console` opens a console at the bottom
of the window. It runs Python *inside* the running Slicer, which is how the
project's tools create the field cube, run the health check, and inspect the
current scene. Nothing you paste there is saved or rerun automatically.

---

## Scope: what Slicer provides, what this repository adds

Slicer is already a complete visualization and interaction environment; this
project does not rebuild any of it. It contributes one discipline — turning a
screen position into a verified, recorded 3D target — and borrows everything
else from Slicer.

| Capability | Provided by | Role in this project |
|---|---|---|
| Slice views — Red (axial), Green (coronal), Yellow (sagittal) — plus the 3D view, in the four-up layout | Slicer | The click surface and the visualization; the project adds no views of its own |
| Slice sliders / slice scrolling | Slicer | Supply each target's depth (the third coordinate); the module only reads the value |
| Mouse handling, pan, zoom, crosshair | Slicer | Untouched. While Pick targets is on, the module borrows clicks in the three views; unticking hands them straight back |
| Data Probe (shows the RAS point under the mouse) | Slicer | The independent reference the project's health check is verified against |
| The RAS frame and millimetre units | Slicer | Displayed in the panel alongside the project's Cube frame |
| Scene, nodes, saving and loading | Slicer | `Targets` (a markups list) and `VirtualTip` (a model) are ordinary Slicer nodes: inspect them with Slicer's own modules, export them, or save them with the scene |
| Python console and the module system | Slicer | How the Targeting module and all `tools/` scripts plug in; nothing is compiled or packaged |
| **The operating field** — a declared, enforced region where targets are allowed | **this repository** (`frame.json`, `targeting/`) | Slicer has no notion of "allowed target volume". The module refuses anything outside the field, loudly, and never clamps |
| **Pixel + depth → 3D conversion** | **this repository** (`targeting/converter.py`) | Pure maths with no Slicer imports; verified at 0.000 mm against Slicer's own hit-testing; portable to the future robot side |
| **Live pixel→RAS mapping** | **this repository** (`slicer_bridge/live_frame.py`, `targeting/view_mapping.py`) | Reads Slicer's own matrices on every click, so window resizes, zoom, monitor changes and DPI differences need no recalibration |
| **The Targeting module** | **this repository** (`SlicerTargeting/`) | Pick mode with safe click capture, the virtual tip sphere, accumulating labelled targets (T1, T2, …), numbers in both frames, counters, Clear |
| **Audit trail** | **this repository** (`targeting/logbook.py`) | One CSV row per accepted click: pixel, depth, and the same point in the Cube and RAS frames |
| **Verification and operations tooling** | **this repository** (`tools/`) | Field-cube builder, guided health check against the Data Probe, reference-point check, and a synthetic-click smoke suite |

## What has been done so far

The complete history of the project, in the order it was built:

1. **The pure maths layer** (`targeting/`) — coordinate frames, the
   pixel + depth → Cube converter with its loud out-of-field refusal, and the
   daily CSV writer. Fully testable without Slicer; the suite currently stands
   at **123 tests**.
2. **The live view mapping** (`slicer_bridge/`, `targeting/view_mapping.py`) —
   reads Slicer's own pixel→RAS matrices and derives the converter's constants
   from them (scale, view centre, screen directions, the Qt row flip).
   Consequence: nothing screen-dependent is ever stored, so resizing, zooming
   or changing monitors needs no adjustment.
3. **Verification against Slicer itself** — five exact reference points
   checked through the terminal tool (the RAS↔Cube relationship), then a
   guided in-Slicer health check that compares the project's conversion with
   Slicer's Data Probe at the same pixels. Result on the development setup:
   agreement at **0.000 mm** in all three views, with every screen convention
   confirmed empirically (radiological nose-up orientation, slider directions,
   and the one-pixel row flip).
4. **The Targeting module** (`SlicerTargeting/`) — pick mode that safely
   consumes clicks, target placement with the virtual tip and labelled,
   accumulating points, the refusal path, panel numbers in both frames,
   counters, "Open log folder", and the CSV record. A human acceptance run —
   install, place targets, verify the log, restore normal Slicer behaviour —
   passed all six checks.
5. **Test infrastructure** — the 123-test headless suite plus a 15-check
   in-Slicer smoke suite that drives the module with synthetic clicks and
   compares every placed target against Slicer's own crosshair reading.
6. **Operations tooling and records** — the field-cube builder, log
   inspection snippets, reference-point tools, this README, and an
   append-only research log under `logs/`.

**Not yet built:** robot-arm integration (no hardware), a finger-sized depth
control (waits for the touchscreen device), and patient-specific field shapes
(the field is the declared 100 mm cube). These are the roadmap items at the
end of this document.

---

## How the targeting chain works

```
click (u,v) in a slice view + that view's slider depth (mm)
        │
        ▼
slicer_bridge/live_frame()      reads Slicer's live pixel→RAS matrix
        │                       (window size, zoom, DPI, orientation)
        ▼
targeting/converter.py          pixels + depth → Cube (x,y,z), pure arithmetic
        │                       rejects anything outside the field, loudly
        ▼
SlicerTargeting module          sphere (virtual tip), labelled point (T1, T2, …),
                                panel numbers (Cube + RAS), CSV row
```

- **Two coordinate frames.** *RAS* is Slicer's millimetre frame (right /
  anterior / superior); its origin is the field centre. *Cube* is the project's
  frame with the origin at the field's lower corner. For the default 100 mm
  field every axis shifts by 50 mm: `RAS = Cube − 50`. The panel shows both, so
  points can be read back in whichever frame a collaborator uses.
- **The field.** `frame.json` declares the working volume (currently a 100 mm
  cube centred on RAS 0,0,0). A click that lands outside it is refused with a
  message and a counter; nothing is ever clamped or "snapped" to something
  plausible.
- **Nothing screen-dependent is stored.** The pixel↔millimetre mapping is read
  from Slicer itself on every click (see the next section), so resizing the
  window, zooming, or moving to another monitor needs no recalibration.
- **Every accepted selection is logged** to `logs/targets-YYYY-MM-DD.csv` for
  audit and replay.

### The machinery of one click, in detail

1. **Click capture.** When **Pick targets** is ticked, the module installs a
   small event filter on each of the three slice views. While pick mode is on,
   a left-click in those views is intercepted before Slicer's own view controls
   see it — so a target click can never accidentally pan the view or drag the
   slider. Unticked, everything behaves like stock Slicer.
2. **Pixel + depth.** The click's position is taken in view pixels, and the
   *clicked view's* slider value is taken as the depth, in millimetres.
3. **Live mapping.** Slicer's slice node stores the exact pixel→RAS mapping for
   its view (`GetXYToRAS()`) and recomputes it whenever the window, zoom or
   screen changes. The module reads it fresh — this is why the project has no
   stored screen constants and nothing to tune after a monitor change.
4. **Conversion and the field check.** The two in-plane axes come from the
   mapping, the third from the slider. The resulting point must fall inside the
   declared field; otherwise the click is rejected (`Outside the field: …`) and
   counted, but nothing is placed.
5. **Presentation and record.** An accepted point moves the `VirtualTip`
   sphere, appends a labelled point to the `Targets` list, updates the panel's
   numbers, and appends one row to today's CSV.

### What the project adds to a Slicer scene

| Node | Created by | Behaviour |
|---|---|---|
| `FieldCube` | `tools/make_field_cube.py` | Opaque wireframe cube with visible edges, shown in the 3D view and in all three slice views (its cross-section is the orange square). Represents exactly the region `frame.json` enforces |
| `Targets` | The module, on the first accepted click | A standard Slicer markups list; one labelled point (`T1`, `T2`, …) per accepted click. Select it in the `Markups` module to inspect or export; `Clear targets` empties it |
| `VirtualTip` | The module, on the first accepted click | A small green sphere at the latest accepted point — "where the robot tip would be". Moves on every accepted click; removed by `Clear targets` |

If you use File → Save, these nodes are saved with the scene like any other
Slicer content. Without saving, they live only for the session.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Linux desktop with display | Tested platform. Windows/macOS untested |
| 3D Slicer 5.12.3 | <https://download.slicer.org> (stable release, Linux). Older versions untested. Needs OpenGL 3.2+ (any normal desktop GPU/driver from the last decade) |
| Python 3.12 | For the headless maths tests. Standard library only — the runtime needs **no pip packages** |
| pytest | `python3 -m pytest --version`; if missing: `python3 -m pip install --user pytest` |
| git | Optional; this checkout is a local repository with no remote |
| Disk space | ~2 GB for Slicer plus a little for this repository and its logs |

---

## Repository layout

```
himx-cam/
├── frame.json             the field: size, centre, reference axis maps
├── env.example            HIMX_CAM_ROOT template (once per machine)
├── targeting/             pure maths layer — no Slicer imports (enforced by a test)
│   ├── converter.py       pixels + depth -> Cube; field bounds check; cube_to_ras
│   ├── view_mapping.py    Slicer's live matrix -> scales, centres, screen signs
│   ├── field_fit.py       fit helpers for the declared field
│   ├── frame.py           Frame data model (load/save)
│   └── logbook.py         daily CSV log writer
├── slicer_bridge/
│   └── live_frame.py      the only module that imports Slicer; reads live views
├── SlicerTargeting/       the Slicer module (install this folder)
│   └── Targeting/
│       ├── Targeting.py           module + panel + click capture
│       └── TargetingLib/logic.py  placement, nodes, logging
├── tools/                 console utilities (run inside Slicer by exec)
├── tests/                 headless pytest suite (123 tests)
├── logs/                  research log (created on first use; git-ignored)
└── docs/                  design/overview/guide notes (local only; git-ignored)
```

---

## Installation

### 1. Get the project

```bash
git clone <repository-url> <path-to-himx-cam>    # if a remote exists
# or: copy the folder as-is — it needs no building or packaging
```

Everything is plain Python and JSON; there is no compile step and no package to
install for the project itself.

### 1b. Tell the tools where the checkout lives (once per machine)

The console tools refuse to guess the project location: they panic instead of
using a wrong path. Point them at the checkout once:

```bash
export HIMX_CAM_ROOT=<path-to-himx-cam>   # e.g. $HOME/projects/himx-cam
```

`env.example` shows the same variable for a local `.env` file. Inside Slicer's
Python console (which does not inherit every shell setup and has no reliable
`__file__` under `exec()`), set the equivalent global once per session before
running any tool:

```python
import pathlib
PROJECT_ROOT_OVERRIDE = pathlib.Path('<path-to-himx-cam>')
```

### 2. Verify the maths on the laptop

No Slicer required. Run, from the project folder:

```bash
cd <path-to-himx-cam>
python3 -m pytest tests/ -q
```

Expected final line: `123 passed`. This exercises the conversion maths, the
CSV writer, and the Slicer-facing glue (against fakes) entirely outside Slicer.
If anything fails here, stop — later steps would be built on unverified maths.

If `python3 -m pytest` is not found, install pytest once:
`python3 -m pip install --user pytest` — or on Ubuntu:
`sudo apt update && sudo apt install -y python3-pytest`.

### 3. Install Slicer

1. Open <https://download.slicer.org>.
2. In the **Download** section, pick the **Stable Release** (this project is
   developed and tested against **5.12.3**), then the **Linux** package.
3. Extract the downloaded archive anywhere you like. Inside the extracted
   folder there is an executable named `Slicer`. Its full path is referred to
   below as `<slicer>`.
4. Start it (double-click or `./Slicer` in a terminal). The first launch takes
   some seconds. You should see the four panes described earlier and a Welcome
   panel you can close.
5. Optional sanity check: `Help → About 3D Slicer` should report version
   5.12.3.

### 4. Install the Targeting module into Slicer

The module is a *scripted* module: Slicer loads it directly from source. There
is nothing to build. Two equivalent ways to register it, A then B as fallback.

**Option A — Extension Wizard (in-session; can be made permanent):**

1. In Slicer: switch to the **Extension Wizard** module. The surest way:
   type `Extension Wizard` into the **Modules:** search box at the top of the
   window and select it (it also appears under the Developer Tools section of
   the toolbar).
2. Click **Select Extension** and navigate to this repository. Select the
   folder **`SlicerTargeting`** (the one containing `CMakeLists.txt`), and
   confirm.
3. The wizard fills in the extension info and then asks whether to load the
   scripted modules it found — you should see `Targeting` in that dialog.
   **Tick "Add to search paths"** before accepting: that checkbox writes the
   path into Slicer's user settings so the module is available in every future
   session. Without it, the module is loaded only for the current session.
4. Accept. The module is now registered.
5. Verify: type `Targeting` into the **Modules:** search box at the top of
   Slicer. The Targeting panel should open, with a status line reading
   `Ready - tick Pick targets and click in a view.`

**Option B — Application Settings (permanent, no wizard):**

1. In Slicer: **Edit → Application Settings**.
2. In the left list choose **Modules**, find **Additional module paths**, and
   click **Add**.
3. Select this repository's **`SlicerTargeting`** folder, then OK/close.
4. **Restart Slicer.** Type `Targeting` into the Modules search box; the panel
   should appear as in Option A.

Either way, this is a one-time installation on a given machine.

### 5. Health check (recommended, one minute)

This proves the pixel→3D geometry on *your* window, against Slicer's own
readings. Open the Python console (**View → Python Console**) and run
(requires the section 1b setup above — otherwise the tool panics with
`could not locate himx-cam checkout` instead of guessing):

```python
exec(open(PROJECT_ROOT_OVERRIDE.joinpath("tools/check_live_mapping.py").read_text()).read())
```

It prints the live mapping for the three views, then walks you through three
hover spots — Red, then Green, then Yellow:

1. Move the mouse into the named view, inside the cube if you have built it
   (see the walkthrough below; if not, near the middle),
2. **hold still** for about half a second,
3. it prints a `spot N done` line with the error and a PASS/FAIL.

Expected: every spot `PASS` with errors at or near `0.000mm` and a cursor
offset of `(+0.00, +0.00) px`. If a spot fails, hover another point and hold
still again; the script reloads the project's code on every run, so a rerun
always tests the current files.

---

## First run: guided walkthrough

This is the whole workflow once, with a checkpoint at every step. Budget five
minutes.

**Checkpoint 1 — start clean.** Open Slicer. Close the Welcome panel if it
covers the module area. You should see the four-pane layout.

**Checkpoint 2 — build the field cube.** The cube lives in the scene, so it has
to be created each time Slicer starts. In the Python console
(requires the section 1b setup above):

```python
exec(open(PROJECT_ROOT_OVERRIDE.joinpath("tools/make_field_cube.py").read_text()).read())
```

The console prints the field geometry and a line ending in
`visible in 3D and all slice views.` You should now see a small orange square
in the middle of the Red, Green and Yellow views, and a wireframe cube in
the 3D view (left-drag in the 3D pane rotates it so you can see the shape).

**Checkpoint 3 — open the module.** Type `Targeting` in the Modules search box
and select it. The panel shows the status line
`Ready - tick Pick targets and click in a view.`, a checkbox, counters, and two
buttons.

**Checkpoint 4 — place a target in the Red view.** Tick **Pick targets**. Move
the mouse into the Red view, inside the orange square, and click. Expected:

- the green sphere jumps to the clicked point (clearest in the 3D view),
- a blue label `T1` appears at the point in all views,
- the panel shows `Last: XY view, depth …`, plus `Cube (…) mm` and
  `RAS (…) mm`,
- the counter reads `Placed: 1`.

**Checkpoint 5 — a second point at a different depth.** Move the Red view's
slider (the bar above the Red pane, labelled `S:`) a few millimetres, click
another spot inside the orange square. `T2` appears and the sphere moves; the
`depth` value in the panel changes accordingly.

**Checkpoint 6 — the other views.** Repeat once in the Green view and once in
the Yellow view (each with its own slider). The panel and points behave the
same; `Placed` climbs.

**Checkpoint 7 — the refusal path.** Click well outside the orange square, in
any view. Expected: nothing is placed; the status line explains, e.g.
`Outside the field: R=-184.363mm is outside the field -50.000..50.000mm`, and
`Rejected` increases while `Placed` stays put.

**Checkpoint 8 — the log.** Press **Open log folder** and open today's file
`targets-YYYY-MM-DD.csv` (or check it from the console; snippet under
"Tools"). Expected: a header row plus one row per accepted click — the
rejected click is deliberately absent.

**Checkpoint 9 — back to normal Slicer.** Untick **Pick targets**. Clicks and
drags in the views now behave like stock Slicer again (pan, zoom, slice
scrolling — whatever Slicer's mouse settings define). `Clear targets` removes
the points and the sphere and resets the counters; the CSV keeps its history.

---

## Daily use

Once installed, a working session is:

1. Start Slicer → run the field-cube command (Checkpoint 2 above).
2. Open **Targeting**, tick **Pick targets**.
3. For each target: set the depth with the view's slider, click inside the
   orange square, confirm the sphere and the panel numbers.
4. Untick **Pick targets** when done; find the day's rows in
   `logs/targets-YYYY-MM-DD.csv`.

Notes:

- While pick mode is on, clicks in the three views belong to the module
  (they cannot drag or pan). After changing the Slicer layout (e.g. to One-Up),
  untick and re-tick **Pick targets** so the click filters are reinstalled.
- If Slicer ever shows a warning `Placed, but the log write failed: …`, the
  target *was* placed but not recorded — fix write access to `logs/` before
  continuing.

### The CSV log

`logs/targets-YYYY-MM-DD.csv`, one file per day, one row per **accepted**
click:

```
timestamp,view,u_px,v_px,depth_mm,cube_x,cube_y,cube_z,ras_r,ras_a,ras_s
2026-10-11T23:06:11,XY,357.0,117.0,0.000,60.618,61.100,50.000,10.618,11.100,0.000
```

`view` names the plane (`XY` = Red/axial, `XZ` = Green/coronal, `YZ` =
Yellow/sagittal), `u_px`/`v_px` is the clicked pixel, `depth_mm` the slider
value, and the final six numbers are the same point in the Cube and RAS
frames. `timestamp` is local time, ISO-8601, seconds precision.

---

## Configuration — `frame.json`

```json
{
  "field_size_mm":    { "R": 100.0, "A": 100.0, "S": 100.0 },
  "field_center_ras": { "R": 0.0,   "A": 0.0,   "S": 0.0   },
  "view_axis_map":    { "XY": { "u": ["R", -1], "v": ["A", -1], "depth": ["S", 1] }, … }
}
```

- `field_size_mm` / `field_center_ras` describe the working volume. Edit them
  to move or resize the field. The module and the cube tool read the file on
  their next run — no restart needed, but re-run the cube command to redraw the
  new region.
- `view_axis_map` is documentation: the screen conventions observed on the
  development setup. The runtime derives the real values from Slicer on every
  click and does not depend on this block. The health check compares live vs
  stored and reports any disagreement.
- Screen-dependent constants (mm/px, view centres) are deliberately **not**
  stored anywhere — they are read live from Slicer.

---

## Tools (`tools/`)

All console tools are run from Slicer's Python Console in this form
(requires the section 1b setup — `PROJECT_ROOT_OVERRIDE` set once per
session, or `HIMX_CAM_ROOT` exported before launching Slicer; otherwise the
tool raises `could not locate himx-cam checkout` instead of guessing):

```python
exec(open(PROJECT_ROOT_OVERRIDE.joinpath("tools/<tool>.py").read_text()).read())
```

`exec(open(...).read())` is deliberate: Slicer caches imported modules for the
whole session, while this form always reads the current file from disk. All
tools are safe to re-run.

| Tool | Purpose |
|---|---|
| `make_field_cube.py` | (Re)build the field cube in the scene; prints the field geometry. Re-run after every Slicer restart and after editing `frame.json` |
| `check_live_mapping.py` | Guided verification of the pixel→3D chain against Slicer's Data Probe (three hover spots, PASS/FAIL per spot) |
| `make_refpoints.py` | Creates five exact reference points used by `verify_converter.py` |
| `verify_converter.py` | **Terminal** tool (no Slicer): checks the RAS↔Cube relationship against the reference points; pass criterion 0.01 mm |
| `smoke_targeting_module.py` | **Developer** test: launches a separate Slicer, drives the module with synthetic clicks, checks 15 behaviours, prints `SMOKE PASS 15/15`, quits itself |
| `measure_now.py` / `print_slicer_constants.py` | Legacy: raw view-matrix inspection utilities |

Handy console snippet to check today's log without leaving Slicer:

```python
import pathlib
folder = pathlib.Path('<path-to-himx-cam>/logs')
for path in sorted(folder.glob('targets-*.csv')):
    lines = path.read_text().splitlines()
    print(path.name, '->', len(lines) - 1, 'data rows')
    for line in lines[:3]:
        print('   ', line)
```

---

## Testing

```bash
# headless (laptop, no Slicer): 123 tests
python3 -m pytest tests/ -q

# in-Slicer smoke: loads the module in a separate Slicer instance,
# injects synthetic clicks, and checks 15 behaviours, then quits itself
timeout 300 <slicer> \
    --no-splash --python-script tools/smoke_targeting_module.py 2>&1 | grep SMOKE
# expect: SMOKE PASS 15/15
```

The smoke run opens a Slicer window; it closes itself when finished (1–3
minutes depending on the machine). The `timeout` is only a safety net.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Targeting` not in the module list | Redo Installation step 4. If the wizard was used, ensure **Add to search paths** was ticked; otherwise use Option B plus a Slicer restart |
| `No module named 'targeting'` in the console | Don't `import` tools; run them with the section 1b + Tools-section `exec(...)` form — Slicer's console provides no reliable `__file__`. A `could not locate himx-cam checkout` error means the 1b setup is missing: set `PROJECT_ROOT_OVERRIDE` or export `HIMX_CAM_ROOT` |
| Orange square invisible | The cube lives in the scene only; run `make_field_cube.py` after any restart |
| Click refused with `Outside the field` | Working as designed — aim inside the orange square, or widen the field in `frame.json` |
| CSV not updating | Check the panel: a failed write is reported as `Placed, but the log write failed: …`. Verify `logs/` exists and is writable |
| Module edits don't take effect in a running Slicer | Use the module's **Reload & Test → Reload** button, or restart Slicer. Console tools reload the project's modules automatically |
| Slider moved, but the target depth didn't change | The depth comes from the **clicked view's** slider; check you changed that view's bar, not another pane's |
| Everything loads but targets land oddly on a HiDPI screen | Run `check_live_mapping.py`; it reports any pixel-geometry disagreement per spot |
| Moving the project to another path | Nothing to edit: the Slicer module and `slicer_bridge` locate the project from their own file location, and the **console tools** resolve it as `PROJECT_ROOT_OVERRIDE` → `HIMX_CAM_ROOT` env → own file location, else panic. Set `HIMX_CAM_ROOT` once per machine (see `env.example` and section 1b) |

---

## Design notes

- **One source of truth for pixels.** Slicer's `vtkMRMLSliceNode.GetXYToRAS()`
  is documented as "the mapping from XY pixel coordinates on an image window
  into RAS world coordinates" and is the same mapping Slicer uses to render
  views and place the Data Probe crosshair. Reading it per click is why no
  recalibration exists.
- **Refuse loudly.** Out-of-field selections raise rather than clamp; the
  module surfaces the reason and counts it.
- **Measured screen conventions** (verified against Slicer's own hit-testing):
  in the Red view, screen-right is `−R` and screen-down is `−A` (radiological
  nose-up); Green: screen-right `−R`, screen-down `−S`; Yellow: screen-right
  `−A`, screen-down `−S`. Qt rows flip as `H−1−y` when converting display
  positions.
- **The maths layer never imports Slicer** (enforced by a test), which keeps
  the conversion portable — the eventual robot side talks Cube millimetres,
  not Slicer.
- Deeper notes (design history, overview, operator guide) live in `docs/`
  locally; they are kept out of version control on purpose.

---

## Roadmap

1. **Field region as data** — support any closed shape (e.g. a patient
   segmentation) for containment, instead of the current cube. Interface
   sketch: a region object answering "is this point inside?"; box/cone as pure
   implementations, mesh/labelmap implemented on the Slicer side.
2. **Finger-sized depth control** — designed only once the touchscreen device
   exists.
3. **Robot arm** — measure the arm's own frame (`robot_from_cube` matrix),
   re-check containment before motion, add safety interlocks. Blocked on
   hardware.
4. **Touchscreen deployment** — an always-on panel for the operating room.

---

## License

No license is declared yet. Until one is added, treat this as internal
research software.
