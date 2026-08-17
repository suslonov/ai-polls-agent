"""Load the model instructions this repo sends, from ``config/prompts/``.

Every prompt this pipeline sends to a model lives in a `.txt` file there, so it
can be edited and reviewed without touching Python — the same convention
`ai-news-agent` uses. Placeholders are ``{{ name }}``; JSON examples inside a
prompt use ordinary single braces and need no escaping.

    prefilter.txt       cheap screening pass          (Gemini)
    translate.txt       Hebrew → English enrichment   (Gemini)
    selector.txt        the day's shortlist           (Claude)
    quiz_designer.txt   title/description/question    (Claude)

The echo prompt itself is a separate thing entirely: it is the Kvasir template's
own text asset, cloned and filled in S3 (see :mod:`src.echo_builder`).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


class PromptError(RuntimeError):
    """Raised when a prompt file is missing or a placeholder is left unfilled."""


def prompt_path(name: str) -> Path:
    """Absolute path of one prompt file."""
    return PROMPT_DIR / f"{name}.txt"


def load(name: str) -> str:
    """Read a prompt template by name (without the .txt extension)."""
    path = prompt_path(name)
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise PromptError(f"Prompt file not found: {path}") from exc


def render(name: str, **values: object) -> str:
    """Fill ``{{ placeholders }}`` and refuse to send a half-filled prompt."""
    text = load(name)
    for key, value in values.items():
        text = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", str(value), text)

    leftover = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if leftover:
        raise PromptError(
            f"{prompt_path(name)} still has unfilled placeholder(s): {leftover}"
        )
    return text


def placeholders(name: str) -> set[str]:
    """Placeholder names a prompt file expects — used by the tests."""
    return set(_PLACEHOLDER_RE.findall(load(name)))
