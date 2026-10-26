"""Moonraker HTTP client with an injectable transport."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

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

    def gcode_store(self, count: int = 50) -> object:
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
