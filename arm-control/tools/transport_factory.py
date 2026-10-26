"""Select the Moonraker transport for the operator tools."""

from src.moonraker import HttpTransport


def build_transport(url: str, transport: str = "ws", timeout: float = 30.0):
    if transport == "http":
        return HttpTransport(url, timeout=timeout)
    if transport != "ws":
        raise ValueError(f"unknown transport {transport!r}")
    from ws_transport import WebSocketTransport

    return WebSocketTransport(url, timeout=timeout)
