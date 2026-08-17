"""Shared text normalization for everything this project publishes.

House rule: no fancy dashes anywhere the reader or the chat can see — page
entries, echo titles and descriptions, categories, greetings, the filled prompt
and Telegram posts all use a plain hyphen.
"""

from __future__ import annotations

from typing import Any

# Em dash, en dash, horizontal bar, figure dash, minus sign, non-breaking hyphen.
_DASHES = {
    "—": "-",  # —
    "–": "-",  # –
    "―": "-",  # ―
    "‒": "-",  # ‒
    "−": "-",  # −
    "‑": "-",  # ‑
}
_TRANSLATION = str.maketrans(_DASHES)


def normalize_dashes(value: str) -> str:
    """Replace typographic dashes with a plain hyphen."""
    if not value:
        return value
    return value.translate(_TRANSLATION)


def clean(value: str) -> str:
    """Normalize dashes and collapse whitespace — for short single-line fields."""
    if not value:
        return value
    return " ".join(normalize_dashes(value).split())


def clean_deep(value: Any) -> Any:
    """Apply :func:`normalize_dashes` through nested lists/dicts of strings."""
    if isinstance(value, str):
        return normalize_dashes(value)
    if isinstance(value, list):
        return [clean_deep(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_deep(item) for key, item in value.items()}
    return value
