"""Thin wrappers around the two model providers.

Claude (Anthropic) does the judgement work: final story selection and quiz
design. Gemini is the cheap layer: batch prefiltering and Hebrew→English
translation. Both SDKs are imported lazily so the rest of the pipeline — and
the test suite — runs without them installed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    """Raised when a model call fails after retries or returns unusable output."""


def strip_code_fences(text: str) -> str:
    """Remove ```json fences some models wrap JSON in."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped).strip()
    return stripped


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output.

    Tolerates code fences and leading/trailing prose by falling back to the
    outermost ``{...}`` span.
    """
    candidate = strip_code_fences(text)
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"model did not return JSON: {candidate[:200]!r}")
        try:
            value = json.loads(candidate[start : end + 1])
        except ValueError as exc:
            raise LLMError(f"model returned malformed JSON: {exc}") from exc

    if not isinstance(value, dict):
        raise LLMError(f"expected a JSON object, got {type(value).__name__}")
    return value


# ── Anthropic ─────────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20), reraise=True)
def _anthropic_create(client, **kwargs):
    return client.messages.create(**kwargs)


def claude_json(
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Call Claude and parse a single JSON object out of the reply."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - environment problem
        raise LLMError("anthropic package is not installed (pip install anthropic)") from exc

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = _anthropic_create(
            client,
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as LLMError
        raise LLMError(f"Claude call failed: {exc}") from exc

    usage = getattr(message, "usage", None)
    logger.info(
        "Claude %s — in=%s out=%s stop=%s",
        model,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
        getattr(message, "stop_reason", "?"),
    )
    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning("Claude response hit max_tokens (%s); output may be truncated", max_tokens)

    texts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    if not texts:
        raise LLMError("Claude returned no text content")
    return parse_json_object(texts[0])


# ── Gemini ────────────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20), reraise=True)
def _gemini_generate(client, **kwargs):
    return client.models.generate_content(**kwargs)


def gemini_json(
    api_key: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    system: Optional[str] = None,
) -> dict[str, Any]:
    """Call Gemini in JSON mode and parse the reply."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - environment problem
        raise LLMError("google-genai package is not installed (pip install google-genai)") from exc

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        max_output_tokens=max_output_tokens,
        system_instruction=system or None,
    )

    try:
        response = _gemini_generate(client, model=model, contents=prompt, config=config)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as LLMError
        raise LLMError(f"Gemini call failed: {exc}") from exc

    text = getattr(response, "text", None)
    if not text:
        raise LLMError("Gemini returned no text")
    return parse_json_object(text)
