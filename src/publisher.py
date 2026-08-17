"""Publish a finalized poll onto the stable daily-israel-polls pages.

Per the project decision, the stable pages are files inside the kvasir_proto
working tree (they ship with the rest of the site), not objects written
straight to S3. Each publish inserts one entry at the top of a marked region and
never removes an existing one: a second poll on a day that already has one is
added above it, not swapped in. Everything outside the region is left untouched.

Repeated Finalize clicks do not reach here at all - `workflow.finalize` skips the
page write once the publish event for that echo and scroll is recorded - so the
page is append-only without also being duplicate-prone.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.models import PublishingConfig
from src.text_utils import clean

logger = logging.getLogger(__name__)

START_MARKER = "<!-- POLLS:START -->"
END_MARKER = "<!-- POLLS:END -->"

_LABELS = {
    "en": {"take": "Take today's poll →", "source": "source"},
    "ru": {"take": "Пройти опрос дня →", "source": "источник"},
}


class PublishError(RuntimeError):
    """Raised when a stable page cannot be updated."""


def _valid_http_url(url: str, field: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PublishError(f"{field} must be an http(s) URL, got {url!r}")
    return parsed.geturl()


def render_entry(
    day: str,
    target_language: str,
    title: str,
    description_text: str,
    quiz_url: str,
    source_url: Optional[str] = None,
    published_at: Optional[datetime] = None,
) -> str:
    """Render one poll entry, escaping every model- and source-supplied value."""
    quiz_url = _valid_http_url(quiz_url, "quiz_url")
    labels = _LABELS.get(target_language, _LABELS["en"])

    # The visible date is the poll's own day, not the moment we happened to write it.
    stamp = published_at.strftime("%Y-%m-%d") if published_at else day
    safe_title = html.escape(clean(title), quote=False)
    safe_desc = html.escape(clean(description_text), quote=False)
    safe_quiz = html.escape(quiz_url, quote=True)

    source_html = ""
    if source_url:
        try:
            safe_source = html.escape(_valid_http_url(source_url, "source_url"), quote=True)
            source_html = (
                f' <a href="{safe_source}" target="_blank" rel="noopener noreferrer">'
                f"{labels['source']}</a>"
            )
        except PublishError:
            logger.warning("Skipping unusable source URL %r on the stable page", source_url)

    return (
        f'<article class="blog-post" data-day="{html.escape(day, quote=True)}" '
        f'data-language="{html.escape(target_language, quote=True)}">\n'
        f'      <h2><a href="{safe_quiz}">{safe_title}</a></h2>\n'
        f'      <div class="resource-post-body">\n'
        f'        <p><time datetime="{html.escape(stamp, quote=True)}">{stamp}</time> — '
        f"{safe_desc}{source_html}</p>\n"
        f"      </div>\n"
        f'      <a class="resource-read-more" href="{safe_quiz}">{labels["take"]}</a>\n'
        f"    </article>"
    )


def _entry_day(entry_html: str) -> str:
    """The ``data-day`` an entry carries, used to keep the page chronological."""
    match = re.search(r'data-day="([^"]*)"', entry_html)
    return match.group(1) if match else ""


def _split_region(page_html: str, path: Path) -> tuple[str, str, str]:
    start = page_html.find(START_MARKER)
    end = page_html.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise PublishError(
            f"{path} has no {START_MARKER} … {END_MARKER} region — "
            "the stable page must keep those markers for automated updates."
        )
    head = page_html[: start + len(START_MARKER)]
    body = page_html[start + len(START_MARKER) : end]
    tail = page_html[end:]
    return head, body, tail


def update_page(
    path: Path,
    entry_html: str,
    day: str,
    target_language: str,
    max_entries: int,
) -> None:
    """Add one entry at the top of a stable page's poll region.

    Append-only: an entry that is already on the page is never removed, not even
    another poll from the same day and language. Two polls on one day both stay,
    newest first.
    """
    path = Path(path)
    if not path.exists():
        raise PublishError(f"Stable page {path} does not exist")

    page_html = path.read_text(encoding="utf-8")
    head, body, tail = _split_region(page_html, path)

    entries = re.findall(r'<article class="blog-post".*?</article>', body, flags=re.DOTALL)
    # The "no poll published yet" placeholder carries an empty data-day and is
    # dropped as soon as there is something real to show.
    entries = [entry for entry in entries if 'data-day=""' not in entry]
    entries.insert(0, entry_html)

    # Keep the page chronological even when an older day is published late; the
    # sort is stable, so entries from one day keep insertion order - the fresh
    # one above the poll it joins.
    entries.sort(key=_entry_day, reverse=True)
    if max_entries and max_entries > 0:
        # A configured cap, not a cleanup: set publishing.max_entries_per_page
        # to 0 to keep every poll ever published on the page.
        if len(entries) > max_entries:
            logger.info(
                "%s is at its %d-entry cap; %d older entr%s drop off the page",
                path, max_entries, len(entries) - max_entries,
                "y" if len(entries) - max_entries == 1 else "ies",
            )
        entries = entries[:max_entries]

    new_body = "\n\n    " + "\n\n    ".join(entry.strip() for entry in entries) + "\n\n    "
    updated = head + new_body + tail

    # Write via a temporary file so a crash can never truncate a live page.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, path)
    logger.info("Updated %s (%d entries)", path, len(entries))


def publish_poll(
    config: PublishingConfig,
    day: str,
    target_language: str,
    title: str,
    description_text: str,
    quiz_url: str,
    source_url: Optional[str] = None,
) -> list[str]:
    """Write the poll onto every configured copy of its stable page.

    Only the selected language's page is touched — a Russian-only day never
    rewrites the English page, and vice versa.

    Returns the paths written. Raises :class:`PublishError` if no page could be
    updated; a partial success (one mirror missing) is logged and accepted.
    """
    filename = config.file_for(target_language)
    entry = render_entry(
        day=day,
        target_language=target_language,
        title=title,
        description_text=description_text,
        quiz_url=quiz_url,
        source_url=source_url,
    )

    written: list[str] = []
    failures: list[str] = []

    for directory in config.html_dirs:
        path = Path(os.path.expanduser(directory)) / filename
        try:
            update_page(path, entry, day, target_language, config.max_entries_per_page)
            written.append(str(path))
        except PublishError as exc:
            failures.append(str(exc))
            logger.warning("Could not update %s: %s", path, exc)

    if not written:
        raise PublishError(
            "No stable page could be updated: " + ("; ".join(failures) or "no html_dirs configured")
        )
    if failures:
        logger.warning("Published to %d of %d pages", len(written), len(config.html_dirs))
    return written


def verify_published(paths: list[str], day: str, target_language: str) -> bool:
    """Confirm the entry is actually present in the written files."""
    needle = f'data-day="{day}" data-language="{target_language}"'
    for raw_path in paths:
        try:
            if needle not in Path(raw_path).read_text(encoding="utf-8"):
                return False
        except OSError:
            return False
    return bool(paths)
