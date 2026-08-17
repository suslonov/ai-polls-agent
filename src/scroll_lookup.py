"""Find the public scroll-quiz belonging to an echo.

The supported path is the existing ``kv2_text`` scroll API (``list_scrolls``);
no new Kvasir endpoint is introduced for this project. Everything lives behind
:class:`ScrollLookup` so tests can substitute a fake.

Shape of one ``list_scrolls`` entry (kv2_text ``_scroll_list_entry``)::

    {"scroll_id": "...", "title": "...", "visibility": 0|1, "private": 0|1,
     "anonymous": 0|1, "keep_stats": 0|1, "content_type": "...",
     "item_type": "scroll"|"quiz"|"block", "updated_at": "...", ...}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

QUIZ_ITEM_TYPE = "quiz"


class ScrollLookupError(RuntimeError):
    """Raised when an echo does not have exactly one public scroll-quiz."""


@dataclass
class PublicQuizScroll:
    """The single public quiz an echo publishes."""

    component_id: int
    scroll_id: str
    title: str
    public_url: str


def is_public_quiz(scroll: dict) -> bool:
    """True for a quiz scroll that is visible without logging in.

    ``visibility`` is normalized to 0/1 by kv2_text before it reaches us, and a
    ``private`` scroll is per-user chat state rather than a published quiz.
    """
    if str(scroll.get("item_type") or "").lower() != QUIZ_ITEM_TYPE:
        return False
    if int(scroll.get("private") or 0):
        return False
    return int(scroll.get("visibility") or 0) == 1


class ScrollLookup:
    """Adapter over the Kvasir scroll API."""

    def __init__(self, client):
        self.client = client

    def list_quiz_scrolls(self, echo_id: Any) -> list[dict]:
        """All public quiz scrolls of an echo."""
        scrolls = self.client.list_scrolls(echo_id, limit=50)
        return [s for s in scrolls if is_public_quiz(s)]

    def get_public_quiz(self, echo_id: Any) -> PublicQuizScroll:
        """Return the echo's one public quiz, or explain what to fix.

        Deliberately refuses to guess: zero and many are both errors, and
        neither publishes anything.
        """
        quizzes = self.list_quiz_scrolls(echo_id)

        if not quizzes:
            raise ScrollLookupError(
                f"No public scroll-quiz found for echo {echo_id}.\n"
                "Open the echo, create/publish the quiz, then retry Finalize."
            )

        if len(quizzes) > 1:
            listed = ", ".join(
                f"{s.get('scroll_id')} ({s.get('title') or 'untitled'})" for s in quizzes
            )
            raise ScrollLookupError(
                f"More than one public scroll-quiz found for echo {echo_id}: {listed}.\n"
                "Leave exactly one public quiz or extend the UI to select one explicitly.\n"
                "Nothing was published."
            )

        scroll = quizzes[0]
        scroll_id = str(scroll.get("scroll_id") or "").strip()
        if not scroll_id:
            raise ScrollLookupError(f"Public quiz for echo {echo_id} has no scroll_id")

        return PublicQuizScroll(
            component_id=int(echo_id),
            scroll_id=scroll_id,
            title=str(scroll.get("title") or "").strip(),
            public_url=self.client.scroll_quiz_url(echo_id, scroll_id),
        )


def optional_lookup(client) -> Optional[ScrollLookup]:
    return ScrollLookup(client) if client is not None else None
