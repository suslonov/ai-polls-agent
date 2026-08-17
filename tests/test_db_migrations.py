"""An existing state.db must survive `init_db` from any earlier release.

The hub calls :func:`db.init_db` on every module load, against the operator's
live ``~/polls_data/state.db``. A schema statement that assumes a column added
by a later migration takes the whole `/polls` module down at startup, which is
exactly what happened with the partial index on ``closed_at``.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from src import db

DAY = "2026-08-16"

# Columns that did not exist in the first release. Stripping them reproduces an
# old database no matter how the current schema is edited later.
LATER_COLUMNS = {column for table, column, _ in db._MIGRATIONS if table == "echoes"}
LATER_WORKFLOW_COLUMNS = {
    column for table, column, _ in db._MIGRATIONS if table == "daily_workflow"
}


def create_table_sql(table: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", db._SCHEMA_SQL, re.DOTALL
    )
    assert match, f"{table} not found in the schema"
    return match.group(0)


def strip_columns(statement: str, columns: set[str]) -> str:
    kept = [
        line
        for line in statement.splitlines()
        if line.strip().split(" ")[0] not in columns
    ]
    return "\n".join(kept)


@pytest.fixture
def legacy_db(tmp_path):
    """A database shaped like the release before per-language closing."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        strip_columns(create_table_sql("daily_workflow"), LATER_WORKFLOW_COLUMNS)
        + ";\n"
        + strip_columns(create_table_sql("echoes"), LATER_COLUMNS)
        + ";\n"
        # The index this release replaced: one echo per day and language, ever.
        + "CREATE UNIQUE INDEX IF NOT EXISTS idx_echoes_day_lang "
        "ON echoes (day, target_language);"
    )
    conn.execute(
        "INSERT INTO daily_workflow (day, status, created_at, updated_at) "
        "VALUES (?, 'published', '2026-08-16T06:00:00+00:00', '2026-08-16T06:00:00+00:00')",
        (DAY,),
    )
    conn.execute(
        "INSERT INTO echoes (day, target_language, kvasir_echo_id, title, status,"
        " created_at, updated_at) VALUES (?, 'ru', 4242, 'Опрос дня', 'published',"
        " '2026-08-16T06:00:00+00:00', '2026-08-16T06:00:00+00:00')",
        (DAY,),
    )
    conn.commit()
    conn.close()
    return path


def columns_of(path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def indexes_of(path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


def test_init_db_upgrades_a_database_that_predates_closed_at(legacy_db):
    """The failure mode: `no such column: closed_at` during executescript."""
    assert "closed_at" not in columns_of(legacy_db, "echoes")

    db.init_db(legacy_db)  # must not raise

    assert LATER_COLUMNS <= columns_of(legacy_db, "echoes")
    assert LATER_WORKFLOW_COLUMNS <= columns_of(legacy_db, "daily_workflow")


def test_the_upgrade_replaces_the_old_unique_index(legacy_db):
    assert "idx_echoes_day_lang" in indexes_of(legacy_db, "echoes")

    db.init_db(legacy_db)

    indexes = indexes_of(legacy_db, "echoes")
    assert "idx_echoes_day_lang" not in indexes, "the total index blocked a second poll"
    assert "idx_echoes_day_lang_open" in indexes


def test_existing_rows_survive_the_upgrade(legacy_db):
    db.init_db(legacy_db)

    echo = db.get_echo(legacy_db, DAY, "ru")
    assert echo["kvasir_echo_id"] == 4242
    assert echo["closed_at"] is None, "an old row counts as open"
    assert db.get_day(legacy_db, DAY)["status"] == "published"


def test_an_upgraded_database_can_close_and_start_another_poll(legacy_db):
    db.init_db(legacy_db)
    db.close_echo(legacy_db, DAY, "ru")

    db.upsert_echo(legacy_db, DAY, "ru", {"kvasir_echo_id": 4243, "title": "Второй опрос"})

    assert db.get_echo(legacy_db, DAY, "ru")["kvasir_echo_id"] == 4243
    assert len(db.get_echoes_for_day(legacy_db, DAY, include_closed=True)) == 2


def test_init_db_is_idempotent(legacy_db):
    db.init_db(legacy_db)
    db.init_db(legacy_db)
    db.init_db(legacy_db)

    assert LATER_COLUMNS <= columns_of(legacy_db, "echoes")
    assert db.get_echo(legacy_db, DAY, "ru")["kvasir_echo_id"] == 4242


def test_a_fresh_database_gets_the_partial_index_too(tmp_path):
    """The index moved out of the schema script — a new DB must still get it."""
    path = tmp_path / "fresh.db"
    db.init_db(path)

    assert "idx_echoes_day_lang_open" in indexes_of(path, "echoes")

    db.ensure_day(path, DAY)
    db.upsert_echo(path, DAY, "ru", {"kvasir_echo_id": 1})
    db.upsert_echo(path, DAY, "ru", {"kvasir_echo_id": 2})
    assert db.get_echo(path, DAY, "ru")["kvasir_echo_id"] == 2, "one open echo per language"
    assert len(db.get_echoes_for_day(path, DAY, include_closed=True)) == 1
