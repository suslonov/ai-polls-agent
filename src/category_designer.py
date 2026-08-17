"""Generate the participant categories a poll is weighed against.

A category is not an article topic. It is a person who could answer the poll from
their own perspective — "You are a reservist", "You already checked apartment
prices abroad" — so the chat can compare how different groups see one question.

The generator runs *after* the quiz concept exists, because it needs the final
yes/no question: the same story yields different constituencies depending on what
is actually being asked. Output is validated (taxonomy labels, duplicates, length,
safety) and falls back to a small curated library rather than shipping filler.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

from src import prompts
from src.dedupe import jaccard, title_tokens
from src.llm import LLMError, claude_json
from src.models import Settings
from src.text_utils import normalize_dashes

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}

# Taxonomy labels: the exact thing this amendment exists to stop producing.
GENERIC_CATEGORY_TERMS = {
    # English
    "politics", "political", "economy", "economics", "society", "social", "israel",
    "israeli", "news", "current events", "government", "people", "citizens", "public",
    "security", "defence", "defense", "army", "military", "transport", "transportation",
    "technology", "tech", "culture", "media", "sport", "sports", "health", "education",
    "environment", "consumer", "consumers", "business", "drivers", "residents",
    "everyday life", "bureaucracy", "science", "weird news", "municipality", "voters",
    # Russian
    "политика", "политики", "экономика", "общество", "израиль", "новости",
    "правительство", "люди", "граждане", "безопасность", "армия", "транспорт",
    "технологии", "культура", "медиа", "спорт", "здоровье", "образование",
    "экология", "потребители", "бизнес", "водители", "жители", "повседневная жизнь",
    "бюрократия", "наука", "странные новости", "муниципалитет", "избиратели",
}

# Words every category shares ("You are a …" / "Вы …") carry no identity, and
# would otherwise make unrelated categories look similar to each other.
_CATEGORY_STOPWORDS = {
    "you", "your", "yours", "are", "is", "was", "the", "a", "an", "of", "in", "on",
    "to", "and", "with", "for", "who", "that", "this", "who's", "person", "one",
    "вы", "ваш", "ваша", "ваше", "ваши", "вас", "вам", "у", "и", "в", "на", "с",
    "по", "за", "не", "тот", "который", "которая", "которые", "это", "как", "кто",
}

# Crude suffix stripping — enough to see that "renter" and "rent" are the same
# idea without pulling in a stemmer dependency.
_EN_SUFFIXES = ("ings", "ing", "ers", "er", "ies", "ed", "s")
_RU_SUFFIXES = ("ами", "ями", "ов", "ев", "ий", "ая", "ое", "ые", "ыми", "ам", "ах",
                "ом", "ы", "и", "а", "у", "е", "о")

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
# Two or more sentence-ending marks followed by more words = a paragraph, not a label.
_MULTI_SENTENCE_RE = re.compile(r"[.!?…]\s+\S")


class CategoryResult(BaseModel):
    """Validated categories for one echo."""

    categories: list[str] = Field(default_factory=list)
    party_categories_used: bool = False
    fallback_used: bool = False
    rejected_count: int = 0
    rationale: Optional[str] = None  # logs/debugging only — never sent to a model


@dataclass
class Rejection:
    value: str
    reason: str


@dataclass
class ValidationOutcome:
    kept: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


# ── Validation ────────────────────────────────────────────────────────────────


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", normalize_dashes(value or "")).strip()
    return " ".join(text.split())


def _comparison_key(value: str) -> str:
    text = _normalize(value).lower().rstrip(".!?…")
    return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)


def _stem(word: str) -> str:
    for suffix in _EN_SUFFIXES + _RU_SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def category_tokens(value: str) -> set[str]:
    """Identity-bearing stems of a category, for near-duplicate detection."""
    words = _comparison_key(value).split()
    return {
        _stem(word)
        for word in words
        if len(word) > 2 and word not in _CATEGORY_STOPWORDS
    }


def _is_near_duplicate(tokens: set[str], existing: set[str], threshold: float) -> bool:
    """Near-duplicate by lexical overlap of identity-bearing stems.

    Deliberately lexical only. Containment ("You are a renter" inside "You rent
    your home") is tempting, but it is indistinguishable from a genuinely
    different role: "You are a reservist" is likewise contained in "You employ
    reservists", and those are two constituencies the poll wants to compare.
    Semantic pairs are the prompt's job; over-merging here would silently delete
    a real perspective.
    """
    if not tokens or not existing:
        return False
    return jaccard(tokens, existing) >= threshold


def looks_generic(value: str) -> bool:
    """True for taxonomy labels rather than participant identities."""
    key = _comparison_key(value)
    if key in GENERIC_CATEGORY_TERMS:
        return True
    # "Israeli society", "Current events in Israel" — a bare noun phrase built
    # only from taxonomy words is still a taxonomy label.
    words = [word for word in key.split() if word not in {"in", "of", "the", "and", "в", "и", "на"}]
    return bool(words) and len(words) <= 3 and all(
        word in GENERIC_CATEGORY_TERMS for word in words
    )


def validate_categories(
    raw: list[str],
    max_chars: int = 90,
    near_duplicate_threshold: float = 0.6,
) -> ValidationOutcome:
    """Drop everything that is not a usable participant identity.

    Rejects blanks, over-long entries, URLs, multi-sentence paragraphs, generic
    taxonomy labels, exact duplicates and near-duplicates ("You are a renter" vs
    "You rent your home"). Order is preserved: the first phrasing wins.
    """
    outcome = ValidationOutcome()
    seen_keys: set[str] = set()
    seen_tokens: list[set[str]] = []

    for entry in raw or []:
        if not isinstance(entry, str):
            outcome.rejected.append(Rejection(str(entry), "not a string"))
            continue

        value = _normalize(entry)
        if not value:
            outcome.rejected.append(Rejection(entry, "blank"))
            continue
        if len(value) > max_chars:
            outcome.rejected.append(Rejection(value, f"longer than {max_chars} characters"))
            continue
        if _URL_RE.search(value):
            outcome.rejected.append(Rejection(value, "contains a URL"))
            continue
        if _MULTI_SENTENCE_RE.search(value):
            outcome.rejected.append(Rejection(value, "multi-sentence"))
            continue
        if value.startswith(("{", "[")) or value.endswith(("}", "]")):
            outcome.rejected.append(Rejection(value, "JSON fragment"))
            continue
        if looks_generic(value):
            outcome.rejected.append(Rejection(value, "generic taxonomy label"))
            continue

        key = _comparison_key(value)
        if key in seen_keys:
            outcome.rejected.append(Rejection(value, "duplicate"))
            continue

        tokens = category_tokens(value)
        if any(
            _is_near_duplicate(tokens, existing, near_duplicate_threshold)
            for existing in seen_tokens
        ):
            outcome.rejected.append(Rejection(value, "near-duplicate"))
            continue

        seen_keys.add(key)
        if tokens:
            seen_tokens.append(tokens)
        outcome.kept.append(value)

    return outcome


# ── Political relevance and party categories ──────────────────────────────────


def looks_political(settings: Settings, target_language: str, *texts: str) -> bool:
    """Cheap keyword check: is this story political enough for party categories?"""
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return False
    return any(keyword.lower() in haystack for keyword in settings.political_keywords_for(target_language))


def party_categories(
    settings: Settings,
    target_language: str,
    template_defaults: Optional[list[str]] = None,
    limit: Optional[int] = None,
) -> list[str]:
    """Party names rendered as participant identities, plus 'undecided'.

    The deployed prompt template carries its own party list in the CATEGORIES
    marker's DEFAULT payload; that list wins over the configured one.
    """
    names = list(template_defaults or []) or settings.party_defaults_for(target_language)
    cap = limit if limit is not None else settings.parties.max_in_poll
    formatted = [settings.parties.format_party(name, target_language) for name in names[:cap]]

    undecided = settings.parties.undecided_for(target_language)
    if undecided:
        formatted.append(undecided)
    return formatted


# ── Fallbacks ─────────────────────────────────────────────────────────────────

# Which fallback pool fits a story, by keyword. Checked in order.
_DOMAIN_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("housing", ("rent", "apartment", "housing", "mortgage", "аренд", "квартир", "жиль", "ипотек")),
    ("transport", ("parking", "traffic", "bus", "train", "scooter", "commut", "road",
                   "парков", "транспорт", "автобус", "поезд", "самокат", "дорог", "машин")),
    ("security", ("reserve", "army", "soldier", "border", "war", "резерв", "армия",
                  "солдат", "границ", "войн", "цахал")),
    ("education", ("school", "teacher", "student", "university", "школ", "учител",
                   "студент", "университет", "образован")),
    ("health", ("hospital", "clinic", "doctor", "health", "больниц", "врач", "здоров",
                "больничн", "клиник")),
    ("consumer", ("price", "shop", "supermarket", "product", "consumer", "цен", "магазин",
                  "супермаркет", "продукт", "потребител", "покупк")),
]


def detect_domain(*texts: str) -> str:
    """Pick the fallback library that best matches the story."""
    haystack = " ".join(t for t in texts if t).lower()
    for domain, hints in _DOMAIN_HINTS:
        if any(hint in haystack for hint in hints):
            return domain
    return "general"


def build_fallback(
    settings: Settings,
    tone: str,
    target_language: str,
    domain: str,
    is_political: bool,
    template_defaults: Optional[list[str]] = None,
) -> tuple[list[str], bool]:
    """Curated last-resort categories. Returns ``(categories, party_used)``.

    Party categories are only ever used for political stories, and never for a
    funny poll unless the story is clearly political.
    """
    categories: list[str] = []
    party_used = False

    if is_political and tone != "funny":
        categories.extend(party_categories(settings, target_language, template_defaults))
        party_used = bool(categories)

    pool = settings.fallback_pool(tone, domain, target_language)
    categories.extend(pool)

    if len(categories) < settings.category_generation.min_count:
        categories.extend(settings.fallback_pool(tone, "general", target_language))

    outcome = validate_categories(
        categories,
        max_chars=settings.category_generation.max_chars,
        near_duplicate_threshold=settings.category_generation.near_duplicate_threshold,
    )
    return outcome.kept[: settings.category_generation.cap], party_used


# ── Generation ────────────────────────────────────────────────────────────────


def default_categories(
    *,
    settings: Settings,
    language: Literal["en", "ru"],
    mode: Literal["important", "funny"],
    news_title: str,
    news_summary: str,
    party_defaults: Optional[list[str]] = None,
) -> CategoryResult:
    """The template's own categories, with no model call at all.

    This is the operator's "use default, don't invent" tick: whatever the prompt
    template carries in its ``{{CATEGORIES DEFAULT=...}}`` payload for this
    language is what the echo gets, used as written — only stripped of blanks
    and duplicates. It is not re-validated against the "You are …" shape and not
    trimmed to the per-poll bounds, because it is the template author's list,
    not a model's guess.

    The automatic path's party gate is deliberately not applied here. That gate
    exists so the machine never invents a party lineup where it does not belong;
    an operator ticking this box on a specific poll is not the machine, and the
    categories are visible in the panel before anything is published.

    A template with no payload for this language falls back to the curated pool
    for the tone and domain — still deterministic, still no model call.
    """
    values = [str(value).strip() for value in (party_defaults or []) if value and str(value).strip()]

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)

    if unique:
        logger.info("Using the template's default categories (%d): %s", len(unique), unique)
        return CategoryResult(
            categories=unique,
            party_categories_used=True,
            rationale="template DEFAULT payload; no model call",
        )

    domain = detect_domain(news_title, news_summary, "")
    categories, _ = build_fallback(
        settings=settings,
        tone=mode,
        target_language=language,
        domain=domain,
        is_political=False,
        template_defaults=None,
    )
    logger.warning(
        "The template carries no default categories for %s; using the curated "
        "%s/%s pool instead", language, mode, domain,
    )
    return CategoryResult(
        categories=categories,
        fallback_used=True,
        rationale=f"template had no DEFAULT payload; curated {mode}/{domain} pool",
    )


def generate_categories(
    *,
    settings: Settings,
    language: Literal["en", "ru"],
    mode: Literal["important", "funny"],
    news_title: str,
    news_summary: str,
    proposed_question: str,
    api_key: str,
    model: str,
    party_defaults: Optional[list[str]] = None,
) -> CategoryResult:
    """Produce validated participant categories for one poll.

    ``party_defaults`` are the bare party names carried by the prompt template's
    CATEGORIES marker; they are offered to the model only when the story is
    political, and used as fallback material on the same condition.
    """
    config = settings.category_generation
    is_political = looks_political(settings, language, news_title, news_summary, proposed_question)
    domain = detect_domain(news_title, news_summary, proposed_question)

    offered_parties: list[str] = []
    if is_political:
        offered_parties = party_categories(settings, language, party_defaults)

    prompt_name = "categories_funny" if mode == "funny" else "categories_important"
    prompt = prompts.render(
        prompt_name,
        language_name=LANGUAGE_NAMES.get(language, "English"),
        min_items=max(config.min_count, config.target_count - 2),
        max_items=config.cap,
        title=news_title,
        summary=news_summary,
        question=proposed_question,
        party_defaults=(
            "\n".join(offered_parties)
            if offered_parties
            else "(none — this story is not political; do not use party categories)"
        ),
    )

    raw: list[str] = []
    claimed_party_use = False
    rationale = None

    try:
        response = claude_json(
            api_key=api_key,
            model=model,
            system=prompt,
            prompt=(
                "Generate the participant categories for this poll. "
                "Reply with the JSON object only."
            ),
            max_tokens=config.max_tokens,
        )
        raw = [str(value) for value in (response.get("categories") or [])]
        claimed_party_use = bool(response.get("party_categories_used"))
        rationale = response.get("rationale")
    except LLMError as exc:
        logger.warning("Category generation failed (%s/%s): %s", language, mode, exc)
        rationale = f"generation failed: {exc}"

    outcome = validate_categories(
        raw,
        max_chars=config.max_chars,
        near_duplicate_threshold=config.near_duplicate_threshold,
    )
    for rejection in outcome.rejected:
        logger.info("Category rejected (%s): %r — %s", mode, rejection.value, rejection.reason)

    kept = outcome.kept[: config.cap]
    fallback_used = False

    if len(kept) < config.min_count:
        fallback, party_used = build_fallback(
            settings, mode, language, domain, is_political, party_defaults
        )
        # Keep whatever survived validation, then top up from the library.
        merged = validate_categories(
            kept + fallback,
            max_chars=config.max_chars,
            near_duplicate_threshold=config.near_duplicate_threshold,
        )
        kept = merged.kept[: config.cap]
        fallback_used = True
        claimed_party_use = claimed_party_use or party_used
        logger.warning(
            "Only %d valid categories for %s/%s — topped up from the %s fallback library",
            len(outcome.kept), language, mode, domain,
        )

    result = CategoryResult(
        categories=kept,
        party_categories_used=bool(claimed_party_use and is_political),
        fallback_used=fallback_used,
        rejected_count=len(outcome.rejected),
        rationale=rationale,
    )
    logger.info(
        "Categories %s/%s: %d kept, %d rejected, political=%s domain=%s "
        "parties=%s fallback=%s",
        language, mode, len(result.categories), result.rejected_count,
        is_political, domain, result.party_categories_used, result.fallback_used,
    )
    return result


def serialize_for_template(categories: list[str]) -> str:
    """Render categories as the *contents* of the template's JSON array.

    The deployed template reads ``"categories": [{{CATEGORIES DEFAULT={...}}}]``,
    so the substitution must produce comma-separated JSON strings — not an
    object, and not newline-separated bare words (which is invalid JSON there).
    """
    return ", ".join(json.dumps(category, ensure_ascii=False) for category in categories)
