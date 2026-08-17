"""Database state machine: runs, item identity, workflow and echo rows."""

from __future__ import annotations

from src import db
from src.models import EchoStatus, WorkflowStatus
from tests.conftest import make_item, seed_candidate


def test_run_lifecycle(db_path):
    from src.models import RunStats

    run_id = db.start_run(db_path, "2026-08-16")
    run = db.get_latest_run(db_path, "2026-08-16")
    assert run["status"] == "running"

    stats = RunStats(run_id=run_id, collected=120, prefiltered=40, final_candidates=15)
    db.finish_run(db_path, run_id, stats)

    run = db.get_latest_run(db_path, "2026-08-16")
    assert run["status"] == "complete"
    assert run["collected_count"] == 120
    assert run["final_candidate_count"] == 15


def test_failed_run_records_its_error(db_path):
    from src.models import RunStats

    run_id = db.start_run(db_path, "2026-08-16")
    db.finish_run(db_path, run_id, RunStats(run_id=run_id), error="Claude selection failed")

    run = db.get_latest_run(db_path)
    assert run["status"] == "failed"
    assert "Claude selection failed" in run["error"]


def test_same_canonical_url_keeps_one_row_and_its_id(db_path):
    """A story rediscovered tomorrow keeps its id, so selections stay valid."""
    first = make_item(url="https://example.com/news/story?utm_source=x")
    second = make_item(url="https://example.com/news/story", snippet_original="fuller text")

    first_id = db.upsert_news_item(db_path, first)
    second_id = db.upsert_news_item(db_path, second)

    assert first_id == second_id
    row = db.get_item(db_path, first_id)
    assert row["snippet_original"] == "fuller text", "new detail should fill in blanks"
    assert row["last_seen_at"] >= row["first_seen_at"]


def test_clear_final_candidates_drops_yesterdays_shortlist(db_path):
    item_id = seed_candidate(db_path)
    assert len(db.get_final_candidates(db_path)) == 1

    db.clear_final_candidates(db_path)
    assert db.get_final_candidates(db_path) == []
    assert db.get_item(db_path, item_id) is not None, "the story itself is kept for history"


def test_ensure_day_is_idempotent_and_preserves_selection(db_path):
    item_id = seed_candidate(db_path, language="ru", source_id="newsru_ru")
    db.ensure_day(db_path, "2026-08-16", run_id=1)
    db.set_selection(db_path, "2026-08-16", "ru", item_id, "funny")

    again = db.ensure_day(db_path, "2026-08-16", run_id=2)
    assert again["ru_item_id"] == item_id
    assert again["ru_tone"] == "funny"
    assert again["run_id"] == 2


def test_echo_row_is_unique_per_day_and_language(db_path):
    db.ensure_day(db_path, "2026-08-16")

    db.upsert_echo(db_path, "2026-08-16", "ru", {"status": EchoStatus.creating.value})
    db.upsert_echo(db_path, "2026-08-16", "ru", {"kvasir_echo_id": 4242,
                                                 "status": EchoStatus.editing.value})
    db.upsert_echo(db_path, "2026-08-16", "en", {"kvasir_echo_id": 4243})

    rows = db.get_echoes_for_day(db_path, "2026-08-16")
    assert len(rows) == 2, "one row per (day, language) — a retry must update, not insert"

    ru = db.get_echo(db_path, "2026-08-16", "ru")
    assert ru["kvasir_echo_id"] == 4242
    assert ru["status"] == EchoStatus.editing.value


def test_day_status_transitions(db_path):
    db.ensure_day(db_path, "2026-08-16")
    assert db.get_day(db_path, "2026-08-16")["status"] == WorkflowStatus.ready.value

    db.set_day_status(db_path, "2026-08-16", WorkflowStatus.editing.value, finished=True)
    row = db.get_day(db_path, "2026-08-16")
    assert row["status"] == WorkflowStatus.editing.value
    assert row["generation_finished_at"]


def test_history_excludes_today(db_path):
    for day in ("2026-08-14", "2026-08-15", "2026-08-16"):
        db.ensure_day(db_path, day)

    history = db.get_days(db_path, limit=30, exclude_day="2026-08-16")
    assert [row["day"] for row in history] == ["2026-08-15", "2026-08-14"]


def test_json_list_tolerates_bad_values():
    assert db.json_list(None) == []
    assert db.json_list("not json") == []
    assert db.json_list('{"a": 1}') == []
    assert db.json_list('["a", "b"]') == ["a", "b"]


# ── A new collection voids the pending selection ──────────────────────────────


def test_collection_voids_an_unlocked_selection(db_path):
    """The shortlist is replaced, so yesterday evening's pick means nothing."""
    day = "2026-08-16"
    db.ensure_day(db_path, day)
    ru = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                        url="https://newsru.co.il/a", title="Русская новость дня")
    en = seed_candidate(db_path, language="en", source_id="toi_en",
                        url="https://timesofisrael.com/b", title="An English story today")
    db.set_selection(db_path, day, "ru", ru, "funny")
    db.set_selection(db_path, day, "en", en, "important")
    db.set_default_categories(db_path, day, "ru", True)

    result = db.clear_unlocked_selection(db_path, day)

    assert result == {"cleared": ["ru", "en"], "kept": []}
    row = db.get_day(db_path, day)
    for slot in ("ru", "en"):
        assert row[f"{slot}_item_id"] is None
        assert row[f"{slot}_tone"] is None
        assert row[f"{slot}_default_categories"] == 0
    assert row["status"] == "ready"


def test_collection_keeps_a_locked_language(db_path):
    """A locked language owns an echo tied to that story - it is not voided."""
    day = "2026-08-16"
    db.ensure_day(db_path, day)
    ru = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                        url="https://newsru.co.il/a", title="Русская новость дня")
    en = seed_candidate(db_path, language="en", source_id="toi_en",
                        url="https://timesofisrael.com/b", title="An English story today")
    db.set_selection(db_path, day, "ru", ru, "funny")
    db.set_selection(db_path, day, "en", en, "important")
    db.lock_selection(db_path, day, "ru")

    result = db.clear_unlocked_selection(db_path, day)

    assert result == {"cleared": ["en"], "kept": ["ru"]}
    row = db.get_day(db_path, day)
    assert row["ru_item_id"] == ru and row["ru_tone"] == "funny"
    assert row["en_item_id"] is None
    assert row["status"] != "ready", "the running language keeps the day's status"


def test_voiding_a_day_with_nothing_selected_changes_nothing(db_path):
    day = "2026-08-16"
    db.ensure_day(db_path, day)
    assert db.clear_unlocked_selection(db_path, day) == {"cleared": [], "kept": []}


def test_voiding_an_unknown_day_is_not_an_error(db_path):
    assert db.clear_unlocked_selection(db_path, "2019-01-01") == {"cleared": [], "kept": []}
