import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from distiller import config
from distiller.model import Busy, Cli, Model, ModelError, _Transient, cli_reply


def test_cli_backends_are_named_by_prefix() -> None:
    assert config.cli_backend("claude:opus") == "claude"
    assert config.cli_backend("codex") == "codex"
    assert config.cli_backend("agy/gemini-3.8-flash-high") is None
    assert config.cli_backend("claude-proxy/sonnet") is None


def test_a_cli_cannot_be_the_video_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISTILL_MODEL", "claude:opus")
    with pytest.raises(ValueError, match="watches video"):
        config.load(tmp_path)


def test_text_model_defaults_to_the_video_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISTILL_MODEL", "agy/gemini-3.8-flash-high")
    monkeypatch.delenv("DISTILL_TEXT_MODEL", raising=False)
    assert config.load(tmp_path).text_model == "agy/gemini-3.8-flash-high"
    monkeypatch.setenv("DISTILL_TEXT_MODEL", "codex:gpt-6.1-sol")
    assert config.load(tmp_path).text_model == "codex:gpt-6.1-sol"


def test_cli_runs_without_tools_or_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    claude = Cli("claude:opus", 1, 60).args(Path("out.txt"))
    assert claude[claude.index("--tools") + 1] == ""
    assert claude[claude.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in claude and claude[-2:] == ["--model", "opus"]
    codex = Cli("codex", 1, 60).args(Path("out.txt"))
    assert "--ignore-user-config" in codex and "features.shell_tool=false" in codex
    assert "-m" not in codex and codex[-1] == "-"


def test_cli_replies_and_failures_are_told_apart() -> None:
    ok = json.dumps({"is_error": False, "result": '{"a": 1}'})
    assert cli_reply("claude", 0, ok, "", "") == '{"a": 1}'
    limit = json.dumps({"is_error": True, "result": "Claude AI usage limit reached"})
    with pytest.raises(Busy):
        cli_reply("claude", 1, limit, "", "")
    with pytest.raises(_Transient):
        cli_reply("claude", 1, "not json", "boom", "")
    assert cli_reply("codex", 0, "log lines", "", '{"b": 2}') == '{"b": 2}'
    with pytest.raises(Busy):
        cli_reply("codex", 1, "", "ERROR: rate limit exceeded, retry later", "")
    with pytest.raises(_Transient):
        cli_reply("codex", 0, "", "", "   ")


class FakeCli:
    name = "claude:opus"

    def __init__(self) -> None:
        from distiller.model import AdaptiveGate

        self.gate = AdaptiveGate(1)
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "from cli"


async def test_steps_without_a_clip_go_to_the_text_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISTILL_MODEL", "agy/gemini-3.8-flash-high")
    monkeypatch.setenv("DISTILL_TEXT_MODEL", "agy/other-model")
    model = Model(config.load(tmp_path))
    sent: list[dict[str, Any]] = []

    async def stream(body: dict[str, Any]) -> str:
        sent.append(body)
        return "from omniroute"

    monkeypatch.setattr(model, "_stream", stream)
    video = {"type": "file", "file": {"file_data": "data:video/mp4;base64,", "filename": "c.mp4"}}
    assert await model.chat([{"type": "text", "text": "q"}]) == "from omniroute"
    assert await model.chat([{"type": "text", "text": "q"}, video]) == "from omniroute"
    assert [b["model"] for b in sent] == ["agy/other-model", "agy/gemini-3.8-flash-high"]

    cli = FakeCli()
    model.cli = cli  # type: ignore[assignment]
    assert await model.chat([{"type": "text", "text": "q"}]) == "from cli"
    assert await model.chat([{"type": "text", "text": "q"}, video]) == "from omniroute"
    assert cli.prompts == ["q"]
    await model.close()


def test_a_missing_cli_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ModelError, match="not found"):
        Cli("codex", 1, 60)


def test_provider_and_model_flags_combine_with_the_current_text_model() -> None:
    gemini = "agy/gemini-3.8-flash-high"
    assert config.text_model(gemini, "claude", "opus") == "claude:opus"
    assert config.text_model(gemini, "codex", None) == "codex"
    assert config.text_model("claude:opus", None, "sonnet") == "claude:sonnet"
    assert config.text_model("claude:opus", "codex", None) == "codex"
    assert config.text_model("claude:opus", "claude", None) == "claude:opus"
    assert config.text_model("claude:opus", "omniroute", gemini) == gemini
    assert config.text_model(gemini, None, "agy/other") == "agy/other"
    with pytest.raises(ValueError, match="needs --model"):
        config.text_model("claude:opus", "omniroute", None)


def test_video_model_flag_is_checked_and_carries_a_following_text_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISTILL_MODEL", "agy/gemini-3.8-flash-high")
    monkeypatch.delenv("DISTILL_TEXT_MODEL", raising=False)
    s = config.load(tmp_path)
    assert config.with_models(s, None, None, None) is s
    moved = config.with_models(s, None, None, "agy/gemini-4")
    assert (moved.model, moved.text_model) == ("agy/gemini-4", "agy/gemini-4")
    split = config.with_models(s, "codex", "gpt-6.1-sol", "agy/gemini-4")
    assert (split.model, split.text_model) == ("agy/gemini-4", "codex:gpt-6.1-sol")
    with pytest.raises(ValueError, match="watches video"):
        config.with_models(s, None, None, "claude:opus")


async def test_cancelled_cli_call_kills_and_reaps_its_process(monkeypatch):
    started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        reaped = False

        async def communicate(self, prompt):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

        async def wait(self):
            self.reaped = True

    proc = Process()

    async def spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr("shutil.which", lambda name: name)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(Cli("claude", 1, 60).ask("JSON only"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed and proc.reaped
