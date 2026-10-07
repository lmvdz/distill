from datetime import UTC, datetime
from pathlib import Path

from distiller.db import Queue
from distiller.media import age_days, is_video_entry, youtube_search_url
from distiller.runner import DiscourseSeed, load_seeds


def test_search_url_carries_the_recency_filter() -> None:
    assert youtube_search_url("blender geometry nodes", "year") == (
        "https://www.youtube.com/results?search_query=blender+geometry+nodes&sp=EgIIBQ%3D%3D"
    )
    assert youtube_search_url("a&b", None) == "https://www.youtube.com/results?search_query=a%26b"


def test_age_is_counted_from_the_code_clock() -> None:
    today = datetime(2026, 9, 24, tzinfo=UTC)
    assert age_days("20260227", today) == 209
    assert age_days("20240101", today) == 997


def test_only_single_videos_are_queued() -> None:
    assert is_video_entry({"ie_key": "Youtube", "url": "https://www.youtube.com/watch?v=x"})
    assert not is_video_entry(
        {"ie_key": "YoutubeTab", "url": "https://www.youtube.com/playlist?list=PL1"}
    )
    assert not is_video_entry({"url": "https://www.youtube.com/watch?v=x&list=PL1"})
    assert not is_video_entry({"url": "https://www.youtube.com/channel/UC1"})


def test_seeds_accept_urls_and_recent_queries(tmp_path: Path) -> None:
    f = tmp_path / "seeds.toml"
    f.write_text(
        '[[seed]]\nurl = "https://www.instagram.com/example_artist/"\nlimit = 5\n'
        '[[seed]]\nquery = "blender vfx"\nrecent = "month"\n',
        encoding="utf-8",
    )
    seeds, discourse = load_seeds(f)
    assert discourse == []
    assert [(s.url, s.limit) for s in seeds] == [
        ("https://www.instagram.com/example_artist/", 5),
        ("https://www.youtube.com/results?search_query=blender+vfx&sp=EgIIBA%3D%3D", 50),
    ]


def test_seeds_accept_a_tier_a_match_and_a_discourse_feed(tmp_path: Path) -> None:
    f = tmp_path / "seeds.toml"
    f.write_text(
        '[[seed]]\nurl = "https://www.youtube.com/@ExampleConference/videos"\n'
        'tier = 1\nmatch = "VFX|Shader"\n'
        '[[seed]]\ndiscourse = "https://realtimevfx.com"\ntags = ["shaders"]\nlimit = 10\n',
        encoding="utf-8",
    )
    seeds, discourse = load_seeds(f)
    assert seeds[0].prior == 2.0
    assert seeds[0].match is not None
    assert seeds[0].match.search("VFX breakdown")
    assert not seeds[0].match.search("speedrun")
    assert discourse == [DiscourseSeed("https://realtimevfx.com", ("shaders",), 10)]


def test_upload_date_column_is_added_to_an_old_database(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE videos (id TEXT PRIMARY KEY, url TEXT NOT NULL, site TEXT NOT NULL,"
        " title TEXT, duration REAL, seed TEXT, status TEXT NOT NULL DEFAULT 'new',"
        " attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL, error TEXT, screen TEXT,"
        " created REAL NOT NULL, updated REAL NOT NULL)"
    )
    db.commit()
    db.close()
    q = Queue(path)
    q.enqueue(site="youtube", site_id="a", url="u", channel="https://www.youtube.com/@x")
    q.set_upload_date("youtube:a", "20260101")
    q.set_priority("youtube:a", 12.5)
    assert q.videos_missing_priority() == []
    assert q.add_auto_seed("q", "youtube:a") is True
