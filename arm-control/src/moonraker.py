"""Moonraker HTTP client with an injectable transport."""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Protocol

from .errors import MoonrakerError, MoonrakerTimeout, MoonrakerUnavailable


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> object: ...


class HttpTransport:
    def __init__(
        self, base_url: str = "http://127.0.0.1:7125", timeout: float = 5.0
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> object:
        url = self.base_url + path
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                payload = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise MoonrakerError(
                f"{method} {path} returned HTTP {exc.code}: {detail}"
            ) from None
        except (TimeoutError, socket.timeout) as exc:
            raise MoonrakerTimeout(f"{method} {path} timed out: {exc}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise MoonrakerTimeout(
                    f"{method} {path} timed out: {exc.reason}"
                ) from None
            raise MoonrakerUnavailable(
                f"cannot reach Moonraker at {url}: {exc.reason}"
            ) from None
        except OSError as exc:
            raise MoonrakerUnavailable(
                f"cannot reach Moonraker at {url}: {exc}"
            ) from None

        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MoonrakerError(
                f"Moonraker returned invalid JSON for {path}: {exc}"
            ) from None


class MoonrakerClient:
    def __init__(self, transport: Transport):
        self.transport = transport
        self._notification_handler: Callable[[str, object], None] | None = None
        self._gcode_responses: deque[str] = deque(maxlen=200)
        setter = getattr(transport, "set_notification_handler", None)
        if setter is not None:
            setter(self._handle_notification)

    def set_notification_handler(
        self, handler: Callable[[str, object], None]
    ) -> None:
        self._notification_handler = handler

    def _handle_notification(self, method: str, params: object) -> None:
        if method == "notify_gcode_response":
            if isinstance(params, (list, tuple)) and params:
                self._gcode_responses.append(str(params[0]))
            else:
                self._gcode_responses.append(str(params))
        if self._notification_handler is not None:
            self._notification_handler(method, params)

    def gcode_responses(self) -> list[str]:
        return list(self._gcode_responses)

    def server_info(self) -> dict[str, Any]:
        result = self._result(
            self.transport.request("GET", "/server/info")
        )
        if not isinstance(result, dict):
            raise MoonrakerError(f"unexpected server info: {result!r}")
        return result

    def firmware_restart(self) -> None:
        response = self.transport.request(
            "POST", "/printer/firmware_restart"
        )
        if response == "ok" or (
            isinstance(response, dict) and response.get("result") == "ok"
        ):
            return
        raise MoonrakerError(
            f"Moonraker refused firmware restart: {response!r}"
        )

    def supports_notifications(self) -> bool:
        return bool(getattr(self.transport, "supports_notifications", False))

    def query_endstops_text(self, timeout: float = 2.0) -> dict[str, str]:
        self._gcode_responses.clear()
        self.run_script("QUERY_ENDSTOPS")
        deadline = time.monotonic() + timeout
        pattern = re.compile(r"^([^:]+):\s*(open|TRIGGERED)$")
        while time.monotonic() < deadline:
            result = {}
            for line in self.gcode_responses():
                stripped = line.strip()
                if stripped.startswith("//"):
                    stripped = stripped[2:].lstrip()
                match = pattern.match(stripped)
                if match is not None:
                    result[match.group(1).strip()] = match.group(2)
            if result:
                return result
            time.sleep(0.05)
        raise MoonrakerTimeout("no QUERY_ENDSTOPS text response")

    def _result(self, payload: object) -> Any:
        if not isinstance(payload, dict):
            raise MoonrakerError(f"unexpected Moonraker response: {payload!r}")
        if "error" in payload:
            raise MoonrakerError(str(payload["error"]))
        if "result" not in payload:
            raise MoonrakerError(f"Moonraker response has no result: {payload!r}")
        return payload["result"]

    def printer_info(self) -> dict[str, Any]:
        result = self._result(self.transport.request("GET", "/printer/info"))
        if not isinstance(result, dict):
            raise MoonrakerError(f"unexpected printer info: {result!r}")
        return result

    def klippy_state(self) -> str:
        return str(self.printer_info().get("state", ""))

    def objects(self) -> set[str]:
        result = self._result(
            self.transport.request("GET", "/printer/objects/list")
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("objects"), list
        ):
            raise MoonrakerError(f"unexpected object list: {result!r}")
        return {str(name) for name in result["objects"]}

    def endstop_status(self) -> dict[str, str]:
        result = self._result(
            self.transport.request("GET", "/printer/query_endstops/status")
        )
        if not isinstance(result, dict):
            raise MoonrakerError(f"unexpected endstop status: {result!r}")
        return {str(name): str(value) for name, value in result.items()}

    def run_script(self, script: str, timeout: float = 120.0) -> None:
        response = self.transport.request(
            "POST",
            "/printer/gcode/script",
            {"script": script},
            timeout=timeout,
        )
        if response == "ok" or (
            isinstance(response, dict) and response.get("result") == "ok"
        ):
            return
        raise MoonrakerError(f"Moonraker refused script: {response!r}")

    def emergency_stop(self) -> None:
        response = self.transport.request("POST", "/printer/emergency_stop")
        if response == "ok" or (
            isinstance(response, dict) and response.get("result") == "ok"
        ):
            return
        raise MoonrakerError(f"Moonraker refused emergency stop: {response!r}")

    def gcode_store(self, count: int = 100) -> object:
        return self._result(
            self.transport.request("GET", f"/server/gcode_store?count={count}")
        )

    def wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = self.klippy_state()
            if state == "ready":
                return
            if state in {"shutdown", "error"}:
                raise MoonrakerError(f"Klipper is {state}, not ready")
            if time.monotonic() >= deadline:
                raise MoonrakerTimeout(
                    f"Klipper did not become ready within {timeout:g}s"
                )
            time.sleep(0.2)
