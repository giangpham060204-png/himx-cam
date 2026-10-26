"""Moonraker client tests: fake transport plus one local HTTP server."""

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from arm_control.errors import (  # noqa: E402
    MoonrakerError,
    MoonrakerTimeout,
    MoonrakerUnavailable,
)
from arm_control.moonraker import (  # noqa: E402
    HttpTransport,
    MoonrakerClient,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_klippy_state_and_objects():
    transport = FakeTransport(
        [
            {"result": {"state": "ready"}},
            {"result": {"objects": ["manual_stepper m1", "query_endstops"]}},
        ]
    )
    client = MoonrakerClient(transport)

    assert client.klippy_state() == "ready"
    assert client.objects() == {"manual_stepper m1", "query_endstops"}


def test_run_script_posts_the_script():
    transport = FakeTransport([{"result": "ok"}])
    client = MoonrakerClient(transport)

    client.run_script("MANUAL_STEPPER STEPPER=m1 MOVE=10")

    assert transport.calls == [
        (
            "POST",
            "/printer/gcode/script",
            {"script": "MANUAL_STEPPER STEPPER=m1 MOVE=10"},
            120.0,
        )
    ]


def test_run_script_accepts_bare_ok():
    client = MoonrakerClient(FakeTransport(["ok"]))

    client.run_script("M114")


def test_run_script_refuses_unexpected_response():
    client = MoonrakerClient(FakeTransport([{"result": "error"}]))

    with pytest.raises(MoonrakerError, match="refused"):
        client.run_script("M114")


def test_emergency_stop_posts_and_accepts_ok():
    transport = FakeTransport([{"result": "ok"}])
    client = MoonrakerClient(transport)

    client.emergency_stop()

    assert transport.calls[0][:2] == ("POST", "/printer/emergency_stop")


def test_endstop_status():
    client = MoonrakerClient(
        FakeTransport([{"result": {"m1": "open", "m2": "TRIGGERED"}}])
    )

    assert client.endstop_status() == {"m1": "open", "m2": "TRIGGERED"}


def test_wait_ready_times_out_on_startup():
    client = MoonrakerClient(FakeTransport([{"result": {"state": "startup"}}]))

    with pytest.raises(MoonrakerTimeout, match="ready"):
        client.wait_ready(timeout=0.0)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/printer/info":
            self._send({"result": {"state": "ready"}})
        elif self.path == "/printer/objects/list":
            self._send({"result": {"objects": ["query_endstops"]}})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        assert body == {"script": "M114"}
        self._send({"result": "ok"})

    def _send(self, payload):
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        return None


def test_http_transport_round_trip():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = MoonrakerClient(
            HttpTransport(f"http://127.0.0.1:{server.server_port}", timeout=2.0)
        )
        assert client.klippy_state() == "ready"
        client.run_script("M114")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_http_transport_reports_unreachable():
    client = MoonrakerClient(
        HttpTransport("http://127.0.0.1:1", timeout=0.1)
    )

    with pytest.raises(MoonrakerUnavailable):
        client.klippy_state()


def test_http_transport_maps_bare_oserror(monkeypatch):
    import urllib.request

    def _raise(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    client = MoonrakerClient(HttpTransport("http://127.0.0.1:7125", timeout=0.1))

    with pytest.raises(MoonrakerUnavailable):
        client.klippy_state()
