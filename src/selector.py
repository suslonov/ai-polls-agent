"""Claude final candidate selection.

Only the prefilter-kept set reaches Claude, and each story is sent as a short
record (title, dek, capped body excerpt, prefilter scores, duplicate group).
Claude returns a single global shortlist of 10-20 stories across all three
source languages — not a per-language quota.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src import prompts
from src.llm import LLMError, claude_json
from src.models import NewsItem, SelectorPick

logger = logging.getLogger(__name__)

# Prompt text lives in config/prompts/selector.txt (see src/prompts.py).
PROMPT_NAME = "selector"


def build_payload(items: list[NewsItem], max_chars: int, now: datetime) -> list[dict]:
    """One compact record per candidate."""
    payload = []
    for index, item in enumerate(items):
        entry: dict = {
            "id": index,
            "lang": item.source_language,
            "source": item.source_name,
            "title": item.title_original,
            "text": item.text_for_prompt(max_chars),
        }
        if item.published_at:
            published = item.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            entry["published_at"] = published.isoformat()
            entry["age_hours"] = round((now - published).total_seconds() / 3600, 1)
        if item.duplicate_group:
            entry["duplicate_group"] = item.duplicate_group
        if item.prefilter_interesting_score is not None:
            entry["prefilter"] = {
                "israel_relevance": item.prefilter_relevance_score,
                "interesting": item.prefilter_interesting_score,
                "funny": item.prefilter_funny_score,
            }
        payload.append(entry)
    return payload


# A topic is a filing label ("bureaucracy"), not a headline. Models drift into
# restating - or translating - the title here, which then shows up as an English
# sentence above a Russian card. Anything longer is dropped so the prefilter's
# own label survives (update_selection keeps the existing topic on an empty one).
MAX_TOPIC_WORDS = 3
MAX_TOPIC_CHARS = 32


def clean_topic(value: str) -> str:
    """Keep a short label, drop a restated headline."""
    topic = " ".join(str(value or "").strip().split())
    if not topic:
        return ""
    if len(topic) > MAX_TOPIC_CHARS or len(topic.split()) > MAX_TOPIC_WORDS:
        logger.info("Dropping selector topic (not a label): %r", topic)
        return ""
    return topic.lower()


def parse_picks(response: dict, valid_ids: set[int]) -> list[SelectorPick]:
    """Validate the model's shortlist against the ids we actually sent."""
    picks: list[SelectorPick] = []
    seen: set[int] = set()

    for raw in response.get("selected", []) or []:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        try:
            item_id = int(raw["id"])
        except (TypeError, ValueError):
            continue
        if item_id not in valid_ids or item_id in seen:
            continue
        seen.add(item_id)
        picks.append(
            SelectorPick(
                id=item_id,
                rank=int(raw.get("rank", len(picks) + 1) or len(picks) + 1),
                interesting_score=float(raw.get("interesting_score", 0) or 0),
                funny_score=float(raw.get("funny_score", 0) or 0),
                topic=clean_topic(raw.get("topic", "")),
                why_candidate=str(raw.get("why_candidate", "") or "").strip(),
            )
        )

    picks.sort(key=lambda pick: pick.rank)
    return picks


def select_candidates(
    items: list[NewsItem],
    api_key: str,
    model: str,
    min_items: int,
    max_items: int,
    max_chars: int,
    max_tokens: int = 8192,
    now: Optional[datetime] = None,
) -> list[tuple[NewsItem, SelectorPick]]:
    """Ask Claude for the day's shortlist.

    Raises :class:`~src.llm.LLMError` on failure — the caller marks the run
    failed and leaves the previous day's state untouched.
    """
    if not items:
        return []

    now = now or datetime.now(timezone.utc)
    payload = build_payload(items, max_chars, now)
    system = prompts.render(PROMPT_NAME, min_items=min_items, max_items=max_items)
    prompt = (
        f"Today is {now.date().isoformat()}. Candidate stories:\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    response = claude_json(
        api_key=api_key,
        model=model,
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
    )
    picks = parse_picks(response, valid_ids=set(range(len(items))))

    if not picks:
        raise LLMError("Claude returned no usable selection")
    if len(picks) > max_items:
        picks = picks[:max_items]

    logger.info("Claude selected %d candidates: %s", len(picks), [p.id for p in picks])
    return [(items[pick.id], pick) for pick in picks]
