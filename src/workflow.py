"""Operator-triggered steps: generate echoes, finalize, publish, retry.

Everything here runs only after a human clicks something in /polls. The cron
pipeline (:mod:`src.pipeline`) never calls into this module.

Ordering rule that the whole design rests on: the selection lock is taken and
committed *before* any LLM or Kvasir work starts, so two browser tabs pressing
Start can never create two echoes for the same day.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src import (
    category_designer,
    db,
    echo_builder,
    publisher,
    quiz_designer,
    telegram_publish,
)
from src.db import SelectionLocked
from src.kvasir_client import KvasirClient, KvasirError
from src.models import EchoStatus, NewsItem, PublishStatus, Settings, WorkflowStatus
from src.scroll_lookup import ScrollLookup, ScrollLookupError
from src.secrets import Secrets

logger = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    """Raised when an operator action cannot be completed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def item_from_row(row: dict) -> NewsItem:
    """Rebuild a NewsItem from its database row."""
    published_at = None
    if row.get("published_at"):
        try:
            published_at = datetime.fromisoformat(str(row["published_at"]))
        except ValueError:
            published_at = None

    return NewsItem(
        id=row["id"],
        run_id=row.get("run_id"),
        source_id=row["source_id"],
        source_name=row["source_name"],
        source_type=row["source_type"],
        source_language=row["source_language"],
        title_original=row["title_original"],
        title_en=row.get("title_en"),
        url=row["url"],
        canonical_url=row["canonical_url"],
        discovery_url=row.get("discovery_url"),
        published_at=published_at,
        dek_original=row.get("dek_original"),
        snippet_original=row.get("snippet_original"),
        full_text=row.get("full_text"),
        short_en=row.get("short_en"),
        content_hash=row.get("content_hash") or "",
        duplicate_group=row.get("duplicate_group"),
        topic=row.get("topic"),
    )


# ── Generation ────────────────────────────────────────────────────────────────


def generate_language(
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
    day: str,
    target_language: str,
    item_id: int,
    tone: str,
    client: Optional[KvasirClient] = None,
    force_new: bool = False,
) -> dict:
    """Design the quiz and build the Kvasir echo for one language.

    Idempotent by default: if a previous attempt already created the component,
    the retry reuses it (``existing_echo_id``) instead of creating a second one.
    ``force_new=True`` is the debug path — it deliberately creates a fresh echo
    from the same story so a poll can be re-generated while iterating.
    """
    row = db.get_item(db_path, item_id)
    if row is None:
        raise WorkflowError(f"news item {item_id} no longer exists")
    item = item_from_row(row)

    if not item.eligible_for(target_language):
        if target_language == "en" and item.source_language == "he":
            raise WorkflowError("this Hebrew story has no English translation yet")
        if target_language == "ru":
            raise WorkflowError("the Russian slot accepts Russian or Hebrew stories")
        raise WorkflowError("the English slot accepts English or Hebrew stories")

    existing = db.get_echo(db_path, day, target_language) or {}
    existing_echo_id = None if force_new else existing.get("kvasir_echo_id")

    db.upsert_echo(
        db_path,
        day,
        target_language,
        {
            "news_item_id": item_id,
            "tone": tone,
            "template_echo_id": secrets.kvasir_template,
            "kvasir_course_id": secrets.course_id_for(target_language),
            "status": EchoStatus.creating.value,
            "error": None,
            **({"kvasir_echo_id": None} if force_new else {}),
        },
    )

    try:
        persona = settings.persona_for(tone, target_language)

        design = quiz_designer.design_quiz(
            item=item,
            target_language=target_language,
            tone=tone,
            persona=persona,
            api_key=secrets.anthropic_api_key,
            model=settings.models.quiz_designer,
            max_chars=settings.collection.max_article_chars_for_enrichment,
            max_tokens=settings.models.quiz_designer_max_tokens,
        )

        # The source link is labelled with the article's own headline.
        description_html = quiz_designer.build_description_html(
            design.description_text,
            item.url,
            target_language,
            link_text=quiz_designer.link_text_for(item, target_language),
        )
        if quiz_designer.count_anchors(description_html) != 1:
            raise WorkflowError("composed description must contain exactly one link")

        # The chat host gets the raw article, not only the model's retelling.
        news_block = quiz_designer.build_news_summary_block(
            design.news_summary_for_prompt,
            design.yes_no_question,
            target_language,
            article_text=item.full_text or item.snippet_original or "",
            original_title=item.title_original,
            max_article_chars=settings.collection.max_article_chars_for_prompt,
        )

        db.upsert_echo(
            db_path,
            day,
            target_language,
            {
                "title": design.title,
                "description_html": description_html,
                "yes_no_question": design.yes_no_question,
                "greeting": design.greeting,
                "picture_suggestions_json": json.dumps(
                    design.picture_suggestions, ensure_ascii=False
                ),
            },
        )

        client = client or KvasirClient(secrets, settings.kvasir)

        # The template prompt is read before the echo exists: its CATEGORIES
        # marker carries the party list the category generator works from.
        template, template_prompt, _ = echo_builder.load_template(
            client, secrets.kvasir_template
        )
        party_defaults = echo_builder.marker_default(
            template_prompt, "CATEGORIES", target_language
        )

        # The operator can tick "use default categories" on the slot card: then
        # the template's own DEFAULT= payload is used as-is and no model is asked
        # to invent participants.
        day_row = db.get_day(db_path, day) or {}
        use_defaults = bool(day_row.get(f"{target_language}_default_categories"))

        if use_defaults:
            category_result = category_designer.default_categories(
                settings=settings,
                language=target_language,
                mode=tone,
                news_title=item.title_en or item.title_original,
                news_summary=design.news_summary_for_prompt,
                party_defaults=party_defaults,
            )
        else:
            # Categories are generated from the *final* yes/no question, not the
            # article title — the same story yields different constituencies
            # depending on what is actually being asked.
            category_result = category_designer.generate_categories(
                settings=settings,
                language=target_language,
                mode=tone,
                news_title=item.title_en or item.title_original,
                news_summary=design.news_summary_for_prompt,
                proposed_question=design.yes_no_question,
                api_key=secrets.anthropic_api_key,
                model=settings.models.quiz_designer,
                party_defaults=party_defaults,
            )

        db.upsert_echo(
            db_path,
            day,
            target_language,
            {
                "categories_json": json.dumps(category_result.categories, ensure_ascii=False),
                "categories_default_used": int(use_defaults),
                "category_fallback_used": int(category_result.fallback_used),
                "category_party_used": int(category_result.party_categories_used),
            },
        )
        logger.info(
            "Categories for %s/%s/%s: %s",
            day, target_language, tone, category_result.categories,
        )

        def remember_id(new_id: int) -> None:
            # Persist the component id the instant it exists: if S3 or the
            # second update fails, the retry must not create a second echo.
            db.upsert_echo(db_path, day, target_language, {"kvasir_echo_id": new_id})

        built = echo_builder.create_echo(
            client=client,
            template_echo_id=secrets.kvasir_template,
            target_course_id=secrets.course_id_for(target_language),
            target_language=target_language,
            design=design,
            description_html=description_html,
            news_summary_block=news_block,
            categories_value=category_designer.serialize_for_template(
                category_result.categories
            ),
            persona=persona,
            status=settings.kvasir.initial_component_status,
            existing_echo_id=existing_echo_id,
            on_created=remember_id,
            template=template,
            author_id=secrets.kvasir_author_id,
        )

        return db.upsert_echo(
            db_path,
            day,
            target_language,
            {
                "kvasir_echo_id": built.echo_id,
                "editor_url": built.editor_url,
                "prompt_s3_key": built.prompt_key,
                "prompt_sha256": built.prompt_hash,
                "status": EchoStatus.editing.value,
                "error": None,
            },
        )

    except Exception as exc:  # noqa: BLE001 - recorded per language, never fatal
        logger.error("Generation failed for %s/%s: %s", day, target_language, exc, exc_info=True)
        db.upsert_echo(
            db_path,
            day,
            target_language,
            {"status": EchoStatus.error.value, "error": str(exc)[:500]},
        )
        raise


def _selected_languages(workflow: dict) -> list[tuple[str, int, str]]:
    selected = []
    if workflow.get("ru_item_id"):
        selected.append(("ru", int(workflow["ru_item_id"]), workflow.get("ru_tone") or "important"))
    if workflow.get("en_item_id"):
        selected.append(("en", int(workflow["en_item_id"]), workflow.get("en_tone") or "important"))
    return selected


def _settle_day_status(db_path: Path, day: str) -> str:
    """Derive the day's status from the per-language state.

    The day-level status is only a summary now: each language advances on its
    own, so the day reports the furthest coherent point reached.
    """
    workflow = db.get_day(db_path, day) or {}
    echoes = {row["target_language"]: row for row in db.get_echoes_for_day(db_path, day)}
    locked = [lang for lang in ("ru", "en") if workflow.get(f"{lang}_locked_at")]

    if not locked:
        status = WorkflowStatus.ready.value
    else:
        published = [
            lang for lang in locked
            if (echoes.get(lang) or {}).get("status") == EchoStatus.published.value
        ]
        built = [
            lang for lang in locked
            if (echoes.get(lang) or {}).get("kvasir_echo_id")
            and (echoes.get(lang) or {}).get("status") != EchoStatus.error.value
        ]
        if published and len(published) == len(locked):
            status = WorkflowStatus.finalized.value
        elif published:
            status = WorkflowStatus.partially_finalized.value
        elif built:
            status = WorkflowStatus.editing.value
        else:
            status = WorkflowStatus.generation_failed.value

    db.set_day_status(db_path, day, status)
    return status


def start_generation(
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
    day: str,
    target_language: Optional[str] = None,
    client: Optional[KvasirClient] = None,
) -> dict:
    """Lock one language's selection and create its echo.

    Languages are independent: locking Russian leaves the English slot editable,
    so one poll can be taken all the way to publication while the other is still
    being chosen. Passing no language starts every selected one (each still
    locked separately).
    """
    secrets.require_llm()
    secrets.require_kvasir()

    db.ensure_day(db_path, day)
    workflow = db.get_day(db_path, day) or {}

    wanted = _selected_languages(workflow)
    if target_language:
        wanted = [entry for entry in wanted if entry[0] == target_language]
        if not wanted:
            raise SelectionLocked(f"nothing selected for the {target_language.upper()} slot")

    results = []
    errors = []

    for language, item_id, tone in wanted:
        # Atomic per language: raises before any external call if already locked.
        db.lock_selection(db_path, day, language)
        try:
            echo = generate_language(
                settings, secrets, db_path, day, language, item_id, tone, client=client
            )
            results.append(
                {
                    "language": language,
                    "echo_id": echo.get("kvasir_echo_id"),
                    "editor_url": echo.get("editor_url"),
                    "title": echo.get("title"),
                    "picture_suggestions": db.json_list(echo.get("picture_suggestions_json")),
                    "categories": db.json_list(echo.get("categories_json")),
                }
            )
        except Exception as exc:  # noqa: BLE001 - per-language isolation
            errors.append({"language": language, "error": str(exc)})

    status = _settle_day_status(db_path, day)
    return {
        "ok": bool(results) and not errors,
        "workflow_status": status,
        "echoes": results,
        "errors": errors,
    }


def close_language(db_path: Path, day: str, target_language: str) -> dict:
    """Retire a finished poll so another can be started for that language today.

    "One poll per language per day" is the intended rhythm, not a rule the code
    enforces: closing frees the slot and keeps everything already published.
    """
    result = db.close_echo(db_path, day, target_language)
    _settle_day_status(db_path, day)
    return result


def retry_language(
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
    day: str,
    target_language: str,
    client: Optional[KvasirClient] = None,
    force_new: bool = False,
) -> dict:
    """Retry generation for one language without touching the other.

    The day's selection stays locked — a retry re-runs generation for the story
    the operator already chose, it never reopens the choice.
    """
    secrets.require_llm()
    secrets.require_kvasir()

    workflow = db.get_day(db_path, day)
    if not workflow:
        raise WorkflowError(f"no workflow for {day}")
    if not workflow.get(f"{target_language}_locked_at"):
        raise WorkflowError(
            f"the {target_language.upper()} selection has not been locked yet — "
            "press Start for that language first"
        )

    item_id = workflow.get(f"{target_language}_item_id")
    tone = workflow.get(f"{target_language}_tone")
    if not item_id:
        raise WorkflowError(f"no {target_language.upper()} story was selected for {day}")

    echo = generate_language(
        settings,
        secrets,
        db_path,
        day,
        target_language,
        int(item_id),
        tone or "important",
        client=client,
        force_new=force_new,
    )
    status = _settle_day_status(db_path, day)
    return {
        "ok": True,
        "workflow_status": status,
        "echo": {
            "language": target_language,
            "echo_id": echo.get("kvasir_echo_id"),
            "editor_url": echo.get("editor_url"),
            "title": echo.get("title"),
            "picture_suggestions": db.json_list(echo.get("picture_suggestions_json")),
        },
    }


# ── Finalization ──────────────────────────────────────────────────────────────


def finalize(
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
    day: str,
    target_language: str,
    client: Optional[KvasirClient] = None,
    lookup: Optional[ScrollLookup] = None,
) -> dict:
    """Publish one finished language: find its quiz, update the page, announce it.

    Nothing is published unless exactly one public scroll-quiz exists. If the
    page write succeeds and Telegram then fails, the page stays published and
    the event is left retryable.
    """
    secrets.require_kvasir()

    echo = db.get_echo(db_path, day, target_language)
    if not echo:
        raise WorkflowError(f"no {target_language.upper()} echo exists for {day}")
    if echo.get("target_language") != target_language:
        raise WorkflowError("echo language mismatch")
    echo_id = echo.get("kvasir_echo_id")
    if not echo_id:
        raise WorkflowError("this echo was never created in Kvasir")

    client = client or KvasirClient(secrets, settings.kvasir)
    lookup = lookup or ScrollLookup(client)

    # 1. The component must still exist and still be ours.
    try:
        component = client.get_component(echo_id)
    except KvasirError as exc:
        raise WorkflowError(f"cannot read echo {echo_id} from Kvasir: {exc}") from exc
    if component.get("language") and component.get("language") != target_language:
        raise WorkflowError(
            f"echo {echo_id} is {component.get('language')!r}, expected {target_language!r}"
        )

    # 2. Exactly one public quiz, or nothing is published.
    try:
        quiz = lookup.get_public_quiz(echo_id)
    except ScrollLookupError as exc:
        db.upsert_echo(db_path, day, target_language, {"error": str(exc)[:500]})
        raise WorkflowError(str(exc)) from exc

    db.upsert_echo(
        db_path,
        day,
        target_language,
        {
            "scroll_id": quiz.scroll_id,
            "scroll_public_url": quiz.public_url,
            "status": EchoStatus.ready_to_publish.value,
            "error": None,
        },
    )

    key = db.idempotency_key(day, target_language, echo_id, quiz.scroll_id)
    event = db.get_publish_event(db_path, key) or {}
    stable_url = settings.publishing.public_url_for(target_language)

    item = db.get_item(db_path, echo.get("news_item_id")) if echo.get("news_item_id") else None
    description_text = _plain_description(echo.get("description_html") or "")

    # 3. Stable page (only this language's file).
    if not event.get("page_published_at"):
        try:
            written = publisher.publish_poll(
                config=settings.publishing,
                day=day,
                target_language=target_language,
                title=echo.get("title") or "",
                description_text=description_text,
                quiz_url=quiz.public_url,
                source_url=(item or {}).get("url"),
            )
        except publisher.PublishError as exc:
            db.upsert_publish_event(
                db_path,
                key,
                {
                    "day": day,
                    "target_language": target_language,
                    "kvasir_echo_id": echo_id,
                    "scroll_id": quiz.scroll_id,
                    "quiz_target_url": quiz.public_url,
                    "stable_public_url": stable_url,
                    "status": PublishStatus.failed.value,
                    "error": str(exc)[:500],
                },
            )
            raise WorkflowError(f"publishing the stable page failed: {exc}") from exc

        if not publisher.verify_published(written, day, target_language):
            raise WorkflowError("the stable page was written but the entry is not present in it")

        event = db.upsert_publish_event(
            db_path,
            key,
            {
                "day": day,
                "target_language": target_language,
                "kvasir_echo_id": echo_id,
                "scroll_id": quiz.scroll_id,
                "quiz_target_url": quiz.public_url,
                "stable_public_url": stable_url,
                "page_files_json": json.dumps(written, ensure_ascii=False),
                "page_published_at": _now(),
                "status": PublishStatus.page_published.value,
                "error": None,
            },
        )

    # 4. Telegram — at most once per publish event.
    telegram = _send_telegram_once(
        settings, secrets, db_path, key, event, target_language,
        title=echo.get("title") or "",
        description_text=description_text,
    )

    db.upsert_echo(
        db_path,
        day,
        target_language,
        {"status": EchoStatus.published.value, "finalized_at": _now(), "error": None},
    )
    _settle_day_status(db_path, day)

    return {
        "ok": True,
        "language": target_language,
        "scroll_id": quiz.scroll_id,
        "quiz_target_url": quiz.public_url,
        "stable_public_url": stable_url,
        "telegram": telegram,
    }


def _plain_description(description_html: str) -> str:
    """Strip the anchor back out of the stored description for plain-text uses."""
    import re

    text = re.sub(r"<a\b[^>]*>.*?</a>", "", description_html or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    import html as html_module

    return " ".join(html_module.unescape(text).split())


def _send_telegram_once(
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
    key: str,
    event: dict,
    target_language: str,
    title: str,
    description_text: str,
) -> dict:
    """Send the announcement unless this publish event already sent one."""
    if not settings.publishing.telegram_enabled:
        db.upsert_publish_event(db_path, key, {"status": PublishStatus.complete.value})
        return {"sent": False, "reason": "telegram publishing is disabled in settings.yaml"}

    if event.get("telegram_sent_at"):
        return {"sent": False, "reason": "already announced", "at": event["telegram_sent_at"]}

    try:
        secrets.require_telegram()
        message_id = telegram_publish.announce(
            bot_token=secrets.telegram_bot_token,
            chat_id=secrets.telegram_channel_for(target_language),
            title=title,
            description_text=description_text,
            public_url=settings.publishing.public_url_for(target_language),
        )
    except Exception as exc:  # noqa: BLE001 - page stays published either way
        logger.warning("Telegram announcement failed: %s", exc)
        db.upsert_publish_event(
            db_path,
            key,
            {
                "status": PublishStatus.page_published_telegram_failed.value,
                "error": str(exc)[:500],
            },
        )
        return {"sent": False, "error": str(exc), "retryable": True}

    db.upsert_publish_event(
        db_path,
        key,
        {
            "telegram_sent_at": _now(),
            "telegram_message_id": message_id,
            "status": PublishStatus.complete.value,
            "error": None,
        },
    )
    return {"sent": True, "message_id": message_id}


def retry_telegram(
    settings: Settings,
    secrets: Secrets,
    db_path: Path,
    day: str,
    target_language: str,
) -> dict:
    """Re-send only the announcement for an already-published page."""
    event = db.get_publish_event_for(db_path, day, target_language)
    if not event:
        raise WorkflowError("nothing has been published for this day and language yet")
    if not event.get("page_published_at"):
        raise WorkflowError("the stable page was never published — finalize first")
    if event.get("telegram_sent_at"):
        return {"sent": False, "reason": "already announced", "at": event["telegram_sent_at"]}

    echo = db.get_echo(db_path, day, target_language) or {}
    result = _send_telegram_once(
        settings,
        secrets,
        db_path,
        event["idempotency_key"],
        event,
        target_language,
        title=echo.get("title") or "",
        description_text=_plain_description(echo.get("description_html") or ""),
    )
    _settle_day_status(db_path, day)
    return result


__all__ = [
    "WorkflowError",
    "SelectionLocked",
    "start_generation",
    "retry_language",
    "generate_language",
    "close_language",
    "finalize",
    "retry_telegram",
    "item_from_row",
]
