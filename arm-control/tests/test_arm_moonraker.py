"""Moonraker client tests: fake transport plus one local HTTP server."""

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.errors import (  # noqa: E402
    MoonrakerError,
    MoonrakerTimeout,
    MoonrakerUnavailable,
)
from src.moonraker import (  # noqa: E402
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


def test_has_object_queries_objects_omitted_from_list():
    transport = FakeTransport(
        [{"result": {"status": {"manual_stepper m1": {}}}}]
    )
    client = MoonrakerClient(transport)

    assert client.has_object("manual_stepper m1") is True
    assert transport.calls == [
        (
            "GET",
            "/printer/objects/query?manual_stepper%20m1",
            None,
            5.0,
        )
    ]


def test_has_object_returns_false_when_absent():
    client = MoonrakerClient(FakeTransport([{"result": {"status": {}}}]))

    assert client.has_object("manual_stepper m1") is False


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


def test_wait_ready_retries_while_klippy_connects():
    transport = FakeTransport(
        [
            MoonrakerError("Method not found"),
            {"result": {"state": "ready"}},
        ]
    )
    client = MoonrakerClient(transport)

    client.wait_ready(timeout=1.0)

    assert len(transport.calls) == 2


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


class NotifyingTransport:
    def __init__(self):
        self.handler = None
        self.calls = []

    def set_notification_handler(self, handler):
        self.handler = handler

    def request(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body, timeout))
        if path == "/server/info":
            return {"result": {"moonraker_version": "v0.9.3"}}
        if path == "/printer/firmware_restart":
            return {"result": "ok"}
        if path == "/printer/gcode/script":
            self.handler("notify_gcode_response", ["// m1: open"])
            return {"result": "ok"}
        raise AssertionError(path)


def test_notification_handler_and_gcode_buffer():
    transport = NotifyingTransport()
    client = MoonrakerClient(transport)
    seen = []
    client.set_notification_handler(
        lambda method, params: seen.append((method, params))
    )

    transport.handler("notify_gcode_response", ["// hello"])
    transport.handler("notify_klippy_shutdown", [])

    assert client.gcode_responses() == ["// hello"]
    assert seen[0][0] == "notify_gcode_response"
    assert seen[1][0] == "notify_klippy_shutdown"


def test_server_info_and_firmware_restart():
    transport = NotifyingTransport()
    client = MoonrakerClient(transport)

    assert client.server_info() == {"moonraker_version": "v0.9.3"}
    client.firmware_restart()

    assert transport.calls[0][:2] == ("GET", "/server/info")
    assert transport.calls[1][:2] == ("POST", "/printer/firmware_restart")


def test_query_endstops_text_parses_ws_responses():
    transport = NotifyingTransport()
    client = MoonrakerClient(transport)

    assert client.query_endstops_text() == {"m1": "open"}


def test_query_endstops_text_strips_comment_prefix():
    transport = NotifyingTransport()
    client = MoonrakerClient(transport)

    assert client.query_endstops_text() == {"m1": "open"}
    assert client.gcode_responses() == ["// m1: open"]


def test_supports_notifications_reflects_transport():
    assert MoonrakerClient(NotifyingTransport()).supports_notifications() is False

    class WsLike:
        supports_notifications = True

        def request(self, method, path, body=None, timeout=5.0):
            raise AssertionError("unused")

    assert MoonrakerClient(WsLike()).supports_notifications() is True


def test_gcode_store_defaults_to_spec_count():
    transport = FakeTransport([{"result": {"gcode_store": []}}])
    client = MoonrakerClient(transport)

    assert client.gcode_store() == {"gcode_store": []}
    assert transport.calls == [
        ("GET", "/server/gcode_store?count=100", None, 5.0)
    ]
