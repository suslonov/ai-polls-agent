"""Telegram announcement of a published poll.

Only ever called after the stable page has been updated, and only once per
publish event — the caller checks ``telegram_sent_at`` first, so a second
Finalize click cannot post the same poll twice.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.text_utils import clean

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT = 20


class TelegramError(RuntimeError):
    """Raised when Telegram rejects or drops the message."""


def build_message(title: str, description_text: str, public_url: str) -> str:
    """Compose the channel post: title, tiny description, stable URL."""
    parts = [part for part in (clean(title), clean(description_text), public_url) if part]
    return "\n\n".join(parts)


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: int = TIMEOUT,
) -> str:
    """Post to a channel and return the Telegram message id.

    Link previews stay on: the preview of the stable page is the point of the
    post.
    """
    if not bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is empty")
    if not chat_id:
        raise TelegramError("Telegram channel is not configured for this language")

    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    try:
        response = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - normalized to TelegramError
        raise TelegramError(f"Telegram request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code != 200 or not payload.get("ok"):
        # The token is in the URL, so never log the URL itself.
        raise TelegramError(
            f"Telegram API error {response.status_code}: "
            f"{payload.get('description') or response.text[:200]}"
        )

    message_id = str((payload.get("result") or {}).get("message_id") or "")
    logger.info("Telegram message %s sent to %s", message_id or "?", chat_id)
    return message_id


def announce(
    bot_token: str,
    chat_id: str,
    title: str,
    description_text: str,
    public_url: str,
) -> Optional[str]:
    """Send the announcement, returning the message id."""
    text = build_message(title, description_text, public_url)
    return send_message(bot_token, chat_id, text)
