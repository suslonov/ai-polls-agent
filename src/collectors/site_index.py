"""Listing-page collector for publishers without a usable feed.

Source-specific knowledge lives in ``config/sources.yaml`` (``link_pattern`` and
``min_title_len``), not in this module, so adding a site never means editing
collector code.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from src.collectors.base import CollectorResult, build_item
from src.extraction import absolutize, canonicalize_url, fetch, looks_like_index_page
from src.models import SourceConfig

logger = logging.getLogger(__name__)


def _matches(href: str, pattern: Optional[str]) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, href) is not None
    except re.error:
        return pattern in href


def extract_links(
    html: str,
    base_url: str,
    link_pattern: Optional[str],
    min_title_len: int,
) -> list[tuple[str, str]]:
    """Return ``(title, url)`` pairs for article anchors on a listing page."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = absolutize(str(anchor["href"]).strip(), base_url)
        if not href or not _matches(href, link_pattern):
            continue

        title = anchor.get_text(" ", strip=True)
        if not title:
            # Some cards put the headline in a nested attribute instead.
            title = (anchor.get("title") or anchor.get("aria-label") or "").strip()
        title = " ".join(title.split())
        if len(title) < min_title_len:
            continue

        canonical = canonicalize_url(href)
        if not canonical or canonical in seen or looks_like_index_page(canonical):
            continue

        seen.add(canonical)
        found.append((title, href))

    return found


def collect(
    source: SourceConfig,
    user_agent: str,
    max_items: int,
    timeout: int,
) -> CollectorResult:
    """Scrape each configured listing page for article links."""
    result = CollectorResult()
    errors: list[str] = []

    for page_url in source.urls:
        if len(result.items) >= max_items:
            break

        response = fetch(page_url, user_agent=user_agent, timeout=timeout)
        if response is None:
            errors.append(f"{page_url}: fetch failed")
            continue

        result.http_status = response.status_code
        links = extract_links(
            response.text, page_url, source.link_pattern, source.min_title_len
        )
        if not links:
            errors.append(f"{page_url}: no article links matched {source.link_pattern!r}")
            continue

        for title, url in links:
            if len(result.items) >= max_items:
                break
            item = build_item(source=source, title=title, url=url)
            if item:
                result.items.append(item)

    if not result.items and errors:
        result.error = "; ".join(errors)[:500]

    logger.info("Source %s: %d items", source.id, len(result.items))
    return result
