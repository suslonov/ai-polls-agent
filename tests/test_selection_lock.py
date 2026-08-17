"""Selection locking, slot eligibility, and the concurrency guarantee."""

from __future__ import annotations

import threading

import pytest

from src import db
from src.db import SelectionLocked
from tests.conftest import make_item, seed_candidate

DAY = "2026-08-16"


@pytest.fixture
def ready_day(db_path):
    db.ensure_day(db_path, DAY)
    ru = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                        url="https://newsru.co.il/a", title="Русская новость дня")
    en = seed_candidate(db_path, language="en", source_id="toi_en",
                        url="https://timesofisrael.com/b", title="An English story of the day")
    return {"ru": ru, "en": en}


# ── Eligibility ───────────────────────────────────────────────────────────────


def test_slot_eligibility_by_source_language():
    russian = make_item(language="ru")
    english = make_item(language="en")
    hebrew_translated = make_item(language="he", title_en="Translated", short_en="One sentence.")
    hebrew_raw = make_item(language="he")

    assert russian.eligible_for("ru") and not russian.eligible_for("en")
    assert english.eligible_for("en") and not english.eligible_for("ru")

    # Hebrew feeds both slots.
    assert hebrew_translated.eligible_for("ru") and hebrew_translated.eligible_for("en")
    assert hebrew_raw.eligible_for("ru"), "the RU quiz is written from the Hebrew original"
    assert not hebrew_raw.eligible_for("en"), "untranslated Hebrew cannot fill the EN slot"


def test_hebrew_story_can_be_selected_for_both_slots_on_the_same_day(db_path):
    """Nothing stops one Hebrew story from carrying both languages of a day."""
    db.ensure_day(db_path, DAY)
    hebrew = seed_candidate(db_path, language="he", source_id="ynet_he",
                            url="https://ynet.co.il/x", title="כותרת בעברית ארוכה מספיק",
                            title_en="A Hebrew headline", short_en="One sentence.")

    db.set_selection(db_path, DAY, "ru", hebrew, "funny")
    db.set_selection(db_path, DAY, "en", hebrew, "important")
    db.lock_selection(db_path, DAY, "ru")
    locked = db.lock_selection(db_path, DAY, "en")

    assert locked["ru_item_id"] == hebrew and locked["en_item_id"] == hebrew


# ── Before the lock ───────────────────────────────────────────────────────────


def test_selection_can_be_changed_and_cleared_before_start(db_path, ready_day):
    db.set_selection(db_path, DAY, "ru", ready_day["ru"], "important")
    assert db.get_day(db_path, DAY)["ru_tone"] == "important"

    db.set_selection(db_path, DAY, "ru", ready_day["ru"], "funny")
    assert db.get_day(db_path, DAY)["ru_tone"] == "funny"

    db.set_selection(db_path, DAY, "ru", None, None)
    row = db.get_day(db_path, DAY)
    assert row["ru_item_id"] is None and row["ru_tone"] is None


def test_one_language_only_is_allowed(db_path, ready_day):
    db.set_selection(db_path, DAY, "ru", ready_day["ru"], "important")
    locked = db.lock_selection(db_path, DAY, "ru")
    assert locked["ru_item_id"] == ready_day["ru"]
    assert locked["en_item_id"] is None


# ── The lock ──────────────────────────────────────────────────────────────────


def test_lock_requires_a_selection(db_path, ready_day):
    with pytest.raises(SelectionLocked, match="nothing selected"):
        db.lock_selection(db_path, DAY, "ru")


def test_lock_requires_a_tone(db_path, ready_day):
    db.set_selection(db_path, DAY, "en", ready_day["en"], None)
    with pytest.raises(SelectionLocked, match="tone"):
        db.lock_selection(db_path, DAY, "en")


def test_locking_one_language_leaves_the_other_editable(db_path, ready_day):
    """Parallel paths: starting RU must not freeze the EN slot."""
    db.set_selection(db_path, DAY, "ru", ready_day["ru"], "important")
    db.lock_selection(db_path, DAY, "ru")

    with pytest.raises(SelectionLocked, match="RU"):
        db.set_selection(db_path, DAY, "ru", ready_day["en"], "funny")

    # The English slot is untouched and still fully editable.
    db.set_selection(db_path, DAY, "en", ready_day["en"], "funny")
    row = db.get_day(db_path, DAY)
    assert row["ru_item_id"] == ready_day["ru"], "the locked story must survive the attempt"
    assert row["en_item_id"] == ready_day["en"]
    assert row["en_locked_at"] is None

    # And English can then be locked on its own schedule.
    db.lock_selection(db_path, DAY, "en")
    assert db.get_day(db_path, DAY)["en_locked_at"]


def test_lock_is_stamped_and_status_moves_to_generating(db_path, ready_day):
    db.set_selection(db_path, DAY, "en", ready_day["en"], "funny")
    locked = db.lock_selection(db_path, DAY, "en")

    assert locked["en_locked_at"]
    assert locked["ru_locked_at"] is None
    assert locked["selection_locked_at"], "day-level stamp is kept as a summary"
    assert locked["generation_started_at"]
    assert locked["status"] == "generating"


def test_second_lock_attempt_is_rejected(db_path, ready_day):
    db.set_selection(db_path, DAY, "en", ready_day["en"], "important")
    db.lock_selection(db_path, DAY, "en")

    with pytest.raises(SelectionLocked):
        db.lock_selection(db_path, DAY, "en")


def test_two_concurrent_starts_lock_exactly_once(db_path, ready_day):
    """Two browser tabs pressing Start must not create two sets of echoes."""
    db.set_selection(db_path, DAY, "ru", ready_day["ru"], "important")
    db.set_selection(db_path, DAY, "en", ready_day["en"], "funny")

    winners: list[str] = []
    losers: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(name: str) -> None:
        barrier.wait()
        try:
            db.lock_selection(db_path, DAY, "ru")
            winners.append(name)
        except SelectionLocked:
            losers.append(name)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            losers.append(f"{name}:{exc}")

    threads = [threading.Thread(target=attempt, args=(f"tab-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1, f"exactly one caller may proceed to generation, got {winners}"
    assert len(losers) == 7
