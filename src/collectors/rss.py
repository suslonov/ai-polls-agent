"""RSS/Atom collector.

Feeds are parsed from raw bytes rather than decoded text: several Israeli feeds
declare a non-UTF-8 encoding (utf-16, windows-1255) that feedparser only honours
when it can see the XML declaration itself.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import feedparser
from dateutil import parser as dateutil_parser

from src.collectors.base import CollectorResult, build_item
from src.extraction import fetch, html_to_text
from src.models import SourceConfig

logger = logging.getLogger(__name__)

MAX_SNIPPET_CHARS = 500


def parse_datetime(value) -> Optional[datetime]:
    """Parse whatever a feed puts in its date fields into aware UTC."""
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        try:
            return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
        except (OverflowError, ValueError):
            return None
    try:
        parsed = dateutil_parser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_summary(entry) -> str:
    summary = entry.get("summary") or ""
    if isinstance(summary, list):
        summary = " ".join(
            part.get("value", "") for part in summary if isinstance(part, dict)
        )
    if not summary:
        content = entry.get("content") or []
        if isinstance(content, list):
            summary = " ".join(
                part.get("value", "") for part in content if isinstance(part, dict)
            )
    return html_to_text(str(summary))[:MAX_SNIPPET_CHARS].strip()


def normalize_entry(entry, source: SourceConfig):
    """Convert one feedparser entry into a NewsItem (or None)."""
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None

    published = parse_datetime(
        entry.get("published_parsed")
        or entry.get("updated_parsed")
        or entry.get("published")
        or entry.get("updated")
    )
    snippet = _entry_summary(entry)

    return build_item(
        source=source,
        title=title,
        url=link,
        published_at=published,
        snippet=snippet or None,
    )


def collect(
    source: SourceConfig,
    user_agent: str,
    max_items: int,
    timeout: int,
) -> CollectorResult:
    """Collect items from every feed URL of a source."""
    result = CollectorResult()
    errors: list[str] = []

    for feed_url in source.urls:
        if len(result.items) >= max_items:
            break

        response = fetch(feed_url, user_agent=user_agent, timeout=timeout)
        if response is None:
            errors.append(f"{feed_url}: fetch failed")
            continue

        result.http_status = response.status_code
        parsed = feedparser.parse(response.content)
        if not parsed.entries:
            errors.append(f"{feed_url}: no entries ({parsed.get('bozo_exception', 'empty feed')})")
            continue

        for entry in parsed.entries:
            if len(result.items) >= max_items:
                break
            item = normalize_entry(entry, source)
            if item:
                result.items.append(item)

    if not result.items and errors:
        result.error = "; ".join(errors)[:500]
    elif errors:
        logger.warning("Source %s partial failure: %s", source.id, "; ".join(errors)[:300])

    logger.info("Source %s: %d items", source.id, len(result.items))
    return result
