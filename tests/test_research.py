import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from distiller.db import Queue
from distiller.research import capture, enqueue_captures, video_candidate


@pytest.fixture(autouse=True)
def _no_live_gateway(monkeypatch):
    monkeypatch.delenv("EXPERIENTIAL_API_KEY", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.setattr("distiller.research.local_cdp", nullcontext)


class Agent:
    ticks = 0
    terminal = "done"
    text = "observed source"

    def __init__(self, url, goal, screenshots):
        type(self).last = self
        self.url = url
        self.ticks = 0
        assert not screenshots
        self.browser = SimpleNamespace(evaluate=lambda expression: {"url": url, "links": []})

    def snapshot(self):
        return {"page": {"url": self.url, "text": self.text}, "status": "ready"}

    def command(self, name):
        assert name == "tick"
        self.ticks += 1
        return {
            "page": {"url": self.url, "text": self.text},
            "status": self.terminal,
            "history": [{"text": "PRIVATE typed value"}],
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.mark.parametrize(
    "terminal, expected", [("done", "done"), ("blocked", "blocked"), ("ready", "step-limit")]
)
def test_bounded_capture_is_unverified_and_does_not_save_action_trace(
    tmp_path, monkeypatch, terminal, expected
):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "jev_ultrafast", SimpleNamespace(Agent=Agent))
    monkeypatch.setattr(Agent, "terminal", terminal)
    path = capture("https://example.com", "Find noise docs", tmp_path, max_steps=2)
    saved = path.read_text()
    result = json.loads(saved)
    assert result["status"] == expected
    assert result["evidence"] == "unverified-web-observation"
    assert result["pages"][0]["text"] == "observed source"
    assert len(result["pages"][0]["observations"]) == 1
    assert "PRIVATE" not in saved and "history" not in result
    assert Agent.last.ticks == (2 if terminal == "ready" else 1)


def test_empty_capture_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "jev_ultrafast", SimpleNamespace(Agent=Agent))
    monkeypatch.setattr(Agent, "text", "")
    with pytest.raises(ValueError, match="no source text"):
        capture("https://example.com", "Find docs", tmp_path)
    assert list(tmp_path.glob("*.json")) == []


def test_capture_validates_before_connecting_to_browser(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="HTTP"):
        capture("file:///private", "Read it", tmp_path)
    with pytest.raises(ValueError, match="step limit"):
        capture("https://example.com", "Read it", tmp_path, 0)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        capture("https://example.com", "Read it", tmp_path)


@pytest.mark.parametrize(
    "url, site, ident",
    [
        ("https://youtu.be/abcdef12345?t=4", "youtube", "abcdef12345"),
        ("https://www.youtube.com/shorts/abcdef12345", "youtube", "abcdef12345"),
        ("https://youtube.com/watch?list=123&v=abcdef12345", "youtube", "abcdef12345"),
        ("https://instagram.com/reel/ABC_123/", "instagram", "ABC_123"),
        ("https://vimeo.com/123456", "other", "123456"),
        ("https://www.tiktok.com/@creator/video/123456", "tiktok", "123456"),
    ],
)
def test_video_links_are_canonicalized(url, site, ident):
    found = video_candidate(url)
    assert found[:2] == (site, ident)
    assert "?list=" not in found[2] and "?t=" not in found[2]


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com.evil.example/watch?v=abcdef12345",
        "https://evil.example/?url=https://youtu.be/abcdef12345",
        "javascript:alert(1)",
        "https://youtube.com/@channel",
        "https://instagram.com/creator/",
        "file:///private.mp4",
    ],
)
def test_discovery_rejects_non_video_and_spoofed_urls(url):
    assert video_candidate(url) is None


def test_enqueue_uses_observed_links_dedupes_and_limit_counts_new_rows(tmp_path):
    q = Queue(":memory:")
    (tmp_path / "sources.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "url": "https://example.com/tutorials",
                        "links": [
                            {"url": "https://youtu.be/abcdef12345", "title": "Noise explained"},
                            {"url": "https://youtube.com/watch?v=abcdef12345&t=2"},
                            {"url": "https://youtu.be/second12345", "title": "Deep dive"},
                            {"url": "https://youtube.com.evil/watch?v=third123456"},
                        ],
                    }
                ]
            }
        )
    )
    assert enqueue_captures(q, tmp_path, limit=1) == (2, 1)
    assert enqueue_captures(q, tmp_path, limit=1) == (2, 1)
    assert enqueue_captures(q, tmp_path, limit=1) == (2, 0)
    assert q.counts()["new"] == 2 and q.counts()["cards"] == 0
    video = q.lease(60)
    assert video.seed == "https://example.com/tutorials"
