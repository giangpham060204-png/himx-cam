#!/usr/bin/env python3
"""Local web console for the 3-axis arm.

The server is intentionally local-only by default. It is a thin UI over the
same ArmController used by the CLI, so every motion still goes through the
host-side homed/soft-limit/audit checks.
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arm_control.audit import AuditLog  # noqa: E402
from arm_control.config import AXIS_NAMES, LINEAR, RobotConfig, load_config  # noqa: E402
from arm_control.controller import ArmController  # noqa: E402
from arm_control.errors import ArmError  # noqa: E402
from arm_control.moonraker import MoonrakerClient  # noqa: E402
from run_context import build_context  # noqa: E402
from transport_factory import build_transport  # noqa: E402

WEB_DIR = ROOT / "web"
STATIC_FILES = {
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class WebError(Exception):
    """A bad request from the browser."""


def _axis_name(value: object) -> str:
    if not isinstance(value, str) or value not in AXIS_NAMES:
        raise WebError("axis must be m1, m2, or m3")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WebError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise WebError(f"{name} must be finite")
    return number


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name)


class WebState:
    """Thread-safe bridge between HTTP requests and the arm controller."""

    def __init__(self, controller: ArmController, token: str | None = None):
        self.controller = controller
        self.token = token or secrets.token_urlsafe(18)
        self.lock = threading.RLock()
        self.connected = False
        self.last_error = ""

    def _axis_meta(self, config: RobotConfig) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for name in AXIS_NAMES:
            axis = config.axes[name]
            result[name] = {
                "kind": axis.kind,
                "units": "mm" if axis.kind == LINEAR else "deg",
                "min": axis.min_pos,
                "max": axis.max_pos,
                "home": axis.home_position,
                "max_speed": axis.max_speed,
                "max_accel": axis.max_accel,
            }
        return result

    def status(self) -> dict[str, object]:
        with self.lock:
            config = self.controller.config
            payload: dict[str, object] = {
                "connected": self.connected,
                "klippy_state": "offline" if not self.connected else "unknown",
                "estopped": self.controller.estopped,
                "faulted": getattr(self.controller, "faulted", False),
                "fault_reason": getattr(self.controller, "fault_reason", ""),
                "homed": dict(self.controller.homed),
                "positions": dict(self.controller.positions),
                "tip": None,
                "config_hash": config.config_hash,
                "stack": config.stack,
                "home_order": list(config.home_order),
                "axes": self._axis_meta(config),
                "error": self.last_error,
            }
            if not self.connected:
                return payload
            try:
                payload.update(self.controller.status())
                payload["connected"] = True
                payload["error"] = self.last_error
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                payload["connected"] = False
                payload["klippy_state"] = "offline"
                payload["error"] = self.last_error
            return payload

    def connect(self) -> dict[str, object]:
        with self.lock:
            try:
                self.controller.connect()
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                raise
            self.connected = True
            self.last_error = ""
            return self.status()

    def _motion(self, action) -> dict[str, object]:
        with self.lock:
            if not self.connected:
                raise WebError("not connected to Moonraker; press Connect")
            try:
                action()
            except Exception as exc:
                self.last_error = str(exc)
                raise
            self.last_error = ""
            return self.status()

    def home(self, axis: object) -> dict[str, object]:
        if axis == "all":
            return self._motion(self.controller.home_all)
        name = _axis_name(axis)
        return self._motion(lambda: self.controller.home(name))

    def jog(
        self,
        axis: object,
        delta: object,
        speed: object = None,
        accel: object = None,
    ) -> dict[str, object]:
        name = _axis_name(axis)
        amount = _number(delta, "delta")
        if amount == 0:
            raise WebError("jog delta must not be zero")
        speed_value = _optional_number(speed, "speed")
        accel_value = _optional_number(accel, "accel")
        return self._motion(
            lambda: self.controller.jog(name, amount, speed_value, accel_value)
        )

    def move(
        self,
        axis: object,
        position: object,
        speed: object = None,
        accel: object = None,
    ) -> dict[str, object]:
        name = _axis_name(axis)
        target = _number(position, "position")
        speed_value = _optional_number(speed, "speed")
        accel_value = _optional_number(accel, "accel")
        return self._motion(
            lambda: self.controller.move_joint(
                name, target, speed_value, accel_value
            )
        )

    def estop(self) -> dict[str, object]:
        with self.lock:
            try:
                self.controller.stop()
            except Exception as exc:
                self.last_error = str(exc)
                raise
            self.last_error = ""
            return self.status()


class ArmRequestHandler(BaseHTTPRequestHandler):
    state: WebState
    token: str
    web_dir: Path

    server_version = "ArmWeb/1.0"

    def log_message(self, format: str, *args) -> None:
        # Keep the console quiet; the audit log is the durable record.
        return

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        self._send(
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _check_token(self) -> bool:
        supplied = self.headers.get("X-Arm-Token", "")
        if not secrets.compare_digest(supplied, self.token):
            self._json({"ok": False, "error": "invalid or missing token"}, 403)
            return False
        return True

    def _read_json(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise WebError("invalid Content-Length") from None
        if length <= 0:
            return {}
        if length > 64 * 1024:
            raise WebError("request body is too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WebError("request body must be valid JSON") from None
        if not isinstance(payload, dict):
            raise WebError("request body must be a JSON object")
        return payload

    def _serve_static(self, path: str) -> bool:
        if path == "/":
            filename = "index.html"
            content_type = "text/html; charset=utf-8"
            text = (self.web_dir / filename).read_text(encoding="utf-8")
            text = text.replace("__ARM_TOKEN__", self.token)
            self._send(text.encode("utf-8"), content_type)
            return True
        entry = STATIC_FILES.get(path)
        if entry is None:
            return False
        filename, content_type = entry
        file_path = self.web_dir / filename
        if not file_path.is_file():
            return False
        self._send(file_path.read_bytes(), content_type)
        return True

    def _dispatch(self, path: str) -> None:
        try:
            if path == "/api/status":
                self._json({"ok": True, "status": self.state.status()})
            elif path == "/api/connect":
                self._json({"ok": True, "status": self.state.connect()})
            elif path == "/api/home":
                payload = self._read_json()
                self._json(
                    {
                        "ok": True,
                        "status": self.state.home(payload.get("axis")),
                    }
                )
            elif path == "/api/jog":
                payload = self._read_json()
                self._json(
                    {
                        "ok": True,
                        "status": self.state.jog(
                            payload.get("axis"),
                            payload.get("delta"),
                            payload.get("speed"),
                            payload.get("accel"),
                        ),
                    }
                )
            elif path == "/api/move":
                payload = self._read_json()
                self._json(
                    {
                        "ok": True,
                        "status": self.state.move(
                            payload.get("axis"),
                            payload.get("position"),
                            payload.get("speed"),
                            payload.get("accel"),
                        ),
                    }
                )
            elif path == "/api/estop":
                self._json({"ok": True, "status": self.state.estop()})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except WebError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except ArmError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._check_token():
            return
        if path == "/api/status":
            self._dispatch(path)
            return
        if self._serve_static(path):
            return
        if path == "/favicon.ico":
            self._send(b"", "image/x-icon", 204)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._check_token():
            return
        self._dispatch(path)


def build_server(
    state: WebState,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundArmRequestHandler",
        (ArmRequestHandler,),
        {
            "state": state,
            "token": state.token,
            "web_dir": WEB_DIR,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local arm web console")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "arm_control" / "config" / "arm.toml",
    )
    parser.add_argument("--url", default="http://127.0.0.1:7125")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--transport", choices=["ws", "http"], default="ws")
    parser.add_argument("--operator", default="")
    parser.add_argument("--hardware-serial", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the console in the default browser",
    )
    return parser


def build_controller(args: argparse.Namespace) -> ArmController:
    config = load_config(args.config)
    log_path = args.log or (
        ROOT
        / "logs"
        / "arm"
        / f"web-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    )
    audit = AuditLog(log_path, context=build_context(ROOT, args.operator, args.hardware_serial))
    transport = build_transport(args.url, args.transport, args.timeout)
    return ArmController(config, MoonrakerClient(transport), audit)


def main(
    argv: list[str] | None = None,
    controller_factory=build_controller,
) -> int:
    args = build_parser().parse_args(argv)
    controller = controller_factory(args)
    state = WebState(controller)
    try:
        state.connect()
    except ArmError as exc:
        print(f"arm-web: {exc}; starting offline", file=sys.stderr)

    server = build_server(state, host=args.host, port=args.port)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"arm-web: {url}")
    print("arm-web: physical e-stop remains the primary stop")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\narm-web: stopped", file=sys.stderr)
    finally:
        server.server_close()
        audit = getattr(controller, "audit", None)
        if audit is not None and hasattr(audit, "close"):
            audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
