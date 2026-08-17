"""Quiz design: turn one selected story into an echo title, description and prompt block.

Runs only after the day's selection is locked, once per selected language.
The model returns plain text; this module — not the model — builds the HTML, so
the description can never contain anything but the one source link we put there.
"""

from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import urlparse

from src import prompts
from src.llm import claude_json
from src.models import NewsItem, QuizDesign
from src.text_utils import clean, normalize_dashes

logger = logging.getLogger(__name__)

SOURCE_LABEL = {"en": "source", "ru": "источник"}

# Prompt text lives in config/prompts/quiz_designer.txt (see src/prompts.py).
PROMPT_NAME = "quiz_designer"

LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}


def _valid_http_url(url: str) -> str:
    """Return the URL if it is a plain http(s) link, else raise."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"source URL must be http(s): {url!r}")
    return parsed.geturl()


MAX_LINK_TEXT_CHARS = 120


def link_text_for(item: NewsItem, target_language: str) -> str:
    """The article title to show as the link text, in the most readable language.

    A Hebrew headline is unreadable to most RU/EN readers, so a translated title
    is preferred when the source language differs from the target.
    """
    if item.source_language == target_language:
        title = item.title_original
    else:
        title = item.title_en or item.title_original
    title = clean(title)
    if len(title) > MAX_LINK_TEXT_CHARS:
        title = title[: MAX_LINK_TEXT_CHARS - 1].rstrip() + "…"
    return title


def build_description_html(
    description_text: str,
    source_url: str,
    target_language: str,
    link_text: str = "",
) -> str:
    """Compose the echo description: escaped model text + exactly one source link.

    The link is labelled with the article's own title (falling back to a plain
    "source" label when no title is available), so a reader can see where the
    poll comes from before clicking.

    The model's text is HTML-escaped before concatenation, so a model that tries
    to emit its own ``<a>`` produces visible text, not a second link.
    """
    url = _valid_http_url(source_url)
    label = clean(link_text) or SOURCE_LABEL.get(target_language, SOURCE_LABEL["en"])
    text = html.escape(clean(description_text), quote=False)
    href = html.escape(url, quote=True)
    safe_label = html.escape(label, quote=False)
    return f'{text} <a href="{href}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'


def count_anchors(description_html: str) -> int:
    """Number of ``<a`` tags in the composed description."""
    return len(re.findall(r"<a\b", description_html or "", flags=re.IGNORECASE))


_BLOCK_LABELS = {
    "en": {
        "news": "News",
        "question": "Proposed yes/no question",
        "article": "Full article text (raw source material)",
        "title": "Original headline",
    },
    "ru": {
        "news": "Новость",
        "question": "Предлагаемый вопрос да/нет",
        "article": "Полный текст статьи (исходный материал)",
        "title": "Оригинальный заголовок",
    },
}


def build_news_summary_block(
    summary: str,
    question: str,
    target_language: str,
    article_text: str = "",
    original_title: str = "",
    max_article_chars: int = 6000,
) -> str:
    """The block substituted for ``{{NEWS_SUMMARY}}`` in the template prompt.

    Carries the model's summary, the proposed question, and as much of the raw
    article as fits: the chat host writes better poll questions when it can see
    the source material rather than a compressed retelling.

    The article URL deliberately stays out of this block — the required source
    link belongs in the echo description.
    """
    labels = _BLOCK_LABELS.get(target_language, _BLOCK_LABELS["en"])
    parts = [f"{labels['news']}:\n{normalize_dashes(summary).strip()}"]

    if original_title:
        parts.append(f"{labels['title']}:\n{clean(original_title)}")

    parts.append(f"{labels['question']}:\n{normalize_dashes(question).strip()}")

    body = normalize_dashes(article_text or "").strip()
    if body:
        parts.append(f"{labels['article']}:\n{body[:max_article_chars].strip()}")

    return "\n\n".join(parts)


def design_quiz(
    item: NewsItem,
    target_language: str,
    tone: str,
    persona: str,
    api_key: str,
    model: str,
    max_chars: int,
    max_tokens: int = 2048,
) -> QuizDesign:
    """Ask Claude to design the poll for one selected story.

    Participant categories are *not* produced here: they are generated
    separately from the final yes/no question (see :mod:`src.category_designer`).
    """
    language_name = LANGUAGE_NAMES.get(target_language, "English")
    system = prompts.render(
        PROMPT_NAME, language_name=language_name, persona=persona.strip()
    )

    payload = {
        "target_language": target_language,
        "tone": tone,
        "source_language": item.source_language,
        "original_title": item.title_original,
        "english_title": item.title_en,
        "english_summary": item.short_en,
        "article_excerpt": item.text_for_prompt(max_chars),
        "original_url": item.url,
    }
    prompt = (
        "Design today's poll from this story.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    response = claude_json(
        api_key=api_key,
        model=model,
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
    )

    design = QuizDesign(
        title=clean(str(response.get("title", ""))),
        description_text=clean(str(response.get("description_text", ""))),
        news_summary_for_prompt=normalize_dashes(
            str(response.get("news_summary_for_prompt", ""))
        ),
        yes_no_question=clean(str(response.get("yes_no_question", ""))),
        greeting=clean(str(response.get("greeting", "") or "")),
        picture_suggestions=[
            clean(str(s))
            for s in (response.get("picture_suggestions") or [])
            if str(s).strip()
        ][:5],
    )
    logger.info(
        "Quiz designed for %s/%s: %r", target_language, tone, design.title[:60]
    )
    return design
