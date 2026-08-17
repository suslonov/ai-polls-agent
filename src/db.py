"""SQLite schema and helpers.

SQLite is the source of truth for the whole workflow: collected stories, the
per-day operator selection, the Kvasir echoes created from it, and the publish
events. Rendered HTML is always a projection of these tables, never state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from src.models import NewsItem, RunStats

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date              TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'running',
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    collected_count       INTEGER DEFAULT 0,
    prefiltered_count     INTEGER DEFAULT 0,
    final_candidate_count INTEGER DEFAULT 0,
    error                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_run_date ON runs (run_date);

CREATE TABLE IF NOT EXISTS source_fetches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    source_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'ok',
    http_status INTEGER,
    items_found INTEGER DEFAULT 0,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_fetches_run ON source_fetches (run_id);

CREATE TABLE IF NOT EXISTS news_items (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      INTEGER,
    source_id                   TEXT NOT NULL,
    source_name                 TEXT NOT NULL,
    source_type                 TEXT NOT NULL,
    source_language             TEXT NOT NULL,

    title_original              TEXT NOT NULL,
    title_en                    TEXT,

    url                         TEXT NOT NULL,
    canonical_url               TEXT NOT NULL,
    discovery_url               TEXT,

    published_at                TEXT,
    fetched_at                  TEXT NOT NULL,

    dek_original                TEXT,
    snippet_original            TEXT,
    full_text                   TEXT,
    short_en                    TEXT,

    content_hash                TEXT,
    duplicate_group             TEXT,

    prefilter_keep              INTEGER,
    prefilter_relevance_score   REAL,
    prefilter_interesting_score REAL,
    prefilter_funny_score       REAL,

    selector_rank               INTEGER,
    selector_interesting_score  REAL,
    selector_funny_score        REAL,
    why_candidate               TEXT,
    topic                       TEXT,
    final_candidate             INTEGER DEFAULT 0,

    first_seen_at               TEXT NOT NULL,
    last_seen_at                TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_news_canonical_url ON news_items (canonical_url);
CREATE INDEX IF NOT EXISTS idx_news_published_at   ON news_items (published_at);
CREATE INDEX IF NOT EXISTS idx_news_run_id         ON news_items (run_id);
CREATE INDEX IF NOT EXISTS idx_news_final          ON news_items (final_candidate);
CREATE INDEX IF NOT EXISTS idx_news_language       ON news_items (source_language);
CREATE INDEX IF NOT EXISTS idx_news_dupe_group     ON news_items (duplicate_group);
CREATE INDEX IF NOT EXISTS idx_news_hash           ON news_items (content_hash);

CREATE TABLE IF NOT EXISTS daily_workflow (
    day                    TEXT PRIMARY KEY,
    status                 TEXT NOT NULL DEFAULT 'ready',
    run_id                 INTEGER,
    ru_item_id             INTEGER,
    en_item_id             INTEGER,
    ru_tone                TEXT,
    en_tone                TEXT,
    ru_default_categories  INTEGER NOT NULL DEFAULT 0,
    en_default_categories  INTEGER NOT NULL DEFAULT 0,
    selection_locked_at    TEXT,
    generation_started_at  TEXT,
    generation_finished_at TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS echoes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    day                      TEXT NOT NULL,
    target_language          TEXT NOT NULL,
    news_item_id             INTEGER,
    tone                     TEXT,

    kvasir_course_id         TEXT,
    kvasir_echo_id           INTEGER,
    template_echo_id         TEXT,

    title                    TEXT,
    description_html         TEXT,
    prompt_s3_key            TEXT,
    prompt_sha256            TEXT,
    editor_url               TEXT,

    picture_suggestions_json TEXT DEFAULT '[]',
    categories_json          TEXT DEFAULT '[]',
    categories_default_used  INTEGER NOT NULL DEFAULT 0,
    category_fallback_used   INTEGER NOT NULL DEFAULT 0,
    category_party_used      INTEGER NOT NULL DEFAULT 0,
    yes_no_question          TEXT,
    greeting                 TEXT,
    closed_at                TEXT,

    scroll_id                TEXT,
    scroll_public_url        TEXT,

    status                   TEXT NOT NULL DEFAULT 'creating',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    finalized_at             TEXT,
    error                    TEXT
);

-- The partial index on `echoes` is deliberately NOT here: on a database created
-- before `closed_at` existed, CREATE TABLE IF NOT EXISTS is a no-op and the
-- index would reference a column this script has not added. It is created in
-- _INDEX_MIGRATIONS, after the column migrations have run.

CREATE TABLE IF NOT EXISTS publish_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    day               TEXT NOT NULL,
    target_language   TEXT NOT NULL,
    kvasir_echo_id    INTEGER,
    scroll_id         TEXT,
    stable_public_url TEXT,
    quiz_target_url   TEXT,
    page_files_json   TEXT DEFAULT '[]',
    page_published_at TEXT,
    telegram_sent_at  TEXT,
    telegram_message_id TEXT,
    idempotency_key   TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_idempotency ON publish_events (idempotency_key);
"""

_NEWS_COLUMNS = (
    "run_id, source_id, source_name, source_type, source_language, "
    "title_original, title_en, url, canonical_url, discovery_url, "
    "published_at, fetched_at, dek_original, snippet_original, full_text, short_en, "
    "content_hash, duplicate_group, prefilter_keep, prefilter_relevance_score, "
    "prefilter_interesting_score, prefilter_funny_score, selector_rank, "
    "selector_interesting_score, selector_funny_score, why_candidate, topic, "
    "final_candidate, first_seen_at, last_seen_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL enabled and row access by column name."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _tx(db_path: Path, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Run a statement block in one transaction.

    ``immediate=True`` takes the write lock up front, which is what makes the
    selection lock safe against two browser tabs pressing Start at once.
    """
    conn = connect(db_path)
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release. Applied on every init so an existing
# ~/polls_data/state.db picks them up without a manual migration step.
_MIGRATIONS = [
    ("echoes", "categories_json", "TEXT DEFAULT '[]'"),
    ("echoes", "category_fallback_used", "INTEGER NOT NULL DEFAULT 0"),
    ("echoes", "category_party_used", "INTEGER NOT NULL DEFAULT 0"),
    ("echoes", "greeting", "TEXT"),
    ("echoes", "closed_at", "TEXT"),
    ("daily_workflow", "ru_locked_at", "TEXT"),
    ("daily_workflow", "en_locked_at", "TEXT"),
    ("daily_workflow", "ru_default_categories", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_workflow", "en_default_categories", "INTEGER NOT NULL DEFAULT 0"),
    ("echoes", "categories_default_used", "INTEGER NOT NULL DEFAULT 0"),
]


# Run after the column migrations, since the partial index needs `closed_at`.
_INDEX_MIGRATIONS = [
    # The old index allowed only one echo per (day, language) ever, which made
    # closing a finished poll and starting another the same day impossible.
    "DROP INDEX IF EXISTS idx_echoes_day_lang",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_echoes_day_lang_open "
    "ON echoes (day, target_language) WHERE closed_at IS NULL",
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    for table, column, definition in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logger.info("Migrated %s: added %s", table, column)
        except sqlite3.OperationalError:
            pass  # column already exists

    for statement in _INDEX_MIGRATIONS:
        conn.execute(statement)


def init_db(db_path: Path) -> None:
    """Create tables and indexes if they do not exist."""
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()
    logger.info("Database ready at %s", db_path)


# ── runs ──────────────────────────────────────────────────────────────────────


def start_run(db_path: Path, run_date: str) -> int:
    with _tx(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO runs (run_date, status, started_at) VALUES (?, 'running', ?)",
            (run_date, _now()),
        )
        return int(cur.lastrowid)


def finish_run(db_path: Path, run_id: int, stats: RunStats, error: Optional[str] = None) -> None:
    status = "failed" if error else "complete"
    with _tx(db_path) as conn:
        conn.execute(
            """UPDATE runs SET status = ?, finished_at = ?, collected_count = ?,
                   prefiltered_count = ?, final_candidate_count = ?, error = ?
               WHERE id = ?""",
            (
                status,
                _now(),
                stats.collected,
                stats.prefiltered,
                stats.final_candidates,
                error,
                run_id,
            ),
        )


def get_latest_run(db_path: Path, run_date: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        if run_date:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_date = ? ORDER BY id DESC LIMIT 1", (run_date,)
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def log_source_fetch(
    db_path: Path,
    run_id: int,
    source_id: str,
    started_at: str,
    items_found: int,
    status: str = "ok",
    http_status: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    with _tx(db_path) as conn:
        conn.execute(
            """INSERT INTO source_fetches
                   (run_id, source_id, started_at, finished_at, status, http_status, items_found, error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, source_id, started_at, _now(), status, http_status, items_found, error),
        )


def get_source_fetches(db_path: Path, run_id: int) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM source_fetches WHERE run_id = ? ORDER BY source_id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── news items ────────────────────────────────────────────────────────────────


def upsert_news_item(db_path: Path, item: NewsItem) -> int:
    """Insert a story, or refresh ``last_seen_at`` when we've seen it before.

    Canonical URL is the identity. A story rediscovered on a later run keeps its
    original row (and id), so operator selections stay stable.
    """
    now = _now()
    with _tx(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM news_items WHERE canonical_url = ?", (item.canonical_url,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE news_items SET
                       last_seen_at = ?,
                       run_id = COALESCE(?, run_id),
                       dek_original = COALESCE(dek_original, ?),
                       snippet_original = COALESCE(snippet_original, ?),
                       full_text = COALESCE(?, full_text),
                       discovery_url = COALESCE(discovery_url, ?)
                   WHERE id = ?""",
                (
                    now,
                    item.run_id,
                    item.dek_original,
                    item.snippet_original,
                    item.full_text,
                    item.discovery_url,
                    existing["id"],
                ),
            )
            return int(existing["id"])

        cur = conn.execute(
            f"""INSERT INTO news_items ({_NEWS_COLUMNS})
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.run_id,
                item.source_id,
                item.source_name,
                item.source_type,
                item.source_language,
                item.title_original,
                item.title_en,
                item.url,
                item.canonical_url,
                item.discovery_url,
                _iso(item.published_at),
                _iso(item.fetched_at),
                item.dek_original,
                item.snippet_original,
                item.full_text,
                item.short_en,
                item.content_hash,
                item.duplicate_group,
                None if item.prefilter_keep is None else int(item.prefilter_keep),
                item.prefilter_relevance_score,
                item.prefilter_interesting_score,
                item.prefilter_funny_score,
                item.selector_rank,
                item.selector_interesting_score,
                item.selector_funny_score,
                item.why_candidate,
                item.topic,
                1 if item.final_candidate else 0,
                _iso(item.first_seen_at) or now,
                _iso(item.last_seen_at) or now,
            ),
        )
        return int(cur.lastrowid)


def update_prefilter_scores(db_path: Path, item_id: int, verdict: dict) -> None:
    with _tx(db_path) as conn:
        conn.execute(
            """UPDATE news_items SET
                   prefilter_keep = ?, prefilter_relevance_score = ?,
                   prefilter_interesting_score = ?, prefilter_funny_score = ?,
                   topic = COALESCE(?, topic), duplicate_group = COALESCE(?, duplicate_group)
               WHERE id = ?""",
            (
                1 if verdict.get("keep") else 0,
                verdict.get("israel_relevance"),
                verdict.get("interesting_score"),
                verdict.get("funny_score"),
                verdict.get("topic") or None,
                verdict.get("duplicate_group") or None,
                item_id,
            ),
        )


def update_selection(
    db_path: Path,
    item_id: int,
    rank: int,
    interesting: float,
    funny: float,
    topic: str,
    why: str,
) -> None:
    with _tx(db_path) as conn:
        conn.execute(
            """UPDATE news_items SET
                   selector_rank = ?, selector_interesting_score = ?, selector_funny_score = ?,
                   topic = COALESCE(NULLIF(?, ''), topic), why_candidate = ?, final_candidate = 1
               WHERE id = ?""",
            (rank, interesting, funny, topic, why, item_id),
        )


def update_translation(db_path: Path, item_id: int, title_en: str, short_en: str) -> None:
    with _tx(db_path) as conn:
        conn.execute(
            "UPDATE news_items SET title_en = ?, short_en = ? WHERE id = ?",
            (title_en, short_en, item_id),
        )


def update_full_text(db_path: Path, item_id: int, full_text: str) -> None:
    with _tx(db_path) as conn:
        conn.execute("UPDATE news_items SET full_text = ? WHERE id = ?", (full_text, item_id))


def clear_final_candidates(db_path: Path) -> None:
    """Drop the previous day's shortlist before writing a new one."""
    with _tx(db_path) as conn:
        conn.execute("UPDATE news_items SET final_candidate = 0 WHERE final_candidate = 1")


def get_item(db_path: Path, item_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM news_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_final_candidates(db_path: Path, run_id: Optional[int] = None) -> list[dict]:
    conn = connect(db_path)
    try:
        if run_id is not None:
            rows = conn.execute(
                """SELECT * FROM news_items
                   WHERE final_candidate = 1 AND run_id = ?
                   ORDER BY selector_rank ASC, id ASC""",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM news_items WHERE final_candidate = 1
                   ORDER BY selector_rank ASC, id ASC"""
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


COLLECTED_PAGE_SIZE = 100


def count_items(db_path: Path, run_id: Optional[int] = None) -> int:
    """How many stories were collected (optionally for one run)."""
    conn = connect(db_path)
    try:
        if run_id is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM news_items WHERE run_id = ?", (run_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()
        return int(row["n"])
    finally:
        conn.close()


def get_items_page(
    db_path: Path,
    limit: int = COLLECTED_PAGE_SIZE,
    offset: int = 0,
    run_id: Optional[int] = None,
) -> list[dict]:
    """One page of collected stories, newest first — the full haul, not the shortlist."""
    if limit < 1:
        return []
    offset = max(0, offset)
    order = (
        "ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC LIMIT ? OFFSET ?"
    )
    conn = connect(db_path)
    try:
        if run_id is not None:
            rows = conn.execute(
                f"SELECT * FROM news_items WHERE run_id = ? {order}", (run_id, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM news_items {order}", (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_manual_candidate(db_path: Path, item_id: int) -> bool:
    """Promote a collected story into today's shortlist by hand.

    Ranks it after the model's picks so the operator can tell the two apart.
    Returns False if it was already a candidate.
    """
    with _tx(db_path, immediate=True) as conn:
        row = conn.execute(
            "SELECT final_candidate FROM news_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown item {item_id}")
        if row["final_candidate"]:
            return False

        next_rank = conn.execute(
            "SELECT COALESCE(MAX(selector_rank), 0) + 1 AS r FROM news_items WHERE final_candidate = 1"
        ).fetchone()["r"]
        conn.execute(
            """UPDATE news_items
               SET final_candidate = 1,
                   selector_rank = ?,
                   why_candidate = COALESCE(NULLIF(why_candidate, ''), 'added by the operator')
               WHERE id = ?""",
            (next_rank, item_id),
        )
    return True


def get_recent_items(db_path: Path, since_iso: str, limit: int = 2000) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM news_items
               WHERE COALESCE(published_at, first_seen_at) >= ?
               ORDER BY COALESCE(published_at, first_seen_at) DESC LIMIT ?""",
            (since_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def known_canonical_urls(db_path: Path) -> set[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT canonical_url FROM news_items").fetchall()
        return {r["canonical_url"] for r in rows}
    finally:
        conn.close()


# ── daily workflow ────────────────────────────────────────────────────────────


def ensure_day(db_path: Path, day: str, run_id: Optional[int] = None) -> dict:
    """Create today's workflow row if missing, then return it."""
    now = _now()
    with _tx(db_path) as conn:
        conn.execute(
            """INSERT INTO daily_workflow (day, status, run_id, created_at, updated_at)
               VALUES (?, 'ready', ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                   run_id = COALESCE(excluded.run_id, daily_workflow.run_id),
                   updated_at = excluded.updated_at""",
            (day, run_id, now, now),
        )
    return get_day(db_path, day) or {}


def get_day(db_path: Path, day: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM daily_workflow WHERE day = ?", (day,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_days(db_path: Path, limit: int = 30, exclude_day: Optional[str] = None) -> list[dict]:
    conn = connect(db_path)
    try:
        if exclude_day:
            rows = conn.execute(
                "SELECT * FROM daily_workflow WHERE day != ? ORDER BY day DESC LIMIT ?",
                (exclude_day, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM daily_workflow ORDER BY day DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class SelectionLocked(RuntimeError):
    """Raised when a selection change is attempted after generation started."""


def set_selection(
    db_path: Path,
    day: str,
    slot: str,
    item_id: Optional[int],
    tone: Optional[str],
) -> dict:
    """Write one slot's selection.

    Locking is per language: starting the Russian poll must not freeze the
    English slot, so only that slot's own lock blocks a change.
    """
    if slot not in ("ru", "en"):
        raise ValueError(f"unknown slot {slot!r}")
    if tone is not None and tone not in ("important", "funny"):
        raise ValueError(f"unknown tone {tone!r}")

    with _tx(db_path, immediate=True) as conn:
        row = conn.execute("SELECT * FROM daily_workflow WHERE day = ?", (day,)).fetchone()
        if row is None:
            raise SelectionLocked(f"no workflow row for {day}")
        if row[f"{slot}_locked_at"]:
            raise SelectionLocked(f"the {slot.upper()} selection is locked for this day")

        # A different story means the category decision was made about the old
        # one: clear "use default categories" so the tick is always deliberate.
        story_changed = row[f"{slot}_item_id"] != item_id
        if story_changed:
            conn.execute(
                f"UPDATE daily_workflow SET {slot}_default_categories = 0 WHERE day = ?",
                (day,),
            )

        conn.execute(
            f"UPDATE daily_workflow SET {slot}_item_id = ?, {slot}_tone = ?, updated_at = ? "
            "WHERE day = ?",
            (item_id, tone, _now(), day),
        )
    return get_day(db_path, day) or {}


def clear_unlocked_selection(db_path: Path, day: str) -> dict:
    """Void the day's pending selection - what a fresh collection invalidates.

    A collection run replaces the shortlist, so yesterday evening's pick, its
    tone and its category choice no longer refer to anything the operator is
    looking at. Languages that are already locked keep everything: their echo
    exists and is tied to that story.

    Returns ``{"cleared": [...], "kept": [...]}``.
    """
    cleared: list[str] = []
    kept: list[str] = []

    with _tx(db_path, immediate=True) as conn:
        row = conn.execute("SELECT * FROM daily_workflow WHERE day = ?", (day,)).fetchone()
        if row is None:
            return {"cleared": [], "kept": []}

        now = _now()
        for slot in ("ru", "en"):
            if row[f"{slot}_locked_at"]:
                kept.append(slot)
                continue
            if row[f"{slot}_item_id"] is None and not row[f"{slot}_default_categories"]:
                continue
            conn.execute(
                f"""UPDATE daily_workflow SET {slot}_item_id = NULL, {slot}_tone = NULL,
                        {slot}_default_categories = 0, updated_at = ?
                    WHERE day = ?""",
                (now, day),
            )
            cleared.append(slot)

        if not kept:
            # Nothing is running: the day goes back to a clean 'ready'.
            conn.execute(
                """UPDATE daily_workflow SET status = 'ready', selection_locked_at = NULL,
                       generation_started_at = NULL, generation_finished_at = NULL,
                       updated_at = ?
                   WHERE day = ?""",
                (now, day),
            )

    if cleared:
        logger.info("Collection voided the pending selection for %s: %s", day, ", ".join(cleared))
    if kept:
        logger.info("Kept the locked selection for %s: %s", day, ", ".join(kept))
    return {"cleared": cleared, "kept": kept}


def set_default_categories(db_path: Path, day: str, slot: str, enabled: bool) -> dict:
    """Choose whether this slot uses the template's own category list.

    With it on, generation substitutes the CATEGORIES marker's ``DEFAULT=``
    payload and never asks a model to invent participant categories. Editable
    until that language locks, like the story and the tone.
    """
    if slot not in ("ru", "en"):
        raise ValueError(f"unknown slot {slot!r}")

    with _tx(db_path, immediate=True) as conn:
        row = conn.execute("SELECT * FROM daily_workflow WHERE day = ?", (day,)).fetchone()
        if row is None:
            raise SelectionLocked(f"no workflow row for {day}")
        if row[f"{slot}_locked_at"]:
            raise SelectionLocked(f"the {slot.upper()} selection is locked for this day")

        conn.execute(
            f"UPDATE daily_workflow SET {slot}_default_categories = ?, updated_at = ? "
            "WHERE day = ?",
            (1 if enabled else 0, _now(), day),
        )
    return get_day(db_path, day) or {}


def lock_selection(db_path: Path, day: str, slot: str) -> dict:
    """Atomically lock one language's selection before its generation starts.

    Locking is per language so the two poll languages advance independently: you
    can take Russian all the way to a published quiz while English is still
    being chosen. Raises :class:`SelectionLocked` when another request got there
    first or that slot has nothing valid selected — the caller must not start
    any LLM or Kvasir work unless this succeeds.
    """
    if slot not in ("ru", "en"):
        raise ValueError(f"unknown slot {slot!r}")

    now = _now()
    with _tx(db_path, immediate=True) as conn:
        row = conn.execute("SELECT * FROM daily_workflow WHERE day = ?", (day,)).fetchone()
        if row is None:
            raise SelectionLocked(f"no workflow row for {day}")
        if row[f"{slot}_locked_at"]:
            raise SelectionLocked(f"the {slot.upper()} selection is already locked")
        if not row[f"{slot}_item_id"]:
            raise SelectionLocked(f"nothing selected for the {slot.upper()} slot")
        if not row[f"{slot}_tone"]:
            raise SelectionLocked(f"the {slot.upper()} selection has no tone")

        conn.execute(
            f"""UPDATE daily_workflow
                SET {slot}_locked_at = ?,
                    status = 'generating',
                    selection_locked_at = COALESCE(selection_locked_at, ?),
                    generation_started_at = COALESCE(generation_started_at, ?),
                    updated_at = ?
                WHERE day = ? AND {slot}_locked_at IS NULL""",
            (now, now, now, now, day),
        )
        updated = conn.execute(
            "SELECT * FROM daily_workflow WHERE day = ?", (day,)
        ).fetchone()
        if not updated[f"{slot}_locked_at"]:
            raise SelectionLocked("selection lock lost a race")
        return dict(updated)


class ResetRefused(RuntimeError):
    """Raised when a day cannot be reset because something is already public."""


def reset_generation(db_path: Path, day: str, target_language: Optional[str] = None) -> dict:
    """Throw away a language's local generation state and unlock its selection.

    With ``target_language`` omitted this resets every language of the day.

    Used when a generation attempt went wrong and retrying the same echo is not
    what you want. The selected stories and tones are kept (so Start can simply
    be pressed again) but become editable.

    Kvasir components already created are **not** deleted — this project never
    deletes Kvasir data. Their ids are returned so the caller can report which
    drafts are now orphaned; they stay in the target course as `raw` echoes.

    This is an operator tool and it never refuses. Resetting an already
    published language is allowed: the entry stays on the stable page and the
    Telegram message stays sent (neither is ours to retract), so the publish
    events go too and are returned as ``dropped_publish_events`` — the next poll
    for that day and language republishes over the page entry.
    """
    languages = [target_language] if target_language else ["ru", "en"]
    for language in languages:
        if language not in ("ru", "en"):
            raise ValueError(f"unknown language {language!r}")

    placeholders = ",".join("?" * len(languages))

    with _tx(db_path, immediate=True) as conn:
        row = conn.execute("SELECT * FROM daily_workflow WHERE day = ?", (day,)).fetchone()
        if row is None:
            # Nothing to reset is not an error: the day is already in the state
            # the operator asked for.
            conn.execute(
                "INSERT OR IGNORE INTO daily_workflow (day, status, created_at, updated_at) "
                "VALUES (?, 'ready', ?, ?)",
                (day, _now(), _now()),
            )

        event_rows = conn.execute(
            f"""SELECT target_language, kvasir_echo_id, scroll_id FROM publish_events
                WHERE day = ? AND target_language IN ({placeholders})""",
            (day, *languages),
        ).fetchall()
        dropped_events = [
            {"language": r["target_language"], "echo_id": r["kvasir_echo_id"],
             "scroll_id": r["scroll_id"]}
            for r in event_rows
        ]
        conn.execute(
            f"""DELETE FROM publish_events
                WHERE day = ? AND target_language IN ({placeholders})""",
            (day, *languages),
        )

        echo_rows = conn.execute(
            f"""SELECT target_language, kvasir_echo_id, status FROM echoes
                WHERE day = ? AND target_language IN ({placeholders})
                  AND closed_at IS NULL""",
            (day, *languages),
        ).fetchall()

        orphaned = [
            {"language": r["target_language"], "echo_id": r["kvasir_echo_id"]}
            for r in echo_rows
            if r["kvasir_echo_id"]
        ]

        conn.execute(
            f"""DELETE FROM echoes
                WHERE day = ? AND target_language IN ({placeholders}) AND closed_at IS NULL""",
            (day, *languages),
        )

        now = _now()
        for language in languages:
            conn.execute(
                f"UPDATE daily_workflow SET {language}_locked_at = NULL, updated_at = ? "
                "WHERE day = ?",
                (now, day),
            )

        still_locked = conn.execute(
            "SELECT ru_locked_at, en_locked_at FROM daily_workflow WHERE day = ?", (day,)
        ).fetchone()
        if not (still_locked["ru_locked_at"] or still_locked["en_locked_at"]):
            conn.execute(
                """UPDATE daily_workflow SET
                       status = 'ready',
                       selection_locked_at = NULL,
                       generation_started_at = NULL,
                       generation_finished_at = NULL,
                       updated_at = ?
                   WHERE day = ?""",
                (now, day),
            )

    if orphaned:
        logger.warning(
            "Day %s reset (%s); these Kvasir echoes are now orphaned drafts: %s",
            day,
            ", ".join(languages),
            ", ".join(f"{o['language']}={o['echo_id']}" for o in orphaned),
        )
    if dropped_events:
        logger.warning(
            "Day %s reset (%s) dropped %d publish event(s); the stable page entry and "
            "any Telegram message stay as they were",
            day, ", ".join(languages), len(dropped_events),
        )
    return {
        "day": day,
        "languages": languages,
        "orphaned_echoes": orphaned,
        "dropped_publish_events": dropped_events,
    }


def set_day_status(db_path: Path, day: str, status: str, finished: bool = False) -> None:
    now = _now()
    with _tx(db_path) as conn:
        if finished:
            conn.execute(
                """UPDATE daily_workflow SET status = ?, generation_finished_at = ?,
                       updated_at = ? WHERE day = ?""",
                (status, now, now, day),
            )
        else:
            conn.execute(
                "UPDATE daily_workflow SET status = ?, updated_at = ? WHERE day = ?",
                (status, now, day),
            )


# ── echoes ────────────────────────────────────────────────────────────────────


def get_echo(db_path: Path, day: str, target_language: str) -> Optional[dict]:
    """The *open* echo for a day and language (closed ones are history)."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM echoes
               WHERE day = ? AND target_language = ? AND closed_at IS NULL""",
            (day, target_language),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_echoes_for_day(db_path: Path, day: str, include_closed: bool = False) -> list[dict]:
    conn = connect(db_path)
    try:
        query = "SELECT * FROM echoes WHERE day = ?"
        if not include_closed:
            query += " AND closed_at IS NULL"
        query += " ORDER BY target_language, id"
        return [dict(r) for r in conn.execute(query, (day,)).fetchall()]
    finally:
        conn.close()


def close_echo(db_path: Path, day: str, target_language: str) -> dict:
    """Retire a finished poll so the language is free for another one today.

    The row is kept (history, publish events, the Kvasir component all remain);
    it simply stops being the open echo for that day and language.
    """
    now = _now()
    with _tx(db_path, immediate=True) as conn:
        row = conn.execute(
            """SELECT id, status FROM echoes
               WHERE day = ? AND target_language = ? AND closed_at IS NULL""",
            (day, target_language),
        ).fetchone()
        if row is None:
            raise ResetRefused(f"no open {target_language.upper()} echo for {day}")

        conn.execute("UPDATE echoes SET closed_at = ?, updated_at = ? WHERE id = ?",
                     (now, now, row["id"]))
        # Free the slot: the selection unlocks and empties so a new story can be
        # chosen for this language today.
        conn.execute(
            f"""UPDATE daily_workflow SET
                    {target_language}_item_id = NULL,
                    {target_language}_tone = NULL,
                    {target_language}_locked_at = NULL,
                    updated_at = ?
                WHERE day = ?""",
            (now, day),
        )
    return {"day": day, "target_language": target_language, "closed_echo_id": row["id"]}


def upsert_echo(db_path: Path, day: str, target_language: str, fields: dict[str, Any]) -> dict:
    """Create or update the single echo row for (day, language).

    The unique index on (day, target_language) is what makes echo creation
    idempotent: a retry updates the existing row instead of creating a second
    Kvasir component.
    """
    now = _now()
    allowed = {
        "news_item_id", "tone", "kvasir_course_id", "kvasir_echo_id", "template_echo_id",
        "title", "description_html", "prompt_s3_key", "prompt_sha256", "editor_url",
        "picture_suggestions_json", "yes_no_question", "scroll_id", "scroll_public_url",
        "status", "finalized_at", "error",
        "categories_json", "categories_default_used", "category_fallback_used",
        "category_party_used", "greeting", "closed_at",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}

    with _tx(db_path) as conn:
        existing = conn.execute(
            """SELECT id FROM echoes
               WHERE day = ? AND target_language = ? AND closed_at IS NULL""",
            (day, target_language),
        ).fetchone()

        if existing:
            if payload:
                assignments = ", ".join(f"{k} = ?" for k in payload)
                conn.execute(
                    f"UPDATE echoes SET {assignments}, updated_at = ? WHERE id = ?",
                    (*payload.values(), now, existing["id"]),
                )
        else:
            columns = ["day", "target_language", *payload.keys(), "created_at", "updated_at"]
            values = [day, target_language, *payload.values(), now, now]
            placeholders = ",".join("?" * len(columns))
            conn.execute(
                f"INSERT INTO echoes ({','.join(columns)}) VALUES ({placeholders})", values
            )
    return get_echo(db_path, day, target_language) or {}


# ── publish events ────────────────────────────────────────────────────────────


def idempotency_key(day: str, target_language: str, echo_id: Any, scroll_id: str) -> str:
    return f"{day}:{target_language}:{echo_id}:{scroll_id}"


def get_publish_event(db_path: Path, key: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM publish_events WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_publish_event_for(db_path: Path, day: str, target_language: str) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM publish_events WHERE day = ? AND target_language = ?
               ORDER BY id DESC LIMIT 1""",
            (day, target_language),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_publish_event(db_path: Path, key: str, fields: dict[str, Any]) -> dict:
    """Create or update the publish event identified by ``key``.

    The unique index on ``idempotency_key`` is what stops a second Finalize
    click from sending a duplicate Telegram message.
    """
    now = _now()
    allowed = {
        "day", "target_language", "kvasir_echo_id", "scroll_id", "stable_public_url",
        "quiz_target_url", "page_files_json", "page_published_at", "telegram_sent_at",
        "telegram_message_id", "status", "error",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}

    with _tx(db_path, immediate=True) as conn:
        existing = conn.execute(
            "SELECT id FROM publish_events WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing:
            if payload:
                assignments = ", ".join(f"{k} = ?" for k in payload)
                conn.execute(
                    f"UPDATE publish_events SET {assignments}, updated_at = ? WHERE id = ?",
                    (*payload.values(), now, existing["id"]),
                )
        else:
            columns = ["idempotency_key", *payload.keys(), "created_at", "updated_at"]
            values = [key, *payload.values(), now, now]
            placeholders = ",".join("?" * len(columns))
            conn.execute(
                f"INSERT INTO publish_events ({','.join(columns)}) VALUES ({placeholders})",
                values,
            )
    return get_publish_event(db_path, key) or {}


def get_publish_events_for_days(db_path: Path, days: list[str]) -> dict[str, dict]:
    """Return {``day:language`` -> event} for the given days."""
    if not days:
        return {}
    placeholders = ",".join("?" * len(days))
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM publish_events WHERE day IN ({placeholders}) ORDER BY id ASC",
            days,
        ).fetchall()
        return {f"{r['day']}:{r['target_language']}": dict(r) for r in rows}
    finally:
        conn.close()


def json_list(raw: Optional[str]) -> list:
    """Parse a JSON list column, tolerating nulls and malformed values."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []
