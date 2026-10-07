from distiller.db import Queue
from distiller.media import site_of


def make() -> Queue:
    return Queue(":memory:")


def test_enqueue_dedupes_by_site_id() -> None:
    q = make()
    assert q.enqueue(site="youtube", site_id="abc", url="u1") is True
    assert q.enqueue(site="youtube", site_id="abc", url="u2") is False
    assert q.counts()["new"] == 1


def test_lease_takes_highest_priority_first_ties_broken_by_age() -> None:
    q = make()
    q.enqueue(site="youtube", site_id="old-low", url="u1", priority=1.0)
    q.enqueue(site="youtube", site_id="new-high", url="u2", priority=9.0)
    q.enqueue(site="youtube", site_id="no-score", url="u3")  # priority NULL, treated as 0
    first = q.lease(60, now=1000)
    assert first is not None and first.id == "youtube:new-high"
    second = q.lease(60, now=1001)
    assert second is not None and second.id == "youtube:old-low"
    third = q.lease(60, now=1002)
    assert third is not None and third.id == "youtube:no-score"


def test_lease_takes_each_video_once_until_it_expires() -> None:
    q = make()
    q.enqueue(site="youtube", site_id="a", url="u")
    first = q.lease(60, now=1000)
    assert first is not None and first.attempts == 1
    assert q.lease(60, now=1030) is None
    again = q.lease(60, now=1061)
    assert again is not None and again.id == first.id and again.attempts == 2


def test_retry_then_fail_after_max_attempts() -> None:
    q = make()
    q.enqueue(site="youtube", site_id="a", url="u")
    v = q.lease(60)
    assert v is not None
    assert q.retry_or_fail(v, "boom", max_attempts=2) == "new"
    v = q.lease(60)
    assert v is not None
    assert q.retry_or_fail(v, "boom", max_attempts=2) == "failed"
    assert q.lease(60) is None


def test_finished_videos_are_not_leased() -> None:
    q = make()
    q.enqueue(site="youtube", site_id="a", url="u")
    v = q.lease(60)
    assert v is not None
    q.finish(v.id, "carded")
    assert q.lease(60, now=10**12) is None
    assert q.pending() == 0


def test_site_of() -> None:
    assert site_of("https://www.youtube.com/watch?v=x") == "youtube"
    assert site_of("https://youtu.be/x") == "youtube"
    assert site_of("ytsearch10:blender") == "youtube"
    assert site_of("https://www.instagram.com/p/ExamplePost1") == "instagram"
    assert site_of("https://www.tiktok.com/@a/video/1") == "tiktok"
    assert site_of("https://vimeo.com/1") == "other"


def test_instagram_listing_and_seed() -> None:
    from distiller.media import instagram_seed, is_instagram_listing

    assert is_instagram_listing("https://www.instagram.com/example_artist/")
    assert is_instagram_listing("https://www.instagram.com/explore/tags/b3d/")
    assert not is_instagram_listing("https://www.instagram.com/p/ExamplePost1")
    assert not is_instagram_listing("https://www.instagram.com/reel/ExamplePost1/")
    assert instagram_seed("https://www.instagram.com/example_artist") == (
        "https://www.instagram.com/example_artist/reels/"
    )


def test_instagram_found_keeps_videos_once() -> None:
    from distiller.media import instagram_found

    items = [
        [2, {"post_shortcode": "A1", "video_duration": 40.0, "description": " tip "}],
        [3, "https://cdn/a.mp4", {"post_shortcode": "A1", "extension": "mp4"}],
        [3, "https://cdn/b.jpg", {"post_shortcode": "B2", "extension": "jpg"}],
        [2, {"post_shortcode": "C3", "extension": "mp4"}],
    ]
    found = instagram_found(items, limit=10)
    assert [(f.site_id, f.title, f.duration) for f in found] == [
        ("A1", "tip", 40.0),
        ("C3", None, None),
    ]
    assert found[0].url == "https://www.instagram.com/p/A1/"


def test_release_returns_a_leased_video_without_spending_an_attempt() -> None:
    q = make()
    q.enqueue(site="youtube", site_id="a", url="u")
    v = q.lease(3600)
    assert v is not None
    q.release(v.id)
    again = q.lease(3600)
    assert again is not None and again.attempts == 1


def test_recard_drops_old_cards_and_requeues_finished_videos() -> None:
    q = make()
    for vid, status, error in (
        ("a", "carded", None),
        ("b", "dropped", "no exact values"),
        ("c", "dropped", "published 2019-01-01, 2000 days old > 730"),
        ("d", "new", None),
    ):
        q.enqueue(site="youtube", site_id=vid, url=vid)
        q.finish(f"youtube:{vid}", status, error=error)
    q.add_card("youtube:a", "blender", "vfx", "old", {"goal": "x", "steps": []})
    q.add_card("youtube:a", "blender", "mask", "new", {"idea": "x", "produces": ["mask"]})
    assert sorted(q.requeue_for_new_forms()) == ["youtube:a", "youtube:b"]
    assert [c["technique"] for c in q.cards()] == ["new"]
    assert q.counts()["new"] == 3 and q.counts()["dropped"] == 1


def test_seed_and_channel_stats_count_watched_carded_and_card_depth() -> None:
    q = make()
    channel = "https://www.youtube.com/channel/UCx"
    q.enqueue(site="youtube", site_id="a", url="a", seed="s1", channel=channel)
    q.enqueue(site="youtube", site_id="b", url="b", seed="s1", channel=channel)
    q.enqueue(site="youtube", site_id="c", url="c", seed="s2")
    q.finish("youtube:a", "carded")
    q.finish("youtube:b", "dropped")
    q.finish("youtube:c", "carded")
    q.add_card("youtube:a", "t", "top", "tech-1", {"produces": ["mask"], "depth": 3})
    q.add_card("youtube:c", "t", "top", "tech-2", {"produces": ["mask"]})  # no depth: counts as 2

    seed_stats = q.seed_stats()
    assert seed_stats["s1"].watched == 2
    assert seed_stats["s1"].carded == 1
    assert seed_stats["s1"].value == 3 / 9
    assert seed_stats["s2"].value == 2 / 9

    channel_stats = q.channel_stats()
    assert channel_stats[channel].carded == 1
    assert channel_stats[channel].value == 3 / 9


def test_auto_seeds_keep_their_kind() -> None:
    q = make()
    assert q.add_auto_seed("blender fluid shader", "youtube:a") is True
    assert q.add_auto_seed("https://www.youtube.com/@x/videos", "channel:x", kind="channel") is True
    assert {(a["query"], a["kind"]) for a in q.auto_seeds()} == {
        ("blender fluid shader", "search"),
        ("https://www.youtube.com/@x/videos", "channel"),
    }


def test_web_rows_are_never_leased_recarded_or_retried() -> None:
    q = make()
    q.upsert_web_video(id="web:src/proj", url="https://x.example/p", title="P", seed="studied:src")
    q.add_card("web:src/proj", "figma", "light", "tech", {"produces": ["light"], "studied": True})

    assert q.counts()["carded"] == 1
    assert q.lease(3600) is None

    # A forms upgrade must not send a studied project back to a nonexistent download.
    assert q.requeue_for_new_forms() == []
    assert len(q.cards()) == 1  # its (old-shape) card is untouched
    assert q.counts()["carded"] == 1

    # `distill retry` must never pick it up even if its status were ever corrupted to leased.
    q._write("UPDATE videos SET status='leased' WHERE id=?", ("web:src/proj",))
    assert q.requeue_failed() == 0
    assert q.lease(3600) is None


def test_web_rows_are_excluded_from_discovery_stats_and_snowball() -> None:
    q = make()
    q.upsert_web_video(id="web:src/proj", url="https://x.example/p", title="P", seed="studied:src")
    q.add_card("web:src/proj", "figma", "light", "tech", {"produces": ["light"], "depth": 5})

    assert q.seed_stats() == {}
    assert q.channel_stats() == {}
    assert q.carded_videos() == []
    assert q.waiting_videos() == []


def test_set_priorities_writes_all_rows_in_one_go() -> None:
    q = make()
    for i in range(3):
        q.enqueue(site="youtube", site_id=f"v{i}", url=f"u{i}")
    q.set_priorities([("youtube:v0", 1.0), ("youtube:v2", 5.0)])
    assert [v["id"] for v in q.videos_missing_priority()] == ["youtube:v1"]
    assert q.lease(60).id == "youtube:v2"
