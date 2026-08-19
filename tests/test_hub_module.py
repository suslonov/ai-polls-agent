"""The ai-home-hub module: routing, slot validation, and the 409 on a locked day."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import db
from src.hub_module import PollsModule
from src.pipeline import local_day
from tests.conftest import REPO_ROOT, make_item, seed_candidate


def make_collected_item(index: int = 0):
    """A collected story that the model did not shortlist."""
    return make_item(
        language="ru",
        source_id="cursor_ru",
        url=f"https://cursorinfo.co.il/story-{index}",
        title=f"Не отобрано моделью {index}",
    )


@pytest.fixture
def module(tmp_path) -> PollsModule:
    return PollsModule(
        prefix="/polls",
        config={
            "settings_yaml": "config/settings.yaml",
            "sources_yaml": "config/sources.yaml",
            "db_path": str(tmp_path / "state.db"),
            "output_html": str(tmp_path / "rendered" / "index.html"),
        },
        repo_path=REPO_ROOT,
    )


@pytest.fixture
def today(module) -> str:
    return local_day(module.settings.app.timezone)


def call(module, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    status, content_type, response = module.handle(method, path, body, {})
    data = None
    if "json" in content_type:
        data = json.loads(response)
    return status, content_type, data, response


def test_constructor_matches_the_hub_loader_signature(module):
    assert module.prefix == "/polls"
    assert module.name and module.description
    assert callable(module.handle)


def test_unknown_route_is_404(module):
    status, _, _, _ = call(module, "GET", "/nope")
    assert status == 404


def test_index_renders_html_with_the_mounted_api_base(module):
    status, content_type, _, body = call(module, "GET", "/")
    assert status == 200
    assert "text/html" in content_type
    assert b'const API_BASE = "/polls";' in body, "fetch() must target the hub mount point"


def test_trailing_slash_and_query_string_route_the_same(module):
    assert call(module, "GET", "/api/day/current")[0] == 200
    assert call(module, "GET", "/api/day/current/")[0] == 200
    assert call(module, "GET", "/api/day/current?x=1")[0] == 200


def test_current_day_returns_candidates_and_workflow(module, today):
    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")

    status, _, data, _ = call(module, "GET", "/api/day/current")

    assert status == 200 and data["ok"] is True
    assert data["day"] == today
    assert [c["id"] for c in data["candidates"]] == [item_id]
    assert data["workflow"]["selection_locked"] is False
    assert data["workflow"]["can_start"] is False, "nothing selected yet"


def test_select_requires_a_valid_slot_and_tone(module):
    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")

    status, _, data, _ = call(module, "POST", "/api/select", {"slot": "de", "item_id": item_id})
    assert status == 400 and "slot" in data["error"]

    status, _, data, _ = call(module, "POST", "/api/select",
                              {"slot": "ru", "item_id": item_id, "tone": "sarcastic"})
    assert status == 400 and "tone" in data["error"]

    status, _, data, _ = call(module, "POST", "/api/select",
                              {"slot": "ru", "item_id": 999999, "tone": "funny"})
    assert status == 400 and "unknown item" in data["error"]


def test_slot_eligibility_is_enforced_by_the_api(module):
    russian = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    english = seed_candidate(module.db_path, language="en", source_id="toi_en",
                             url="https://timesofisrael.com/b", title="An English story today")
    hebrew_raw = seed_candidate(module.db_path, language="he", source_id="ynet_he",
                                url="https://ynet.co.il/c", title="כותרת בעברית ארוכה מספיק")
    hebrew_ready = seed_candidate(module.db_path, language="he", source_id="ynet_he",
                                  url="https://ynet.co.il/d", title="עוד כותרת בעברית ארוכה",
                                  title_en="Another Hebrew headline", short_en="One sentence.")

    ok = {"tone": "funny"}
    assert call(module, "POST", "/api/select", {"slot": "ru", "item_id": russian, **ok})[0] == 200
    assert call(module, "POST", "/api/select", {"slot": "en", "item_id": english, **ok})[0] == 200
    # Hebrew feeds both slots; only the EN slot needs the English rendering.
    assert call(module, "POST", "/api/select", {"slot": "en", "item_id": hebrew_ready, **ok})[0] == 200
    assert call(module, "POST", "/api/select", {"slot": "ru", "item_id": hebrew_ready, **ok})[0] == 200
    assert call(module, "POST", "/api/select", {"slot": "ru", "item_id": hebrew_raw, **ok})[0] == 200

    # Russian may fill the English slot too; it is translated when Start is pressed.
    assert call(module, "POST", "/api/select", {"slot": "en", "item_id": russian, **ok})[0] == 200

    # Rejections
    assert call(module, "POST", "/api/select", {"slot": "ru", "item_id": english, **ok})[0] == 400

    status, _, data, _ = call(module, "POST", "/api/select",
                              {"slot": "en", "item_id": hebrew_raw, **ok})
    assert status == 400 and "not eligible" in data["error"]


def test_current_day_marks_hebrew_as_eligible_for_both_slots(module):
    seed_candidate(module.db_path, language="he", source_id="ynet_he",
                   url="https://ynet.co.il/c", title="כותרת בעברית ארוכה מספיק",
                   title_en="A Hebrew headline", short_en="One sentence.")

    _, _, data, _ = call(module, "GET", "/api/day/current")
    candidate = data["candidates"][0]
    assert candidate["eligible_ru"] is True and candidate["eligible_en"] is True


def test_clearing_a_slot_needs_no_tone(module):
    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": item_id, "tone": "funny"})

    status, _, data, _ = call(module, "POST", "/api/select",
                              {"slot": "ru", "item_id": None, "tone": None})
    assert status == 200
    assert data["workflow"]["ru_item_id"] is None


def test_select_returns_409_once_that_language_is_locked(module, today):
    ru_item = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    en_item = seed_candidate(module.db_path, language="en", source_id="toi_en",
                             url="https://timesofisrael.com/b", title="An English story today")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": ru_item, "tone": "important"})
    db.lock_selection(module.db_path, today, "ru")

    status, _, data, _ = call(module, "POST", "/api/select",
                              {"slot": "ru", "item_id": ru_item, "tone": "funny"})
    assert status == 409 and data["ok"] is False

    # The other language keeps running on its own path.
    assert call(module, "POST", "/api/select",
                {"slot": "en", "item_id": en_item, "tone": "funny"})[0] == 200


def test_start_generation_returns_409_when_nothing_is_selected(module):
    status, _, data, _ = call(module, "POST", "/api/start-generation", {"language": "ru"})
    assert status == 409
    assert "nothing selected" in data["error"]


def test_can_start_becomes_true_once_a_slot_has_a_tone(module):
    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": item_id, "tone": "funny"})

    _, _, data, _ = call(module, "GET", "/api/day/current")
    assert data["workflow"]["can_start"] is True


def test_finalize_and_retry_validate_the_language(module):
    for route in ("/api/finalize", "/api/retry-generation", "/api/retry-telegram"):
        status, _, data, _ = call(module, "POST", route, {"language": "de"})
        assert status == 400 and "language" in data["error"]


def test_history_is_read_only_json(module, today):
    db.ensure_day(module.db_path, "2026-01-01")
    db.upsert_echo(module.db_path, "2026-01-01", "ru", {"title": "Вчерашний опрос"})

    status, _, data, _ = call(module, "GET", "/api/history")
    assert status == 200 and data["ok"] is True
    assert [entry["day"] for entry in data["history"]] == ["2026-01-01"]
    assert data["history"][0]["languages"][0]["echo_title"] == "Вчерашний опрос"


def test_page_javascript_survives_a_selected_tone(module):
    """Regression: HTML-escaping a tone into the <script> broke every button.

    `ru: &#34;funny&#34;` is a syntax error, so the whole script block failed to
    parse and no onclick handler existed any more.
    """
    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": item_id, "tone": "funny"})

    _, _, _, body = call(module, "GET", "/")
    html = body.decode()
    script = html.split("<script>")[-1]

    assert "&#34;" not in script and "&#39;" not in script, "escaped quotes break the script block"
    assert 'ru: "funny"' in script
    assert 'const API_BASE = "/polls";' in script


def test_collected_page_lists_everything_with_add_buttons(module):
    from src import db

    shortlisted = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                                 url="https://newsru.co.il/a", title="Уже в шортлисте")
    other = db.upsert_news_item(module.db_path, make_collected_item())

    status, content_type, _, body = call(module, "GET", "/collected")
    html = body.decode()

    assert status == 200 and "text/html" in content_type
    assert "Не отобрано моделью" in html, "a non-shortlisted story is listed"
    assert "Уже в шортлисте" in html
    assert f'onclick="addCandidate({other})"' in html, "it can be promoted"
    assert f'onclick="addCandidate({shortlisted})"' not in html
    assert ">on shortlist</button>" in html


def test_add_candidate_promotes_a_collected_story(module):
    from src import db

    item_id = db.upsert_news_item(module.db_path, make_collected_item())
    assert db.get_final_candidates(module.db_path) == []

    status, _, data, _ = call(module, "POST", "/api/add-candidate", {"item_id": item_id})

    assert status == 200 and data["added"] is True
    candidates = db.get_final_candidates(module.db_path)
    assert [row["id"] for row in candidates] == [item_id]
    assert candidates[0]["why_candidate"] in ("added by the operator", "добавлено оператором")

    # It is now selectable on the main page.
    _, _, current, _ = call(module, "GET", "/api/day/current")
    assert [c["id"] for c in current["candidates"]] == [item_id]


def test_add_candidate_is_idempotent_and_validates(module):
    from src import db

    item_id = db.upsert_news_item(module.db_path, make_collected_item())
    call(module, "POST", "/api/add-candidate", {"item_id": item_id})

    status, _, data, _ = call(module, "POST", "/api/add-candidate", {"item_id": item_id})
    assert status == 200 and data["added"] is False, "already a candidate — no duplicate"

    assert call(module, "POST", "/api/add-candidate", {"item_id": "abc"})[0] == 400
    status, _, data, _ = call(module, "POST", "/api/add-candidate", {"item_id": 999999})
    assert status == 400 and "unknown item" in data["error"]


def test_collected_pagination_uses_path_segments(module):
    """The hub router drops query strings, so pages live in the path."""
    from src import db

    for index in range(3):
        db.upsert_news_item(module.db_path, make_collected_item(index))

    status, _, _, body = call(module, "GET", "/collected/p/2")
    assert status == 200
    assert b"page 2 of" in body


def test_reset_generation_unlocks_the_language_and_clears_its_echo(module, today):
    from src import db

    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": item_id, "tone": "funny"})
    db.lock_selection(module.db_path, today, "ru")
    db.upsert_echo(module.db_path, today, "ru", {"kvasir_echo_id": 4242, "status": "error"})

    status, _, data, _ = call(module, "POST", "/api/reset-generation", {"language": "ru"})

    assert status == 200 and data["ok"] is True
    assert data["orphaned_echoes"] == [{"language": "ru", "echo_id": 4242}], (
        "the operator must be told which Kvasir drafts are left behind"
    )

    workflow = db.get_day(module.db_path, today)
    assert workflow["status"] == "ready"
    assert workflow["ru_locked_at"] is None
    assert workflow["ru_item_id"] == item_id, "the chosen story is kept, just editable again"
    assert db.get_echoes_for_day(module.db_path, today) == []

    # Selection is editable again, and Start is available.
    assert call(module, "POST", "/api/select",
                {"slot": "ru", "item_id": item_id, "tone": "important"})[0] == 200
    _, _, current, _ = call(module, "GET", "/api/day/current")
    assert current["workflow"]["can_start"] is True


def test_reset_clears_a_published_language_too(module, today):
    """This is an operator tool: Reset never refuses and never explains itself."""
    from src import db

    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": item_id, "tone": "funny"})
    db.lock_selection(module.db_path, today, "ru")
    db.upsert_echo(module.db_path, today, "ru", {"kvasir_echo_id": 4242, "status": "published"})
    db.upsert_publish_event(
        module.db_path,
        db.idempotency_key(today, "ru", 4242, "q1"),
        {"day": today, "target_language": "ru", "kvasir_echo_id": 4242,
         "scroll_id": "q1", "page_published_at": "2026-08-16T10:00:00+00:00"},
    )

    status, _, data, _ = call(module, "POST", "/api/reset-generation", {"language": "ru"})

    assert status == 200 and data["ok"] is True
    assert data["orphaned_echoes"] == [{"language": "ru", "echo_id": 4242}]
    assert data["dropped_publish_events"] == [
        {"language": "ru", "echo_id": 4242, "scroll_id": "q1"}
    ]
    assert db.get_echoes_for_day(module.db_path, today) == []
    assert db.get_day(module.db_path, today)["ru_locked_at"] is None
    assert db.get_publish_event_for(module.db_path, today, "ru") is None

    # And the slot is immediately usable again.
    assert call(module, "POST", "/api/select",
                {"slot": "ru", "item_id": item_id, "tone": "important"})[0] == 200


def test_reset_on_a_day_with_nothing_to_reset_is_not_an_error(module, today):
    status, _, data, _ = call(module, "POST", "/api/reset-generation", {"language": "en"})
    assert status == 200 and data["ok"] is True
    assert data["orphaned_echoes"] == [] and data["dropped_publish_events"] == []


def test_start_generation_has_no_confirm_dialog(module):
    """Item 1 of problems.md: the confirm() on Start was removed."""
    _, _, _, body = call(module, "GET", "/")
    html = body.decode()
    start_fn = html.split("function startGeneration(")[1].split("function ")[0]
    assert "confirm(" not in start_fn
    # Reset and Close are local state changes — no dialog either.
    assert "confirm(" not in html.split("function resetGeneration(")[1].split("function ")[0]
    assert "confirm(" not in html.split("function closeLanguage(")[1].split("function ")[0]
    # Finalize keeps its dialog: it is the only button that publishes outward.
    assert "confirm(" in html.split("function finalize(")[1].split("function ")[0]


def test_actions_reload_the_page_and_carry_the_message_across(module):
    """Item 2: the page must reload after success *and* error."""
    _, _, _, body = call(module, "GET", "/")
    script = body.decode().split("<script>")[-1]

    assert "sessionStorage.setItem(\"pollsFlash\", message)" in script
    assert "sessionStorage.getItem(\"pollsFlash\")" in script
    post_fn = script.split("async function post(")[1].split("function select(")[0]
    assert post_fn.count("reloadWith(") >= 4, "success, api error, per-language error, exception"
    assert "flash(" not in post_fn, "errors must survive the reload, not flash on a dying page"


def test_re_render_writes_the_configured_output_file(module):
    status, _, data, _ = call(module, "POST", "/api/re-render")
    assert status == 200 and data["ok"] is True
    assert Path(data["rendered"]).exists()


# ── "use default categories, don't invent" ────────────────────────────────────


def test_default_categories_endpoint_toggles_one_slot(module, today):
    from src import db

    status, _, data, _ = call(module, "POST", "/api/default-categories",
                              {"slot": "ru", "enabled": True})
    assert status == 200 and data["ok"] is True
    assert db.get_day(module.db_path, today)["ru_default_categories"] == 1
    assert db.get_day(module.db_path, today)["en_default_categories"] == 0

    call(module, "POST", "/api/default-categories", {"slot": "ru", "enabled": False})
    assert db.get_day(module.db_path, today)["ru_default_categories"] == 0


def test_default_categories_endpoint_validates_the_slot(module):
    status, _, data, _ = call(module, "POST", "/api/default-categories", {"slot": "de"})
    assert status == 400 and "slot" in data["error"]


def test_default_categories_is_409_once_that_language_locks(module, today):
    from src import db

    item_id = seed_candidate(module.db_path, language="ru", source_id="newsru_ru",
                             url="https://newsru.co.il/a", title="Русская новость дня")
    call(module, "POST", "/api/select", {"slot": "ru", "item_id": item_id, "tone": "funny"})
    db.lock_selection(module.db_path, today, "ru")

    status, _, data, _ = call(module, "POST", "/api/default-categories",
                              {"slot": "ru", "enabled": True})
    assert status == 409 and "RU" in data["error"]

    # The English slot is unaffected.
    assert call(module, "POST", "/api/default-categories",
                {"slot": "en", "enabled": True})[0] == 200


def test_the_flag_is_visible_in_the_state_and_on_the_page(module, today):
    call(module, "POST", "/api/default-categories", {"slot": "en", "enabled": True})

    _, _, current, _ = call(module, "GET", "/api/day/current")
    assert current["workflow"]["slots"]["en"]["default_categories"] is True
    assert current["workflow"]["slots"]["ru"]["default_categories"] is False

    _, _, _, body = call(module, "GET", "/")
    html = body.decode()
    assert "setDefaultCategories(" in html
    assert html.count("use default categories") == 2, "one checkbox per slot card"


def test_the_panel_offers_an_editable_category_box(module, today, monkeypatch):
    from src import db

    db.upsert_echo(module.db_path, today, "ru", {
        "kvasir_echo_id": 4242,
        "prompt_s3_key": "501/text/4242.ru.txt",
        "categories_json": '["Вы резервист", "Вы студент"]',
    })

    _, _, _, page = call(module, "GET", "/")
    html = page.decode()

    assert 'id="cats-ru"' in html
    assert "saveCategories(" in html
    assert "Вы резервист\nВы студент" in html, "one per line, ready to edit"


def test_edit_categories_endpoint_validates_its_input(module):
    status, _, data, _ = call(module, "POST", "/api/categories",
                              {"language": "de", "categories": ["x"]})
    assert status == 400 and "language" in data["error"]

    status, _, data, _ = call(module, "POST", "/api/categories",
                              {"language": "ru", "categories": "not a list"})
    assert status == 400 and "list" in data["error"]

    status, _, data, _ = call(module, "POST", "/api/categories",
                              {"language": "ru", "categories": ["Вы пассажир"]})
    assert status == 400 and "no RU echo" in data["error"], "nothing generated yet"
