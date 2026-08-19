"""Create a Kvasir echo from the template echo.

Mirrors what the echo editor does in the browser, in the same order:

1. read the template component,
2. ``component_update`` without an id  → the new echo exists and has an id,
3. copy the template prompt inside S3 and fill it in at the destination key,
4. ``component_update`` with the id    → persist ``assets.text``.

The template's own S3 object is never written to.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from src.kvasir_client import KvasirClient, KvasirError
from src.models import QuizDesign
from src.text_utils import clean, clean_deep, normalize_dashes

logger = logging.getLogger(__name__)

MANDATORY_MARKERS = ("LOCALE", "CATEGORIES", "NEWS_SUMMARY", "PERSONA")
_MARKER_NAME_RE = re.compile(r"\A([A-Z_]+)\s*(.*)\Z", re.DOTALL)
_LEFTOVER_MARKER_RE = re.compile(r"\{\{\s*[A-Z_]+")

LOCALE_NAMES = {"en": "English (en)", "ru": "Russian (ru)"}

# Server-derived or per-instance fields that must never be copied onto a new
# component record.
_READONLY_TOP_LEVEL = {
    "id", "nickname", "course_title", "course_status", "is_creator",
    "components", "languages", "color", "likes", "likes_count", "visits",
    "statistics", "base_text_usages", "additions", "created_at", "updated_at",
    "published_at", "course",
}
# `accounts` is derived by kv2_course from the author's PayPal details, and
# donations_collected is a per-instance statistic — neither is ours to send.
_READONLY_DETAILS = {"contest", "accounts", "donations_collected"}

# Assets copied from the template into the new echo's own course prefix, so the
# clone does not reference objects living under the template's course.
# Key layout (see asset_name() in common.js): {course}/{field}/{name}[.lang].{ext}
_COPIED_ASSET_FIELDS = ("title_picture",)


class EchoBuildError(RuntimeError):
    """Raised when the template is unusable or a build step fails."""


# ── Prompt template ───────────────────────────────────────────────────────────


@dataclass
class Marker:
    """One ``{{NAME}}`` or ``{{NAME DEFAULT=<json>}}`` placeholder in the template."""

    name: str
    start: int
    end: int          # exclusive
    raw: str
    default: Any = None


def _marker_end(text: str, start: int) -> Optional[int]:
    """Index just past the marker that begins at ``start``.

    Brace-aware, because the deployed template embeds a JSON object in the
    marker itself: ``{{CATEGORIES DEFAULT={"ru": [...], "en": [...]}}}``. A plain
    regex would stop at the first ``}}`` inside the payload.
    """
    depth = 0
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def find_markers(text: str) -> list[Marker]:
    """Every placeholder in the template, with its DEFAULT payload parsed."""
    markers: list[Marker] = []
    search_from = 0

    while True:
        start = (text or "").find("{{", search_from)
        if start == -1:
            return markers

        end = _marker_end(text, start)
        if end is None:
            return markers

        body = text[start + 2 : end - 2].strip()
        match = _MARKER_NAME_RE.match(body)
        if not match:
            search_from = start + 2
            continue

        name, remainder = match.group(1), match.group(2).strip()
        default: Any = None
        if remainder.startswith("DEFAULT="):
            payload = remainder[len("DEFAULT=") :].strip()
            try:
                default = json.loads(payload)
            except ValueError:
                # Keep the raw text: a malformed default is the template
                # author's problem, not a reason to refuse to build.
                default = payload
        markers.append(
            Marker(name=name, start=start, end=end, raw=text[start:end], default=default)
        )
        search_from = end


def marker_default(text: str, name: str, language: str) -> list[str]:
    """The DEFAULT list a marker carries for one language, if any.

    The deployed CATEGORIES marker ships the Israeli party list keyed by
    language; that list is the primary source of party categories.
    """
    for marker in find_markers(text):
        if marker.name != name:
            continue
        default = marker.default
        if isinstance(default, dict):
            values = default.get(language) or default.get("en") or []
            return [str(value) for value in values if str(value).strip()]
        if isinstance(default, list):
            return [str(value) for value in default if str(value).strip()]
    return []


def validate_template_prompt(text: str) -> None:
    """Fail loudly when the template prompt is missing a mandatory marker."""
    present = {marker.name for marker in find_markers(text or "")}
    missing = [name for name in MANDATORY_MARKERS if name not in present]
    if missing:
        raise EchoBuildError(
            "Template prompt is missing mandatory marker(s): "
            + ", ".join(f"{{{{{name}}}}}" for name in missing)
        )


def fill_prompt_template(
    template_text: str,
    locale: str,
    categories_value: str,
    news_summary_block: str,
    persona: str,
) -> str:
    """Substitute every mandatory marker and assert none survive.

    ``categories_value`` is already serialized for the template's context — the
    deployed template wraps the marker in ``"categories": [ ... ]``, so it
    expects the array's contents rather than a list or an object.
    """
    validate_template_prompt(template_text)

    values = {
        "LOCALE": LOCALE_NAMES.get(locale, locale),
        "CATEGORIES": categories_value,
        "NEWS_SUMMARY": news_summary_block.strip(),
        "PERSONA": persona.strip(),
    }

    # Replace right-to-left so earlier offsets stay valid.
    filled = template_text
    for marker in sorted(find_markers(template_text), key=lambda m: m.start, reverse=True):
        if marker.name not in values:
            continue
        filled = filled[: marker.start] + values[marker.name] + filled[marker.end :]

    leftover = _LEFTOVER_MARKER_RE.findall(filled)
    if leftover:
        raise EchoBuildError(f"Unsubstituted marker(s) remain in prompt: {sorted(set(leftover))}")

    # House rule: plain hyphens everywhere we publish, the inner prompt included.
    return normalize_dashes(filled)


# ── Component record ──────────────────────────────────────────────────────────


def _clone_details(
    template_details: Any,
    description_html: str,
    greeting: str = "",
    has_author_id: bool = False,
) -> dict:
    details = dict(template_details or {}) if isinstance(template_details, dict) else {}
    for key in _READONLY_DETAILS:
        details.pop(key, None)

    # kv2_course's update path reads component_record["author_id"] unguarded when
    # details.allow_donations is truthy. We normally send author_id, but if we
    # have none the flag has to go off or the update 500s after the echo exists.
    if details.get("allow_donations") and not has_author_id:
        logger.warning("No author_id available — disabling allow_donations on the clone")
        details["allow_donations"] = False

    # Overrides: the description is ours, and the echo must be publicly usable.
    details["description"] = description_html
    details["public"] = True
    if greeting:
        details["greetings"] = greeting
    return clean_deep(details)


def _clone_assets(
    template_assets: Any,
    template_course_id: Any,
    target_course_id: Any,
    template_component_id: Any,
) -> dict:
    """Copy the template's assets, minus the ones handled per-echo.

    ``text`` is added by the second update, and the fields in
    ``_COPIED_ASSET_FIELDS`` are re-pointed at the new echo once its own S3
    objects exist. Anything else that names the template component would dangle
    under a different course, so it is dropped.
    """
    assets = dict(template_assets or {}) if isinstance(template_assets, dict) else {}
    assets.pop("text", None)
    for field in _COPIED_ASSET_FIELDS:
        assets.pop(field, None)

    same_course = str(template_course_id) == str(target_course_id)
    if same_course:
        return assets

    cleaned: dict = {}
    for key, value in assets.items():
        name = str(value.get("name", "")) if isinstance(value, dict) else ""
        if name and name.split(".")[0] == str(template_component_id):
            logger.info(
                "Dropping asset %r from clone: it points at the template's course", key
            )
            continue
        cleaned[key] = value
    return cleaned


def asset_key(course_id: Any, field: str, name: Any, ext: str, language_postfix: str = "") -> str:
    """``{course}/{field}/{name}[.lang].{ext}`` — the layout asset_name() builds."""
    suffix = f".{ext}" if ext else ""
    return f"{course_id}/{field}/{name}{language_postfix}{suffix}"


def copy_template_assets(
    client: KvasirClient,
    template: dict,
    target_course_id: Any,
    echo_id: int,
) -> dict:
    """Duplicate the template's title picture (and friends) for the new echo.

    Returns the asset entries to merge into the component record. A failed copy
    is logged and skipped: a missing picture must not cost us the whole echo.
    """
    copied: dict = {}
    template_assets = template.get("assets") or {}

    for field in _COPIED_ASSET_FIELDS:
        asset = template_assets.get(field)
        if not isinstance(asset, dict) or not asset.get("name"):
            continue

        ext = str(asset.get("ext") or "").strip()
        source = asset_key(template.get("course_id"), field, asset["name"], ext)
        destination = asset_key(target_course_id, field, echo_id, ext)
        if source == destination:
            copied[field] = dict(asset)
            continue

        try:
            client.copy_object(source, destination)
        except KvasirError as exc:
            logger.warning("Could not copy %s for echo %s: %s", field, echo_id, exc)
            continue

        copied[field] = {
            "region": asset.get("region") or client.region,
            "name": str(echo_id),
            "ext": ext,
        }
        logger.info("Copied %s to %s for echo %s", field, destination, echo_id)

    return copied


def build_component_record(
    template: dict,
    target_course_id: Any,
    target_language: str,
    title: str,
    description_html: str,
    status: str = "raw",
    author_id: Any = None,
    greeting: str = "",
) -> dict:
    """Assemble the new echo's component record from the template.

    ``author_id`` is the poll account's author id from .env; kv2_course needs it
    whenever donations are enabled, and it makes the generated echoes belong to
    that author rather than the template's.
    """
    if template.get("type") != "echo":
        raise EchoBuildError(f"Template component is not an echo (type={template.get('type')!r})")

    assets = template.get("assets")
    if not isinstance(assets, dict) or not isinstance(assets.get("text"), dict):
        raise EchoBuildError("Template echo has no assets.text — nothing to clone")
    if not assets["text"].get("name"):
        raise EchoBuildError("Template echo assets.text has no name")

    resolved_author = author_id if author_id not in (None, "") else template.get("author_id")

    record = {
        "course_id": target_course_id,
        "title": clean(title),
        "type": "echo",
        "language": target_language,
        "status": status,
        "total_adviser": template.get("total_adviser", 0) or 0,
        "voice": template.get("voice", "") or "",
        "dependency": template.get("dependency", 0) or 0,
        "assets": _clone_assets(
            assets,
            template.get("course_id"),
            target_course_id,
            template.get("id"),
        ),
        "details": _clone_details(
            template.get("details"),
            description_html,
            greeting=greeting,
            has_author_id=bool(resolved_author),
        ),
    }

    for key in _READONLY_TOP_LEVEL:
        record.pop(key, None)

    if resolved_author:
        record["author_id"] = resolved_author
    return record


_CATEGORIES_KEY_RE = re.compile(r'"categories"\s*:\s*\[')


def _array_end(text: str, start: int) -> Optional[int]:
    """Index of the ``]`` closing the array whose ``[`` is at ``start``.

    String-aware, so a bracket inside a category ("You rent [in Tel Aviv]")
    cannot end the array early.
    """
    depth = 0
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
    return None


def replace_categories_in_prompt(prompt_text: str, categories_value: str) -> str:
    """Rewrite the ``"categories": [...]`` array of an already filled prompt.

    Editing categories after generation cannot go through the marker: it was
    substituted when the echo was built and no longer exists. Replacing just the
    array contents keeps every other edit the operator has made to the prompt in
    the Kvasir editor - a full re-fill from the template would discard them.
    """
    match = _CATEGORIES_KEY_RE.search(prompt_text)
    if not match:
        raise EchoBuildError(
            'This echo\'s prompt has no "categories": [ ... ] array to edit. '
            "It was probably built from a different template."
        )

    open_bracket = match.end() - 1
    close_bracket = _array_end(prompt_text, open_bracket)
    if close_bracket is None:
        raise EchoBuildError('The "categories" array in this prompt is not closed')

    return prompt_text[: open_bracket + 1] + categories_value + prompt_text[close_bracket:]


def load_template(client: KvasirClient, template_echo_id: Any) -> tuple[dict, str, str]:
    """Fetch the template component and its prompt text.

    Callers need the prompt before the echo exists, because the CATEGORIES
    marker carries the party defaults the category generator works from.
    Returns ``(component, prompt_text, prompt_key)``.
    """
    template = client.get_component(template_echo_id)
    key = template_prompt_key(client, template)
    return template, client.get_text(key), key


def template_prompt_key(client: KvasirClient, template: dict) -> str:
    """S3 key of the template's prompt, using the template's *own* language."""
    assets_text = template["assets"]["text"]
    return client.text_key(
        course_id=template["course_id"],
        name=assets_text["name"],
        language=template.get("language", "en"),
        ext=assets_text.get("ext", "txt") or "txt",
    )


# ── Build ─────────────────────────────────────────────────────────────────────


SECOND_UPDATE_RETRY_DELAY = 3.0


def _update_with_one_retry(client: KvasirClient, record: dict) -> dict:
    """Send the second update, retrying once after a short pause.

    The update is idempotent — it rewrites a component we have just created —
    so a single retry costs nothing and covers a kv2_course call that fails
    while the freshly created component is not yet visible to its access check.
    A persistent refusal (deleted component, wrong course) fails the same way,
    three seconds later.
    """
    try:
        return client.component_update(record)
    except KvasirError as exc:
        logger.warning(
            "Second update of echo %s failed (%s); retrying once in %.0fs",
            record.get("id"), exc, SECOND_UPDATE_RETRY_DELAY,
        )
        time.sleep(SECOND_UPDATE_RETRY_DELAY)
        return client.component_update(record)


def check_course_access(client: KvasirClient, course_id: Any, author_id: Any) -> dict:
    """Fail before creating anything if the target course is not ours to write.

    kv2_course only checks the course on the *update* path
    (``check_component_access``); creating a component accepts any
    ``course_id``. Without this preflight a wrong or deleted `KVASIR_COURSE_*`
    produces a component first and an opaque "Course not found or you are not
    the author" 404 second, leaving an orphan draft behind.
    """
    try:
        course = client.get_course(course_id)
    except KvasirError as exc:
        raise EchoBuildError(
            f"Course {course_id} could not be read ({exc}). Check KVASIR_COURSE_EN / "
            f"KVASIR_COURSE_RU in .env — nothing was created."
        ) from exc

    if not course or not course.get("id"):
        raise EchoBuildError(
            f"Course {course_id} does not exist. Check KVASIR_COURSE_EN / "
            f"KVASIR_COURSE_RU in .env — nothing was created."
        )

    course_author = str(course.get("author_id") or "")
    if author_id and course_author and course_author != str(author_id):
        raise EchoBuildError(
            f"Course {course_id} belongs to author {course_author}, but KVASIR_USER is "
            f"{author_id}. kv2_course would create the echo and then refuse to update it. "
            f"Nothing was created."
        )
    return course


class BuiltEcho:
    """Result of a successful (or resumed) echo build."""

    def __init__(
        self,
        echo_id: int,
        editor_url: str,
        prompt_key: str,
        title: str,
        description_html: str,
    ):
        self.echo_id = echo_id
        self.editor_url = editor_url
        self.prompt_key = prompt_key
        self.title = title
        self.description_html = description_html


def create_echo(
    client: KvasirClient,
    template_echo_id: Any,
    target_course_id: Any,
    target_language: str,
    design: QuizDesign,
    description_html: str,
    news_summary_block: str,
    categories_value: str,
    persona: str,
    status: str = "raw",
    existing_echo_id: Optional[int] = None,
    on_created=None,
    template: Optional[dict] = None,
    author_id: Any = None,
) -> BuiltEcho:
    """Create one echo, or finish creating one that a previous run left half-built.

    ``existing_echo_id`` makes the whole function idempotent: when a prior
    attempt created the component but failed on S3, the retry reuses that
    component instead of creating a second one.

    ``on_created(echo_id)`` is called the moment an id exists, so the caller can
    persist it before any further step can fail.

    ``template`` may be supplied by a caller that already fetched it (the
    category step needs the template prompt first), avoiding a second read.
    """
    template = template or client.get_component(template_echo_id)
    source_key = template_prompt_key(client, template)

    # Cheap read that turns a misconfigured course into a clear message instead
    # of an orphan component plus a 404 from the second update.
    check_course_access(client, target_course_id, author_id)

    record = build_component_record(
        template=template,
        target_course_id=target_course_id,
        target_language=target_language,
        title=design.title,
        description_html=description_html,
        status=status,
        author_id=author_id,
        greeting=design.greeting,
    )

    # 1. Create the component (or reuse the one from an interrupted attempt).
    if existing_echo_id:
        echo_id = int(existing_echo_id)
        logger.info("Reusing existing echo %s for %s", echo_id, target_language)
    else:
        created = client.component_update(record)
        raw_id = created.get("component_id")
        if not raw_id:
            raise EchoBuildError(f"component_update returned no component_id: {created}")
        echo_id = int(raw_id)
        logger.info("Created echo %s in course %s", echo_id, target_course_id)

    if on_created:
        on_created(echo_id)

    # 2. Copy the template prompt to the new echo's key, then fill it in there.
    destination_key = client.text_key(
        course_id=target_course_id, name=echo_id, language=target_language, ext="txt"
    )
    if destination_key == source_key:
        raise EchoBuildError("Refusing to overwrite the template prompt object")

    client.copy_object(source_key, destination_key)
    template_text = client.get_text(destination_key)
    filled = fill_prompt_template(
        template_text=template_text,
        locale=target_language,
        categories_value=categories_value,
        news_summary_block=news_summary_block,
        persona=persona,
    )
    client.put_text(destination_key, filled)
    logger.info(
        "Echo %s prompt written: s3://%s/%s (%d chars), cloned from s3://%s/%s",
        echo_id,
        client.config.courses_bucket,
        destination_key,
        len(filled),
        client.config.courses_bucket,
        source_key,
    )

    # 3. Copy the template's title picture into this echo's own course prefix.
    copied_assets = copy_template_assets(client, template, target_course_id, echo_id)

    # 4. Second update: persist assets.text (and the picture) now they exist.
    record["id"] = echo_id
    record.setdefault("assets", {})
    record["assets"]["text"] = {
        "region": client.region,
        "name": str(echo_id),
        "ext": "txt",
    }
    record["assets"].update(copied_assets)
    try:
        _update_with_one_retry(client, record)
    except KvasirError as exc:
        # The component exists by now, so this must not read like "creation
        # failed": the id is already persisted and Retry resumes from here.
        raise EchoBuildError(
            f"Echo {echo_id} was created in course {target_course_id}, but writing its "
            f"assets back was refused ({exc}). The prompt is already at "
            f"{destination_key}; press Retry to finish this echo — a retry reuses "
            f"component {echo_id} instead of creating another one. A 404 here means "
            f"kv2_course no longer sees {echo_id} under a course you author (deleted "
            f"component, or the wrong KVASIR_COURSE_* / KVASIR_USER)."
        ) from exc

    verify_echo(client, echo_id, target_language, design.title)

    return BuiltEcho(
        echo_id=echo_id,
        editor_url=client.editor_url(echo_id),
        prompt_key=destination_key,
        title=design.title,
        description_html=description_html,
    )


def verify_echo(client: KvasirClient, echo_id: int, target_language: str, title: str) -> None:
    """Read the component back and check the fields we just wrote.

    A mismatch is logged rather than raised: the echo exists and the operator
    can inspect it, and failing here would strand a component that was created
    successfully.
    """
    try:
        component = client.get_component(echo_id)
    except KvasirError as exc:
        logger.warning("Could not verify echo %s: %s", echo_id, exc)
        return

    problems = []
    if int(component.get("id", 0)) != int(echo_id):
        problems.append(f"id mismatch ({component.get('id')})")
    if component.get("language") != target_language:
        problems.append(f"language={component.get('language')!r}")
    if component.get("title") != title:
        problems.append(f"title={component.get('title')!r}")

    assets_text = (component.get("assets") or {}).get("text") or {}
    if str(assets_text.get("name", "")) != str(echo_id):
        problems.append(f"assets.text.name={assets_text.get('name')!r}")

    description = ((component.get("details") or {}).get("description")) or ""
    anchors = description.lower().count("<a ")
    if anchors != 1:
        problems.append(f"description has {anchors} link(s), expected exactly 1")

    if problems:
        logger.warning("Echo %s verification: %s", echo_id, "; ".join(problems))
    else:
        logger.info("Echo %s verified", echo_id)
