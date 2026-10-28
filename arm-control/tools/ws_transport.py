"""Moonraker JSON-RPC over a persistent WebSocket.

The arm package stays standard-library-only; this transport lives in tools
and is injected into MoonrakerClient through the existing Transport protocol.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import websocket

from src.errors import (
    MoonrakerError,
    MoonrakerTimeout,
    MoonrakerUnavailable,
)

NotificationHandler = Callable[[str, object], None]


class WebSocketTransport:
    supports_notifications = True

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7125",
        timeout: float = 5.0,
        client_name: str | None = None,
    ):
        self.ws_url = self._ws_url(base_url)
        self.timeout = timeout
        # Moonraker rejects a second agent with the same name.  Web, bench,
        # and CLI processes must therefore identify uniquely.
        self.client_name = client_name or f"src-{os.getpid()}-{id(self):x}"
        self._ws: websocket.WebSocket | None = None
        self._reader: threading.Thread | None = None
        self._closed = True
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self._ids = itertools.count(1)
        self._notification_handler: NotificationHandler | None = None

    @staticmethod
    def _ws_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.startswith(("ws://", "wss://")):
            return base if base.endswith("/websocket") else base + "/websocket"
        if base.startswith("http://"):
            base = "ws://" + base[7:]
            return base if base.endswith("/websocket") else base + "/websocket"
        if base.startswith("https://"):
            base = "wss://" + base[8:]
            return base if base.endswith("/websocket") else base + "/websocket"
        raise MoonrakerError(f"unsupported Moonraker URL: {base_url!r}")

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handler = handler

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> object:
        rpc_method, params = self._route(method, path, body)
        for attempt in range(2):
            self._ensure_connected()
            try:
                result = self._rpc(rpc_method, params, timeout)
                return {"result": result}
            except (websocket.WebSocketConnectionClosedException, OSError) as exc:
                self._disconnect()
                if attempt == 1:
                    raise MoonrakerUnavailable(
                        f"WebSocket disconnected: {exc}"
                    ) from None
        raise AssertionError("unreachable")

    def _route(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> tuple[str, object | None]:
        if method == "GET" and path == "/printer/info":
            return "printer.info", None
        if method == "GET" and path == "/printer/objects/list":
            return "printer.objects.list", None
        if method == "GET" and path.startswith("/printer/objects/query?"):
            query = parse_qs(
                urlparse(path).query, keep_blank_values=True
            )
            if not query:
                raise MoonrakerError(
                    f"unsupported WebSocket route: {method} {path}"
                )
            return "printer.objects.query", {
                "objects": {name: None for name in query}
            }
        if method == "GET" and path == "/printer/query_endstops/status":
            return "printer.query_endstops.status", None
        if method == "GET" and path == "/server/info":
            return "server.info", None
        if method == "POST" and path == "/printer/gcode/script":
            return "printer.gcode.script", body
        if method == "POST" and path == "/printer/emergency_stop":
            return "printer.emergency_stop", None
        if method == "POST" and path == "/printer/firmware_restart":
            return "printer.firmware_restart", None
        if method == "GET" and path.startswith("/server/gcode_store"):
            query = parse_qs(urlparse(path).query)
            try:
                count = int(query.get("count", ["50"])[0])
            except ValueError:
                raise MoonrakerError(
                    f"unsupported WebSocket route: {method} {path}"
                ) from None
            return "server.gcode_store", {"count": count}
        raise MoonrakerError(f"unsupported WebSocket route: {method} {path}")

    def _ensure_connected(self) -> None:
        with self._connect_lock:
            if self._ws is not None and not self._closed:
                return
            try:
                ws = websocket.create_connection(
                    self.ws_url,
                    timeout=self.timeout,
                    enable_multithread=True,
                )
            except Exception as exc:
                raise MoonrakerUnavailable(
                    f"cannot reach Moonraker WebSocket at {self.ws_url}: {exc}"
                ) from None
            self._ws = ws
            self._closed = False
            self._reader = threading.Thread(
                target=self._read_loop, args=(ws,), daemon=True
            )
            self._reader.start()
            try:
                self._rpc(
                    "server.connection.identify",
                    {
                        "client_name": self.client_name,
                        "version": "1.0",
                        "type": "agent",
                        "url": "https://localhost/arm-control",
                    },
                    self.timeout,
                )
            except Exception:
                self._disconnect()
                raise

    def _rpc(
        self,
        method: str,
        params: object | None,
        timeout: float,
    ) -> object:
        if self._ws is None:
            raise MoonrakerUnavailable("WebSocket is not connected")
        request_id = next(self._ids)
        event = threading.Event()
        holder: dict[str, Any] = {}
        with self._pending_lock:
            self._pending[request_id] = (event, holder)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
        }
        if params is not None:
            payload["params"] = params
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload))
            if not event.wait(timeout):
                raise MoonrakerTimeout(
                    f"WebSocket {method} timed out after {timeout:g}s"
                )
            if "error" in holder:
                raise MoonrakerError(str(holder["error"]))
            return holder.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _read_loop(self, ws=None) -> None:
        if ws is None:
            ws = self._ws
        try:
            while ws is not None and not self._closed:
                raw = ws.recv()
                if raw in ("", b""):
                    break
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in message:
                    try:
                        request_id = int(message["id"])
                    except (ValueError, TypeError):
                        continue
                    with self._pending_lock:
                        item = self._pending.get(request_id)
                    if item is not None:
                        event, holder = item
                        holder.update(message)
                        event.set()
                    continue
                method = message.get("method")
                if method and self._notification_handler is not None:
                    try:
                        self._notification_handler(
                            str(method), message.get("params", [])
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if self._ws is ws:
                self._closed = True

    def _disconnect(self) -> None:
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self) -> None:
        self._disconnect()
