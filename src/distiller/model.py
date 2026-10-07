"""Model client: send a form (and a clip), get the form back filled and validated.

Steps with a clip go to OmniRoute. Steps without one go to the text model, which may be a
headless `claude` or `codex` run, for their stronger reasoning (SPEC section 2)."""

import asyncio
import base64
import json
import logging
import random
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings, cli_backend

log = logging.getLogger("distill")


class ModelError(RuntimeError):
    pass


class Busy(ModelError):
    """OmniRoute turned the call away for load (429, 503 `chat_admission_busy`, 504 from its
    local limiter), every account is out of quota, or it is not running. Not the video's fault,
    so it never uses up a video's attempts."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _Transient(ModelError):
    pass


class Truncated(_Transient):
    """The reply hit the output limit. Gemini's hidden reasoning counts toward it, so a hard
    question can use the whole budget thinking and return nothing."""


# Measured 2026-09-24: a 25-name grouping spent 16,340 tokens reasoning and wrote nothing.
MAX_TOKENS = 65536


BUSY_STATUS = (429, 503, 504)
TRANSIENT_STATUS = (500, 502)
TRANSIENT_RETRIES = 4
BUSY_PATIENCE_S = 1800


class AdaptiveGate:
    """How many calls may be in flight. Halves when OmniRoute says busy, creeps back up on
    success, never above the configured maximum or below one."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.limit = float(maximum)
        self.in_flight = 0
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self.in_flight < max(1, int(self.limit)))
            self.in_flight += 1

    async def release(self, *, busy: bool) -> None:
        async with self._cond:
            self.in_flight -= 1
            if busy:
                self.limit = max(1.0, self.limit / 2)
            else:
                self.limit = min(float(self.maximum), self.limit + 1 / max(self.limit, 1.0))
            self._cond.notify_all()


def _classify(status: int, text: str, retry_after: str | None = None) -> ModelError:
    err = f"HTTP {status}: {text}"
    if status in BUSY_STATUS:
        return Busy(err, _seconds(retry_after))
    return (_Transient if status in TRANSIENT_STATUS else ModelError)(err)


def _seconds(header: str | None) -> float | None:
    try:
        return max(1.0, float(header)) if header else None
    except ValueError:
        return None


def busy_wait(err: Busy, waits: int) -> float:
    """OmniRoute's own retry-after when it gives one (a quota reset), else backoff."""
    if err.retry_after:
        return err.retry_after + random.uniform(1, 10)
    return min(120.0, 10 * 2 ** min(waits, 4)) * random.uniform(0.7, 1.3)


def _chunk_error(error: object) -> ModelError:
    text = str(error)[:300]
    busy = any(
        k in text for k in ("admission", "rate-limit", "quota", "exhausted", "429", "503", "504")
    )
    return (Busy if busy else _Transient)(text)


def check_finish(finish: str | None, text: str) -> str:
    """Only a reply that ended normally is an answer. A cut-off or empty one is retried."""
    if finish == "length":
        raise Truncated(f"reply cut off at the output limit after {len(text)} chars")
    if finish == "error":
        raise _Transient(f"stream ended with an error after {len(text)} chars")
    if not text.strip():
        raise _Transient(f"empty reply (finish_reason={finish})")
    return text


def video_part(path: Path) -> dict[str, Any]:
    # A `file` part is what OmniRoute forwards to Gemini as video. `video_url` and
    # `input_video` are dropped without an error (SPEC section 2).
    data = base64.b64encode(path.read_bytes()).decode()
    return {
        "type": "file",
        "file": {"file_data": f"data:video/mp4;base64,{data}", "filename": path.name},
    }


def extract_json(text: str) -> Any:
    """The JSON value in a reply. Only a fence around the whole reply is unwrapped: a fence
    inside a JSON string (a recipe's code block) is content, not a wrapper."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        text = stripped
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON in reply: {text[:200]!r}")
    body = text[start:]
    # strict=False accepts raw line breaks inside strings, which long Markdown fields contain.
    decoder = json.JSONDecoder(strict=False)
    try:
        return decoder.raw_decode(body)[0]
    except json.JSONDecodeError:
        # The usual model slip: a trailing comma before a closing bracket.
        return decoder.raw_decode(re.sub(r",\s*([}\]])", r"\1", body))[0]


# Words in a failed CLI run that mean the subscription is out or rate-limited, not a bad call.
CLI_BUSY = (
    "usage limit",
    "monthly spend limit",
    "weekly limit",
    "daily limit",
    "rate limit",
    "rate_limit",
    "429",
    "529",
    "overloaded",
    "quota",
)

CLI_SYSTEM = "You fill JSON forms. Reply with only the JSON the user asks for."


class Cli:
    """One headless `claude -p` or `codex exec` run per call, text only. Tools, hooks, MCP
    servers and user config are off and it runs in an empty directory: the prompt carries
    uploader text, which must not be able to make the agent do anything but answer."""

    def __init__(self, spec: str, concurrency: int, timeout_s: float) -> None:
        self.kind, _, self.model = spec.partition(":")
        self.name, self.timeout_s = spec, timeout_s
        self.gate = AdaptiveGate(concurrency)
        exe = shutil.which(self.kind)
        if exe is None:
            raise ModelError(f"{self.kind} CLI not found on PATH")
        self._exe = exe

    def args(self, out: Path) -> list[str]:
        if self.kind == "claude":
            args = [self._exe, "-p", "--output-format", "json", "--tools", ""]
            args += ["--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"]
            args += ["--system-prompt", CLI_SYSTEM]
            return args + (["--model", self.model] if self.model else [])
        args = [self._exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config"]
        args += ["--ignore-rules", "-s", "read-only", "--color", "never", "-o", str(out)]
        args += ["-c", "features.shell_tool=false", "-c", "features.unified_exec=false"]
        return args + (["-m", self.model] if self.model else []) + ["-"]

    async def ask(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="distill-") as tmp:
            out = Path(tmp) / "reply.txt"
            proc = await asyncio.create_subprocess_exec(
                *self.args(out),
                cwd=tmp,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), self.timeout_s
                )
            except TimeoutError as err:
                proc.kill()
                await proc.wait()
                raise _Transient(f"{self.name} gave no answer in {self.timeout_s:.0f} s") from err
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                raise
            last = out.read_text(encoding="utf-8") if out.exists() else ""
        return cli_reply(
            self.kind,
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            last,
        )


def cli_reply(kind: str, code: int, stdout: str, stderr: str, last: str) -> str:
    """The answer from a finished CLI run, or the error it counts as. Claude prints a JSON
    envelope; Codex writes its final message to the `-o` file."""
    if kind == "claude":
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            envelope = {"is_error": True, "result": stdout or stderr}
        failed = code != 0 or bool(envelope.get("is_error"))
        text = str(envelope.get("result") or "")
    else:
        failed, text = code != 0, last
    if failed:
        err = f"{kind} exited {code}: {(text or stderr or stdout)[-300:]}"
        if any(k in err.lower() for k in CLI_BUSY):
            raise Busy(err)
        raise _Transient(err)
    return check_finish("stop", text)


class Model:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.gate = AdaptiveGate(settings.model_concurrency)
        self._last_wait_log = -60.0
        self.calls = {"video": 0, "text": 0}  # requests sent, for a run's cost summary
        self._http = httpx.AsyncClient(timeout=settings.request_timeout_s)
        # Clip-free steps go to the text model: a CLI, another OmniRoute model, or this one.
        self.cli = (
            Cli(settings.text_model, settings.text_concurrency, settings.request_timeout_s)
            if cli_backend(settings.text_model)
            else None
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def chat(self, content: list[dict[str, Any]], max_tokens: int = MAX_TOKENS) -> str:
        text_only = all(part["type"] == "text" for part in content)
        self.calls["text" if text_only else "video"] += 1
        if text_only and self.cli is not None:
            cli = self.cli
            prompt = "\n\n".join(part["text"] for part in content)
            return await self._retrying(cli.gate, cli.name, lambda: cli.ask(prompt))
        # Streamed: OmniRoute expires a request that sends nothing back for 15 s, and a long
        # extraction thinks for about a minute before its first token. Streaming keeps it alive.
        body = {
            "model": self._s.text_model if text_only else self._s.model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": content}],
        }
        return await self._retrying(self.gate, "OmniRoute", lambda: self._stream(body))

    async def _retrying(
        self, gate: AdaptiveGate, name: str, send: Callable[[], Awaitable[str]]
    ) -> str:
        give_up = time.monotonic() + BUSY_PATIENCE_S
        busy_waits = transient = 0
        while True:
            await gate.acquire()
            try:
                reply = await send()
            except (httpx.ConnectError, httpx.ConnectTimeout) as err:
                # OmniRoute is not running. Treat it like busy: wait for it, never blame the video.
                await gate.release(busy=True)
                if time.monotonic() > give_up:
                    raise Busy(f"OmniRoute unreachable: {err}") from err
                busy_waits += 1
                wait = min(120.0, 10 * 2 ** min(busy_waits, 4))
                log.warning(
                    "OmniRoute unreachable at %s, retrying in %.0f s", self._s.omniroute_url, wait
                )
                await asyncio.sleep(wait)
            except Busy as err:
                await gate.release(busy=True)
                if time.monotonic() > give_up:
                    raise
                busy_waits += 1
                wait = busy_wait(err, busy_waits)
                self._say_waiting(name, err, wait)
                await asyncio.sleep(wait)
            except (_Transient, httpx.TransportError) as err:
                await gate.release(busy=False)
                transient += 1
                if transient > TRANSIENT_RETRIES:
                    raise ModelError(str(err)) from err
                await asyncio.sleep(5 * 2**transient)
            except BaseException:
                await gate.release(busy=False)
                raise
            else:
                await gate.release(busy=False)
                return reply

    def _say_waiting(self, name: str, err: Busy, wait: float) -> None:
        """At most one line a minute: many calls wait on the same cause at once."""
        now = time.monotonic()
        if now - self._last_wait_log >= 60:
            self._last_wait_log = now
            log.warning("%s busy, waiting %.0f s: %s", name, wait, str(err)[:140])

    async def _stream(self, body: dict[str, Any]) -> str:
        url = f"{self._s.omniroute_url}/chat/completions"
        pieces: list[str] = []
        finish: str | None = None
        async with self._http.stream("POST", url, json=body) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode(errors="replace")[:300]
                raise _classify(r.status_code, text, r.headers.get("retry-after"))
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise _chunk_error(chunk["error"])
                for choice in chunk.get("choices") or []:
                    pieces.append((choice.get("delta") or {}).get("content") or "")
                    finish = choice.get("finish_reason") or finish
        return check_finish(finish, "".join(pieces))

    async def fill[T: BaseModel](
        self, form: type[T], instructions: str, clip: Path | None = None, data: str | None = None
    ) -> T:
        """Asks for `form` as JSON. On a malformed reply, asks once more with the error."""
        schema = json.dumps(form.model_json_schema())
        text = (
            f"{instructions}\n\nReply with only a JSON object matching this JSON Schema:\n{schema}"
        )
        if data is not None:
            text += f"\n\n<data>\n{data}\n</data>"
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if clip is not None:
            content.append(video_part(clip))
        reply = await self.chat(content)
        try:
            return form.model_validate(extract_json(reply))
        except (ValidationError, ValueError) as err:
            content[0] = {
                "type": "text",
                "text": f"{text}\n\nYour previous reply was invalid: "
                f"{str(err)[:1500]}\nReply again, JSON only.",
            }
            reply = await self.chat(content)
            try:
                return form.model_validate(extract_json(reply))
            except (ValidationError, ValueError) as err2:
                raise ModelError(f"invalid {form.__name__}: {err2}") from err2
