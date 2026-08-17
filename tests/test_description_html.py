"""The echo description must carry exactly one link — the source link we add."""

from __future__ import annotations

import pytest

from src.quiz_designer import build_description_html, build_news_summary_block, count_anchors

URL = "https://www.timesofisrael.com/news/story-1"


def test_english_description_has_one_anchor_labelled_source():
    html = build_description_html("The council votes this week.", URL, "en")
    assert count_anchors(html) == 1
    assert f'href="{URL}"' in html
    assert ">source</a>" in html
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html


def test_russian_description_uses_the_russian_label():
    html = build_description_html("Совет проголосует на этой неделе.", URL, "ru")
    assert count_anchors(html) == 1
    assert ">источник</a>" in html


def test_model_text_cannot_inject_a_second_anchor():
    """Escaping is what guarantees the 'exactly one link' rule."""
    malicious = 'Vote now <a href="https://evil.example.com">click here</a> please'
    html = build_description_html(malicious, URL, "en")

    assert count_anchors(html) == 1
    assert "evil.example.com" not in html.split("<a ")[1], "the injected href must not become a link"
    assert "&lt;a href=" in html, "the model's markup is rendered as visible text"


def test_script_and_quotes_in_model_text_are_escaped():
    html = build_description_html('</p><script>alert("x")</script>', URL, "en")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_non_http_source_urls_are_rejected():
    for bad in ("javascript:alert(1)", "ftp://example.com/x", "", "not-a-url"):
        with pytest.raises(ValueError):
            build_description_html("text", bad, "en")


def test_url_with_quotes_is_attribute_escaped():
    html = build_description_html("text", 'https://example.com/a"onmouseover="alert(1)', "en")
    assert count_anchors(html) == 1
    assert 'onmouseover="alert(1)"' not in html


def test_whitespace_is_collapsed():
    html = build_description_html("  many\n\n  spaces  ", URL, "en")
    assert html.startswith("many spaces <a ")


def test_news_summary_block_uses_localized_labels():
    english = build_news_summary_block("A summary.", "Should it happen?", "en")
    assert english.startswith("News:\n")
    assert "Proposed yes/no question:" in english

    russian = build_news_summary_block("Сводка.", "Стоит ли?", "ru")
    assert russian.startswith("Новость:\n")
    assert "Предлагаемый вопрос да/нет:" in russian


def test_news_summary_block_omits_the_article_url():
    """The source link belongs in the description, not in the chat prompt."""
    block = build_news_summary_block("A summary mentioning nothing.", "Should it?", "en")
    assert "http" not in block
