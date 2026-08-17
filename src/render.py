"""Build the operator view-model from SQLite and render it with Jinja2.

The page is always a projection of the database — nothing is ever patched into
generated HTML in place. The same :func:`build_state` output backs both the
rendered file and the ``/api/day/current`` JSON, so the page and the API can't
drift apart.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import db
from src.models import Settings
from src.settings import repo_root, resolve_path

logger = logging.getLogger(__name__)


def _local_time(value: Optional[str], timezone_name: str) -> str:
    """Render a stored ISO timestamp in the operator's timezone."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")


def candidate_view(row: dict, timezone_name: str) -> dict:
    """One candidate card."""
    language = row.get("source_language") or ""
    title_en = row.get("title_en")
    has_translation = bool(title_en and row.get("short_en"))

    return {
        "id": row["id"],
        "source_name": row.get("source_name") or row.get("source_id"),
        "source_language": language,
        "source_type": row.get("source_type"),
        "published_at": _local_time(row.get("published_at") or row.get("first_seen_at"), timezone_name),
        "title_original": row.get("title_original") or "",
        "title_en": title_en if language != "en" else None,
        "summary": row.get("short_en") or row.get("snippet_original") or row.get("dek_original") or "",
        "topic": row.get("topic") or "",
        "interesting_score": row.get("selector_interesting_score"),
        "funny_score": row.get("selector_funny_score"),
        "why_candidate": row.get("why_candidate") or "",
        "url": row.get("url"),
        "discovery_url": row.get("discovery_url"),
        # Slot eligibility: Hebrew feeds both slots, but the EN slot needs the
        # English rendering first.
        "eligible_ru": language in ("ru", "he"),
        "eligible_en": language == "en" or (language == "he" and has_translation),
        "translation_missing": language == "he" and not has_translation,
    }


def echo_view(
    row: dict,
    publish: Optional[dict],
    timezone_name: str,
    courses_bucket: str = "",
) -> dict:
    """One generated-chat panel."""
    prompt_key = row.get("prompt_s3_key") or ""
    return {
        "language": row.get("target_language"),
        "echo_id": row.get("kvasir_echo_id"),
        "template_echo_id": row.get("template_echo_id") or "",
        # Where this echo's filled prompt actually lives, so it can be inspected.
        "prompt_location": (f"s3://{courses_bucket}/{prompt_key}" if prompt_key else ""),
        "prompt_sha256": row.get("prompt_sha256") or "",
        "title": row.get("title") or "",
        "tone": row.get("tone") or "",
        "news_item_id": row.get("news_item_id"),
        "editor_url": row.get("editor_url") or "",
        "picture_suggestions": db.json_list(row.get("picture_suggestions_json")),
        "categories": db.json_list(row.get("categories_json")),
        "categories_default_used": bool(row.get("categories_default_used")),
        "category_fallback_used": bool(row.get("category_fallback_used")),
        "category_party_used": bool(row.get("category_party_used")),
        "yes_no_question": row.get("yes_no_question") or "",
        "status": row.get("status") or "",
        "error": row.get("error") or "",
        "scroll_id": row.get("scroll_id") or "",
        "scroll_public_url": row.get("scroll_public_url") or "",
        "finalized_at": _local_time(row.get("finalized_at"), timezone_name),
        "publish": {
            "status": (publish or {}).get("status") or "",
            "stable_public_url": (publish or {}).get("stable_public_url") or "",
            "page_published_at": _local_time((publish or {}).get("page_published_at"), timezone_name),
            "telegram_sent_at": _local_time((publish or {}).get("telegram_sent_at"), timezone_name),
            "error": (publish or {}).get("error") or "",
        } if publish else None,
    }


def build_state(settings: Settings, db_path: Path, day: str) -> dict[str, Any]:
    """Assemble everything the operator page needs for one day."""
    timezone_name = settings.app.timezone
    workflow = db.ensure_day(db_path, day)
    run = db.get_latest_run(db_path, day) or db.get_latest_run(db_path)

    run_id = workflow.get("run_id") or (run or {}).get("id")
    candidates = db.get_final_candidates(db_path, run_id=run_id)
    if not candidates:
        # A run may have failed before writing a shortlist; fall back to the
        # most recent shortlist so the operator still sees something usable.
        candidates = db.get_final_candidates(db_path)

    echo_rows = db.get_echoes_for_day(db_path, day)
    publish_events = db.get_publish_events_for_days(db_path, [day])

    # Locking is per language: one poll can be published while the other is
    # still being chosen.
    slots = {}
    for slot in ("ru", "en"):
        item_id = workflow.get(f"{slot}_item_id")
        tone = workflow.get(f"{slot}_tone")
        slot_locked = bool(workflow.get(f"{slot}_locked_at"))
        slots[slot] = {
            "item_id": item_id,
            "tone": tone,
            "locked": slot_locked,
            "locked_at": _local_time(workflow.get(f"{slot}_locked_at"), timezone_name),
            "can_start": bool(item_id) and bool(tone) and not slot_locked,
            # "use default, don't invent": the template's own category list.
            "default_categories": bool(workflow.get(f"{slot}_default_categories")),
        }

    locked = all(slots[slot]["locked"] for slot in ("ru", "en"))
    ru_selected = slots["ru"]["item_id"]
    en_selected = slots["en"]["item_id"]
    can_start = any(slots[slot]["can_start"] for slot in ("ru", "en"))

    return {
        "day": day,
        "timezone": timezone_name,
        "run": {
            "id": (run or {}).get("id"),
            "status": (run or {}).get("status") or "unknown",
            "started_at": _local_time((run or {}).get("started_at"), timezone_name),
            "finished_at": _local_time((run or {}).get("finished_at"), timezone_name),
            "collected_count": (run or {}).get("collected_count") or 0,
            "prefiltered_count": (run or {}).get("prefiltered_count") or 0,
            "final_candidate_count": (run or {}).get("final_candidate_count") or 0,
            "error": (run or {}).get("error") or "",
        },
        "workflow": {
            "status": workflow.get("status") or "ready",
            "ru_item_id": ru_selected,
            "en_item_id": en_selected,
            "ru_tone": workflow.get("ru_tone"),
            "en_tone": workflow.get("en_tone"),
            "selection_locked": locked,
            "slots": slots,
            "selection_locked_at": _local_time(workflow.get("selection_locked_at"), timezone_name),
            "generation_started_at": _local_time(workflow.get("generation_started_at"), timezone_name),
            "generation_finished_at": _local_time(workflow.get("generation_finished_at"), timezone_name),
            "can_start": can_start,
        },
        "candidates": [candidate_view(row, timezone_name) for row in candidates],
        "echoes": [
            echo_view(
                row,
                publish_events.get(f"{day}:{row.get('target_language')}"),
                timezone_name,
                courses_bucket=settings.kvasir.courses_bucket,
            )
            for row in echo_rows
        ],
    }


def build_history(settings: Settings, db_path: Path, day: str) -> list[dict]:
    """Previous days, read-only."""
    timezone_name = settings.app.timezone
    rows = db.get_days(db_path, limit=settings.app.history_days_in_ui, exclude_day=day)
    if not rows:
        return []

    days = [row["day"] for row in rows]
    publish_events = db.get_publish_events_for_days(db_path, days)

    history = []
    for row in rows:
        entry: dict[str, Any] = {"day": row["day"], "status": row.get("status"), "languages": []}
        for language in ("ru", "en"):
            echo = db.get_echo(db_path, row["day"], language)
            item_id = row.get(f"{language}_item_id")
            if not echo and not item_id:
                continue
            item = db.get_item(db_path, item_id) if item_id else None
            publish = publish_events.get(f"{row['day']}:{language}")
            entry["languages"].append(
                {
                    "language": language,
                    "tone": row.get(f"{language}_tone") or "",
                    "news_title": (item or {}).get("title_original") or "",
                    "news_url": (item or {}).get("url") or "",
                    "source_name": (item or {}).get("source_name") or "",
                    "echo_title": (echo or {}).get("title") or "",
                    "editor_url": (echo or {}).get("editor_url") or "",
                    "stable_public_url": (publish or {}).get("stable_public_url") or "",
                    "quiz_target_url": (echo or {}).get("scroll_public_url") or "",
                    "publish_status": (publish or {}).get("status") or (echo or {}).get("status") or "",
                    "telegram_sent_at": _local_time((publish or {}).get("telegram_sent_at"), timezone_name),
                }
            )
        if entry["languages"]:
            history.append(entry)
    return history


def build_collected_state(
    settings: Settings,
    db_path: Path,
    day: str,
    page: int = 1,
    page_size: int = db.COLLECTED_PAGE_SIZE,
) -> dict[str, Any]:
    """Every story the last run collected, paginated — not just the shortlist."""
    timezone_name = settings.app.timezone
    workflow = db.get_day(db_path, day) or {}
    run = db.get_latest_run(db_path, day) or db.get_latest_run(db_path) or {}
    run_id = workflow.get("run_id") or run.get("id")

    page = max(1, int(page or 1))
    total = db.count_items(db_path, run_id=run_id)
    offset = (page - 1) * page_size
    rows = db.get_items_page(db_path, limit=page_size, offset=offset, run_id=run_id)

    selected_ids = {workflow.get("ru_item_id"), workflow.get("en_item_id")} - {None}
    locked = bool(workflow.get("selection_locked_at"))

    # Deliberately not called "items": Jinja resolves `state.items` to dict.items.
    listed = []
    for row in rows:
        language = row.get("source_language") or ""
        listed.append(
            {
                "id": row["id"],
                "source_name": row.get("source_name") or row.get("source_id"),
                "source_language": language,
                "published_at": _local_time(
                    row.get("published_at") or row.get("first_seen_at"), timezone_name
                ),
                "title": row.get("title_original") or "",
                "title_en": row.get("title_en") if language != "en" else None,
                "url": row.get("url"),
                "topic": row.get("topic") or "",
                "prefilter_keep": bool(row.get("prefilter_keep")),
                "prefilter_interesting": row.get("prefilter_interesting_score"),
                "prefilter_funny": row.get("prefilter_funny_score"),
                "is_candidate": bool(row.get("final_candidate")),
                "is_selected": row["id"] in selected_ids,
            }
        )

    total_pages = max(1, -(-total // page_size))
    return {
        "day": day,
        "run_id": run_id,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "candidate_count": len(db.get_final_candidates(db_path, run_id=run_id)),
        "selection_locked": locked,
        "rows": listed,
    }


def render_collected_page(
    settings: Settings,
    db_path: Path,
    day: str,
    page: int = 1,
    api_base: str = "",
    repo_path: Optional[Path] = None,
) -> str:
    """Render the all-collected table (returned as a string, not written to disk)."""
    state = build_collected_state(settings, db_path, day, page=page)
    template = _environment(repo_path).get_template("hub/collected.jinja2")
    return template.render(
        api_base=api_base,
        state=state,
        generated_at=datetime.now(ZoneInfo(settings.app.timezone)).strftime("%Y-%m-%d %H:%M"),
    )


def _environment(repo_path: Optional[Path] = None) -> Environment:
    templates_dir = (repo_path or repo_root()) / "templates"
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "jinja2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_operator_page(
    settings: Settings,
    db_path: Path,
    day: str,
    output_path: Optional[Path] = None,
    api_base: str = "",
    repo_path: Optional[Path] = None,
) -> Path:
    """Render the operator page to disk and return its path."""
    output = Path(output_path) if output_path else resolve_path(settings.app.render_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    state = build_state(settings, db_path, day)
    history = build_history(settings, db_path, day)

    template = _environment(repo_path).get_template("hub/index.jinja2")
    html = template.render(
        api_base=api_base,
        state=state,
        history=history,
        state_json=json.dumps(state, ensure_ascii=False),
        generated_at=datetime.now(ZoneInfo(settings.app.timezone)).strftime("%Y-%m-%d %H:%M"),
    )
    output.write_text(html, encoding="utf-8")
    logger.info("Rendered operator page to %s (%d candidates)", output, len(state["candidates"]))
    return output
