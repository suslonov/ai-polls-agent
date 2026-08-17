"""Command-line entry point.

    python -m src.main                 # full collection pass
    python -m src.main --dry-run       # collect + filter + dedupe, no LLM calls
    python -m src.main --render-only   # re-render the operator page from SQLite
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from src import db, dedupe, pipeline, prefilter, render
from src.collectors.base import collect_source
from src.settings import load_settings, load_sources, repo_root, resolve_path

logger = logging.getLogger("ai-polls-agent")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def dry_run(settings, sources, db_path) -> int:
    """Exercise collection and filtering without spending a token."""
    day = pipeline.local_day(settings.app.timezone)
    logger.info("Dry run for %s — no LLM calls, no Kvasir, no publishing", day)

    collected = []
    for source in sources:
        if not source.enabled:
            continue
        result = collect_source(
            source=source,
            user_agent=settings.app.user_agent,
            max_items=settings.collection.max_items_per_source,
            timeout=settings.collection.http_timeout_seconds,
        )
        status = "ok" if not result.error else f"FAILED ({result.error[:120]})"
        logger.info("  %-16s %3d items  %s", source.id, len(result.items), status)
        collected.extend(result.items)

    kept, dropped = dedupe.deterministic_filter(
        collected, lookback_hours=settings.collection.lookback_hours
    )
    unique, duplicates = dedupe.deduplicate(kept)

    now = datetime.now(timezone.utc)
    unique.sort(key=lambda item: prefilter.deterministic_score(item, now), reverse=True)

    by_language = {lang: sum(1 for i in unique if i.source_language == lang) for lang in ("he", "ru", "en")}
    logger.info(
        "Funnel: collected=%d → kept=%d → unique=%d (he=%d ru=%d en=%d), dropped=%d, duplicates=%d",
        len(collected), len(kept), len(unique),
        by_language["he"], by_language["ru"], by_language["en"],
        len(dropped), len(duplicates),
    )
    logger.info("Top 10 by deterministic score:")
    for item in unique[:10]:
        logger.info("  [%s] %-14s %s", item.source_language, item.source_id, item.title_original[:80])

    # Hebrew feeds both slots, so a slot only runs dry when its own languages do.
    if by_language["ru"] < 5:
        logger.warning(
            "Only %d Russian-language stories — the RU slot would lean on Hebrew sources",
            by_language["ru"],
        )
    if by_language["en"] + by_language["he"] < 5:
        logger.warning("Few English/Hebrew stories — the EN slot may have little choice")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Polls Agent — daily collection pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect and filter only; no LLM calls, no writes to the shortlist")
    parser.add_argument("--render-only", action="store_true",
                        help="re-render the operator page from the current database state")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    settings = load_settings()
    sources = load_sources()
    db_path = resolve_path(settings.app.db_path)

    if args.render_only:
        db.init_db(db_path)
        day = pipeline.local_day(settings.app.timezone)
        path = render.render_operator_page(settings=settings, db_path=db_path, day=day)
        logger.info("Rendered %s", path)
        return 0

    if args.dry_run:
        return dry_run(settings, sources, db_path)

    from src.secrets import load_secrets

    secrets = load_secrets(repo_root())
    stats = pipeline.run_pipeline(settings, sources, secrets, db_path)

    for error in stats.errors:
        logger.warning("Source/step error: %s", error)

    return 0 if stats.final_candidates else 1


if __name__ == "__main__":
    raise SystemExit(main())
