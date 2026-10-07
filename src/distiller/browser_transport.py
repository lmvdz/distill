"""Resume newly attached Chrome targets, with optional direct local CDP transport.

Jev's DOM observation, freshness checks and action executor remain unchanged.
"""

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

import httpx


class CDP:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.sequence = 0

    def call(self, method: str, session_id: str | None = None, **params: Any) -> dict[str, Any]:
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        if session_id:
            request["sessionId"] = session_id
        self.websocket.send(json.dumps(request))
        deadline = time.monotonic() + 30
        while (remaining := deadline - time.monotonic()) > 0:
            reply = json.loads(self.websocket.recv(timeout=remaining))
            if reply.get("id") != self.sequence:
                continue  # Browser events do not block the reader while a command is pending.
            if "error" in reply:
                raise RuntimeError(
                    f"CDP {method} failed: {reply['error'].get('message', 'unknown error')}"
                )
            return reply.get("result", {})
        raise TimeoutError(f"CDP {method} did not answer within 30 seconds")


def resumed_call(send: Callable, method: str, session_id: str | None = None, **params: Any) -> dict:
    result = send(method, session_id=session_id, **params)
    if method == "Target.attachToTarget" and result.get("sessionId"):
        send("Runtime.runIfWaitingForDebugger", session_id=result["sessionId"])
    return result


@contextmanager
def _direct_connection() -> Iterator[Callable | None]:
    base = os.environ.get("BU_CDP_URL")
    if not base or urlparse(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        yield None
        return
    from websockets.sync.client import connect

    response = httpx.get(base.rstrip("/") + "/json/version", timeout=10)
    response.raise_for_status()
    endpoint = response.json()["webSocketDebuggerUrl"]
    if urlparse(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local Chrome returned a non-local WebSocket endpoint")
    with connect(endpoint, open_timeout=10, close_timeout=3, max_size=4 * 1024 * 1024) as websocket:
        yield CDP(websocket).call


@contextmanager
def local_cdp() -> Iterator[None]:
    from jev_ultrafast import browser as browser_module

    old_cdp, old_ensure = browser_module.cdp, browser_module.ensure_daemon
    with _direct_connection() as direct:
        selected = direct or old_cdp
        browser_module.cdp = lambda method, session_id=None, **params: resumed_call(
            selected, method, session_id=session_id, **params
        )
        if direct:
            browser_module.ensure_daemon = lambda: None
        try:
            yield
        finally:
            browser_module.cdp = old_cdp
            browser_module.ensure_daemon = old_ensure
