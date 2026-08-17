"""Cheap first-stage prefilter and Hebrew→English enrichment (Gemini).

The prefilter never sees article bodies — only title, dek and a short snippet —
which is what keeps a 250-story day affordable. It returns keep/drop plus three
scores and a free-form ``story_group_hint`` used to merge the same event across
Hebrew, Russian and English.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src import prompts
from src.llm import LLMError, gemini_json
from src.models import NewsItem, PrefilterVerdict

logger = logging.getLogger(__name__)

MAX_SNIPPET_CHARS = 400

# Aim for at least this many candidates per operator slot when the day's
# material allows it (plan §7).
LANGUAGE_FLOOR = 5

# Prompt text lives in config/prompts/prefilter.txt (see src/prompts.py).
PROMPT_NAME = "prefilter"


def _age_hours(item: NewsItem, now: datetime) -> Optional[float]:
    if not item.published_at:
        return None
    published = item.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return round((now - published).total_seconds() / 3600, 1)


def build_payload(items: list[NewsItem], offset: int, now: datetime) -> list[dict]:
    """Compact per-item payload. Deliberately excludes full article text."""
    payload = []
    for index, item in enumerate(items):
        snippet = (item.snippet_original or item.dek_original or "")[:MAX_SNIPPET_CHARS]
        entry = {
            "id": offset + index,
            "lang": item.source_language,
            "source": item.source_name,
            "title": item.title_original,
            "dek": (item.dek_original or "")[:200],
            "snippet": snippet,
        }
        age = _age_hours(item, now)
        if age is not None:
            entry["age_hours"] = age
        payload.append(entry)
    return payload


def parse_verdicts(response: dict) -> dict[int, PrefilterVerdict]:
    """Turn the model's JSON into verdicts keyed by payload id."""
    verdicts: dict[int, PrefilterVerdict] = {}
    for raw in response.get("items", []) or []:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        try:
            verdict = PrefilterVerdict(
                id=int(raw["id"]),
                keep=bool(raw.get("keep", False)),
                israel_relevance=float(raw.get("israel_relevance", 0) or 0),
                interesting_score=float(raw.get("interesting_score", 0) or 0),
                funny_score=float(raw.get("funny_score", 0) or 0),
                topic=str(raw.get("topic", "") or "").strip(),
                story_group_hint=str(raw.get("story_group_hint", "") or "").strip(),
                reason=str(raw.get("reason", "") or "").strip(),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Unparsable prefilter verdict %r: %s", raw, exc)
            continue
        verdicts[verdict.id] = verdict
    return verdicts


def deterministic_score(item: NewsItem, now: datetime) -> float:
    """Fallback ranking when Gemini is unavailable.

    Recency plus source priority — crude, but it keeps the day running instead
    of shipping an empty candidate list to the operator.
    """
    age = _age_hours(item, now)
    recency = 100.0 if age is None else max(0.0, 100.0 - age * 2.0)
    body_bonus = min(len(item.snippet_original or item.dek_original or "") / 20.0, 20.0)
    telegram_penalty = 15.0 if item.source_type == "telegram_public" else 0.0
    return recency + body_bonus - telegram_penalty


def run_prefilter(
    items: list[NewsItem],
    api_key: str,
    model: str,
    keep_target: int,
    batch_size: int = 40,
    max_output_tokens: int = 8192,
    now: Optional[datetime] = None,
) -> tuple[list[NewsItem], dict[int, str]]:
    """Score every candidate and return the top ``keep_target`` items.

    Returns ``(kept_items, story_group_hints)`` where hints map the *index in
    the input list* to the model's story key. On total model failure we fall
    back to deterministic ranking so the pipeline still produces candidates.
    """
    if not items:
        return [], {}

    now = now or datetime.now(timezone.utc)
    scored: list[tuple[float, NewsItem]] = []
    hints: dict[int, str] = {}
    failures = 0

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        payload = build_payload(batch, start, now)
        prompt = (
            "Screen these Israeli news stories.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

        try:
            response = gemini_json(
                api_key=api_key,
                model=model,
                prompt=prompt,
                max_output_tokens=max_output_tokens,
                system=prompts.load(PROMPT_NAME),
            )
            verdicts = parse_verdicts(response)
        except LLMError as exc:
            failures += 1
            logger.warning("Prefilter batch %d failed: %s", start // batch_size, exc)
            verdicts = {}

        for index, item in enumerate(batch):
            verdict = verdicts.get(start + index)
            if verdict is None:
                # No verdict (batch failed, or the model skipped the item):
                # keep it in play with a deterministic score.
                scored.append((deterministic_score(item, now), item))
                continue

            item.prefilter_keep = verdict.keep
            item.prefilter_relevance_score = verdict.israel_relevance
            item.prefilter_interesting_score = verdict.interesting_score
            item.prefilter_funny_score = verdict.funny_score
            if verdict.topic:
                item.topic = verdict.topic
            if verdict.story_group_hint:
                hints[start + index] = verdict.story_group_hint

            if not verdict.keep:
                continue
            rank_score = max(verdict.interesting_score, verdict.funny_score) + (
                verdict.israel_relevance * 0.25
            )
            scored.append((rank_score, item))

    if failures:
        logger.warning("Prefilter completed with %d failed batch(es)", failures)

    scored.sort(key=lambda pair: pair[0], reverse=True)
    kept = [item for _, item in scored[:keep_target]]

    kept = _ensure_language_floor(kept, scored, floor=min(LANGUAGE_FLOOR, keep_target))
    logger.info(
        "Prefilter: %d in → %d kept (he=%d ru=%d en=%d)",
        len(items),
        len(kept),
        sum(1 for i in kept if i.source_language == "he"),
        sum(1 for i in kept if i.source_language == "ru"),
        sum(1 for i in kept if i.source_language == "en"),
    )
    return kept, hints


def _ensure_language_floor(
    kept: list[NewsItem],
    scored: list[tuple[float, NewsItem]],
    floor: int = 5,
) -> list[NewsItem]:
    """Top up under-represented languages so both operator slots have options.

    We only promote items the model already scored — never invent filler to hit
    a quota. Hebrew stories are eligible for both slots, but a shortlist made of
    Hebrew alone gives the Russian slot no native-language choice, so Russian
    items get their own floor. Topping up can push the list slightly past
    ``prefilter_keep``; that is deliberate, since a shortlist with nothing for
    one slot is useless.
    """
    kept_ids = {id(item) for item in kept}

    def top_up(predicate, needed: int) -> None:
        if needed <= 0:
            return
        for _, item in scored:
            if needed <= 0:
                break
            if id(item) in kept_ids or not predicate(item):
                continue
            kept.append(item)
            kept_ids.add(id(item))
            needed -= 1

    ru_count = sum(1 for i in kept if i.source_language == "ru")
    en_count = sum(1 for i in kept if i.source_language in ("en", "he"))
    top_up(lambda i: i.source_language == "ru", floor - ru_count)
    top_up(lambda i: i.source_language in ("en", "he"), floor - en_count)

    return kept


# ── Hebrew enrichment ─────────────────────────────────────────────────────────

# Prompt text lives in config/prompts/translate.txt.
TRANSLATE_PROMPT_NAME = "translate"


def translate_item(
    item: NewsItem,
    api_key: str,
    model: str,
    max_output_tokens: int = 512,
) -> Optional[tuple[str, str]]:
    """Translate one Hebrew story's headline and gist into English.

    Returns None on failure — the caller keeps the Hebrew original and marks
    the translation missing rather than blocking the run.
    """
    body = item.text_for_prompt(1200)
    prompt = json.dumps(
        {
            "title": item.title_original,
            "text": body,
            "source": item.source_name,
        },
        ensure_ascii=False,
    )

    try:
        response = gemini_json(
            api_key=api_key,
            model=model,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            system=prompts.load(TRANSLATE_PROMPT_NAME),
        )
    except LLMError as exc:
        logger.warning("Translation failed for item %s: %s", item.id, exc)
        return None

    title_en = str(response.get("title_en", "") or "").strip()
    short_en = str(response.get("short_en", "") or "").strip()
    if not title_en or not short_en:
        logger.warning("Translation for item %s missing fields", item.id)
        return None
    return title_en, short_en
