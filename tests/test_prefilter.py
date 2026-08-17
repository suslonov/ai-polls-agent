"""Cheap prefilter: payload shape, verdict parsing, ranking and fallback."""

from __future__ import annotations

import json

import pytest

from src import prefilter
from src.llm import LLMError, parse_json_object
from tests.conftest import REPO_ROOT, make_item


def test_payload_never_contains_full_article_text():
    """Sending bodies to the cheap model is exactly what this stage avoids."""
    item = make_item(
        title="Council votes on scooters",
        snippet_original="A short snippet. " * 5,
        full_text="SECRET FULL BODY " * 500,
    )
    payload = prefilter.build_payload([item], offset=0, now=item.fetched_at)

    serialized = json.dumps(payload)
    assert "SECRET FULL BODY" not in serialized
    assert len(payload[0]["snippet"]) <= prefilter.MAX_SNIPPET_CHARS
    assert payload[0]["id"] == 0


def test_payload_ids_are_offset_by_batch():
    items = [make_item(url=f"https://example.com/news/{i}") for i in range(3)]
    payload = prefilter.build_payload(items, offset=40, now=items[0].fetched_at)
    assert [entry["id"] for entry in payload] == [40, 41, 42]


def test_parse_verdicts_skips_unusable_entries():
    response = {
        "items": [
            {"id": 0, "keep": True, "israel_relevance": 90, "interesting_score": 80,
             "funny_score": 10, "topic": "transport", "story_group_hint": "scooters"},
            {"keep": True},                      # no id
            {"id": "not-a-number", "keep": True},  # unparsable id
            {"id": 2, "keep": False},
        ]
    }
    verdicts = prefilter.parse_verdicts(response)
    assert set(verdicts) == {0, 2}
    assert verdicts[0].keep and verdicts[0].topic == "transport"
    assert verdicts[2].keep is False


def test_run_prefilter_keeps_only_kept_items_up_to_target(monkeypatch):
    items = [make_item(url=f"https://example.com/news/{i}", title=f"Story number {i}") for i in range(6)]

    def fake_gemini_json(**kwargs):
        return {
            "items": [
                {"id": i, "keep": i < 4, "israel_relevance": 50,
                 "interesting_score": 100 - i, "funny_score": 10, "topic": "society",
                 "story_group_hint": ""}
                for i in range(6)
            ]
        }

    monkeypatch.setattr(prefilter, "gemini_json", fake_gemini_json)
    kept, hints = prefilter.run_prefilter(items, api_key="k", model="m", keep_target=3)

    assert len(kept) == 3
    assert [item.title_original for item in kept] == ["Story number 0", "Story number 1", "Story number 2"]
    assert hints == {}
    assert items[5].prefilter_keep is False


def test_run_prefilter_collects_story_group_hints(monkeypatch):
    items = [make_item(url="https://a.example.com/1", language="he"),
             make_item(url="https://b.example.com/2", language="en")]

    monkeypatch.setattr(
        prefilter, "gemini_json",
        lambda **kwargs: {"items": [
            {"id": 0, "keep": True, "story_group_hint": "same-event"},
            {"id": 1, "keep": True, "story_group_hint": "same-event"},
        ]},
    )
    _, hints = prefilter.run_prefilter(items, api_key="k", model="m", keep_target=10)
    assert hints == {0: "same-event", 1: "same-event"}


def test_run_prefilter_falls_back_to_deterministic_ranking_when_gemini_fails(monkeypatch):
    """A model outage must degrade the day, not end it."""
    items = [make_item(url=f"https://example.com/news/{i}") for i in range(5)]

    def boom(**kwargs):
        raise LLMError("quota exceeded")

    monkeypatch.setattr(prefilter, "gemini_json", boom)
    kept, hints = prefilter.run_prefilter(items, api_key="k", model="m", keep_target=3)

    assert len(kept) == 3, "deterministic ranking still produces a candidate pool"
    assert hints == {}


def test_language_floor_tops_up_the_thin_slot(monkeypatch):
    """A Russian-poor day still leaves the RU slot something to choose from."""
    items = [make_item(url=f"https://en.example.com/{i}", language="en") for i in range(8)]
    items += [make_item(url=f"https://ru.example.com/{i}", language="ru",
                        source_id="newsru_ru") for i in range(3)]

    monkeypatch.setattr(
        prefilter, "gemini_json",
        lambda **kwargs: {"items": [
            {"id": i, "keep": True, "israel_relevance": 50,
             # English stories score higher, so a plain top-N would drop all Russian ones.
             "interesting_score": 90 if i < 8 else 10, "funny_score": 0}
            for i in range(11)
        ]},
    )
    kept, _ = prefilter.run_prefilter(items, api_key="k", model="m", keep_target=5)

    assert sum(1 for item in kept if item.source_language == "ru") == 3
    assert sum(1 for item in kept if item.source_language == "en") >= 5


def test_translate_item_returns_none_on_failure(monkeypatch):
    item = make_item(language="he", title="כותרת בעברית")
    monkeypatch.setattr(prefilter, "gemini_json", lambda **kwargs: {"title_en": "", "short_en": ""})
    assert prefilter.translate_item(item, api_key="k", model="m") is None


def test_translate_item_returns_title_and_summary(monkeypatch):
    item = make_item(language="he", title="כותרת בעברית")
    monkeypatch.setattr(
        prefilter, "gemini_json",
        lambda **kwargs: {"title_en": "A Hebrew headline", "short_en": "One sentence."},
    )
    assert prefilter.translate_item(item, api_key="k", model="m") == (
        "A Hebrew headline",
        "One sentence.",
    )


def test_parse_json_object_handles_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 1} — hope that helps') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json_object("no json here")


# ── Which languages get translated ────────────────────────────────────────────


def test_only_hebrew_finalists_are_translated(tmp_path, monkeypatch):
    """Russian and English stories never go to the translator.

    Russian is written from the Russian original and only ever fills the RU
    slot, so an English rendering would be a pointless Gemini call.
    """
    from src import db, pipeline
    from src.secrets import Secrets
    from src.settings import load_settings

    translated: list[str] = []

    def fake_translate(item, api_key, model):
        translated.append(item.source_language)
        return ("An English headline", "One English sentence.")

    monkeypatch.setattr(pipeline.prefilter, "translate_item", fake_translate)
    # No article fetching in this test — only the translation branch matters.
    monkeypatch.setattr(pipeline, "fetch", lambda *args, **kwargs: None)

    items = [
        make_item(language="ru", url="https://newsru.co.il/a", title="Русская новость"),
        make_item(language="en", url="https://timesofisrael.com/b", title="An English story"),
        make_item(language="he", url="https://ynet.co.il/c", title="כותרת בעברית"),
    ]
    db_path = tmp_path / "state.db"
    db.init_db(db_path)

    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    secrets = Secrets(google_api_key="test-google")

    pipeline.enrich_finalists(items, settings, secrets, db_path)

    assert translated == ["he"], "only Hebrew needs an English rendering"
    assert items[0].title_en is None and items[0].short_en is None, "Russian stays Russian"


# ── The selector's topic label ────────────────────────────────────────────────


def test_a_restated_headline_is_not_kept_as_a_topic():
    """The card shows `topic` above the title; a sentence there reads as a translation."""
    from src.selector import clean_topic, parse_picks

    assert clean_topic("bureaucracy") == "bureaucracy"
    assert clean_topic("  Consumer  ") == "consumer"
    assert clean_topic("public transport") == "public transport"

    assert clean_topic("Bituah Leumi branches closing to visitors for two weeks") == ""
    assert clean_topic("Israeli tourists overcharged €940 at Rhodes restaurant") == ""
    assert clean_topic("") == ""

    picks = parse_picks(
        {"selected": [
            {"id": 1, "rank": 1, "topic": "Bituah Leumi branches closing for two weeks"},
            {"id": 2, "rank": 2, "topic": "bureaucracy"},
        ]},
        valid_ids={1, 2},
    )
    assert [pick.topic for pick in picks] == ["", "bureaucracy"]


def test_an_empty_topic_keeps_the_prefilter_label(tmp_path):
    """update_selection must not blank a good label when the selector sends none."""
    from src import db

    db_path = tmp_path / "state.db"
    db.init_db(db_path)
    item_id = db.upsert_news_item(db_path, make_item(language="ru", topic="bureaucracy"))

    db.update_selection(db_path, item_id, rank=1, interesting=70, funny=10, topic="", why="w")

    assert db.get_item(db_path, item_id)["topic"] == "bureaucracy"
