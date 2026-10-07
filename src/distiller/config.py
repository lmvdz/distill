"""Settings, read once from the environment (and `.env` if present)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class SiteLimit:
    concurrency: int
    min_spacing_s: float


# Downloads per site. Instagram and TikTok block fast scrapers, so they go slowly.
SITE_LIMITS: dict[str, SiteLimit] = {
    "youtube": SiteLimit(concurrency=2, min_spacing_s=5.0),
    "instagram": SiteLimit(concurrency=1, min_spacing_s=20.0),
    "tiktok": SiteLimit(concurrency=1, min_spacing_s=15.0),
    "other": SiteLimit(concurrency=2, min_spacing_s=5.0),
}


@dataclass(frozen=True)
class Settings:
    omniroute_url: str
    model: str
    text_model: str
    data: Path
    workers: int
    model_concurrency: int
    daily_cap: int
    keep_threshold: int
    max_age_days: int
    cookies_browser: str
    text_concurrency: int = 2
    prescreen_min_depth: int = 2
    segment_s: int = 600
    lease_s: int = 3600
    max_attempts: int = 3
    request_timeout_s: float = 600.0

    @property
    def db_path(self) -> Path:
        return self.data / "queue.sqlite"

    @property
    def work_dir(self) -> Path:
        return self.data / "work"

    @property
    def catalog_dir(self) -> Path:
        return self.data / "catalog"

    @property
    def workflow_catalog_dir(self) -> Path:
        return self.data / "catalog-workflows"


# A model named `claude:<model>` or `codex:<model>` runs through that CLI instead of OmniRoute.
# Neither can watch video, so they may only take the steps that send no clip.
CLI_BACKENDS = ("claude", "codex")


PROVIDERS = ("omniroute", *CLI_BACKENDS)


def cli_backend(model: str) -> str | None:
    kind = model.split(":", 1)[0]
    return kind if kind in CLI_BACKENDS else None


def _video_model(model: str) -> str:
    if cli_backend(model):
        raise ValueError(
            f"video model {model}: video steps need a model that watches video. "
            "Use Claude or Codex as the text model instead."
        )
    return model


def text_model(current: str, provider: str | None, model: str | None) -> str:
    """`--provider` and `--model` applied to the current text model; either alone keeps the
    other part. `claude` or `codex` with no model is that CLI's own default."""
    old = cli_backend(current) or "omniroute"
    kind = provider or old
    if model is not None:
        name = model
    elif kind != old:
        name = ""  # a new provider on its own gets its default, not the old provider's model
    else:
        name = current.split(":", 1)[1] if old != "omniroute" else current
    if kind == "omniroute":
        if not name:
            raise ValueError("--provider omniroute needs --model")
        return name
    return f"{kind}:{name}" if name else kind


def with_models(
    s: Settings, provider: str | None, model: str | None, video_model: str | None
) -> Settings:
    """Settings with the command line's --provider/--model (text steps) and --video-model."""
    changes: dict[str, str] = {}
    if video_model:
        changes["model"] = _video_model(video_model)
        if s.text_model == s.model:
            changes["text_model"] = video_model  # the text model was following it
    if provider or model:
        changes["text_model"] = text_model(changes.get("text_model", s.text_model), provider, model)
    return Settings(**{**s.__dict__, **changes}) if changes else s


def load(root: Path | None = None) -> Settings:
    root = root or Path.cwd()
    _load_dotenv(root / ".env")
    env = os.environ.get
    model = _video_model(env("DISTILL_MODEL", "agy/gemini-3.8-flash-high"))
    return Settings(
        omniroute_url=env("OMNIROUTE_URL", "http://127.0.0.1:20128/v1").rstrip("/"),
        model=model,
        text_model=env("DISTILL_TEXT_MODEL") or model,
        text_concurrency=int(env("DISTILL_TEXT_CONCURRENCY", "2")),
        data=(root / env("DISTILL_DATA", "data")).resolve(),
        workers=int(env("DISTILL_WORKERS", "2")),
        model_concurrency=int(env("DISTILL_MODEL_CONCURRENCY", "4")),
        daily_cap=int(env("DISTILL_DAILY_CAP", "500")),
        keep_threshold=int(env("DISTILL_KEEP_THRESHOLD", "2")),
        max_age_days=int(env("DISTILL_MAX_AGE_DAYS", "1825")),
        cookies_browser=env("DISTILL_COOKIES_BROWSER", "firefox"),
        prescreen_min_depth=int(env("DISTILL_PRESCREEN_MIN_DEPTH", "2")),
    )
