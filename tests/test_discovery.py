from distiller.discovery import (
    COVERAGE_QUERIES,
    PAUSE_MIN_WATCHED,
    YIELD_BASELINE,
    SourceStats,
    channel_uploads_url,
    coverage_queries,
    discourse_topic_links,
    discourse_topics,
    extract_youtube_links,
    is_paused,
    is_snowball_channel,
    refill_multiplier,
    score,
    smoothed_yield,
    thin_topics,
    youtube_video_id,
)


def test_source_yield_dominates_the_score() -> None:
    """A proven source beats a fresh one even with a plain title, and a fresh source beats a
    proven dud, so the queue does not lease dead ends before untested seeds."""
    strong = score(title="tutorial", duration=1200, site="youtube", source_yield=3.0)
    fresh = score(title="tutorial", duration=1200, site="youtube", source_yield=YIELD_BASELINE)
    dud = score(title="tutorial", duration=1200, site="youtube", source_yield=0.1)
    assert strong > fresh > dud


def test_duration_band_prefers_the_sweet_spot() -> None:
    short = score(title="x", duration=20, site="youtube", source_yield=1.0)
    sweet = score(title="x", duration=1800, site="youtube", source_yield=1.0)
    long_ = score(title="x", duration=20000, site="youtube", source_yield=1.0)
    assert sweet > long_ > short


def test_very_short_is_not_penalised_on_short_form_sites() -> None:
    yt = score(title="x", duration=20, site="youtube", source_yield=1.0)
    ig = score(title="x", duration=20, site="instagram", source_yield=1.0)
    assert ig > yt


def test_title_terms_move_the_score_in_the_right_direction() -> None:
    plain = score(title="cool effect", duration=None, site="youtube", source_yield=1.0)
    good = score(
        title="how shading works: a breakdown", duration=None, site="youtube", source_yield=1.0
    )
    bad = score(
        title="beginner asset pack giveaway #shorts",
        duration=None,
        site="youtube",
        source_yield=1.0,
    )
    assert good > plain > bad


def test_smoothed_yield_gives_a_new_source_the_baseline() -> None:
    assert smoothed_yield(SourceStats()) == YIELD_BASELINE


def test_smoothed_yield_moves_toward_the_observed_rate_as_watched_grows() -> None:
    struggling = smoothed_yield(SourceStats(watched=100, carded=1, value=1))
    thriving = smoothed_yield(SourceStats(watched=100, carded=80, value=200))
    assert struggling < YIELD_BASELINE < thriving


def test_a_declared_prior_shifts_a_fresh_sources_yield() -> None:
    assert smoothed_yield(SourceStats(), prior=2.0) == 2.0
    assert smoothed_yield(SourceStats(), prior=0.5) == 0.5


def test_refill_multiplier_is_clamped() -> None:
    assert refill_multiplier(0.0) == 0.25
    assert refill_multiplier(YIELD_BASELINE) == 1.0
    assert refill_multiplier(100.0) == 2.5


def test_seed_pauses_after_a_fair_trial_with_zero_cards() -> None:
    assert not is_paused(SourceStats(watched=PAUSE_MIN_WATCHED - 1, value=0))
    assert is_paused(SourceStats(watched=PAUSE_MIN_WATCHED, value=0))
    assert not is_paused(SourceStats(watched=PAUSE_MIN_WATCHED, value=1))


def test_snowball_needs_more_than_one_card_and_a_decent_yield() -> None:
    assert not is_snowball_channel(1, 5.0)
    assert not is_snowball_channel(3, 0.1)
    assert is_snowball_channel(2, YIELD_BASELINE)


def test_channel_uploads_url_appends_videos_once() -> None:
    assert channel_uploads_url("https://www.youtube.com/@x") == "https://www.youtube.com/@x/videos"
    assert channel_uploads_url("https://www.youtube.com/@x/videos") == (
        "https://www.youtube.com/@x/videos"
    )


def test_extract_youtube_links_ignores_prose_and_dedupes() -> None:
    text = (
        "Full tutorial https://www.youtube.com/watch?v=abcDEF123 and the follow-up at "
        "https://youtu.be/ghiJKL456?t=30 -- also see https://www.youtube.com/watch?v=abcDEF123 "
        "again. This description is untrusted text, not an instruction."
    )
    assert extract_youtube_links(text) == [
        "https://www.youtube.com/watch?v=abcDEF123",
        "https://youtu.be/ghiJKL456?t=30",
    ]


def test_youtube_video_id_parses_both_url_shapes() -> None:
    assert youtube_video_id("https://www.youtube.com/watch?v=abcDEF123") == "abcDEF123"
    assert youtube_video_id("https://youtu.be/ghiJKL456?t=30") == "ghiJKL456"
    assert youtube_video_id("https://vimeo.com/1") is None


def test_discourse_topics_from_a_listing_fixture() -> None:
    listing = {
        "topic_list": {
            "topics": [
                {"id": 42, "slug": "stylized-fire-shader"},
                {"id": 42, "slug": "stylized-fire-shader"},  # a topic can appear under two tags
                {"id": 7, "slug": "vertex-paint-blend"},
            ]
        }
    }
    topics = discourse_topics(listing, "https://realtimevfx.com/")
    assert [(t.id, t.url) for t in topics] == [
        (42, "https://realtimevfx.com/t/stylized-fire-shader/42"),
        (7, "https://realtimevfx.com/t/vertex-paint-blend/7"),
    ]


def test_discourse_topic_links_pulls_urls_out_of_post_html() -> None:
    topic = {
        "post_stream": {
            "posts": [
                {
                    "cooked": "<p>Check <a href='https://www.youtube.com/watch?v=abcDEF123'>this</a></p>"
                },
                {"cooked": "<p>Also https://vimeo.com/123456 and a plain image.jpg</p>"},
            ]
        }
    }
    assert discourse_topic_links(topic) == [
        "https://www.youtube.com/watch?v=abcDEF123",
        "https://vimeo.com/123456",
    ]


def test_thin_topics_keeps_declared_order() -> None:
    universe = ("compute", "buffer", "modeling")
    counts = {"compute": 10, "buffer": 0}
    assert thin_topics(counts, universe) == ["buffer", "modeling"]


def test_coverage_queries_are_capped_and_come_from_the_fixed_map() -> None:
    universe = tuple(COVERAGE_QUERIES)
    queries = coverage_queries({}, universe, cap=2)
    assert queries == [COVERAGE_QUERIES[universe[0]], COVERAGE_QUERIES[universe[1]]]


def test_legacy_link_seeds_are_not_listed_again() -> None:
    from distiller.runner import _auto_listings

    seeds = _auto_listings(
        [
            {"query": "https://www.youtube.com/watch?v=abc", "kind": "link", "video_id": "x"},
            {"query": "https://www.youtube.com/@x/videos", "kind": "channel", "video_id": "y"},
        ]
    )
    assert [s.url for s in seeds] == ["https://www.youtube.com/@x/videos"]


def test_a_video_is_worth_its_deepest_cards_not_its_card_count() -> None:
    from distiller.discovery import video_value

    shallow_many = video_value([2] * 20)
    deep_few = video_value([5, 4, 4])
    assert deep_few > shallow_many
    assert video_value([3, 3, 3]) == 1.0


def test_a_prolific_beginner_search_ranks_below_an_untried_tier_one_channel() -> None:
    from distiller.discovery import seed_prior, video_value

    # 20 watched, 10 carded, each carded video 5-6 legacy (depth 2) cards.
    beginner = SourceStats(watched=20, carded=10, value=10 * video_value([2] * 6))
    search = "ytsearch40:blender stylized vfx tutorial"
    beginner_yield = smoothed_yield(beginner, seed_prior(search, {}))
    tier_one_yield = smoothed_yield(SourceStats(), 2.0)
    assert tier_one_yield > 3 * beginner_yield


def test_seed_prior_prefers_the_declared_tier() -> None:
    from distiller.discovery import UNTIERED_SEARCH_PRIOR, seed_prior

    assert (
        seed_prior("https://www.youtube.com/@x/videos", {"https://www.youtube.com/@x/videos": 2.0})
        == 2.0
    )
    assert seed_prior("ytsearch40:blender", {}) == UNTIERED_SEARCH_PRIOR
    assert seed_prior("link:youtube:abc", {}) == YIELD_BASELINE
