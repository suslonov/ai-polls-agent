"""Public Telegram channel collector (``https://t.me/s/<channel>``).

Telegram is a discovery layer, not a requirement: it finds stories the feeds
miss, but an outage here must never fail the daily run. No credentials are used
— only the public web view.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.collectors.base import CollectorResult, build_item
from src.extraction import fetch
from src.models import SourceConfig

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 600
MIN_STANDALONE_TEXT = 120  # a link-less post needs this much text to stand alone

_URL_RE = re.compile(r"https?://\S+")
# Trailing call-to-action lines that are channel boilerplate, not story text.
_BOILERPLATE_RE = re.compile(
    r"^(подписаться|подписывайтесь|читайте|наш телеграм|follow us|subscribe|"
    r"הצטרפו|לערוץ|מנוי)\b",
    re.IGNORECASE,
)


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.split("\n"):
        line = " ".join(_URL_RE.sub("", raw).split())
        line = line.strip(" •·—–-👉👇🔴⚡️")
        if not line or _BOILERPLATE_RE.match(line):
            continue
        lines.append(line)
    return lines


def _first_external_url(anchors: list[str], channel_host: str = "t.me") -> Optional[str]:
    for href in anchors:
        if not href or not href.startswith("http"):
            continue
        if channel_host in href:
            continue
        return href
    return None


def parse_messages(html: str, source: SourceConfig, permalink_base: str = "https://t.me"):
    """Parse the public channel page into NewsItem objects."""
    soup = BeautifulSoup(html, "lxml")
    items = []

    for message in soup.select("div.tgme_widget_message"):
        body = message.select_one(".tgme_widget_message_text")
        if body is None:
            continue

        text = body.get_text("\n", strip=True)
        lines = _clean_lines(text)
        if not lines:
            continue

        title = lines[0][:MAX_TITLE_CHARS]
        snippet = " ".join(lines)[:MAX_SNIPPET_CHARS]

        time_tag = message.select_one("time[datetime]")
        published: Optional[datetime] = None
        if time_tag and time_tag.get("datetime"):
            try:
                parsed = dateutil_parser.parse(str(time_tag["datetime"]))
                published = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                published = None

        post = message.get("data-post")
        discovery_url = f"{permalink_base}/{post}" if post else None

        hrefs = [str(a.get("href", "")) for a in body.select("a[href]")]
        article_url = _first_external_url(hrefs)

        if article_url:
            # Prefer the publisher's own URL; keep the post as provenance.
            url = article_url
        else:
            # A link-less post is only usable when it tells the story by itself.
            if len(" ".join(lines)) < MIN_STANDALONE_TEXT or not discovery_url:
                continue
            url = discovery_url

        item = build_item(
            source=source,
            title=title,
            url=url,
            published_at=published,
            snippet=snippet,
            discovery_url=discovery_url,
        )
        if item:
            items.append(item)

    return items


def collect(
    source: SourceConfig,
    user_agent: str,
    max_items: int,
    timeout: int,
) -> CollectorResult:
    """Collect recent posts from every configured public channel."""
    result = CollectorResult()
    errors: list[str] = []

    for channel_url in source.urls:
        if len(result.items) >= max_items:
            break

        response = fetch(channel_url, user_agent=user_agent, timeout=timeout)
        if response is None:
            errors.append(f"{channel_url}: fetch failed")
            continue

        result.http_status = response.status_code
        try:
            parsed = parse_messages(response.text, source)
        except Exception as exc:  # noqa: BLE001 - markup changes must not abort a run
            errors.append(f"{channel_url}: markup changed ({exc})")
            continue

        if not parsed:
            errors.append(f"{channel_url}: no messages parsed (markup may have changed)")
            continue

        for item in parsed:
            if len(result.items) >= max_items:
                break
            result.items.append(item)

    if not result.items and errors:
        result.error = "; ".join(errors)[:500]

    logger.info("Source %s: %d items", source.id, len(result.items))
    return result
