"""The per-slot "use default categories, don't invent" option.

The template's CATEGORIES marker carries its own list; ticking the box on a slot
card uses that list verbatim and skips the model call that designs participants
for the story.
"""

from __future__ import annotations

import pytest

from src import category_designer, db, quiz_designer, workflow
from src.db import SelectionLocked
from tests.conftest import FakeKvasirClient, make_design, seed_candidate

DAY = "2026-08-16"

TEMPLATE_PARTIES_RU = ["Ликуд", "НДИ", "ШАС", "Демократы", "Оцма Йехудит"]


@pytest.fixture
def selected(db_path):
    db.ensure_day(db_path, DAY)
    ru = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                        url="https://newsru.co.il/story", title="Русская новость дня")
    db.set_selection(db_path, DAY, "ru", ru, "important")
    return ru


# ── The flag ──────────────────────────────────────────────────────────────────


def test_the_flag_is_off_by_default(db_path, selected):
    assert db.get_day(db_path, DAY)["ru_default_categories"] == 0


def test_the_flag_is_per_slot_and_editable(db_path, selected):
    db.set_default_categories(db_path, DAY, "ru", True)

    row = db.get_day(db_path, DAY)
    assert row["ru_default_categories"] == 1
    assert row["en_default_categories"] == 0, "the other language is untouched"

    db.set_default_categories(db_path, DAY, "ru", False)
    assert db.get_day(db_path, DAY)["ru_default_categories"] == 0


def test_the_flag_locks_with_its_own_language(db_path, selected):
    db.lock_selection(db_path, DAY, "ru")

    with pytest.raises(SelectionLocked, match="RU"):
        db.set_default_categories(db_path, DAY, "ru", True)

    # English is still free.
    db.set_default_categories(db_path, DAY, "en", True)
    assert db.get_day(db_path, DAY)["en_default_categories"] == 1


def test_changing_the_flag_keeps_the_selection(db_path, selected):
    db.set_default_categories(db_path, DAY, "ru", True)
    row = db.get_day(db_path, DAY)
    assert row["ru_item_id"] == selected and row["ru_tone"] == "important"


# ── The category source ───────────────────────────────────────────────────────


def test_default_categories_use_the_template_payload_verbatim(settings):
    result = category_designer.default_categories(
        settings=settings,
        language="ru",
        mode="important",
        news_title="Новость",
        news_summary="Что-то произошло.",
        party_defaults=TEMPLATE_PARTIES_RU,
    )

    assert result.categories == TEMPLATE_PARTIES_RU, "the template's list, unedited"
    assert result.party_categories_used is True
    assert result.fallback_used is False


def test_default_categories_drop_blanks_and_duplicates(settings):
    result = category_designer.default_categories(
        settings=settings,
        language="en",
        mode="important",
        news_title="A story",
        news_summary="Something happened.",
        party_defaults=["Likud", "  ", "Shas", "likud", "Shas ", None],
    )

    assert result.categories == ["Likud", "Shas"]


def test_a_template_without_defaults_falls_back_without_a_model(settings):
    result = category_designer.default_categories(
        settings=settings,
        language="en",
        mode="funny",
        news_title="A restaurant bill went viral",
        news_summary="Tourists were charged a fortune for lunch.",
        party_defaults=[],
    )

    assert result.categories, "the echo must not be left without categories"
    assert result.fallback_used is True
    assert result.party_categories_used is False


def test_default_categories_never_call_a_model(settings, monkeypatch):
    """conftest blocks model APIs; this asserts the intent explicitly."""
    def explode(**kwargs):
        raise AssertionError("default categories must not reach a model")

    monkeypatch.setattr(category_designer, "claude_json", explode, raising=False)

    result = category_designer.default_categories(
        settings=settings, language="ru", mode="funny",
        news_title="Новость", news_summary="Текст.", party_defaults=TEMPLATE_PARTIES_RU,
    )
    assert result.categories == TEMPLATE_PARTIES_RU


# ── Generation ────────────────────────────────────────────────────────────────


@pytest.fixture
def no_category_model(monkeypatch):
    """Generation must not design categories when the box is ticked."""
    def explode(**kwargs):
        raise AssertionError("generate_categories was called with the flag on")

    monkeypatch.setattr(workflow.category_designer, "generate_categories", explode)
    monkeypatch.setattr(workflow.quiz_designer, "design_quiz",
                        lambda **kwargs: make_design("Опрос дня"))
    monkeypatch.setattr(quiz_designer, "design_quiz",
                        lambda **kwargs: make_design("Опрос дня"))


def test_generation_uses_the_template_list_and_skips_the_designer(
    db_path, settings, secrets, selected, no_category_model
):
    db.set_default_categories(db_path, DAY, "ru", True)
    client = FakeKvasirClient()

    result = workflow.start_generation(
        settings, secrets, db_path, DAY, target_language="ru", client=client
    )
    assert result["echoes"], result

    echo = db.get_echo(db_path, DAY, "ru")
    assert db.json_list(echo["categories_json"]) == TEMPLATE_PARTIES_RU
    assert echo["categories_default_used"] == 1

    # The filled prompt carries the same list.
    prompt = client.s3_objects[echo["prompt_s3_key"]]
    for party in TEMPLATE_PARTIES_RU:
        assert party in prompt


def test_the_flag_off_still_designs_categories(db_path, settings, secrets, selected, monkeypatch):
    from src.category_designer import CategoryResult

    seen: list[dict] = []

    def generate_categories(**kwargs):
        seen.append(kwargs)
        return CategoryResult(categories=["Вы резервист", "Вы родитель школьника"])

    monkeypatch.setattr(workflow.category_designer, "generate_categories", generate_categories)
    monkeypatch.setattr(workflow.quiz_designer, "design_quiz",
                        lambda **kwargs: make_design("Опрос дня"))

    workflow.start_generation(
        settings, secrets, db_path, DAY, target_language="ru", client=FakeKvasirClient()
    )

    assert len(seen) == 1
    echo = db.get_echo(db_path, DAY, "ru")
    assert echo["categories_default_used"] == 0
    assert db.json_list(echo["categories_json"]) == ["Вы резервист", "Вы родитель школьника"]


# ── The checkbox follows the selection ────────────────────────────────────────


def test_changing_the_story_clears_the_checkbox(db_path, selected):
    db.set_default_categories(db_path, DAY, "ru", True)

    another = seed_candidate(db_path, language="ru", source_id="9tv_ru",
                             url="https://9tv.co.il/x", title="Другая новость дня")
    db.set_selection(db_path, DAY, "ru", another, "important")

    assert db.get_day(db_path, DAY)["ru_default_categories"] == 0


def test_changing_only_the_tone_keeps_the_checkbox(db_path, selected):
    """Switching important/funny is not a new story - the tick stands."""
    db.set_default_categories(db_path, DAY, "ru", True)
    db.set_selection(db_path, DAY, "ru", selected, "funny")

    assert db.get_day(db_path, DAY)["ru_default_categories"] == 1


def test_clearing_the_slot_clears_the_checkbox(db_path, selected):
    db.set_default_categories(db_path, DAY, "ru", True)
    db.set_selection(db_path, DAY, "ru", None, None)

    assert db.get_day(db_path, DAY)["ru_default_categories"] == 0


# ── Editing the categories of a generated echo ────────────────────────────────


@pytest.fixture
def generated(db_path, settings, secrets, selected, monkeypatch):
    """One generated RU echo, with its prompt in the fake S3."""
    from src.category_designer import CategoryResult

    monkeypatch.setattr(workflow.quiz_designer, "design_quiz",
                        lambda **kwargs: make_design("Опрос дня"))
    monkeypatch.setattr(workflow.category_designer, "generate_categories",
                        lambda **kwargs: CategoryResult(categories=["Вы резервист", "Вы студент"]))

    client = FakeKvasirClient()
    workflow.start_generation(
        settings, secrets, db_path, DAY, target_language="ru", client=client
    )
    return client


def test_editing_categories_rewrites_the_prompt_and_the_row(db_path, settings, secrets, generated):
    key = db.get_echo(db_path, DAY, "ru")["prompt_s3_key"]
    prompt_before = generated.s3_objects[key]

    result = workflow.update_categories(
        settings, secrets, db_path, DAY, "ru",
        ["Вы водитель автобуса", "Вы пассажир", "У вас нет машины"],
        client=generated,
    )

    assert result["categories"] == ["Вы водитель автобуса", "Вы пассажир", "У вас нет машины"]

    echo = db.get_echo(db_path, DAY, "ru")
    assert db.json_list(echo["categories_json"]) == result["categories"]

    prompt = generated.s3_objects[key]
    assert prompt != prompt_before, "the prompt changed"
    assert '"Вы водитель автобуса", "Вы пассажир", "У вас нет машины"' in prompt
    assert "Вы резервист" not in prompt, "the old list is gone"


def test_editing_categories_keeps_the_rest_of_the_prompt(db_path, settings, secrets, generated):
    """Operator edits made in the Kvasir editor must survive a category change."""
    echo = db.get_echo(db_path, DAY, "ru")
    key = echo["prompt_s3_key"]
    generated.s3_objects[key] = generated.s3_objects[key] + "\n\nHAND-WRITTEN NOTE FROM THE EDITOR\n"

    workflow.update_categories(settings, secrets, db_path, DAY, "ru", ["Вы пассажир"],
                               client=generated)

    prompt = generated.s3_objects[key]
    assert "HAND-WRITTEN NOTE FROM THE EDITOR" in prompt
    assert '"categories": ["Вы пассажир"]' in prompt


def test_editing_categories_never_touches_the_template(db_path, settings, secrets, generated):
    template_before = generated.s3_objects["500/text/9001.txt"]

    workflow.update_categories(settings, secrets, db_path, DAY, "ru", ["Вы пассажир"],
                               client=generated)

    assert generated.s3_objects["500/text/9001.txt"] == template_before


def test_blanks_and_duplicates_are_dropped_but_the_wording_is_kept(
    db_path, settings, secrets, generated
):
    result = workflow.update_categories(
        settings, secrets, db_path, DAY, "ru",
        ["  Вы пассажир  ", "", "Вы пассажир", "политика", "   "],
        client=generated,
    )

    # "политика" would be rejected as a taxonomy label if a model had proposed
    # it; from the operator it is taken as written.
    assert result["categories"] == ["Вы пассажир", "политика"]


def test_an_empty_list_is_refused(db_path, settings, secrets, generated):
    with pytest.raises(workflow.WorkflowError, match="at least one category"):
        workflow.update_categories(settings, secrets, db_path, DAY, "ru", ["", "  "],
                                   client=generated)


def test_editing_categories_before_the_prompt_exists_is_refused(db_path, settings, secrets):
    db.ensure_day(db_path, DAY)
    db.upsert_echo(db_path, DAY, "ru", {"kvasir_echo_id": 4242})

    with pytest.raises(workflow.WorkflowError, match="no prompt yet"):
        workflow.update_categories(settings, secrets, db_path, DAY, "ru", ["Вы пассажир"],
                                   client=FakeKvasirClient())


def test_a_prompt_without_a_categories_array_gives_an_actionable_error(
    db_path, settings, secrets, generated
):
    echo = db.get_echo(db_path, DAY, "ru")
    generated.s3_objects[echo["prompt_s3_key"]] = "a prompt from some other template"

    with pytest.raises(workflow.WorkflowError, match="categories"):
        workflow.update_categories(settings, secrets, db_path, DAY, "ru", ["Вы пассажир"],
                                   client=generated)


def test_a_bracket_inside_a_category_does_not_end_the_array():
    from src.echo_builder import replace_categories_in_prompt

    text = '{"categories": ["You rent [in Tel Aviv]", "You own"], "locale": "en"}'
    updated = replace_categories_in_prompt(text, '"You commute"')

    assert updated == '{"categories": ["You commute"], "locale": "en"}'
