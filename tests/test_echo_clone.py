"""Cloning the template echo: prompt filling, S3 safety, exactly two updates."""

from __future__ import annotations

import pytest

from src import echo_builder
from src.echo_builder import EchoBuildError, build_component_record, fill_prompt_template
from tests.conftest import FakeKvasirClient, make_design

CATEGORIES = ["You are a reservist", "You run a small business"]
CATEGORIES_VALUE = '"You are a reservist", "You run a small business"'
PERSONA = "You are an editor of a concise Israeli public-opinion poll."
NEWS_BLOCK = "News:\nThe council votes on scooters.\n\nProposed yes/no question:\nBan them?"


# ── Prompt template ───────────────────────────────────────────────────────────


SIMPLE_TEMPLATE = "L={{LOCALE}}\nC=[{{CATEGORIES}}]\nN=\n{{NEWS_SUMMARY}}\nP={{PERSONA}}"


def test_all_mandatory_markers_are_replaced():
    filled = fill_prompt_template(SIMPLE_TEMPLATE, "ru", CATEGORIES_VALUE, NEWS_BLOCK, PERSONA)

    assert "{{" not in filled
    assert "Russian (ru)" in filled
    assert f"C=[{CATEGORIES_VALUE}]" in filled
    assert NEWS_BLOCK in filled
    assert PERSONA in filled


def test_deployed_default_payload_form_is_supported():
    """The live template carries a JSON party list inside the marker itself."""
    template = (
        '"categories": [{{CATEGORIES DEFAULT={\n'
        '"ru": ["Ликуд", "ШАС"],\n"en": ["Likud", "Shas"]}}}]\n'
        "{{LOCALE}} {{NEWS_SUMMARY}} {{PERSONA}}"
    )
    filled = fill_prompt_template(template, "ru", CATEGORIES_VALUE, NEWS_BLOCK, PERSONA)

    assert f'"categories": [{CATEGORIES_VALUE}]' in filled
    assert "DEFAULT" not in filled and "Ликуд" not in filled
    assert "{{" not in filled


def test_party_defaults_are_read_out_of_the_marker():
    template = (
        '[{{CATEGORIES DEFAULT={"ru": ["Ликуд", "ШАС"], "en": ["Likud", "Shas"]}}}]'
    )
    assert echo_builder.marker_default(template, "CATEGORIES", "ru") == ["Ликуд", "ШАС"]
    assert echo_builder.marker_default(template, "CATEGORIES", "en") == ["Likud", "Shas"]
    assert echo_builder.marker_default("{{CATEGORIES}}", "CATEGORIES", "en") == []


def test_serialized_categories_make_the_template_json_valid():
    """The marker sits inside `"categories": [...]`, so it needs array contents."""
    import json as json_module

    from src.category_designer import serialize_for_template

    template = '{"categories": [{{CATEGORIES DEFAULT={"en": []}}}], "x": 1}'
    filled = fill_prompt_template(
        template + " {{LOCALE}}{{NEWS_SUMMARY}}{{PERSONA}}",
        "en",
        serialize_for_template(["You are a reservist", 'You said "yes"']),
        "",
        "",
    )
    parsed = json_module.loads(filled.split("} ")[0] + "}")
    assert parsed["categories"] == ["You are a reservist", 'You said "yes"']


def test_missing_marker_fails_generation():
    for missing in echo_builder.MANDATORY_MARKERS:
        template = " ".join(
            "{{" + name + "}}" for name in echo_builder.MANDATORY_MARKERS if name != missing
        )
        with pytest.raises(EchoBuildError, match="missing mandatory marker"):
            fill_prompt_template(template, "en", CATEGORIES_VALUE, NEWS_BLOCK, PERSONA)


def test_unknown_leftover_marker_is_rejected():
    template = SIMPLE_TEMPLATE + " {{SOMETHING_ELSE}}"
    with pytest.raises(EchoBuildError, match="Unsubstituted marker"):
        fill_prompt_template(template, "en", CATEGORIES_VALUE, NEWS_BLOCK, PERSONA)


# ── Component record ──────────────────────────────────────────────────────────


def test_record_keeps_game_settings_and_drops_server_derived_fields():
    template = FakeKvasirClient().template | {
        "author_id": 7,
        "nickname": "someone",
        "course_title": "Template course",
        "is_creator": 1,
        "components": [1, 2],
    }
    record = build_component_record(template, 501, "ru", "Заголовок", "<p>desc</p>")

    assert record["course_id"] == 501
    assert record["language"] == "ru"
    assert record["status"] == "raw"
    assert record["title"] == "Заголовок"
    # Intentional template settings survive.
    assert record["details"]["conv_type"] == "poll"
    assert record["details"]["llm_model"] == "claude"
    assert record["voice"] == "alloy"
    assert record["total_adviser"] == 3
    # Overrides.
    assert record["details"]["description"] == "<p>desc</p>"
    assert record["details"]["public"] is True
    # Read-only / server-derived fields never travel.
    for forbidden in ("id", "nickname", "course_title", "is_creator", "components"):
        assert forbidden not in record
    # author_id is sent (kv2_course needs it for donations); with none supplied
    # it falls back to the template's author.
    assert record["author_id"] == 7
    # assets.text is added only by the second update.
    assert "text" not in record["assets"]


def test_donation_settings_are_inherited_when_an_author_id_is_sent():
    """kv2_course needs author_id whenever donations are on — so we send it."""
    template = FakeKvasirClient().template
    template["details"].update({
        "allow_donations": True,
        "donations_goal": "500",
        "donations_collected": "12",
        "accounts": {"Paypal": "someone@example.com"},
    })

    record = build_component_record(
        template, 501, "ru", "Заголовок", "<p>desc</p>", author_id="16"
    )

    assert record["author_id"] == "16", "the poll account, not the template's author"
    assert record["details"]["allow_donations"] is True
    assert record["details"]["donations_goal"] == "500"
    # Server-derived / per-instance donation fields still never travel.
    for dropped in ("donations_collected", "accounts"):
        assert dropped not in record["details"]


def test_donations_are_disabled_when_no_author_id_is_available():
    """Without an author_id the Lambda would 500 on KeyError: 'author_id'."""
    template = FakeKvasirClient().template
    template["details"]["allow_donations"] = True
    template.pop("author_id", None)

    record = build_component_record(template, 501, "ru", "Заголовок", "<p>desc</p>")

    assert record["details"]["allow_donations"] is False
    assert "author_id" not in record


def test_full_build_with_donations_enabled_completes_both_updates():
    """End to end: both component updates succeed, assets.text is persisted."""
    client = FakeKvasirClient()
    client.template["details"]["allow_donations"] = True

    built = _create(client, author_id="16")

    assert len(client.component_updates) == 2
    assert client.component_updates[1]["details"]["allow_donations"] is True
    assert client.component_updates[1]["author_id"] == "16"
    assert client.component_updates[1]["assets"]["text"]["name"] == str(built.echo_id)


def test_title_picture_is_copied_into_the_new_echo():
    """The template's picture is duplicated under the new echo's own course."""
    client = FakeKvasirClient()

    built = _create(client, language="ru", course_id=501)

    assert ("500/title_picture/9001.jpg", f"501/title_picture/{built.echo_id}.jpg") in client.copies
    picture = client.component_updates[1]["assets"]["title_picture"]
    assert picture == {"region": "us-east-1", "name": str(built.echo_id), "ext": "jpg"}


def test_a_missing_picture_does_not_fail_the_echo():
    """A picture that is not in S3 is skipped, not fatal."""
    client = FakeKvasirClient()
    del client.s3_objects["500/title_picture/9001.jpg"]

    built = _create(client, language="ru", course_id=501)

    assert built.echo_id
    assert "title_picture" not in client.component_updates[1]["assets"]


def test_generated_greeting_replaces_the_template_one():
    template = FakeKvasirClient().template
    template["details"]["greetings"] = "template"

    record = build_component_record(
        template, 501, "ru", "t", "<p>d</p>", greeting="Сегодня в Цфате спорят об автобусах."
    )
    assert record["details"]["greetings"] == "Сегодня в Цфате спорят об автобусах."


def test_dashes_are_normalized_in_component_fields():
    template = FakeKvasirClient().template
    record = build_component_record(
        template, 501, "ru", "Опрос — дня", "<p>текст — тут</p>",
        greeting="Привет — это опрос",
    )
    assert record["title"] == "Опрос - дня"
    assert "—" not in record["details"]["description"]
    assert "—" not in record["details"]["greetings"]


def test_non_echo_template_is_rejected():
    with pytest.raises(EchoBuildError, match="not an echo"):
        build_component_record({"type": "readings", "assets": {}}, 500, "en", "t", "d")


def test_template_without_text_asset_is_rejected():
    with pytest.raises(EchoBuildError, match="assets.text"):
        build_component_record({"type": "echo", "assets": {}}, 500, "en", "t", "d")


# ── Full build ────────────────────────────────────────────────────────────────


def _create(client, language="en", course_id=500, existing=None, seen=None, author_id=None):
    return echo_builder.create_echo(
        client=client,
        template_echo_id=9001,
        target_course_id=course_id,
        target_language=language,
        design=make_design(),
        description_html='desc <a href="https://example.com/a">source</a>',
        news_summary_block=NEWS_BLOCK,
        categories_value=CATEGORIES_VALUE,
        persona=PERSONA,
        existing_echo_id=existing,
        on_created=(seen.append if seen is not None else None),
        author_id=author_id,
    )


def test_creating_a_new_echo_performs_exactly_two_component_updates():
    client = FakeKvasirClient()
    built = _create(client)

    assert len(client.component_updates) == 2, "one create, one to persist assets.text"
    first, second = client.component_updates
    assert "id" not in first, "the first update creates the component"
    assert second["id"] == built.echo_id
    assert second["assets"]["text"] == {"region": "us-east-1", "name": str(built.echo_id), "ext": "txt"}


def test_prompt_is_copied_then_filled_at_the_destination_only():
    client = FakeKvasirClient()
    built = _create(client, language="ru", course_id=501)

    assert ("500/text/9001.txt", f"501/text/{built.echo_id}.ru.txt") in client.copies
    written_keys = [key for key, _ in client.puts]
    assert written_keys == [f"501/text/{built.echo_id}.ru.txt"]

    # The template object is untouched and still has its markers.
    template_text = client.s3_objects["500/text/9001.txt"]
    assert "{{NEWS_SUMMARY}}" in template_text
    assert "{{PERSONA}}" in template_text

    filled = client.s3_objects[f"501/text/{built.echo_id}.ru.txt"]
    assert "{{" not in filled
    assert PERSONA in filled


def test_english_and_russian_destination_keys_differ_by_postfix():
    english = FakeKvasirClient()
    built_en = _create(english, language="en", course_id=500)
    assert built_en.prompt_key == f"500/text/{built_en.echo_id}.txt"

    russian = FakeKvasirClient()
    built_ru = _create(russian, language="ru", course_id=501)
    assert built_ru.prompt_key == f"501/text/{built_ru.echo_id}.ru.txt"


def test_component_id_is_reported_before_s3_work_starts():
    """The caller must be able to persist the id before anything else can fail."""
    client = FakeKvasirClient()
    seen: list[int] = []
    built = _create(client, seen=seen)

    assert seen == [built.echo_id]


def test_retry_reuses_an_existing_component_instead_of_creating_a_second():
    client = FakeKvasirClient()
    built = _create(client, existing=7777)

    assert built.echo_id == 7777
    assert len(client.component_updates) == 1, "no create call on a resumed build"
    assert client.component_updates[0]["id"] == 7777
    assert ("500/text/9001.txt", "500/text/7777.txt") in client.copies


def test_refuses_to_overwrite_the_template_prompt():
    """Cloning into the template's own key would destroy the template."""
    client = FakeKvasirClient()
    with pytest.raises(EchoBuildError, match="Refusing to overwrite"):
        _create(client, language="en", course_id=500, existing=9001)

    assert client.puts == []
    assert "{{PERSONA}}" in client.s3_objects["500/text/9001.txt"]


def test_prompt_hash_is_recorded():
    client = FakeKvasirClient()
    built = _create(client)
    assert len(built.prompt_hash) == 64
    assert built.prompt_hash == echo_builder.prompt_sha256(client.s3_objects[built.prompt_key])


def test_assets_pointing_at_the_template_course_are_dropped_on_cross_course_clone():
    client = FakeKvasirClient()
    client.template["assets"]["questions"] = {"region": "us-east-1", "name": "9001", "ext": "txt"}
    client.template["assets"]["picture"] = {"region": "us-east-1", "name": "shared-image", "ext": "png"}

    record = build_component_record(client.template, 501, "ru", "t", "d")
    assert "questions" not in record["assets"], "would point at the template course's object"
    assert "picture" in record["assets"], "shared assets are safe to keep"
    assert "title_picture" not in record["assets"], "copied per echo in the second update"


# ── The target course ─────────────────────────────────────────────────────────
# kv2_course checks the course only when *updating* a component: creating one
# accepts any course_id. A wrong KVASIR_COURSE_* therefore used to surface as
# "HTTP 404: Course not found or you are not the author" from the second call,
# after a real component had already been created and orphaned.


def test_a_course_that_does_not_exist_is_caught_before_anything_is_created():
    client = FakeKvasirClient()

    with pytest.raises(EchoBuildError, match="does not exist|could not be read"):
        _create(client, course_id=999)

    assert client.component_updates == [], "no orphan component"
    assert client.copies == [] and client.puts == []


def test_a_course_owned_by_someone_else_is_caught_before_anything_is_created():
    client = FakeKvasirClient()
    client.courses[502] = {"id": 502, "author_id": 54, "language": "en", "status": "free"}

    with pytest.raises(EchoBuildError, match="belongs to author 54"):
        _create(client, course_id=502, author_id=16)

    assert client.component_updates == [], "no orphan component"


def test_the_preflight_reads_the_course_before_the_first_update():
    client = FakeKvasirClient()
    _create(client, course_id=500, author_id=16)

    actions = [call["payload"].get("action") for call in client.calls]
    assert actions.index("get_course") < actions.index("component_update")


def test_a_refused_second_update_names_the_echo_and_points_at_retry():
    """The component exists by then — the error must not read like a create failure."""
    client = FakeKvasirClient()
    seen: list[int] = []

    # The course disappears between the two calls, exactly as a deleted course
    # (or a component removed by hand) looks to kv2_course.
    original_copy = client.copy_object

    def copy_and_lose_the_course(source_key, destination_key):
        original_copy(source_key, destination_key)
        client.courses.pop(500, None)

    client.copy_object = copy_and_lose_the_course

    with pytest.raises(EchoBuildError) as excinfo:
        _create(client, course_id=500, seen=seen)

    message = str(excinfo.value)
    assert str(seen[0]) in message, "the operator needs the component id"
    assert "Retry" in message
    assert "Course not found or you are not the author" in message, "the cause is kept"


def test_retry_finishes_the_echo_once_the_course_is_reachable_again():
    client = FakeKvasirClient()
    seen: list[int] = []
    original_copy = client.copy_object

    def copy_and_lose_the_course(source_key, destination_key):
        original_copy(source_key, destination_key)
        client.courses.pop(500, None)

    client.copy_object = copy_and_lose_the_course
    with pytest.raises(EchoBuildError):
        _create(client, course_id=500, seen=seen)

    echo_id = seen[0]
    client.copy_object = original_copy
    client.courses[500] = {"id": 500, "author_id": 16, "language": "en", "status": "free"}

    built = _create(client, course_id=500, existing=echo_id)

    assert built.echo_id == echo_id, "the retry must not create a second echo"
    assert client.components[echo_id]["assets"]["text"]["name"] == str(echo_id)


def test_a_transient_failure_on_the_second_update_is_retried_once(monkeypatch):
    monkeypatch.setattr(echo_builder, "SECOND_UPDATE_RETRY_DELAY", 0)
    client = FakeKvasirClient()
    real_update = client.component_update
    attempts: list[int] = []

    def flaky(record):
        if record.get("id"):
            attempts.append(1)
            if len(attempts) == 1:
                from src.kvasir_client import KvasirError

                raise KvasirError("kv2_course/component_update: HTTP 404: "
                                  "Course not found or you are not the author")
        return real_update(record)

    client.component_update = flaky
    built = _create(client)

    assert len(attempts) == 2, "the update is idempotent — one retry"
    assert client.components[built.echo_id]["assets"]["text"]["name"] == str(built.echo_id)


def test_a_persistent_failure_still_raises_after_the_retry(monkeypatch):
    monkeypatch.setattr(echo_builder, "SECOND_UPDATE_RETRY_DELAY", 0)
    client = FakeKvasirClient()
    original_copy = client.copy_object

    def copy_and_lose_the_course(source_key, destination_key):
        original_copy(source_key, destination_key)
        client.courses.pop(500, None)

    client.copy_object = copy_and_lose_the_course

    with pytest.raises(EchoBuildError, match="press Retry"):
        _create(client, course_id=500)
