"""Languages advance independently, and a finished poll can be closed."""

from __future__ import annotations

import pytest

from src import db, workflow
from src.db import ResetRefused, SelectionLocked
from src.models import EchoStatus
from tests.conftest import seed_candidate

DAY = "2026-08-16"


@pytest.fixture
def both_slots(db_path):
    db.ensure_day(db_path, DAY)
    ru = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                        url="https://newsru.co.il/a", title="Русская новость дня")
    en = seed_candidate(db_path, language="en", source_id="toi_en",
                        url="https://timesofisrael.com/b", title="An English story today")
    db.set_selection(db_path, DAY, "ru", ru, "funny")
    db.set_selection(db_path, DAY, "en", en, "important")
    return {"ru": ru, "en": en}


def test_one_language_can_be_taken_all_the_way_while_the_other_waits(db_path, both_slots):
    """The point of parallel flows: RU can reach publication with EN untouched."""
    db.lock_selection(db_path, DAY, "ru")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 4242,
                                        "status": EchoStatus.published.value})

    workflow_row = db.get_day(db_path, DAY)
    assert workflow_row["ru_locked_at"] and workflow_row["en_locked_at"] is None

    # English is still fully editable — a different story, a different tone.
    other = seed_candidate(db_path, language="en", source_id="jpost_en",
                           url="https://jpost.com/c", title="Another English story today")
    db.set_selection(db_path, DAY, "en", other, "funny")
    assert db.get_day(db_path, DAY)["en_item_id"] == other

    db.lock_selection(db_path, DAY, "en")
    assert db.get_day(db_path, DAY)["en_locked_at"]


def test_resetting_one_language_leaves_the_other_running(db_path, both_slots):
    db.lock_selection(db_path, DAY, "ru")
    db.lock_selection(db_path, DAY, "en")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 1, "status": "error"})
    db.upsert_echo(db_path, DAY, "en", {"kvasir_echo_id": 2, "status": "editing"})

    result = db.reset_generation(db_path, DAY, target_language="ru")

    assert result["orphaned_echoes"] == [{"language": "ru", "echo_id": 1}]
    assert db.get_echo(db_path, DAY, "ru") is None
    assert db.get_echo(db_path, DAY, "en")["kvasir_echo_id"] == 2

    row = db.get_day(db_path, DAY)
    assert row["ru_locked_at"] is None
    assert row["en_locked_at"] is not None, "the other language keeps its lock"
    assert row["ru_item_id"] == both_slots["ru"], "the chosen story is kept"


def test_resetting_a_published_language_touches_only_that_language(db_path, both_slots):
    """Reset never refuses, and it still stops at the language it was given."""
    db.lock_selection(db_path, DAY, "ru")
    db.lock_selection(db_path, DAY, "en")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 1,
                                        "status": EchoStatus.published.value})
    db.upsert_echo(db_path, DAY, "en", {"kvasir_echo_id": 2, "status": "editing"})
    for language, echo_id in (("ru", 1), ("en", 2)):
        db.upsert_publish_event(
            db_path, db.idempotency_key(DAY, language, echo_id, "q1"),
            {"day": DAY, "target_language": language, "kvasir_echo_id": echo_id,
             "scroll_id": "q1", "page_published_at": "2026-08-16T10:00:00+00:00"},
        )

    result = db.reset_generation(db_path, DAY, target_language="ru")

    assert result["orphaned_echoes"] == [{"language": "ru", "echo_id": 1}]
    assert result["dropped_publish_events"] == [
        {"language": "ru", "echo_id": 1, "scroll_id": "q1"}
    ]
    assert db.get_echo(db_path, DAY, "ru") is None
    assert db.get_publish_event_for(db_path, DAY, "ru") is None
    assert db.get_echo(db_path, DAY, "en")["kvasir_echo_id"] == 2
    assert db.get_publish_event_for(db_path, DAY, "en") is not None


# ── Closing a finished poll ───────────────────────────────────────────────────


def test_closing_a_published_poll_frees_the_slot_for_another_one(db_path, both_slots):
    """One per day is the intended rhythm, not a rule the code enforces."""
    db.lock_selection(db_path, DAY, "ru")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 4242, "title": "Первый опрос",
                                        "status": EchoStatus.published.value})

    result = workflow.close_language(db_path, DAY, "ru")
    assert result["closed_echo_id"]

    # The panel is gone, the slot is free, and the story choice is cleared.
    assert db.get_echo(db_path, DAY, "ru") is None
    row = db.get_day(db_path, DAY)
    assert row["ru_locked_at"] is None
    assert row["ru_item_id"] is None and row["ru_tone"] is None

    # A second poll can now be built for the same day and language.
    another = seed_candidate(db_path, language="ru", source_id="9tv_ru",
                             url="https://9tv.co.il/x", title="Вторая новость дня")
    db.set_selection(db_path, DAY, "ru", another, "important")
    db.lock_selection(db_path, DAY, "ru")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 4243, "title": "Второй опрос"})

    assert db.get_echo(db_path, DAY, "ru")["kvasir_echo_id"] == 4243
    everything = db.get_echoes_for_day(db_path, DAY, include_closed=True)
    assert [row["kvasir_echo_id"] for row in everything] == [4242, 4243], "the first is kept"


def test_closed_echoes_are_hidden_from_the_open_view(db_path, both_slots):
    db.lock_selection(db_path, DAY, "ru")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 4242,
                                        "status": EchoStatus.published.value})
    workflow.close_language(db_path, DAY, "ru")

    assert db.get_echoes_for_day(db_path, DAY) == []
    assert len(db.get_echoes_for_day(db_path, DAY, include_closed=True)) == 1


def test_closing_without_an_open_echo_is_refused(db_path, both_slots):
    with pytest.raises(ResetRefused, match="no open RU echo"):
        workflow.close_language(db_path, DAY, "ru")


def test_publish_events_survive_a_close(db_path, both_slots):
    """Closing retires the panel; it never rewrites what was published."""
    db.lock_selection(db_path, DAY, "ru")
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 4242,
                                        "status": EchoStatus.published.value})
    key = db.idempotency_key(DAY, "ru", 4242, "q1")
    db.upsert_publish_event(db_path, key, {
        "day": DAY, "target_language": "ru", "kvasir_echo_id": 4242, "scroll_id": "q1",
        "page_published_at": "2026-08-16T10:00:00+00:00", "telegram_sent_at": "2026-08-16T10:01:00+00:00",
    })

    workflow.close_language(db_path, DAY, "ru")

    event = db.get_publish_event(db_path, key)
    assert event["page_published_at"] and event["telegram_sent_at"]


def test_start_generation_targets_one_language(db_path, settings, secrets, both_slots, monkeypatch):
    from src.category_designer import CategoryResult
    from tests.conftest import FakeKvasirClient, make_design

    monkeypatch.setattr(workflow.quiz_designer, "design_quiz",
                        lambda **kwargs: make_design("Poll"))
    monkeypatch.setattr(workflow.category_designer, "generate_categories",
                        lambda **kwargs: CategoryResult(categories=["You are a reservist"]))

    result = workflow.start_generation(
        settings, secrets, db_path, DAY, target_language="ru", client=FakeKvasirClient()
    )

    assert [echo["language"] for echo in result["echoes"]] == ["ru"]
    assert db.get_echo(db_path, DAY, "en") is None, "English was not started"
    assert db.get_day(db_path, DAY)["en_locked_at"] is None
