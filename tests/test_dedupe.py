"""Deduplication and deterministic filtering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import dedupe
from src.extraction import canonicalize_url, looks_like_index_page
from tests.conftest import make_item


def test_utm_variants_collapse_to_one_item():
    plain = "https://www.timesofisrael.com/news/story-1"
    tracked = plain + "?utm_source=telegram&utm_medium=social&fbclid=abc123"

    assert canonicalize_url(tracked) == canonicalize_url(plain)

    kept, duplicates = dedupe.deduplicate([make_item(url=plain), make_item(url=tracked)])
    assert len(kept) == 1
    assert len(duplicates) == 1


def test_canonicalize_preserves_real_article_identifiers():
    url = "https://www.mako.co.il/news?articleId=1234&utm_source=x"
    canonical = canonicalize_url(url)
    assert "articleId=1234" in canonical
    assert "utm_source" not in canonical


def test_website_and_telegram_post_are_one_story_website_wins():
    """The same headline from a publisher and a Telegram relay is one story."""
    title = "Municipality bans electric scooters from the boardwalk"
    website = make_item(title=title, url="https://www.ynetnews.com/article/abc", source_id="ynetnews_en")
    telegram = make_item(
        title=title,
        url="https://t.me/ynetalerts/1234",
        source_id="tg_ynetalerts",
        source_type="telegram_public",
        discovery_url="https://t.me/ynetalerts/1234",
    )

    kept, duplicates = dedupe.deduplicate([telegram, website])

    assert len(kept) == 1
    assert kept[0].source_type == "rss", "the publisher must be the representative, not the relay"
    assert duplicates[0].duplicate_group == kept[0].duplicate_group


def test_near_duplicate_titles_group_within_the_time_window():
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    first = make_item(
        title="Knesset approves the new transport budget after long debate",
        url="https://a.example.com/1",
        published_at=base,
    )
    second = make_item(
        title="Knesset approves new transport budget after a long debate",
        url="https://b.example.com/2",
        source_id="jpost_en",
        published_at=base + timedelta(minutes=20),
    )

    kept, duplicates = dedupe.deduplicate([first, second])
    assert len(kept) == 1
    assert len(duplicates) == 1


def test_similar_titles_far_apart_in_time_stay_separate():
    old = datetime.now(timezone.utc) - timedelta(hours=80)
    recent = datetime.now(timezone.utc)
    first = make_item(title="Weather warning issued for the coastal plain",
                      url="https://a.example.com/1", published_at=old)
    second = make_item(title="Weather warning issued for the coastal plain",
                       url="https://b.example.com/2", source_id="jpost_en", published_at=recent)

    kept, _ = dedupe.deduplicate([first, second])
    assert len(kept) == 2, "a repeated seasonal headline days apart is a different story"


def test_cross_language_hint_merges_hebrew_and_english_versions():
    """The prefilter's story key is what makes cross-language grouping possible."""
    hebrew = make_item(title="עיריית תל אביב אוסרת קורקינטים", url="https://ynet.co.il/a", language="he")
    english = make_item(title="Tel Aviv bans e-scooters", url="https://timesofisrael.com/b", language="en")
    items = [hebrew, english]
    dedupe.deduplicate(items)
    assert hebrew.duplicate_group != english.duplicate_group

    merged = dedupe.apply_story_group_hints(
        items, {0: "tel-aviv-scooter-ban", 1: "tel-aviv-scooter-ban"}
    )
    assert merged >= 1
    assert hebrew.duplicate_group == english.duplicate_group

    representatives = dedupe.pick_group_representatives(items)
    assert len(representatives) == 1


def test_deterministic_filter_drops_stale_empty_and_index_pages():
    now = datetime.now(timezone.utc)
    fresh = make_item(url="https://a.example.com/news/fresh-story")
    stale = make_item(url="https://a.example.com/news/old-story",
                      published_at=now - timedelta(hours=100))
    short_title = make_item(title="Short", url="https://a.example.com/news/short-one")
    index_page = make_item(url="https://a.example.com/news")
    future = make_item(url="https://a.example.com/news/tomorrow",
                       published_at=now + timedelta(hours=48))

    kept, dropped = dedupe.deterministic_filter(
        [fresh, stale, short_title, index_page, future], lookback_hours=30, now=now
    )

    assert [item.canonical_url for item in kept] == [fresh.canonical_url]
    reasons = {reason for _, reason in dropped}
    assert "older than lookback window" in reasons
    assert "title too short" in reasons
    assert "navigation or index page" in reasons
    assert "implausible future timestamp" in reasons


def test_looks_like_index_page():
    assert looks_like_index_page("https://www.n12.co.il/news")
    assert looks_like_index_page("https://www.n12.co.il/")
    assert not looks_like_index_page("https://www.n12.co.il/news/article-12345")


def test_items_without_timestamps_are_kept():
    """A site_index story has no publication time; discovery still counts."""
    item = make_item(url="https://vesty.co.il/main/article/abc123", published_at=None)
    kept, dropped = dedupe.deterministic_filter([item], lookback_hours=30)
    assert kept and not dropped
