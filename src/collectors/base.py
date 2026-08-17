"""Shared collector plumbing.

Each collector converts one source into normalized :class:`NewsItem` objects and
must never raise: a failing source is recorded and the run continues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.extraction import canonicalize_url, content_hash
from src.models import NewsItem, SourceConfig, SourceType

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    """Outcome of one source fetch."""

    items: list[NewsItem] = field(default_factory=list)
    http_status: Optional[int] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        return "failed" if self.error else "ok"


def build_item(
    source: SourceConfig,
    title: str,
    url: str,
    published_at: Optional[datetime] = None,
    dek: Optional[str] = None,
    snippet: Optional[str] = None,
    discovery_url: Optional[str] = None,
    full_text: Optional[str] = None,
) -> Optional[NewsItem]:
    """Normalize one raw entry, or return None when it is unusable."""
    title = (title or "").strip()
    url = (url or "").strip()
    if not title or not url:
        return None

    canonical = canonicalize_url(url)
    if not canonical:
        return None

    now = datetime.now(timezone.utc)
    return NewsItem(
        source_id=source.id,
        source_name=source.name,
        source_type=source.type.value,
        source_language=source.language,
        title_original=title,
        title_en=title if source.language == "en" else None,
        url=url,
        canonical_url=canonical,
        discovery_url=discovery_url,
        published_at=published_at,
        fetched_at=now,
        dek_original=(dek or None),
        snippet_original=(snippet or None),
        full_text=(full_text or None),
        content_hash=content_hash(title, canonical),
        first_seen_at=now,
        last_seen_at=now,
    )


def collect_source(
    source: SourceConfig,
    user_agent: str,
    max_items: int,
    timeout: int,
) -> CollectorResult:
    """Dispatch to the right collector for ``source.type``."""
    from src.collectors import rss, site_index, telegram_public

    if source.type == SourceType.rss:
        return rss.collect(source, user_agent, max_items, timeout)
    if source.type == SourceType.site_index:
        return site_index.collect(source, user_agent, max_items, timeout)
    if source.type == SourceType.telegram_public:
        return telegram_public.collect(source, user_agent, max_items, timeout)

    return CollectorResult(error=f"unknown source type {source.type}")
