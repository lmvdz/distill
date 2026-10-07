import os
import sys
from types import SimpleNamespace

import httpx
import pytest

from distiller.jev_gateway import (
    EXPERIENTIAL_URL,
    TYPESAFE_URL,
    experiential_post,
    limited_state,
    load_keys,
    transport,
)


class Client:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def post(self, url, json, headers):
        self.calls.append((url, json, headers))
        return httpx.Response(self.status, json={"answers": {}, "usage": {"cost": 0}})


def test_gateway_uses_native_free_model_and_does_not_retry_or_forward_to_typesafe():
    client = Client()
    experiential_post(client, "dummy", {"model": "jev-latest", "state": {}, "questions": {}})
    url, body, headers = client.calls[0]
    assert url == EXPERIENTIAL_URL and body["model"] == "jev-latest:free"
    assert headers == {"Authorization": "Bearer dummy"}
    assert "stream" not in body
    failing = Client(503)
    with pytest.raises(ValueError, match="HTTP 503"):
        experiential_post(failing, "dummy", {"model": "jev-latest"})
    assert len(failing.calls) == 1
    with pytest.raises(ValueError, match="byte limit"):
        experiential_post(client, "dummy", {"state": "x" * 262144})
    assert len(client.calls) == 1


def test_gateway_preserves_structured_target_descriptions_and_ids():
    client = Client()
    question = {
        "type": "choice",
        "criteria": {
            "1": {"element": "Docs", "role": "link"},
            "2": {"element": "Other", "role": "link"},
        },
        "instructions": "Choose",
    }
    experiential_post(client, "dummy", {"questions": {"click_target": question}})
    criteria = client.calls[0][1]["questions"]["click_target"]["criteria"]
    assert list(criteria) == ["1", "2"] and criteria["1"] == {"element": "Docs", "role": "link"}
    assert question["criteria"]["1"] == {"element": "Docs", "role": "link"}


def test_single_compatible_target_is_resolved_in_code_without_a_singleton_model_head():
    client = Client()
    result = experiential_post(
        client,
        "dummy",
        {"questions": {"fill_target": {"type": "choice", "criteria": {"7": "only field"}}}},
    )
    assert client.calls[0][1]["questions"] == {}
    assert result["answers"]["fill_target"] == {
        "choice": "7",
        "probabilities": {"7": 1.0},
        "confidence": 1.0,
    }


def test_provider_errors_redact_credentials_before_truncating_messages():
    class ErrorClient:
        def post(self, url, json, headers):
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": "authentication_error",
                        "message": "x" * 235 + "test-credential-value",
                    }
                },
            )

    with pytest.raises(ValueError) as caught:
        experiential_post(ErrorClient(), "test-credential-value", {})
    assert "test-" not in str(caught.value)


def test_external_file_loads_only_needed_keys_and_does_not_overwrite_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EXPERIENTIAL_API_KEY", "")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("TEXT_MODEL", "configured")
    path = tmp_path / "keys.env"
    path.write_text('EXPERIENTIAL_API_KEY="dummy"\nELEVENLABS_API_KEY=unused\nTEXT_MODEL=other\n')
    load_keys(path)
    assert os.environ["EXPERIENTIAL_API_KEY"] == "dummy"
    assert os.environ["TEXT_MODEL"] == "configured"
    assert "ELEVENLABS_API_KEY" not in os.environ


def test_gateway_limits_preserve_observed_action_ids_and_do_not_mutate_state():
    state = {
        "actions": [
            {"kind": kind, "label": "long" * 100, "id": f"{kind}:{i}", "node": i}
            for kind in ("click", "fill", "select")
            for i in range(80)
        ],
        "fingerprint": "same observed document",
    }
    limited = limited_state(state)
    assert len(limited["actions"]) == 192 and len(state["actions"]) == 240
    assert limited["fingerprint"] == state["fingerprint"]
    assert limited["actions"][0]["id"] == state["actions"][0]["id"]
    assert len(limited["actions"][0]["label"]) == 300


def test_transport_is_scoped_restores_hooks_and_never_sends_gateway_key_upstream(monkeypatch):
    monkeypatch.setenv("EXPERIENTIAL_API_KEY", "dummy-gateway-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "old-direct-key")
    client = Client()

    def original_post(url, key, body):
        return {"direct": True}

    def original_choose(state, goal, history):
        return state

    def original_field(context):
        return "old"

    model = SimpleNamespace(CLIENT=client, post_json=original_post)
    agent = SimpleNamespace(choose=original_choose, field_text=original_field)
    monkeypatch.setitem(sys.modules, "jev_ultrafast", SimpleNamespace(model=model, agent=agent))
    with pytest.raises(RuntimeError, match="done"), transport():
        assert os.environ["TYPESAFE_API_KEY"] == "experiential-gateway-selected"
        model.post_json(TYPESAFE_URL, "placeholder", {"model": "jev-latest"})
        assert client.calls[0][0] == EXPERIENTIAL_URL
        assert client.calls[0][2]["Authorization"] == "Bearer dummy-gateway-key"
        raise RuntimeError("done")
    assert model.post_json is original_post and agent.choose is original_choose
    assert agent.field_text is original_field and os.environ["TYPESAFE_API_KEY"] == "old-direct-key"
