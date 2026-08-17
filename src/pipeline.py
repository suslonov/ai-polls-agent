"""The daily cron pipeline: discovery and candidate preparation only.

This pass never creates a Kvasir echo and never publishes anything. It ends
with 10-20 candidates in SQLite and a re-rendered operator page; every step
after that is triggered by a human in /polls.

    collect → normalize → canonicalize → deterministic filter → exact dedupe
    → cross-source near-dedupe → Gemini prefilter → Claude selection
    → translate/enrich finalists → save candidates → render
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from src import db, dedupe, prefilter, render, selector
from src.collectors.base import collect_source
from src.extraction import extract_meta_description, extract_readable_text, fetch
from src.llm import LLMError
from src.models import NewsItem, RunStats, Settings, SourceConfig
from src.secrets import Secrets

logger = logging.getLogger(__name__)


def local_day(timezone_name: str, moment: Optional[datetime] = None) -> str:
    """Today's date in the configured local timezone, as ``YYYY-MM-DD``.

    The workflow is keyed by local date: a run at 07:15 Asia/Jerusalem belongs
    to that Israeli day regardless of the machine's own clock setting.
    """
    now = moment or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(timezone_name)).date().isoformat()


# ── Collection ────────────────────────────────────────────────────────────────


def collect_all(
    sources: list[SourceConfig],
    settings: Settings,
    db_path: Path,
    run_id: int,
) -> tuple[list[NewsItem], list[str]]:
    """Fetch every enabled source. One failure never aborts the run."""
    collected: list[NewsItem] = []
    errors: list[str] = []

    for source in sources:
        if not source.enabled:
            logger.debug("Source %s disabled, skipping", source.id)
            continue

        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = collect_source(
                source=source,
                user_agent=settings.app.user_agent,
                max_items=settings.collection.max_items_per_source,
                timeout=settings.collection.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - collectors must never kill a run
            logger.warning("Collector crashed for %s: %s", source.id, exc, exc_info=True)
            db.log_source_fetch(
                db_path, run_id, source.id, started_at, 0, status="failed", error=str(exc)[:500]
            )
            errors.append(f"{source.id}: {exc}")
            continue

        for item in result.items:
            item.run_id = run_id
        collected.extend(result.items)

        db.log_source_fetch(
            db_path,
            run_id,
            source.id,
            started_at,
            len(result.items),
            status=result.status,
            http_status=result.http_status,
            error=result.error,
        )
        if result.error:
            errors.append(f"{source.id}: {result.error}")

    logger.info("Collected %d items from %d sources", len(collected), len(sources))
    return collected, errors


# ── Enrichment ────────────────────────────────────────────────────────────────


def enrich_finalists(
    items: list[NewsItem],
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
) -> None:
    """Fetch article bodies and translate Hebrew finalists — shortlist only.

    Both steps are best-effort: a story keeps its snippet when the body cannot
    be fetched, and a Hebrew story that fails translation stays selectable for
    the RU slot logic (it simply isn't eligible for the EN slot until
    ``title_en``/``short_en`` exist).
    """
    max_chars = settings.collection.max_article_chars_for_enrichment

    for item in items:
        if item.full_text or item.source_type == "telegram_public":
            continue
        response = fetch(
            item.url,
            user_agent=settings.app.user_agent,
            timeout=settings.collection.http_timeout_seconds,
        )
        if response is None:
            continue
        text = extract_readable_text(response.text, max_chars=max_chars)
        if text and len(text) > len(item.snippet_original or ""):
            item.full_text = text
            if item.id:
                db.update_full_text(db_path, item.id, text)
        if not item.dek_original:
            dek = extract_meta_description(response.text)
            if dek:
                item.dek_original = dek

    hebrew = [item for item in items if item.source_language == "he"]
    if not hebrew:
        return

    for item in hebrew:
        translated = prefilter.translate_item(
            item, api_key=secrets.google_api_key, model=settings.models.prefilter
        )
        if translated is None:
            logger.warning(
                "Item %s stays Hebrew-only (translation missing); not selectable for the EN slot",
                item.id,
            )
            continue
        item.title_en, item.short_en = translated
        if item.id:
            db.update_translation(db_path, item.id, item.title_en, item.short_en)


# ── Pipeline ──────────────────────────────────────────────────────────────────


def run_pipeline(
    settings: Settings,
    sources: list[SourceConfig],
    secrets: Secrets,
    db_path: Path,
    render_path: Optional[Path] = None,
) -> RunStats:
    """Run one complete collection pass and return its counters."""
    secrets.require_llm()

    db.init_db(db_path)
    day = local_day(settings.app.timezone)
    run_id = db.start_run(db_path, day)
    stats = RunStats(run_id=run_id)
    logger.info("Run %s started for %s", run_id, day)

    try:
        # 1. Collect ----------------------------------------------------------
        collected, errors = collect_all(sources, settings, db_path, run_id)
        stats.collected = len(collected)
        stats.errors = errors

        # 2. Deterministic filter --------------------------------------------
        kept, dropped = dedupe.deterministic_filter(
            collected, lookback_hours=settings.collection.lookback_hours
        )
        stats.deterministic_dropped = len(dropped)

        # 3. Exact + near duplicates -----------------------------------------
        unique, duplicates = dedupe.deduplicate(kept)
        stats.deduped = len(duplicates)

        # 4. Bound the work before spending any tokens ------------------------
        now = datetime.now(timezone.utc)
        unique.sort(key=lambda item: prefilter.deterministic_score(item, now), reverse=True)
        candidates = unique[: settings.collection.max_candidates_before_prefilter]

        # 5. Persist, so every candidate has a stable id ----------------------
        for item in candidates:
            item.id = db.upsert_news_item(db_path, item)

        # 6. Cheap prefilter --------------------------------------------------
        prefiltered, hints = prefilter.run_prefilter(
            candidates,
            api_key=secrets.google_api_key,
            model=settings.models.prefilter,
            keep_target=settings.collection.prefilter_keep,
            batch_size=settings.collection.prefilter_batch_size,
            max_output_tokens=settings.models.prefilter_max_output_tokens,
            now=now,
        )
        stats.prefiltered = len(prefiltered)

        for item in candidates:
            if item.id and item.prefilter_keep is not None:
                db.update_prefilter_scores(
                    db_path,
                    item.id,
                    {
                        "keep": item.prefilter_keep,
                        "israel_relevance": item.prefilter_relevance_score,
                        "interesting_score": item.prefilter_interesting_score,
                        "funny_score": item.prefilter_funny_score,
                        "topic": item.topic,
                        "duplicate_group": item.duplicate_group,
                    },
                )

        # 7. Merge cross-language duplicates the model recognised -------------
        dedupe.apply_story_group_hints(candidates, hints)
        prefiltered = dedupe.pick_group_representatives(prefiltered)

        # 8. Claude selection -------------------------------------------------
        picks = selector.select_candidates(
            prefiltered,
            api_key=secrets.anthropic_api_key,
            model=settings.models.selector,
            min_items=settings.collection.final_candidates_min,
            max_items=settings.collection.final_candidates_max,
            max_chars=settings.collection.max_article_chars_for_selector,
            max_tokens=settings.models.selector_max_tokens,
            now=now,
        )

        # 9. Enrich only the finalists ----------------------------------------
        finalists = [item for item, _ in picks]
        enrich_finalists(finalists, settings, secrets, db_path)

        # 10. Record the shortlist -------------------------------------------
        db.clear_final_candidates(db_path)
        for item, pick in picks:
            if not item.id:
                item.id = db.upsert_news_item(db_path, item)
            db.update_selection(
                db_path,
                item.id,
                rank=pick.rank,
                interesting=pick.interesting_score,
                funny=pick.funny_score,
                topic=pick.topic,
                why=pick.why_candidate,
            )
        stats.final_candidates = len(picks)

        db.ensure_day(db_path, day, run_id=run_id)
        # A new shortlist invalidates a pending pick: the story it named may not
        # even be on the page any more. Locked languages are left alone - their
        # echo already exists and belongs to that story.
        db.clear_unlocked_selection(db_path, day)
        stats.finished_at = datetime.now(timezone.utc)
        db.finish_run(db_path, run_id, stats)

    except LLMError as exc:
        # Claude selection failed: mark this run failed and leave the previous
        # complete day untouched so the operator still has yesterday's state.
        logger.error("Selection failed: %s", exc)
        stats.errors.append(str(exc))
        stats.finished_at = datetime.now(timezone.utc)
        db.finish_run(db_path, run_id, stats, error=str(exc)[:500])
        _render_safely(settings, db_path, render_path, day)
        return stats
    except Exception as exc:  # noqa: BLE001
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        stats.errors.append(str(exc))
        stats.finished_at = datetime.now(timezone.utc)
        db.finish_run(db_path, run_id, stats, error=str(exc)[:500])
        raise

    _render_safely(settings, db_path, render_path, day)

    logger.info(
        "Run %s complete: collected=%d dropped=%d dupes=%d prefiltered=%d final=%d errors=%d",
        run_id,
        stats.collected,
        stats.deterministic_dropped,
        stats.deduped,
        stats.prefiltered,
        stats.final_candidates,
        len(stats.errors),
    )
    return stats


def _render_safely(
    settings: Settings,
    db_path: Path,
    render_path: Optional[Path],
    day: str,
) -> None:
    """Re-render the operator page; a render failure must not fail the run."""
    try:
        render.render_operator_page(
            settings=settings,
            db_path=db_path,
            output_path=render_path,
            day=day,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rendering the operator page failed: %s", exc, exc_info=True)
