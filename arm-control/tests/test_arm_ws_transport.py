"""Moonraker WebSocket transport: identify, requests, notifications, reconnect."""

import json
import pathlib
import sys
import threading
import time

import pytest
from websockets.sync.server import serve

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from src.errors import MoonrakerError  # noqa: E402
from ws_transport import WebSocketTransport  # noqa: E402


class FakeMoonraker:
    def __init__(self, behavior="normal"):
        self.behavior = behavior
        self.connections = 0
        self.notified = threading.Event()
        self.server = serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def _send(self, ws, payload):
        ws.send(json.dumps(payload))

    def _handler(self, ws):
        self.connections += 1
        for raw in ws:
            msg = json.loads(raw)
            method = msg.get("method")
            if method == "server.connection.identify":
                self._send(ws, {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"})
            elif method == "printer.info":
                self._send(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"state": "ready"},
                    },
                )
                if self.behavior == "notify":
                    self._send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "method": "notify_klippy_shutdown",
                        },
                    )
                    self.notified.set()
                if self.behavior == "close_after_info":
                    ws.close()
                    break
            else:
                self._send(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"message": "unknown"},
                    },
                )

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2.0)


def test_identify_and_request():
    fake = FakeMoonraker()
    transport = WebSocketTransport(f"http://127.0.0.1:{fake.port}", timeout=2.0)
    try:
        assert transport.request("GET", "/printer/info") == {
            "result": {"state": "ready"}
        }
        assert fake.connections == 1
    finally:
        transport.close()
        fake.close()


def test_notification_handler_receives_shutdown():
    fake = FakeMoonraker(behavior="notify")
    transport = WebSocketTransport(f"http://127.0.0.1:{fake.port}", timeout=2.0)
    seen = []
    transport.set_notification_handler(
        lambda method, params: seen.append((method, params))
    )
    try:
        transport.request("GET", "/printer/info")
        assert fake.notified.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen[0][0] == "notify_klippy_shutdown"
    finally:
        transport.close()
        fake.close()


def test_reconnects_after_server_close():
    fake = FakeMoonraker(behavior="close_after_info")
    transport = WebSocketTransport(f"http://127.0.0.1:{fake.port}", timeout=2.0)
    try:
        transport.request("GET", "/printer/info")
        time.sleep(0.1)
        assert transport.request("GET", "/printer/info") == {
            "result": {"state": "ready"}
        }
        assert fake.connections >= 2
    finally:
        transport.close()
        fake.close()


def test_unknown_route_raises():
    fake = FakeMoonraker()
    transport = WebSocketTransport(f"http://127.0.0.1:{fake.port}", timeout=2.0)
    try:
        with pytest.raises(MoonrakerError, match="unsupported"):
            transport.request("GET", "/nope")
    finally:
        transport.close()
        fake.close()


def test_bad_gcode_store_count_raises_moonraker_error():
    transport = WebSocketTransport("http://127.0.0.1:9", timeout=0.5)
    try:
        with pytest.raises(MoonrakerError, match="unsupported"):
            transport.request("GET", "/server/gcode_store?count=abc")
    finally:
        transport.close()


def test_stale_reader_does_not_close_fresh_connection():
    """A reader for a superseded socket must not mark the new one closed."""

    class DeadSocket:
        def recv(self):
            raise OSError("socket closed")

        def close(self):
            pass

    transport = WebSocketTransport("http://127.0.0.1:9", timeout=0.5)
    old, new = DeadSocket(), DeadSocket()
    transport._ws = new
    transport._closed = False
    try:
        transport._read_loop(old)
        assert transport._closed is False
        assert transport._ws is new
        transport._read_loop(new)
        assert transport._closed is True
    finally:
        transport.close()


def test_notification_handler_exception_does_not_kill_reader():
    fake = FakeMoonraker(behavior="notify")
    transport = WebSocketTransport(f"http://127.0.0.1:{fake.port}", timeout=2.0)
    calls = []

    def bad_handler(method, params):
        calls.append(method)
        raise RuntimeError("subscriber boom")

    transport.set_notification_handler(bad_handler)
    try:
        assert transport.request("GET", "/printer/info") == {
            "result": {"state": "ready"}
        }
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls, "expected the failing handler to be invoked"
        assert transport.request("GET", "/printer/info") == {
            "result": {"state": "ready"}
        }
        assert fake.connections == 1, (
            "reader loop died on handler exception and forced a reconnect"
        )
    finally:
        transport.close()
        fake.close()


def test_supports_notifications_flag():
    assert WebSocketTransport.supports_notifications is True


def test_malformed_id_frame_does_not_kill_reader():
    transport = WebSocketTransport("http://127.0.0.1:9", timeout=0.5)
    event = threading.Event()
    holder: dict = {}
    with transport._pending_lock:
        transport._pending[7] = (event, holder)
    frames = [
        json.dumps({"jsonrpc": "2.0", "id": "not-an-int", "result": "x"}),
        json.dumps(
            {"jsonrpc": "2.0", "id": 7, "result": {"state": "ready"}}
        ),
    ]

    class SeqSocket:
        def __init__(self):
            self.index = 0

        def recv(self):
            if self.index < len(frames):
                raw = frames[self.index]
                self.index += 1
                return raw
            return ""

        def close(self):
            pass

    sock = SeqSocket()
    transport._ws = sock
    transport._closed = False
    try:
        transport._read_loop(sock)
    finally:
        transport.close()

    assert event.is_set(), "valid response after malformed id was not delivered"
    assert holder.get("result") == {"state": "ready"}
