"""Local arm web UI: API, safety refusals, token, and static page."""

import json
import pathlib
import re
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import arm_web  # noqa: E402
from src.config import AXIS_NAMES, load_config  # noqa: E402
from src.errors import NotHomedError  # noqa: E402

CONFIG = load_config(ROOT / "src" / "config" / "arm.toml")


class FakeController:
    def __init__(self):
        self.config = CONFIG
        self.homed = {name: False for name in AXIS_NAMES}
        self.positions = {name: None for name in AXIS_NAMES}
        self.estopped = False
        self.faulted = False
        self.fault_reason = ""
        self.stop_requested = False
        self.calls = []

    def connect(self):
        self.calls.append(("connect",))

    def status(self):
        return {
            "klippy_state": "ready",
            "homed": dict(self.homed),
            "positions": dict(self.positions),
            "tip": None,
            "estopped": self.estopped,
            "faulted": self.faulted,
            "fault_reason": self.fault_reason,
            "stop_requested": self.stop_requested,
            "config_hash": self.config.config_hash,
        }

    def home(self, axis):
        self.calls.append(("home", axis))
        self.homed[axis] = True
        self.positions[axis] = self.config.axes[axis].home_position

    def home_all(self):
        self.calls.append(("home_all",))
        for axis in AXIS_NAMES:
            self.homed[axis] = True
            self.positions[axis] = self.config.axes[axis].home_position

    def jog(self, axis, delta, speed=None, accel=None):
        if not self.homed[axis]:
            raise NotHomedError(f"axis {axis} is not homed")
        self.calls.append(("jog", axis, delta, speed, accel))
        self.positions[axis] += delta

    def move_joint(self, axis, position, speed=None, accel=None):
        if not self.homed[axis]:
            raise NotHomedError(f"axis {axis} is not homed")
        self.calls.append(("move", axis, position, speed, accel))
        self.positions[axis] = position

    def stop(self):
        self.calls.append(("stop",))
        self.estopped = True
        self.homed = {name: False for name in AXIS_NAMES}
        self.positions = {name: None for name in AXIS_NAMES}

    def restart(self):
        self.calls.append(("restart",))
        self.estopped = False
        self.faulted = False
        self.fault_reason = ""
        self.homed = {name: False for name in AXIS_NAMES}
        self.positions = {name: None for name in AXIS_NAMES}


@pytest.fixture
def web():
    controller = FakeController()
    state = arm_web.WebState(controller, token="test-token")
    server = arm_web.build_server(state, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state, controller, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(base, path, method="GET", body=None, token="test-token"):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Arm-Token"] = token
    request = urllib.request.Request(
        base + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_status_offline_lists_axes(web):
    _, controller, base = web

    status, payload = _request(base, "/api/status")

    assert status == 200
    assert payload["ok"] is True
    assert payload["status"]["connected"] is False
    assert payload["status"]["config_hash"] == CONFIG.config_hash
    assert payload["status"]["axes"]["m1"]["units"] == "mm"
    assert payload["status"]["axes"]["m2"]["units"] == "deg"
    assert payload["status"]["axes"]["m1"]["min"] == 0.0
    assert controller.calls == []


def test_connect_then_status(web):
    _, controller, base = web

    status, payload = _request(base, "/api/connect", method="POST")

    assert status == 200
    assert payload["status"]["connected"] is True
    assert payload["status"]["klippy_state"] == "ready"
    assert controller.calls == [("connect",)]


def test_jog_requires_homed_axis(web):
    _, controller, base = web
    _request(base, "/api/connect", method="POST")

    status, payload = _request(
        base, "/api/jog", method="POST", body={"axis": "m1", "delta": 1.0}
    )

    assert status == 409
    assert payload["ok"] is False
    assert "not homed" in payload["error"]
    assert controller.calls == [("connect",)]


def test_home_and_jog_go_through_controller(web):
    _, controller, base = web
    _request(base, "/api/connect", method="POST")
    _request(base, "/api/home", method="POST", body={"axis": "m1"})

    status, payload = _request(
        base,
        "/api/jog",
        method="POST",
        body={"axis": "m1", "delta": 2.0, "speed": 5.0, "accel": 20.0},
    )

    assert status == 200
    assert payload["status"]["homed"]["m1"] is True
    assert controller.calls == [
        ("connect",),
        ("home", "m1"),
        ("jog", "m1", 2.0, 5.0, 20.0),
    ]


def test_home_all_and_absolute_move(web):
    _, controller, base = web
    _request(base, "/api/connect", method="POST")
    _request(base, "/api/home", method="POST", body={"axis": "all"})

    status, payload = _request(
        base,
        "/api/move",
        method="POST",
        body={"axis": "m2", "position": 10.0},
    )

    assert status == 200
    assert payload["status"]["positions"]["m2"] == 10.0
    assert controller.calls == [
        ("connect",),
        ("home_all",),
        ("move", "m2", 10.0, None, None),
    ]


def test_estop_calls_stop_and_latches(web):
    _, controller, base = web
    _request(base, "/api/connect", method="POST")

    status, payload = _request(base, "/api/estop", method="POST")

    assert status == 200
    assert payload["status"]["estopped"] is True
    assert controller.calls == [("connect",), ("stop",)]


def test_restart_recovers_estop_and_requires_token(web):
    _, controller, base = web
    _request(base, "/api/connect", method="POST")
    _request(base, "/api/estop", method="POST")

    status, payload = _request(base, "/api/restart", method="POST")
    status_bad, _ = _request(
        base, "/api/restart", method="POST", token="wrong"
    )

    assert status == 200
    assert status_bad == 403
    assert payload["ok"] is True
    assert payload["status"]["estopped"] is False
    assert payload["status"]["homed"]["m1"] is False
    assert controller.calls == [("connect",), ("stop",), ("restart",)]


def test_restart_clears_fault_state(web):
    _, controller, base = web
    controller.faulted = True
    controller.fault_reason = "Moonraker is missing objects: query_endstops"

    status, payload = _request(base, "/api/restart", method="POST")

    assert status == 200
    assert payload["status"]["faulted"] is False
    assert payload["status"]["fault_reason"] == ""
    assert controller.calls == [("restart",)]


def test_post_requires_token(web):
    _, controller, base = web

    status, payload = _request(
        base, "/api/connect", method="POST", token="wrong-token"
    )

    assert status == 403
    assert payload["ok"] is False
    assert controller.calls == []


def test_bad_json_is_400(web):
    _, controller, base = web
    request = urllib.request.Request(
        base + "/api/jog",
        data=b"{not json",
        method="POST",
        headers={"X-Arm-Token": "test-token"},
    )

    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
        assert exc.code == 400
        assert payload["ok"] is False
    else:
        raise AssertionError("bad JSON was accepted")

    assert controller.calls == []


def test_index_contains_token_and_static_assets(web):
    _, _, base = web

    with urllib.request.urlopen(base + "/", timeout=5) as response:
        html = response.read().decode()
    assert response.status == 200
    assert "test-token" in html
    assert "Arm control" in html

    for path, content_type in (
        ("/style.css", "text/css"),
        ("/app.js", "application/javascript"),
    ):
        with urllib.request.urlopen(base + path, timeout=5) as response:
            body = response.read()
        assert response.status == 200
        assert content_type in response.headers.get("Content-Type", "")
        assert body


def test_index_has_home_up_down_and_estop_controls(web):
    _, _, base = web

    with urllib.request.urlopen(base + "/", timeout=5) as response:
        html = response.read().decode()

    assert html.count("data-jog-axis=") == 6
    assert 'data-jog-axis="m1"' in html
    assert 'data-jog-axis="m2"' in html
    assert 'data-jog-axis="m3"' in html
    assert 'aria-label="M1 up"' in html
    assert 'aria-label="M1 down"' in html
    assert 'aria-label="M2 left"' in html
    assert 'aria-label="M2 right"' in html
    assert 'aria-label="M3 left"' in html
    assert 'aria-label="M3 right"' in html
    assert 'id="estop"' in html
    assert 'id="home"' in html
    assert 'id="stack-state"' in html
    assert 'id="recover"' in html
    assert 'aria-label="Home all axes"' in html
    assert 'id="connect"' not in html
    assert "data-stick" not in html


def test_unknown_path_is_404(web):
    _, _, base = web

    status, payload = _request(base, "/api/nope")

    assert status == 404
    assert payload["ok"] is False


def test_status_reports_fault(web):
    _, controller, base = web
    controller.faulted = True
    controller.fault_reason = "Klipper is 'shutdown', not ready"

    status, payload = _request(base, "/api/status")

    assert status == 200
    assert payload["status"]["faulted"] is True
    assert "shutdown" in payload["status"]["fault_reason"]


def test_status_reports_controlled_stop(web):
    state, controller, base = web
    controller.stop_requested = True

    status, payload = _request(base, "/api/status")

    assert status == 200
    assert payload["status"]["stop_requested"] is True


CORS_ORIGIN = "http://192.168.7.42"


@pytest.fixture
def web_cors():
    controller = FakeController()
    state = arm_web.WebState(controller, token="test-token")
    server = arm_web.build_server(
        state, host="127.0.0.1", port=0, cors_origins=(CORS_ORIGIN,)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _preflight(base, origin=None):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        base + "/api/status", method="OPTIONS", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_preflight_allows_pinned_origin(web_cors):
    status, headers = _preflight(web_cors, origin=CORS_ORIGIN)

    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == CORS_ORIGIN
    assert "X-Arm-Token" in headers["Access-Control-Allow-Headers"]
    assert "POST" in headers["Access-Control-Allow-Methods"]


def test_preflight_rejects_unpinned_origin(web_cors):
    status, payload = _preflight(web_cors, origin="http://evil.example")

    assert status == 403
    assert payload["ok"] is False


def test_preflight_without_origin_is_rejected(web_cors):
    status, payload = _preflight(web_cors)

    assert status == 403


def test_api_response_echoes_allowed_origin(web_cors):
    request = urllib.request.Request(
        web_cors + "/api/status",
        headers={"X-Arm-Token": "test-token", "Origin": CORS_ORIGIN},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read())

    assert body["ok"] is True
    assert response.headers["Access-Control-Allow-Origin"] == CORS_ORIGIN


def test_api_request_from_unpinned_origin_still_serves(web_cors):
    request = urllib.request.Request(
        web_cors + "/api/status",
        headers={"X-Arm-Token": "test-token", "Origin": "http://evil.example"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read())

    assert body["ok"] is True
    assert "Access-Control-Allow-Origin" not in response.headers


def test_cors_is_off_by_default(web):
    _, _, base = web
    request = urllib.request.Request(
        base + "/api/status",
        headers={"X-Arm-Token": "test-token", "Origin": CORS_ORIGIN},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        json.loads(response.read())

    assert "Access-Control-Allow-Origin" not in response.headers


def test_show_token_prints_token(capsys):
    class StoppedServer:
        daemon_threads = True
        server_port = 8765

        def __init__(self, state, host, port, cors_origins=()):
            self.token = state.token

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    code = arm_web.main(
        ["--show-token"],
        controller_factory=lambda args: FakeController(),
        server_factory=StoppedServer,
    )

    out = capsys.readouterr().out
    assert code == 0
    token_line = re.search(r"^arm-web: token (\S+)$", out, re.M)
    assert token_line is not None, out
    assert len(token_line.group(1)) >= 20  # token_urlsafe(18) byte length
    assert out.index("arm-web: http") < out.index("arm-web: token ")
