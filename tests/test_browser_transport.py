import json

import pytest

from distiller.browser_transport import CDP, resumed_call


class Websocket:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, timeout):
        assert timeout <= 30
        return json.dumps(next(self.replies))


def test_session_requests_keep_unique_ids_and_ignore_browser_events():
    websocket = Websocket(
        [
            {"method": "Target.attachedToTarget", "params": {}},
            {"id": 1, "result": {"value": 42}},
            {"id": 2, "result": {}},
        ]
    )
    client = CDP(websocket)
    assert client.call("Runtime.evaluate", session_id="observed-session", expression="1+1") == {
        "value": 42
    }
    client.call("Target.closeTarget", targetId="owned-target")
    assert websocket.sent[0]["sessionId"] == "observed-session"
    assert websocket.sent[1]["id"] == 2 and "sessionId" not in websocket.sent[1]


def test_cdp_errors_are_explicit_and_do_not_dispatch_another_command():
    websocket = Websocket([{"id": 1, "error": {"message": "stale session"}}])
    with pytest.raises(RuntimeError, match="stale session"):
        CDP(websocket).call("Input.dispatchMouseEvent", session_id="old")
    assert len(websocket.sent) == 1


def test_newly_attached_targets_are_resumed_before_renderer_commands():
    websocket = Websocket(
        [
            {"id": 1, "result": {"sessionId": "owned"}},
            {"id": 2, "result": {}},
            {"id": 3, "result": {}},
        ]
    )
    client = CDP(websocket)
    attached = resumed_call(
        client.call, "Target.attachToTarget", targetId="owned-target", flatten=True
    )
    assert attached == {"sessionId": "owned"}
    resumed_call(client.call, "Emulation.setDeviceMetricsOverride", session_id="owned", width=1120)
    assert [r["method"] for r in websocket.sent] == [
        "Target.attachToTarget",
        "Runtime.runIfWaitingForDebugger",
        "Emulation.setDeviceMetricsOverride",
    ]
