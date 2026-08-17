"""Deterministic filtering and deduplication.

Everything here runs before any LLM call: it is cheap, reproducible, and keeps
the token bill down. Cross-language grouping is only *hinted* at here — the
prefilter model resolves the ambiguous cases (see :mod:`src.prefilter`).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from src.extraction import looks_like_index_page
from src.models import NewsItem

logger = logging.getLogger(__name__)

NEAR_DUP_THRESHOLD = 0.72
NEAR_DUP_WINDOW_HOURS = 36

# Hebrew/Russian/Latin stop words that carry no identity for headline matching.
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "with", "at", "by", "from",
    "is", "are", "was", "were", "be", "after", "over", "as", "that", "this", "it",
    "в", "и", "на", "с", "по", "за", "из", "не", "что", "как", "для", "о", "от", "к",
    "של", "עם", "על", "את", "לא", "הוא", "היא", "אבל", "כי", "אם", "גם", "זה",
}


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/diacritics, collapse whitespace."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    # Hebrew niqqud and other combining marks add noise to comparisons.
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def title_tokens(title: str) -> set[str]:
    """Significant tokens of a headline."""
    return {
        token
        for token in _normalize_title(title).split()
        if len(token) > 2 and token not in _STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def group_id(seed: str) -> str:
    """Stable short id used as ``duplicate_group``."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def within_window(a: Optional[datetime], b: Optional[datetime], hours: int) -> bool:
    """True when two publication times are close enough to be the same event.

    Missing timestamps are treated as "possibly the same" — the title
    similarity check still has to agree before anything is grouped.
    """
    aa, bb = _aware(a), _aware(b)
    if aa is None or bb is None:
        return True
    return abs((aa - bb).total_seconds()) <= hours * 3600


def deterministic_filter(
    items: Iterable[NewsItem],
    lookback_hours: int,
    now: Optional[datetime] = None,
) -> tuple[list[NewsItem], list[tuple[NewsItem, str]]]:
    """Apply the cheap rules from the plan's §6.2.

    Returns ``(kept, dropped)`` where each dropped entry carries a reason so the
    run log can explain where the funnel narrowed.
    """
    now = _aware(now) or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    kept: list[NewsItem] = []
    dropped: list[tuple[NewsItem, str]] = []

    for item in items:
        title = (item.title_original or "").strip()
        if not title:
            dropped.append((item, "empty title"))
            continue
        if len(title) < 12:
            dropped.append((item, "title too short"))
            continue
        if not item.canonical_url:
            dropped.append((item, "no url"))
            continue
        if looks_like_index_page(item.canonical_url):
            dropped.append((item, "navigation or index page"))
            continue

        published = _aware(item.published_at)
        if published is not None:
            if published < cutoff:
                dropped.append((item, "older than lookback window"))
                continue
            # A timestamp far in the future means a broken feed, not a scoop.
            if published > now + timedelta(hours=6):
                dropped.append((item, "implausible future timestamp"))
                continue

        kept.append(item)

    if dropped:
        logger.info("Deterministic filter dropped %d of %d items", len(dropped), len(dropped) + len(kept))
    return kept, dropped


def _source_rank(item: NewsItem) -> tuple:
    """Sort key choosing the representative of a duplicate group.

    Prefers, in order: an authoritative publisher over a Telegram relay, a
    longer body, and an earlier publication timestamp.
    """
    is_telegram = item.source_type == "telegram_public"
    has_article_url = bool(item.url) and "t.me/" not in item.url
    body_len = len(item.full_text or item.snippet_original or item.dek_original or "")
    published = _aware(item.published_at) or datetime.now(timezone.utc)
    return (is_telegram, not has_article_url, -body_len, published)


def deduplicate(
    items: list[NewsItem],
    threshold: float = NEAR_DUP_THRESHOLD,
    window_hours: int = NEAR_DUP_WINDOW_HOURS,
) -> tuple[list[NewsItem], list[NewsItem]]:
    """Collapse exact and near-duplicate stories, across languages.

    Step 1 is exact identity (canonical URL, then content hash). Step 2 is
    normalized-title similarity inside a time window. The best item of each
    group is kept and stamped with a shared ``duplicate_group``; the rest are
    returned as duplicates so callers can count them.
    """
    representatives: list[tuple[set[str], NewsItem]] = []
    duplicates: list[NewsItem] = []
    seen_urls: dict[str, NewsItem] = {}
    seen_hashes: dict[str, NewsItem] = {}

    # Process in representative-preference order so the winner is picked first.
    for item in sorted(items, key=_source_rank):
        canonical = item.canonical_url

        if canonical in seen_urls:
            item.duplicate_group = seen_urls[canonical].duplicate_group
            duplicates.append(item)
            continue
        if item.content_hash and item.content_hash in seen_hashes:
            item.duplicate_group = seen_hashes[item.content_hash].duplicate_group
            duplicates.append(item)
            continue

        tokens = title_tokens(item.title_original)
        matched: Optional[NewsItem] = None
        for existing_tokens, existing in representatives:
            if jaccard(tokens, existing_tokens) < threshold:
                continue
            if not within_window(item.published_at, existing.published_at, window_hours):
                continue
            matched = existing
            break

        if matched is not None:
            item.duplicate_group = matched.duplicate_group
            duplicates.append(item)
            continue

        item.duplicate_group = item.duplicate_group or group_id(canonical)
        seen_urls[canonical] = item
        if item.content_hash:
            seen_hashes[item.content_hash] = item
        representatives.append((tokens, item))

    kept = [item for _, item in representatives]
    logger.info("Dedupe: %d representatives, %d duplicates", len(kept), len(duplicates))
    return kept, duplicates


def apply_story_group_hints(items: list[NewsItem], hints: dict[int, str]) -> int:
    """Merge duplicate groups the prefilter model recognised across languages.

    ``hints`` maps item index → free-form story key from the model. Items that
    share a non-empty key are merged into one ``duplicate_group``.
    """
    by_hint: dict[str, list[NewsItem]] = {}
    for index, item in enumerate(items):
        hint = (hints.get(index) or "").strip().lower()
        if not hint:
            continue
        by_hint.setdefault(hint, []).append(item)

    merged = 0
    for hint, group in by_hint.items():
        if len(group) < 2:
            continue
        canonical_group = group[0].duplicate_group or group_id(hint)
        for item in group:
            if item.duplicate_group != canonical_group:
                item.duplicate_group = canonical_group
                merged += 1
    if merged:
        logger.info("Story-group hints merged %d items across languages", merged)
    return merged


def pick_group_representatives(items: list[NewsItem]) -> list[NewsItem]:
    """Keep one item per ``duplicate_group``, preferring the primary publisher."""
    best: dict[str, NewsItem] = {}
    for item in sorted(items, key=_source_rank):
        key = item.duplicate_group or item.canonical_url
        best.setdefault(key, item)
    return list(best.values())
