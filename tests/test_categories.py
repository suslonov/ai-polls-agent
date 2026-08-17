"""Participant categories: validation, mode differences, parties, fallbacks."""

from __future__ import annotations

import pytest

from src import category_designer as cd
from src.category_designer import (
    CategoryResult,
    build_fallback,
    detect_domain,
    generate_categories,
    looks_generic,
    looks_political,
    party_categories,
    serialize_for_template,
    validate_categories,
)

POLITICAL_QUESTION = (
    "Should reserve-duty compensation increase automatically when service "
    "exceeds a defined number of days?"
)
PARKING_QUESTION = "Will the new digital parking system make parking less annoying?"


def fake_model(categories, party_used=False):
    """Stand in for the Claude call with a fixed category list."""
    def _call(**kwargs):
        return {"categories": categories, "party_categories_used": party_used}
    return _call


# ── Validation ────────────────────────────────────────────────────────────────


def test_taxonomy_labels_are_rejected_and_identities_kept():
    """§20: the whole point of the amendment."""
    outcome = validate_categories(
        ["Politics", "Economy", "You are a reservist", "You run a small business"]
    )
    assert outcome.kept == ["You are a reservist", "You run a small business"]
    assert {r.reason for r in outcome.rejected} == {"generic taxonomy label"}


def test_generic_detection_covers_both_languages_and_short_phrases():
    for generic in ("Politics", "экономика", "Israel", "Current events", "Drivers",
                    "Israeli society", "Общество"):
        assert looks_generic(generic), generic
    for identity in ("You are a reservist", "Вы резервист",
                     "You are the person who reads every parking sign twice"):
        assert not looks_generic(identity), identity


def test_funny_situational_categories_are_accepted():
    funny = [
        "You circle the block three times before giving up",
        "You open three parking apps before leaving the car",
        "Вы уже переслали это в семейный чат",
    ]
    assert validate_categories(funny).kept == funny


def test_exact_and_near_duplicates_are_removed():
    outcome = validate_categories(
        [
            "You are a renter",
            "You are a renter",          # exact duplicate
            "you are a RENTER.",         # same after normalisation
            "You are renters",           # same stem, different wording
            "You own an apartment",
        ]
    )
    assert outcome.kept == ["You are a renter", "You own an apartment"]
    assert {r.reason for r in outcome.rejected} == {"duplicate", "near-duplicate"}


def test_dedupe_is_lexical_and_does_not_merge_different_roles():
    """Over-merging would delete a real constituency, so it is not attempted.

    "You are a reservist" / "You employ reservists" is lexically identical in
    shape to "You are a renter" / "You rent your home" — one pair must survive,
    so neither is merged. Avoiding semantic near-duplicates is the prompt's job.
    """
    outcome = validate_categories(["You are a reservist", "You employ reservists"])
    assert outcome.kept == ["You are a reservist", "You employ reservists"]


def test_malformed_entries_are_rejected():
    outcome = validate_categories(
        [
            "",
            "   ",
            "You are a reservist. You also employ reservists. And you vote.",
            "See https://example.com/story",
            '{"categories": ["x"]}',
            "You are a person " + "who cares deeply about this issue " * 4,
            42,
        ]
    )
    assert outcome.kept == []
    assert len(outcome.rejected) == 7


def test_first_phrasing_wins_and_order_is_preserved():
    outcome = validate_categories(["You rent your home", "You rent a home", "You are a teacher"])
    assert outcome.kept == ["You rent your home", "You are a teacher"]


# ── Political relevance ───────────────────────────────────────────────────────


def test_political_detection(settings):
    assert looks_political(settings, "en", "Government proposes reserve-duty changes", "", "")
    assert looks_political(settings, "ru", "", "Кнессет обсуждает законопроект", "")
    assert not looks_political(
        settings, "en", "A supermarket recalls a yoghurt brand", "", "Should you switch brands?"
    )


def test_party_categories_are_rendered_as_identities(settings):
    formatted = party_categories(settings, "en", ["Likud", "Shas"])
    assert formatted[0] == "You are a Likud voter"
    assert formatted[-1] == "You are an undecided voter"

    russian = party_categories(settings, "ru", ["Ликуд"])
    assert russian[0] == "Вы избиратель партии «Ликуд»"
    assert russian[-1] == "Вы ещё не определились, за кого голосовать"


def test_template_defaults_win_over_configured_parties(settings):
    formatted = party_categories(settings, "en", ["Yashar", "Noam"])
    assert "You are a Yashar voter" in formatted
    assert not any("Likud" in entry for entry in formatted)


def test_party_list_is_capped(settings):
    many = [f"Party {i}" for i in range(20)]
    formatted = party_categories(settings, "en", many)
    assert len(formatted) == settings.parties.max_in_poll + 1  # + undecided


# ── Domain + fallbacks ────────────────────────────────────────────────────────


def test_domain_detection():
    assert detect_domain("New digital parking system", "", PARKING_QUESTION) == "transport"
    assert detect_domain("Rent prices climb again", "", "") == "housing"
    assert detect_domain("Reserve duty compensation", "", "") == "security"
    assert detect_domain("A yoghurt recall", "supermarket prices", "") == "consumer"
    assert detect_domain("Something else entirely", "", "") == "general"


def test_political_fallback_uses_parties_for_important(settings):
    categories, party_used = build_fallback(
        settings, "important", "en", "security", is_political=True,
        template_defaults=["Likud", "Shas"],
    )
    assert party_used is True
    assert "You are a Likud voter" in categories
    assert "You are an undecided voter" in categories
    assert "You are a reservist" in categories, "stakeholders are added too"
    assert len(categories) <= settings.category_generation.cap


def test_non_political_fallback_has_no_parties(settings):
    categories, party_used = build_fallback(
        settings, "important", "en", "consumer", is_political=False,
        template_defaults=["Likud", "Shas"],
    )
    assert party_used is False
    assert not any("voter" in entry for entry in categories)
    assert "You shop for your household" in categories


def test_funny_fallback_never_uses_parties(settings):
    categories, party_used = build_fallback(
        settings, "funny", "ru", "transport", is_political=True,
        template_defaults=["Ликуд"],
    )
    assert party_used is False
    assert "Вы уже переслали это в семейный чат" in categories


# ── Generation ────────────────────────────────────────────────────────────────


def test_generation_keeps_valid_categories_and_drops_taxonomy(settings, monkeypatch):
    monkeypatch.setattr(cd, "claude_json", fake_model([
        "You are a reservist",
        "Politics",                       # taxonomy → rejected
        "You employ reservists",
        "You are a reservist",            # duplicate → rejected
        "You are self-employed",
        "You are a parent of young children",
    ]))
    result = generate_categories(
        settings=settings, language="en", mode="important",
        news_title="Government proposes changes to reserve-duty compensation",
        news_summary="The cabinet discussed automatic compensation.",
        proposed_question=POLITICAL_QUESTION,
        api_key="k", model="m", party_defaults=["Likud"],
    )

    assert result.categories == [
        "You are a reservist", "You employ reservists",
        "You are self-employed", "You are a parent of young children",
    ]
    assert result.rejected_count == 2
    assert result.fallback_used is False


def test_generation_falls_back_when_everything_is_rejected(settings, monkeypatch):
    monkeypatch.setattr(cd, "claude_json", fake_model(["Politics", "Economy", "Israel"]))
    result = generate_categories(
        settings=settings, language="en", mode="important",
        news_title="Knesset debates conscription law",
        news_summary="The coalition discussed the draft bill.",
        proposed_question="Should the conscription law change?",
        api_key="k", model="m", party_defaults=["Likud", "Shas"],
    )

    assert result.fallback_used is True
    assert len(result.categories) >= settings.category_generation.min_count
    assert result.party_categories_used is True
    assert any("voter" in entry for entry in result.categories)


def test_generation_survives_a_model_failure(settings, monkeypatch):
    from src.llm import LLMError

    def boom(**kwargs):
        raise LLMError("quota exceeded")

    monkeypatch.setattr(cd, "claude_json", boom)
    result = generate_categories(
        settings=settings, language="ru", mode="funny",
        news_title="Новая система парковки", news_summary="Муниципалитет запустил приложение.",
        proposed_question="Станет ли парковка менее раздражающей?",
        api_key="k", model="m",
    )

    assert result.fallback_used is True
    assert result.categories, "a failed call must still yield usable categories"
    assert not any("избиратель" in entry for entry in result.categories), "funny stays apolitical"


def test_non_political_story_is_never_given_parties(settings, monkeypatch):
    monkeypatch.setattr(cd, "claude_json", fake_model(
        ["You are a Likud voter", "You shop for your household",
         "You compare prices before buying", "You run a small business",
         "You are raising a family"],
        party_used=True,
    ))
    result = generate_categories(
        settings=settings, language="en", mode="funny",
        news_title="Supermarket chain changes its yoghurt packaging",
        news_summary="Shoppers noticed smaller cups at the same price.",
        proposed_question="Is the new packaging a rip-off?",
        api_key="k", model="m", party_defaults=["Likud"],
    )
    assert result.party_categories_used is False, "the story is not political"


def test_categories_are_capped(settings, monkeypatch):
    monkeypatch.setattr(cd, "claude_json", fake_model(
        [f"You are person number {i} in this story" for i in range(20)]
    ))
    result = generate_categories(
        settings=settings, language="en", mode="funny",
        news_title="t", news_summary="s", proposed_question="q",
        api_key="k", model="m",
    )
    assert len(result.categories) <= settings.category_generation.cap


@pytest.mark.parametrize("language,expected", [("en", "English"), ("ru", "Russian")])
def test_prompt_asks_for_the_target_language(settings, monkeypatch, language, expected):
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return {"categories": ["You are a reservist"] * 1}

    monkeypatch.setattr(cd, "claude_json", capture)
    generate_categories(
        settings=settings, language=language, mode="important",
        news_title="t", news_summary="s", proposed_question="q",
        api_key="k", model="m",
    )
    assert f"in {expected}" in seen["system"]


def test_rationale_is_never_sent_into_the_prompt(settings, monkeypatch):
    monkeypatch.setattr(cd, "claude_json", fake_model(["You are a reservist"]))
    result = generate_categories(
        settings=settings, language="en", mode="important",
        news_title="t", news_summary="s", proposed_question="q",
        api_key="k", model="m",
    )
    assert isinstance(result, CategoryResult)
    # rationale is debug-only; the serialized template value carries categories alone
    assert "rationale" not in serialize_for_template(result.categories)


# ── Serialization ─────────────────────────────────────────────────────────────


def test_serialization_produces_json_array_contents():
    value = serialize_for_template(["You are a reservist", 'You say "no"'])
    assert value == '"You are a reservist", "You say \\"no\\""'

    import json
    assert json.loads(f"[{value}]") == ["You are a reservist", 'You say "no"']


def test_serialization_keeps_cyrillic_readable():
    assert serialize_for_template(["Вы резервист"]) == '"Вы резервист"'
