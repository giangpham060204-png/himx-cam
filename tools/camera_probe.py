#!/usr/bin/env python3
"""Ask a MicroPython RP2040 board whether any HM01B0 camera answers.

The camera board is probed from software only: no soldering iron, no meter,
no BOOTSEL.  The board must be running MicroPython (any recent build); the
probe talks to its raw REPL over USB CDC, so it needs no firmware of ours.

What it reports:

* the board's identity (flash unique id) and the MicroPython version,
* a diagnostic GPIO pull-up/pull-down test (not a continuity test),
* the RP2040's documented I2C pin pairs, scanned for devices,
* a plain-language verdict.

The I2C scan looks for the HM01B0's expected 7-bit address, 0x24. When it
finds one, it reads the 16-bit model-ID register (0x0000) and expects 0x01B0.
This is a communication test, not a continuity or power test. No
acknowledgement only means that no device at 0x24 answered these scan
attempts; it cannot tell whether the camera is absent, unpowered, miswired,
faulty, or affected by a bus/electrical/driver issue. A normal GPIO pull test
also does not prove that nothing is connected.

Run it with the serial group applied to the process, because a login session
started before `dialout` membership exists will not have that group:

    sg dialout -c 'python3 tools/camera_probe.py'

The project venv is deliberately not required: the host's ``python3`` ships
pyserial, the venv does not, and this tool must run with the fewest moving
parts. ``--port`` overrides discovery; use it only for the verified
MicroPython camera board. By default the tool refuses to guess among generic
``/dev/ttyACM*`` ports, which could select the Klipper arm Pico.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

SERIAL_GLOB = "/dev/serial/by-id/*MicroPython*"
CAMERA_ADDRESS = 0x24

PROBE = r"""
import machine, time, sys
print("unique_id", machine.unique_id().hex())
print("version", "%s %s" % (sys.implementation.name, sys.implementation.version))
pins = list(range(0, 23)) + [26, 27, 28]
pull_test = []
for n in pins:
    p = machine.Pin(n, machine.Pin.IN, machine.Pin.PULL_DOWN); time.sleep_ms(2)
    low = p.value()
    p = machine.Pin(n, machine.Pin.IN, machine.Pin.PULL_UP); time.sleep_ms(2)
    high = p.value()
    machine.Pin(n, machine.Pin.IN)
    if not (low == 0 and high == 1):
        pull_test.append((n, low, high))
print("pull_test", pull_test)
pairs = [(0, 0, 1), (0, 4, 5), (0, 8, 9), (0, 12, 13), (0, 16, 17), (0, 20, 21),
         (1, 2, 3), (1, 6, 7), (1, 10, 11), (1, 14, 15), (1, 18, 19),
         (1, 22, 23), (1, 26, 27)]
hits = []
model_id = None
for bus, sda_n, scl_n in pairs:
    sda = machine.Pin(sda_n, machine.Pin.IN, machine.Pin.PULL_UP)
    scl = machine.Pin(scl_n, machine.Pin.IN, machine.Pin.PULL_UP)
    time.sleep_ms(2)
    try:
        i2c = machine.I2C(bus, scl=scl, sda=sda, freq=100000)
        found = i2c.scan()
    except Exception:
        found = []
    if found:
        hits.append((bus, sda_n, scl_n, found))
        if 0x24 in found and model_id is None:
            # The HM01B0 can need a moment and a fresh bus after a full
            # address sweep; an immediate repeated-start read can come back
            # as 0xFFFF even though the scan acknowledged 0x24.
            try:
                i2c.deinit()
            except Exception:
                pass
            time.sleep_ms(50)
            try:
                i2c = machine.I2C(bus, scl=Pin(scl_n), sda=Pin(sda_n), freq=100000)
                i2c.writeto(0x24, b"\x00\x00")
                time.sleep_ms(30)
                raw = i2c.readfrom(0x24, 2)
                model_id = (raw[0] << 8) | raw[1]
            except Exception:
                model_id = -1
    try:
        i2c.deinit()
    except Exception:
        pass
    machine.Pin(sda_n, machine.Pin.IN); machine.Pin(scl_n, machine.Pin.IN)
print("hits", hits)
print("model_id", model_id)
"""


def find_port(override: str | None) -> str:
    if override:
        return override
    for path in sorted(glob.glob(SERIAL_GLOB)):
        real = os.path.realpath(path)
        if os.path.exists(real):
            return path
    raise SystemExit(
        "no MicroPython by-id serial port found; refusing to guess from "
        "/dev/ttyACM* because that could select the Klipper arm Pico. "
        "Check that the camera Pico is plugged in and running MicroPython, "
        "or pass --port with its verified port."
    )


def read_until(ser, sentinel: bytes, timeout: float) -> bytes:
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if sentinel in buf:
                return buf
        else:
            time.sleep(0.02)
    return buf


def run_probe(port: str, timeout: float) -> str:
    import serial

    ser = serial.Serial(port, 115200, timeout=0.2)
    try:
        ser.write(b"\r\x03\x03")
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b"\x01")
        banner = read_until(ser, b">", 5)
        if b"raw REPL" not in banner:
            raise SystemExit(
                "the board did not answer as MicroPython (is it in BOOTSEL, or "
                f"running other firmware?). Reply was: {banner[:120]!r}")
        ser.write(PROBE.encode() + b"\x04")
        reply = read_until(ser, b"\x04>", timeout)
        ser.write(b"\x02")
        if b"\x04" not in reply:
            raise SystemExit(f"no reply from the board within {timeout:.0f}s")
        parts = reply.split(b"\x04")
        body = parts[0].removeprefix(b"OK").decode(errors="replace")
        error = parts[1] if len(parts) > 1 else b""
        if error.strip(b">\r\n\t "):
            raise SystemExit("the board reported an error:\n" + error.decode(errors="replace"))
        return body
    finally:
        ser.close()


def parse(body: str) -> dict:
    found: dict = {"pull_test": [], "hits": [], "model_id": None}
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("unique_id "):
            found["unique_id"] = line.split(None, 1)[1]
        elif line.startswith("version "):
            found["version"] = line.split(None, 1)[1]
        elif line.startswith("pull_test "):
            text = line.split(None, 1)[1]
            found["pull_test"] = eval(text) if text not in ("[]", "") else []
        elif line.startswith("hits "):
            text = line.split(None, 1)[1]
            found["hits"] = eval(text) if text not in ("[]", "") else []
        elif line.startswith("model_id "):
            text = line.split(None, 1)[1]
            try:
                found["model_id"] = int(text)
            except ValueError:
                found["model_id"] = None
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial port (default: discover a MicroPython board)")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the scan")
    parser.add_argument("--verbose", action="store_true", help="print the raw reply")
    args = parser.parse_args()

    port = find_port(args.port)
    print(f"port          {port}")
    body = run_probe(port, args.timeout)
    if args.verbose:
        print("--- raw reply ---")
        print(body)
        print("--- end ---")
    result = parse(body)
    print(f"board         {result.get('unique_id', '?')}")
    print(f"micropython   {result.get('version', '?')}")

    pull_test = result["pull_test"]
    if pull_test:
        print("GPIO pull test " + ", ".join(f"GP{n} (pulldown={lo}, pullup={hi})" for n, lo, hi in pull_test))
        print("              diagnostic only; it does not test continuity")
    else:
        print("GPIO pull test normal switching observed; this does not prove nothing is connected")

    hits = result["hits"]
    cameras = [h for h in hits if CAMERA_ADDRESS in h[3]]
    for bus, sda_n, scl_n, addresses in hits:
        names = ", ".join(hex(a) for a in addresses)
        print(f"i2c           bus {bus}  SDA GP{sda_n}  SCL GP{scl_n}  -> {names}")
    if cameras:
        model_id = result.get("model_id")
        if model_id == 0x01B0:
            print()
            print("VERDICT       HM01B0 camera confirmed: model ID 0x01B0 at I2C address 0x24.")
        elif model_id is not None and model_id >= 0:
            print()
            print(f"VERDICT       a device acknowledged at 0x24, but model ID 0x{model_id:04X} is not the HM01B0's 0x01B0.")
        else:
            print()
            print("VERDICT       a device acknowledged at 0x24, but its model ID could not be read.")
        return 0

    if hits:
        print("i2c           devices answered, but none at the expected camera address 0x24")
    else:
        print("i2c           no device acknowledged on the 13 tested RP2040 I2C pin pairs")
    print()
    print("VERDICT       no acknowledgement at 0x24. This test cannot determine whether the camera")
    print("              is disconnected, unpowered, miswired, faulty, or affected by a bus/scan issue.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
