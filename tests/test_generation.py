"""Start-generation: lock first, one echo per selected language, isolated failures."""

from __future__ import annotations

import pytest

from src import db, quiz_designer, workflow
from src.category_designer import CategoryResult
from src.models import EchoStatus, WorkflowStatus
from tests.conftest import FakeKvasirClient, make_design, seed_candidate

DAY = "2026-08-16"


@pytest.fixture
def selected(db_path):
    """A day with both slots selected but not yet locked."""
    db.ensure_day(db_path, DAY)
    ru = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                        url="https://newsru.co.il/story", title="Русская новость дня")
    en = seed_candidate(db_path, language="en", source_id="toi_en",
                        url="https://timesofisrael.com/story", title="An English story today")
    db.set_selection(db_path, DAY, "ru", ru, "funny")
    db.set_selection(db_path, DAY, "en", en, "important")
    return {"ru": ru, "en": en}


@pytest.fixture
def fake_designer(monkeypatch):
    """Replace both Claude calls (quiz concept and categories) with fixtures."""
    calls: list[dict] = []

    def design_quiz(**kwargs):
        calls.append(kwargs)
        return make_design(f"Poll for {kwargs['target_language']}")

    def generate_categories(**kwargs):
        language = kwargs["language"]
        return CategoryResult(
            categories=(
                ["Вы резервист", "Вы родитель школьника", "У вас малый бизнес",
                 "Вы снимаете жильё", "Вы ещё не определились, за кого голосовать"]
                if language == "ru"
                else ["You are a reservist", "You are a parent of school-age children",
                      "You run a small business", "You rent your home",
                      "You are an undecided voter"]
            ),
            party_categories_used=False,
        )

    monkeypatch.setattr(quiz_designer, "design_quiz", design_quiz)
    monkeypatch.setattr(workflow.quiz_designer, "design_quiz", design_quiz)
    monkeypatch.setattr(
        workflow.category_designer, "generate_categories", generate_categories
    )
    return calls


def _patch_day(monkeypatch):
    """Pin the workflow to a fixed day inside the tests."""
    return DAY


def test_start_generation_creates_one_echo_per_selected_language(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    result = workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    assert result["ok"] is True
    assert result["workflow_status"] == WorkflowStatus.editing.value
    assert {echo["language"] for echo in result["echoes"]} == {"ru", "en"}
    assert not result["errors"]

    rows = db.get_echoes_for_day(db_path, DAY)
    assert len(rows) == 2
    for row in rows:
        assert row["kvasir_echo_id"]
        assert row["status"] == EchoStatus.editing.value
        assert row["editor_url"].startswith("https://quizly.pub/echo-edit?id=")
        assert row["prompt_sha256"]

    # Two component_update calls per echo: create, then persist assets.text.
    assert len(client.component_updates) == 4


def test_generation_uses_the_right_course_and_persona_per_language(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    by_language = {kwargs["target_language"]: kwargs for kwargs in fake_designer}
    assert by_language["ru"]["tone"] == "funny"
    assert by_language["en"]["tone"] == "important"
    assert "остроумен" in by_language["ru"]["persona"], "Russian funny persona"
    assert "analytical" in by_language["en"]["persona"], "English important persona"

    ru_row = db.get_echo(db_path, DAY, "ru")
    en_row = db.get_echo(db_path, DAY, "en")
    assert ru_row["kvasir_course_id"] == secrets.kvasir_course_ru
    assert en_row["kvasir_course_id"] == secrets.kvasir_course_en
    assert ru_row["prompt_s3_key"].endswith(".ru.txt")
    assert en_row["prompt_s3_key"].endswith(".txt") and ".ru." not in en_row["prompt_s3_key"]


def test_second_start_is_rejected_and_creates_nothing(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)
    updates_after_first = len(client.component_updates)

    with pytest.raises(db.SelectionLocked):
        workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    assert len(client.component_updates) == updates_after_first


def test_one_language_failing_keeps_the_other(
    db_path, settings, secrets, selected, fake_designer, monkeypatch
):
    def design_quiz(**kwargs):
        if kwargs["target_language"] == "ru":
            raise RuntimeError("model refused")
        return make_design("English poll")

    monkeypatch.setattr(workflow.quiz_designer, "design_quiz", design_quiz)

    client = FakeKvasirClient()
    result = workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    assert result["workflow_status"] == WorkflowStatus.editing.value, "a partial success is not a failure"
    assert [echo["language"] for echo in result["echoes"]] == ["en"]
    assert result["errors"][0]["language"] == "ru"

    ru_row = db.get_echo(db_path, DAY, "ru")
    en_row = db.get_echo(db_path, DAY, "en")
    assert ru_row["status"] == EchoStatus.error.value and "model refused" in ru_row["error"]
    assert en_row["status"] == EchoStatus.editing.value and en_row["kvasir_echo_id"]


def test_both_languages_failing_marks_the_day_generation_failed(
    db_path, settings, secrets, selected, monkeypatch
):
    monkeypatch.setattr(
        workflow.quiz_designer, "design_quiz",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("API down")),
    )
    result = workflow.start_generation(settings, secrets, db_path, DAY, client=FakeKvasirClient())

    assert result["workflow_status"] == WorkflowStatus.generation_failed.value
    assert not result["echoes"]
    assert len(result["errors"]) == 2


def test_retry_reuses_the_same_component_by_default(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)
    original_id = db.get_echo(db_path, DAY, "ru")["kvasir_echo_id"]

    workflow.retry_language(settings, secrets, db_path, DAY, "ru", client=client)

    assert db.get_echo(db_path, DAY, "ru")["kvasir_echo_id"] == original_id, (
        "a retry must not create a second echo"
    )


def test_debug_regenerate_creates_a_fresh_component(
    db_path, settings, secrets, selected, fake_designer
):
    """The debug path deliberately re-creates the poll for the same story."""
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)
    original_id = db.get_echo(db_path, DAY, "ru")["kvasir_echo_id"]

    workflow.retry_language(settings, secrets, db_path, DAY, "ru", client=client, force_new=True)

    new_id = db.get_echo(db_path, DAY, "ru")["kvasir_echo_id"]
    assert new_id != original_id
    assert db.get_day(db_path, DAY)["ru_item_id"] == selected["ru"], "the story choice is unchanged"


def test_retry_before_start_is_refused(db_path, settings, secrets, selected):
    with pytest.raises(workflow.WorkflowError, match="not been locked"):
        workflow.retry_language(settings, secrets, db_path, DAY, "ru", client=FakeKvasirClient())


def test_generation_rejects_a_story_that_does_not_fit_the_slot(
    db_path, settings, secrets, fake_designer
):
    db.ensure_day(db_path, DAY)
    english = seed_candidate(db_path, language="en", source_id="toi_en",
                             url="https://timesofisrael.com/x", title="An English story today")
    russian = seed_candidate(db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/x", title="Русская новость дня")

    with pytest.raises(workflow.WorkflowError, match="Russian slot"):
        workflow.generate_language(settings, secrets, db_path, DAY, "ru", english, "funny",
                                   client=FakeKvasirClient())
    with pytest.raises(workflow.WorkflowError, match="English slot"):
        workflow.generate_language(settings, secrets, db_path, DAY, "en", russian, "funny",
                                   client=FakeKvasirClient())


def test_hebrew_story_generates_for_the_russian_slot_without_a_translation(
    db_path, settings, secrets, fake_designer
):
    """The RU quiz is written from the Hebrew original; no English step needed."""
    db.ensure_day(db_path, DAY)
    hebrew = seed_candidate(db_path, language="he", source_id="ynet_he",
                            url="https://ynet.co.il/x", title="כותרת בעברית ארוכה מספיק")

    client = FakeKvasirClient()
    echo = workflow.generate_language(settings, secrets, db_path, DAY, "ru", hebrew, "funny",
                                      client=client)

    assert echo["kvasir_echo_id"]
    assert echo["kvasir_course_id"] == secrets.kvasir_course_ru
    assert echo["prompt_s3_key"].endswith(".ru.txt")

    design_call = fake_designer[-1]
    assert design_call["item"].source_language == "he"
    assert design_call["item"].title_en is None, "no English rendering was required"
    assert design_call["target_language"] == "ru"


def test_one_hebrew_story_can_drive_both_languages(
    db_path, settings, secrets, fake_designer
):
    db.ensure_day(db_path, DAY)
    hebrew = seed_candidate(db_path, language="he", source_id="ynet_he",
                            url="https://ynet.co.il/y", title="עוד כותרת בעברית ארוכה",
                            title_en="A Hebrew headline", short_en="One sentence.")
    db.set_selection(db_path, DAY, "ru", hebrew, "funny")
    db.set_selection(db_path, DAY, "en", hebrew, "important")

    client = FakeKvasirClient()
    result = workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    assert {echo["language"] for echo in result["echoes"]} == {"ru", "en"}
    ru_row = db.get_echo(db_path, DAY, "ru")
    en_row = db.get_echo(db_path, DAY, "en")
    assert ru_row["kvasir_echo_id"] != en_row["kvasir_echo_id"], "two separate echoes"
    assert ru_row["kvasir_course_id"] == secrets.kvasir_course_ru
    assert en_row["kvasir_course_id"] == secrets.kvasir_course_en


def test_untranslated_hebrew_story_cannot_be_generated_for_the_en_slot(
    db_path, settings, secrets, fake_designer
):
    db.ensure_day(db_path, DAY)
    hebrew = seed_candidate(db_path, language="he", source_id="ynet_he",
                            url="https://ynet.co.il/x", title="כותרת בעברית ארוכה מספיק")

    with pytest.raises(workflow.WorkflowError, match="no English translation"):
        workflow.generate_language(settings, secrets, db_path, DAY, "en", hebrew, "important",
                                   client=FakeKvasirClient())


def test_description_and_picture_suggestions_are_persisted(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    row = db.get_echo(db_path, DAY, "en")
    assert row["description_html"].count("<a ") == 1
    assert "timesofisrael.com" in row["description_html"]
    assert db.json_list(row["picture_suggestions_json"])
    assert row["yes_no_question"]


def test_filled_prompt_reaches_s3_with_the_news_and_persona(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    ru_key = db.get_echo(db_path, DAY, "ru")["prompt_s3_key"]
    prompt = client.s3_objects[ru_key]

    assert "{{" not in prompt
    assert "Новость:" in prompt and "Предлагаемый вопрос да/нет:" in prompt
    # Categories are participant identities, serialized as JSON array contents.
    assert '"Вы резервист", "Вы родитель школьника"' in prompt
    assert "http" not in prompt, "the article URL belongs in the description, not the prompt"


def test_categories_are_persisted_with_their_provenance(
    db_path, settings, secrets, selected, fake_designer
):
    client = FakeKvasirClient()
    workflow.start_generation(settings, secrets, db_path, DAY, client=client)

    row = db.get_echo(db_path, DAY, "ru")
    assert db.json_list(row["categories_json"])[0] == "Вы резервист"
    assert row["category_fallback_used"] == 0
    assert row["category_party_used"] == 0
