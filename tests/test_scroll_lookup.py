"""Finding the one public scroll-quiz an echo publishes."""

from __future__ import annotations

import pytest

from src.scroll_lookup import ScrollLookup, ScrollLookupError, is_public_quiz
from tests.conftest import FakeKvasirClient


def scroll(scroll_id="q1", item_type="quiz", visibility=1, private=0, title="Poll"):
    return {
        "scroll_id": scroll_id,
        "title": title,
        "item_type": item_type,
        "visibility": visibility,
        "private": private,
        "anonymous": 0,
        "keep_stats": 1,
        "content_type": "json",
    }


def test_is_public_quiz_requires_quiz_type_visibility_and_not_private():
    assert is_public_quiz(scroll())
    assert not is_public_quiz(scroll(item_type="scroll")), "a chat transcript is not a quiz"
    assert not is_public_quiz(scroll(item_type="block"))
    assert not is_public_quiz(scroll(visibility=0)), "unpublished quiz"
    assert not is_public_quiz(scroll(private=1)), "private chat state, not a published quiz"


def test_exactly_one_public_quiz_returns_the_target_url():
    client = FakeKvasirClient()
    client.scrolls = [
        scroll("q1"),
        scroll("s1", item_type="scroll"),
        scroll("q2", visibility=0),
    ]

    quiz = ScrollLookup(client).get_public_quiz(4242)

    assert quiz.scroll_id == "q1"
    assert quiz.component_id == 4242
    assert quiz.public_url == "https://quizly.pub/scroll-quiz?id=4242#q1"


def test_zero_public_quizzes_is_an_actionable_error():
    client = FakeKvasirClient()
    client.scrolls = [scroll("s1", item_type="scroll"), scroll("q1", visibility=0)]

    with pytest.raises(ScrollLookupError) as excinfo:
        ScrollLookup(client).get_public_quiz(4242)

    message = str(excinfo.value)
    assert "No public scroll-quiz found for echo 4242" in message
    assert "retry Finalize" in message


def test_two_public_quizzes_refuse_to_guess():
    client = FakeKvasirClient()
    client.scrolls = [scroll("q1", title="First"), scroll("q2", title="Second")]

    with pytest.raises(ScrollLookupError) as excinfo:
        ScrollLookup(client).get_public_quiz(4242)

    message = str(excinfo.value)
    assert "More than one public scroll-quiz" in message
    assert "q1 (First)" in message and "q2 (Second)" in message
    assert "Nothing was published." in message


def test_quiz_without_a_scroll_id_is_rejected():
    client = FakeKvasirClient()
    client.scrolls = [scroll(scroll_id="")]

    with pytest.raises(ScrollLookupError, match="no scroll_id"):
        ScrollLookup(client).get_public_quiz(4242)


def test_lookup_uses_the_supported_list_scrolls_action():
    """No bespoke Kvasir endpoint — only the existing kv2_text action."""
    client = FakeKvasirClient()
    client.scrolls = [scroll()]
    ScrollLookup(client).get_public_quiz(4242)

    actions = [call["payload"]["action"] for call in client.calls if call["function"] == "kv2_text"]
    assert actions == ["list_scrolls"]
