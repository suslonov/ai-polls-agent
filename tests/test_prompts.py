"""The model instructions this repo sends live in config/prompts/*.txt."""

from __future__ import annotations

import pytest

from src import prompts, quiz_designer, selector
from src.prefilter import PROMPT_NAME as PREFILTER_PROMPT
from src.prefilter import TRANSLATE_PROMPT_NAME

ALL_PROMPTS = {
    PREFILTER_PROMPT: set(),
    TRANSLATE_PROMPT_NAME: set(),
    selector.PROMPT_NAME: {"min_items", "max_items"},
    quiz_designer.PROMPT_NAME: {"language_name", "persona"},
}


@pytest.mark.parametrize("name", sorted(ALL_PROMPTS))
def test_every_prompt_file_exists_and_is_not_empty(name):
    text = prompts.load(name)
    assert len(text) > 200, f"config/prompts/{name}.txt looks truncated"


@pytest.mark.parametrize("name,expected", sorted(ALL_PROMPTS.items()))
def test_placeholders_are_exactly_what_the_code_fills(name, expected):
    """A renamed placeholder must fail here, not silently reach a model."""
    assert prompts.placeholders(name) == expected


def test_render_substitutes_and_leaves_json_examples_alone():
    rendered = prompts.render("selector", min_items=10, max_items=20)
    assert "between 10 and 20 stories" in rendered
    assert "{{" not in rendered
    # JSON examples use single braces and must survive untouched.
    assert '{"selected": [{"id": 0, "rank": 1' in rendered


def test_render_refuses_a_half_filled_prompt():
    with pytest.raises(prompts.PromptError, match="unfilled placeholder"):
        prompts.render("quiz_designer", language_name="Russian")


def test_missing_prompt_file_is_a_clear_error():
    with pytest.raises(prompts.PromptError, match="not found"):
        prompts.load("no_such_prompt")


def test_quiz_designer_prompt_carries_the_persona_and_language():
    rendered = prompts.render(
        "quiz_designer", language_name="Russian", persona="Ты редактор опроса."
    )
    assert rendered.count("Russian") >= 4
    assert rendered.rstrip().endswith("Ты редактор опроса.")


def test_prefilter_prompt_states_the_scoring_contract():
    text = prompts.load("prefilter")
    for token in ("israel_relevance", "interesting_score", "funny_score", "story_group_hint"):
        assert token in text


def test_prompts_are_editable_config_not_code():
    """Editing a prompt must not require touching Python."""
    for name in ALL_PROMPTS:
        path = prompts.prompt_path(name)
        assert path.suffix == ".txt"
        assert path.parent.name == "prompts" and path.parent.parent.name == "config"
