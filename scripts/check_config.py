#!/usr/bin/env python3
"""Pre-flight check: config, credentials, paths, Kvasir template, stable pages.

    python scripts/check_config.py            # offline checks only
    python scripts/check_config.py --remote   # also call Kvasir (read-only)

Never prints a secret — only whether it is present.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.settings import load_settings, load_sources, resolve_path  # noqa: E402

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
_status = {"fail": 0, "warn": 0}


def report(level: str, message: str) -> None:
    print(f"[{level}] {message}")
    if level is FAIL:
        _status["fail"] += 1
    elif level is WARN:
        _status["warn"] += 1


def check_config():
    settings = load_settings()
    sources = load_sources()
    report(OK, f"settings.yaml parsed (timezone {settings.app.timezone})")

    enabled = [s for s in sources if s.enabled]
    by_language = {lang: sum(1 for s in enabled if s.language == lang) for lang in ("he", "ru", "en")}
    report(OK, f"sources.yaml parsed: {len(enabled)} enabled of {len(sources)} "
               f"(he={by_language['he']} ru={by_language['ru']} en={by_language['en']})")
    if by_language["ru"] < 2:
        report(WARN, "fewer than 2 Russian sources — the RU slot may run dry")

    for tone in ("important", "funny"):
        for language in ("en", "ru"):
            if not settings.persona_for(tone, language):
                report(FAIL, f"persona {tone}.{language} is empty in settings.yaml")
    # Categories are generated per poll; what must be configured is the material
    # the generator falls back to when a call fails or returns nothing usable.
    for language in ("en", "ru"):
        if not settings.party_defaults_for(language):
            report(WARN, f"parties.defaults.{language} is empty — the prompt template's "
                         "own party list is then the only source")
        if not settings.parties.format_party("Likud", language):
            report(FAIL, f"parties.category_template.{language} is missing")
        if not settings.fallback_pool("important", "general", language):
            report(FAIL, f"stakeholder_fallbacks.general.{language} is empty")
        if not settings.fallback_pool("funny", "general", language):
            report(FAIL, f"funny_fallbacks.general.{language} is empty")

    return settings


def check_secrets():
    from src.secrets import SecretsError, load_secrets

    try:
        secrets = load_secrets(REPO_ROOT)
    except SecretsError as exc:
        report(FAIL, str(exc))
        return None

    groups = {
        "LLM (needed by cron)": {
            "ANTHROPIC_API_KEY": secrets.anthropic_api_key,
            "GOOGLE_API_KEY": secrets.google_api_key,
        },
        "Kvasir (needed to create echoes)": {
            "AWS_ACCESS_KEY_ID": secrets.aws_access_key_id,
            "AWS_SECRET_ACCESS_KEY": secrets.aws_secret_access_key,
            "KVASIR_USER_SUB": secrets.kvasir_user_sub,
            "KVASIR_TEMPLATE": secrets.kvasir_template,
            "KVASIR_COURSE_EN": secrets.kvasir_course_en,
            "KVASIR_COURSE_RU": secrets.kvasir_course_ru,
        },
        "Telegram (needed to announce)": {
            "TELEGRAM_BOT_TOKEN": secrets.telegram_bot_token,
            "TELEGRAM_CHANNEL_EN": secrets.telegram_channel_en,
            "TELEGRAM_CHANNEL_RU": secrets.telegram_channel_ru,
        },
    }
    for group, values in groups.items():
        missing = [name for name, value in values.items() if not str(value).strip()]
        if missing:
            level = FAIL if group.startswith("LLM") else WARN
            report(level, f"{group}: missing {', '.join(missing)}")
        else:
            report(OK, f"{group}: all present")

    env_path = REPO_ROOT / ".env"
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        report(WARN, f".env is mode {mode:o}; run: chmod 600 {env_path}")
    else:
        report(OK, ".env permissions are 600")

    return secrets


def check_prompts():
    """Every model instruction must be present and fully renderable."""
    from src import prompts

    category_placeholders = {
        "language_name", "min_items", "max_items", "title", "summary",
        "question", "party_defaults",
    }
    expected = {
        "prefilter": set(),
        "translate": set(),
        "selector": {"min_items", "max_items"},
        "quiz_designer": {"language_name", "persona"},
        "categories_important": category_placeholders,
        "categories_funny": category_placeholders,
    }
    for name, placeholders in expected.items():
        try:
            found = prompts.placeholders(name)
        except prompts.PromptError as exc:
            report(FAIL, str(exc))
            continue
        if found != placeholders:
            report(FAIL, f"config/prompts/{name}.txt placeholders {sorted(found)}, "
                         f"expected {sorted(placeholders)}")
        else:
            report(OK, f"prompt config/prompts/{name}.txt ok")


def check_paths(settings):
    db_path = resolve_path(settings.app.db_path)
    render_path = resolve_path(settings.app.render_path)
    for label, path in (("db_path", db_path), ("render_path", render_path)):
        parent = path.parent
        if parent.exists():
            report(OK, f"{label} directory exists: {parent}")
        else:
            report(WARN, f"{label} directory will be created on first run: {parent}")

    if not settings.publishing.html_dirs:
        report(FAIL, "publishing.html_dirs is empty — finalization has nowhere to write")
    for directory in settings.publishing.html_dirs:
        base = Path(directory).expanduser()
        if not base.is_dir():
            report(FAIL, f"publishing dir missing: {base}")
            continue
        for language in ("en", "ru"):
            page = base / settings.publishing.file_for(language)
            if not page.exists():
                report(FAIL, f"stable page missing: {page}")
            elif "<!-- POLLS:START -->" not in page.read_text(encoding="utf-8"):
                report(FAIL, f"stable page has no POLLS:START marker: {page}")
            else:
                report(OK, f"stable page ready: {page}")


def check_remote(settings, secrets):
    from src.echo_builder import template_prompt_key, validate_template_prompt
    from src.kvasir_client import KvasirClient, KvasirError

    try:
        client = KvasirClient(secrets, settings.kvasir)
        template = client.get_component(secrets.kvasir_template)
    except (KvasirError, Exception) as exc:  # noqa: BLE001
        report(FAIL, f"cannot read template echo {secrets.kvasir_template}: {exc}")
        return

    if template.get("type") != "echo":
        report(FAIL, f"template {secrets.kvasir_template} is type {template.get('type')!r}, expected 'echo'")
        return
    report(OK, f"template echo {secrets.kvasir_template} readable "
               f"(course {template.get('course_id')}, language {template.get('language')})")

    try:
        key = template_prompt_key(client, template)
        text = client.get_text(key)
        report(OK, f"template prompt readable: s3://{settings.kvasir.courses_bucket}/{key} ({len(text)} chars)")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, f"cannot read the template prompt: {exc}")
        return

    try:
        validate_template_prompt(text)
        report(OK, "template prompt contains all mandatory markers")
    except Exception as exc:  # noqa: BLE001
        report(FAIL, str(exc))

    # kv2_course accepts any course_id when creating a component and only checks
    # ownership when updating one, so a wrong course id here means a created,
    # orphaned echo followed by a 404. Catch it before generation ever runs.
    from src.echo_builder import EchoBuildError, check_course_access

    for language in ("en", "ru"):
        try:
            course_id = secrets.course_id_for(language)
        except Exception as exc:  # noqa: BLE001
            report(FAIL, f"{language.upper()} course: {exc}")
            continue
        try:
            course = check_course_access(client, course_id, secrets.kvasir_author_id)
        except EchoBuildError as exc:
            report(FAIL, f"{language.upper()} course {course_id}: {exc}")
            continue
        report(OK, f"{language.upper()} course {course_id} writable by author "
                   f"{secrets.kvasir_author_id} ({course.get('title', '')})")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Polls Agent configuration check")
    parser.add_argument("--remote", action="store_true", help="also perform read-only Kvasir checks")
    args = parser.parse_args()

    settings = check_config()
    secrets = check_secrets()
    check_prompts()
    check_paths(settings)

    if args.remote and secrets:
        check_remote(settings, secrets)
    elif not args.remote:
        print("\n(pass --remote to also verify the Kvasir template and its prompt)")

    print(f"\n{_status['fail']} failure(s), {_status['warn']} warning(s)")
    return 1 if _status["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
