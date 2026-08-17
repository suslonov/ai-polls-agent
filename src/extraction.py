"""URL canonicalization, HTTP fetching and article-text extraction."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20

# Query parameters that only track the reader; never part of a story's identity.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "utm_reader", "utm_brand", "utm_social", "utm_social-type",
    "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
    "mc_cid", "mc_eid", "igshid", "ref", "referrer", "_ga", "_gl",
    "cid", "source", "s", "spm", "vgo_ee",
}

# Listing / navigation URLs that never carry a single story.
_INDEX_PATH_RE = re.compile(
    r"^/?(?:news|home|index|category|categories|tag|tags|topics?|section|sections|"
    r"archive|search|live|author|authors|about|contact|rss|feed|sitemap)?/?$",
    re.IGNORECASE,
)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def canonicalize_url(url: str) -> str:
    """Strip tracking noise so the same story has one identity.

    Removes ``utm_*`` and social/ad click ids, lowercases the host, drops a
    default port and a trailing slash, and discards fragments. Query parameters
    that actually identify an article (``id``, ``articleId``, …) are preserved.
    """
    if not url:
        return ""
    url = url.strip()
    parsed = urlparse(url)

    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query = ""
    if parsed.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if k.lower() not in _TRACKING_PARAMS
        ]
        query = urlencode(sorted(kept))

    return urlunparse((scheme, netloc, path, "", query, ""))


def content_hash(title: str, canonical_url: str) -> str:
    """Stable identity hash for exact-duplicate detection."""
    raw = f"{title.strip().lower()}|{canonical_url.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def looks_like_index_page(url: str) -> bool:
    """True for section fronts, tag pages and other non-story URLs."""
    if not url:
        return True
    path = urlparse(url).path or "/"
    if _INDEX_PATH_RE.match(path):
        return True
    # A story URL almost always has a slug or a numeric id in its last segment.
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return True
    return len(last) < 4 and not last.isdigit()


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _http_get(url: str, timeout: int, user_agent: str) -> httpx.Response:
    response = httpx.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": user_agent,
            "Accept-Language": "he,ru;q=0.9,en;q=0.8",
        },
        follow_redirects=True,
    )
    response.raise_for_status()
    return response


def fetch(url: str, user_agent: str = _BROWSER_UA, timeout: int = DEFAULT_TIMEOUT) -> Optional[httpx.Response]:
    """GET a URL, returning None on any failure (never raises)."""
    try:
        return _http_get(url, timeout, user_agent)
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s fetching %s", exc.response.status_code, url)
    except Exception as exc:  # noqa: BLE001 - one bad URL must not stop a run
        logger.warning("Failed fetching %s: %s", url, exc)
    return None


def html_to_text(html: str) -> str:
    """Collapse an HTML fragment to plain text."""
    if not html:
        return ""
    if "<" not in html:
        return " ".join(html.split())
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text(separator=" ", strip=True)


def extract_readable_text(html: str, max_chars: int = 3500) -> str:
    """Extract article body text, capped at ``max_chars``.

    Uses trafilatura when it is installed (better boilerplate removal), and
    otherwise falls back to a BeautifulSoup pass that strips chrome elements.
    """
    if not html:
        return ""

    try:
        import trafilatura  # type: ignore[import-not-found]

        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        if text:
            return " ".join(text.split())[:max_chars]
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("trafilatura failed, falling back to bs4: %s", exc)

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    region = soup.find("article") or soup.find("main") or soup.body or soup
    return " ".join(region.get_text(separator=" ", strip=True).split())[:max_chars]


def extract_meta_description(html: str) -> Optional[str]:
    """Read the og:description / meta description dek."""
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    for finder in (
        lambda: soup.find("meta", property="og:description"),
        lambda: soup.find("meta", attrs={"name": "description"}),
    ):
        tag = finder()
        if tag and tag.get("content"):
            text = str(tag["content"]).strip()
            if text:
                return text
    return None


def extract_canonical_link(html: str, page_url: str) -> str:
    """Prefer the page's own canonical URL over the URL we happened to follow."""
    if html:
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("link", rel="canonical")
        if tag and tag.get("href"):
            href = str(tag["href"]).strip()
            if href.startswith("http"):
                return canonicalize_url(href)
        og_url = soup.find("meta", property="og:url")
        if og_url and og_url.get("content"):
            content = str(og_url["content"]).strip()
            if content.startswith("http"):
                return canonicalize_url(content)
    return canonicalize_url(page_url)


def absolutize(href: str, base_url: str) -> str:
    """Resolve a possibly relative href against its listing page."""
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith(("mailto:", "javascript:", "#")):
        return ""
    return urljoin(base_url, href)
