from distiller.media import take_captions, vtt_to_text

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
a shader<00:00:00.500><c> reads</c><00:00:01.000><c> the</c>

00:00:01.500 --> 00:00:04.000 align:start position:0%
a shader reads the depth buffer

00:00:04.000 --> 00:00:06.000 align:start position:0%
depth buffer to find edges
"""


def test_vtt_to_text_dedupes_the_rolling_window_into_plain_text() -> None:
    text = vtt_to_text(VTT)
    assert "WEBVTT" not in text and "-->" not in text and "<c>" not in text
    # the rolling window repeats "a shader reads the depth buffer" across cues; kept once
    assert text.count("a shader reads the depth buffer") == 1
    assert "depth buffer to find edges" in text


def test_vtt_to_text_truncates_to_the_limit() -> None:
    cues = (f"00:00:0{i}.000 --> 00:00:0{i + 1}.000\nline {i}" for i in range(9))
    long_vtt = "WEBVTT\n\n" + "\n\n".join(cues)
    assert len(vtt_to_text(long_vtt, limit=10)) == 10


def test_no_english_captions_is_not_an_error(tmp_path) -> None:
    assert take_captions(tmp_path) is None


def test_captions_are_read_once_and_removed(tmp_path) -> None:
    vtt = tmp_path / "prescreen.en-orig.vtt"
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nthe fresnel term\n", encoding="utf-8")
    assert "fresnel" in (take_captions(tmp_path) or "")
    assert not vtt.exists()
