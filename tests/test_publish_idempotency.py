"""Finalization: publish once, announce once, never touch the other language."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import db, telegram_publish, workflow
from src.models import EchoStatus, PublishStatus
from src.scroll_lookup import ScrollLookup
from src.workflow import WorkflowError
from tests.conftest import FakeKvasirClient, seed_candidate

DAY = "2026-08-16"


def quiz_scroll(scroll_id="q1", **overrides):
    scroll = {
        "scroll_id": scroll_id, "title": "Poll", "item_type": "quiz",
        "visibility": 1, "private": 0,
    }
    scroll.update(overrides)
    return scroll


@pytest.fixture
def prepared(db_path, settings):
    """A day with both languages generated and awaiting Finalize."""
    db.ensure_day(db_path, DAY)
    ru_item = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/story", title="Русская новость дня")
    en_item = seed_candidate(db_path, language="en", source_id="toi_en",
                             url="https://timesofisrael.com/story", title="An English story today")
    db.set_selection(db_path, DAY, "ru", ru_item, "funny")
    db.set_selection(db_path, DAY, "en", en_item, "important")
    db.lock_selection(db_path, DAY, "ru")
    db.lock_selection(db_path, DAY, "en")

    for language, item_id, echo_id in (("ru", ru_item, 4242), ("en", en_item, 4243)):
        db.upsert_echo(db_path, DAY, language, {
            "news_item_id": item_id,
            "tone": "funny" if language == "ru" else "important",
            "kvasir_echo_id": echo_id,
            "title": "Опрос дня" if language == "ru" else "Poll of the day",
            "description_html": 'Short description <a href="https://example.com/a">source</a>',
            "editor_url": f"https://quizly.pub/echo-edit?id={echo_id}",
            "status": EchoStatus.editing.value,
        })

    pages = Path(settings.publishing.html_dirs[0])
    return {
        "ru_item": ru_item,
        "en_item": en_item,
        "pages": pages,
        # Page names come from settings, never from a literal in the test.
        "ru_page": pages / settings.publishing.file_for("ru"),
        "en_page": pages / settings.publishing.file_for("en"),
    }


def make_client(scrolls, language="ru"):
    client = FakeKvasirClient()
    echo_id = 4242 if language == "ru" else 4243
    client.components[echo_id] = {"id": echo_id, "type": "echo", "language": language,
                                  "title": "Poll", "assets": {}, "details": {}}
    client.scrolls = scrolls
    return client


# ── The happy path ────────────────────────────────────────────────────────────


def test_finalize_publishes_the_page_and_records_the_event(db_path, settings, secrets, prepared):
    client = make_client([quiz_scroll()], "ru")
    result = workflow.finalize(settings, secrets, db_path, DAY, "ru",
                               client=client, lookup=ScrollLookup(client))

    assert result["scroll_id"] == "q1"
    assert result["quiz_target_url"] == "https://quizly.pub/scroll-quiz?id=4242#q1"
    assert result["stable_public_url"] == settings.publishing.public_url_for("ru")

    page = prepared["ru_page"].read_text(encoding="utf-8")
    assert f'data-day="{DAY}" data-language="ru"' in page
    assert "https://quizly.pub/scroll-quiz?id=4242#q1" in page

    key = db.idempotency_key(DAY, "ru", 4242, "q1")
    event = db.get_publish_event(db_path, key)
    assert event["page_published_at"]
    assert event["status"] == PublishStatus.complete.value  # telegram disabled in settings

    echo = db.get_echo(db_path, DAY, "ru")
    assert echo["status"] == EchoStatus.published.value
    assert echo["scroll_id"] == "q1"


def test_ru_finalize_does_not_touch_the_english_page(db_path, settings, secrets, prepared):
    english_before = prepared["en_page"].read_text(encoding="utf-8")

    client = make_client([quiz_scroll()], "ru")
    workflow.finalize(settings, secrets, db_path, DAY, "ru", client=client,
                      lookup=ScrollLookup(client))

    assert prepared["en_page"].read_text(encoding="utf-8") == english_before


def test_en_finalize_does_not_touch_the_russian_page(db_path, settings, secrets, prepared):
    russian_before = prepared["ru_page"].read_text(encoding="utf-8")

    client = make_client([quiz_scroll("qEN")], "en")
    workflow.finalize(settings, secrets, db_path, DAY, "en", client=client,
                      lookup=ScrollLookup(client))

    assert prepared["ru_page"].read_text(encoding="utf-8") == russian_before
    assert f'data-day="{DAY}" data-language="en"' in prepared["en_page"].read_text(
        encoding="utf-8"
    )


# ── Refusals ──────────────────────────────────────────────────────────────────


def test_zero_public_quizzes_publishes_nothing(db_path, settings, secrets, prepared):
    before = prepared["ru_page"].read_text(encoding="utf-8")
    client = make_client([quiz_scroll(item_type="scroll")], "ru")

    with pytest.raises(WorkflowError, match="No public scroll-quiz"):
        workflow.finalize(settings, secrets, db_path, DAY, "ru", client=client,
                          lookup=ScrollLookup(client))

    assert prepared["ru_page"].read_text(encoding="utf-8") == before
    assert db.get_publish_event_for(db_path, DAY, "ru") is None


def test_two_public_quizzes_publish_nothing(db_path, settings, secrets, prepared):
    before = prepared["ru_page"].read_text(encoding="utf-8")
    client = make_client([quiz_scroll("q1"), quiz_scroll("q2")], "ru")

    with pytest.raises(WorkflowError, match="More than one public scroll-quiz"):
        workflow.finalize(settings, secrets, db_path, DAY, "ru", client=client,
                          lookup=ScrollLookup(client))

    assert prepared["ru_page"].read_text(encoding="utf-8") == before
    assert db.get_publish_event_for(db_path, DAY, "ru") is None


def test_finalizing_a_language_that_was_never_generated_fails(db_path, settings, secrets):
    db.ensure_day(db_path, DAY)
    with pytest.raises(WorkflowError, match="no RU echo"):
        workflow.finalize(settings, secrets, db_path, DAY, "ru", client=FakeKvasirClient())


# ── Idempotency ───────────────────────────────────────────────────────────────


def test_repeated_finalize_sends_at_most_one_telegram_message(
    db_path, settings, secrets, prepared, monkeypatch
):
    sent: list[dict] = []
    monkeypatch.setattr(
        telegram_publish, "send_message",
        lambda bot_token, chat_id, text, timeout=20: (
            sent.append({"chat_id": chat_id, "text": text}) or f"msg-{len(sent)}"
        ),
    )
    live = settings.model_copy(
        update={"publishing": settings.publishing.model_copy(update={"telegram_enabled": True})}
    )
    client = make_client([quiz_scroll()], "ru")

    first = workflow.finalize(live, secrets, db_path, DAY, "ru", client=client,
                              lookup=ScrollLookup(client))
    second = workflow.finalize(live, secrets, db_path, DAY, "ru", client=client,
                               lookup=ScrollLookup(client))

    assert first["telegram"]["sent"] is True
    assert second["telegram"]["sent"] is False
    assert len(sent) == 1, "a second Finalize click must not repost"
    assert sent[0]["chat_id"] == "@poll_ru"
    assert settings.publishing.public_url_for("ru") in sent[0]["text"]

    page = prepared["ru_page"].read_text(encoding="utf-8")
    assert page.count(f'data-day="{DAY}" data-language="ru"') == 1, "no duplicate page entry"


def test_page_stays_published_when_telegram_fails_and_can_be_retried(
    db_path, settings, secrets, prepared, monkeypatch
):
    def failing_send(*args, **kwargs):
        raise telegram_publish.TelegramError("chat not found")

    monkeypatch.setattr(telegram_publish, "send_message", failing_send)
    live = settings.model_copy(
        update={"publishing": settings.publishing.model_copy(update={"telegram_enabled": True})}
    )
    client = make_client([quiz_scroll()], "ru")

    result = workflow.finalize(live, secrets, db_path, DAY, "ru", client=client,
                               lookup=ScrollLookup(client))

    assert result["telegram"]["sent"] is False
    assert result["telegram"]["retryable"] is True

    page = prepared["ru_page"].read_text(encoding="utf-8")
    assert f'data-day="{DAY}" data-language="ru"' in page, "the page must not be rolled back"

    event = db.get_publish_event_for(db_path, DAY, "ru")
    assert event["status"] == PublishStatus.page_published_telegram_failed.value

    sent: list[str] = []
    monkeypatch.setattr(
        telegram_publish, "send_message",
        lambda bot_token, chat_id, text, timeout=20: (sent.append(chat_id) or "msg-1"),
    )
    retry = workflow.retry_telegram(live, secrets, db_path, DAY, "ru")

    assert retry["sent"] is True
    assert sent == ["@poll_ru"]
    assert db.get_publish_event_for(db_path, DAY, "ru")["status"] == PublishStatus.complete.value


def test_retry_telegram_without_a_published_page_is_refused(db_path, settings, secrets, prepared):
    with pytest.raises(WorkflowError, match="nothing has been published"):
        workflow.retry_telegram(settings, secrets, db_path, DAY, "ru")


def test_day_becomes_finalized_only_when_both_languages_are_published(
    db_path, settings, secrets, prepared
):
    ru_client = make_client([quiz_scroll("q1")], "ru")
    workflow.finalize(settings, secrets, db_path, DAY, "ru", client=ru_client,
                      lookup=ScrollLookup(ru_client))
    assert db.get_day(db_path, DAY)["status"] == "partially_finalized"

    en_client = make_client([quiz_scroll("q2")], "en")
    workflow.finalize(settings, secrets, db_path, DAY, "en", client=en_client,
                      lookup=ScrollLookup(en_client))
    assert db.get_day(db_path, DAY)["status"] == "finalized"


def test_page_stays_chronological_and_dates_match_the_poll_day(settings, prepared):
    """Publishing an older day late must not reorder the page."""
    from src.publisher import publish_poll

    config = settings.publishing
    publish_poll(config, "2026-08-16", "ru", "Первый", "A", "https://quizly.pub/scroll-quiz?id=1#a")
    publish_poll(config, "2026-08-17", "ru", "Второй", "B", "https://quizly.pub/scroll-quiz?id=2#b")

    page = prepared["ru_page"].read_text(encoding="utf-8")

    assert page.index('data-day="2026-08-17"') < page.index('data-day="2026-08-16"')
    assert 'data-day=""' not in page, "the placeholder entry is dropped once real polls exist"
    assert '<time datetime="2026-08-17">2026-08-17</time>' in page


# ── The page is append-only ───────────────────────────────────────────────────


def test_a_second_poll_on_the_same_day_joins_the_first(settings, prepared):
    """Nothing is ever removed: two polls for one day both stay, newest on top."""
    from src.publisher import publish_poll

    config = settings.publishing
    publish_poll(config, "2026-08-16", "ru", "Первый опрос", "A",
                 "https://quizly.pub/scroll-quiz?id=1#a")
    publish_poll(config, "2026-08-16", "ru", "Второй опрос", "B",
                 "https://quizly.pub/scroll-quiz?id=2#b")

    page = prepared["ru_page"].read_text(encoding="utf-8")

    assert page.count('data-day="2026-08-16"') == 2
    assert "Первый опрос" in page and "Второй опрос" in page
    assert page.index("Второй опрос") < page.index("Первый опрос"), "the new one goes on top"


def test_a_new_entry_never_disturbs_the_ones_already_there(settings, prepared):
    """An earlier entry survives byte for byte, including its link and date."""
    from src.publisher import publish_poll

    config = settings.publishing
    publish_poll(config, "2026-08-16", "ru", "Первый опрос", "Описание",
                 "https://quizly.pub/scroll-quiz?id=1#a", source_url="https://newsru.co.il/a")
    before = prepared["ru_page"].read_text(encoding="utf-8")
    first_entry = before[before.index("<article"):before.index("</article>") + len("</article>")]

    publish_poll(config, "2026-08-17", "ru", "Второй опрос", "Другое описание",
                 "https://quizly.pub/scroll-quiz?id=2#b")

    after = prepared["ru_page"].read_text(encoding="utf-8")
    assert first_entry in after
    assert after.count("<article") == 2


def test_the_other_language_is_still_untouched_by_an_append(settings, prepared):
    from src.publisher import publish_poll

    config = settings.publishing
    english_before = prepared["en_page"].read_text(encoding="utf-8")
    publish_poll(config, "2026-08-16", "ru", "Опрос", "A", "https://quizly.pub/scroll-quiz?id=1#a")
    publish_poll(config, "2026-08-16", "ru", "Опрос 2", "B", "https://quizly.pub/scroll-quiz?id=2#b")

    assert prepared["en_page"].read_text(encoding="utf-8") == english_before


def test_the_entry_cap_can_be_switched_off(settings, prepared):
    """max_entries_per_page: 0 keeps every poll ever published."""
    from src.publisher import publish_poll

    capped = settings.publishing.model_copy(update={"max_entries_per_page": 2})
    uncapped = settings.publishing.model_copy(update={"max_entries_per_page": 0})

    for index in range(4):
        publish_poll(capped, f"2026-08-1{index}", "ru", f"Опрос {index}", "A",
                     f"https://quizly.pub/scroll-quiz?id={index}#a")
    assert prepared["ru_page"].read_text(encoding="utf-8").count("<article") == 2

    for index in range(4, 8):
        publish_poll(uncapped, f"2026-08-1{index}", "ru", f"Опрос {index}", "A",
                     f"https://quizly.pub/scroll-quiz?id={index}#a")
    assert prepared["ru_page"].read_text(encoding="utf-8").count("<article") == 6


def test_entry_escapes_model_supplied_text(settings, prepared):
    from src.publisher import publish_poll

    publish_poll(settings.publishing, "2026-08-16", "en",
                 '<script>alert("x")</script>', "desc & more",
                 "https://quizly.pub/scroll-quiz?id=1#a")

    page = prepared["en_page"].read_text(encoding="utf-8")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page and "desc &amp; more" in page


def test_publishing_refuses_a_page_without_markers(settings, tmp_path):
    from src.publisher import PublishError, publish_poll

    broken = tmp_path / "broken"
    broken.mkdir()
    config = settings.publishing.model_copy(update={"html_dirs": [str(broken)]})
    (broken / config.file_for("en")).write_text(
        "<html><body>no markers</body></html>", encoding="utf-8"
    )

    with pytest.raises(PublishError, match="POLLS:START"):
        publish_poll(config, "2026-08-16", "en", "t", "d", "https://quizly.pub/scroll-quiz?id=1#a")


def test_publish_event_key_is_stable_and_specific():
    assert db.idempotency_key("2026-08-16", "ru", 4242, "q1") == "2026-08-16:ru:4242:q1"
    assert db.idempotency_key("2026-08-16", "en", 4242, "q1") != db.idempotency_key(
        "2026-08-16", "ru", 4242, "q1"
    )
