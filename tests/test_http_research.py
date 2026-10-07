import json

import httpx
import pytest

from distiller.research import PageText, capture_http


def test_static_parser_extracts_links_but_skips_scripts_and_metadata():
    page = PageText("https://docs.example/manual/")
    page.feed(
        "<head><title>Metadata</title><script>evil()</script></head><body>"
        "<h1>Noise</h1><p>Use &amp; combine.</p><style>hidden</style>"
        '<a href="../guide">Guide</a><a href="javascript:alert(1)">ignore</a></body>'
    )
    assert "evil()" not in page.parts and "Metadata" not in page.parts
    assert "Use & combine." in page.parts
    assert page.links == [{"url": "https://docs.example/guide"}]


def test_http_capture_preserves_redirected_source_and_is_unverified(tmp_path, monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main><h1>Noise</h1><p>Scale controls frequency.</p></main>",
                request=httpx.Request("GET", "https://docs.example/latest/noise"),
            )

    monkeypatch.setattr("distiller.research.httpx.Client", Client)
    saved = json.loads(capture_http("https://docs.example/noise", tmp_path).read_text())
    assert saved["evidence"] == "unverified-web-observation"
    assert saved["pages"][0]["url"] == "https://docs.example/latest/noise"
    assert "Scale controls frequency." in saved["pages"][0]["text"]
    with pytest.raises(ValueError, match="HTTP"):
        capture_http("file:///private", tmp_path)
