"""ai-home-hub module: the /polls operator UI and its API.

Implements the Module protocol ai-home-hub's loader expects:

    PollsModule(prefix: str, config: dict, repo_path: Path)
    handle(method, path, body, headers) -> (status, content_type, body_bytes)

Routes
    GET  /                     rendered operator page
    GET  /api/day/current      today's state as JSON
    GET  /api/history          previous days (read-only)
    POST /api/select           set/clear one slot's selection (409 once locked)
    POST /api/default-categories  use the template's category list for one slot
    POST /api/categories       edit one generated echo's categories (prompt included)
    POST /api/start-generation lock the selection, then create the echoes
    POST /api/retry-generation retry one language (optionally as a fresh echo)
    POST /api/finalize         publish one finished language
    POST /api/retry-telegram   re-send only the announcement
    POST /api/re-render        re-render the page from current DB state

Generation and finalization run synchronously inside the request. The hub
serves requests on a threading HTTP server, so a slow call blocks only its own
tab, and the authoritative state lives in SQLite either way.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

Response = tuple[int, str, bytes]

# The hub's router drops query strings, so pagination lives in the path.
_COLLECTED_PAGE_RE = re.compile(r"^/collected/p/(\d+)$")


class PollsModule:
    name = "Daily Polls"
    description = "Israeli daily yes/no poll workflow"

    def __init__(self, prefix: str, config: dict, repo_path: Path) -> None:
        self.prefix = prefix
        self.repo_path = Path(repo_path).resolve()
        _ensure_on_path(self.repo_path)

        from src.settings import load_settings, load_sources, resolve_path

        self.settings_path = self.repo_path / config.get("settings_yaml", "config/settings.yaml")
        self.sources_path = self.repo_path / config.get("sources_yaml", "config/sources.yaml")

        self.settings = load_settings(self.settings_path)
        self.sources = load_sources(self.sources_path)

        # settings.yaml wins over hub.yaml so ~/... paths stay in one place.
        self.db_path = resolve_path(
            config.get("db_path") or self.settings.app.db_path, self.repo_path
        )
        self.output_path = resolve_path(
            config.get("output_html") or self.settings.app.render_path, self.repo_path
        )

        from src import db

        db.init_db(self.db_path)

    # ── Routing ───────────────────────────────────────────────────────────────

    def handle(self, method: str, path: str, body: bytes, headers: dict) -> Response:
        route = (path.split("?", 1)[0] or "/").rstrip("/") or "/"

        try:
            if method in ("GET", "HEAD") and route in ("/", "/index.html"):
                return self._serve_page()
            if method == "GET" and (route == "/collected" or _COLLECTED_PAGE_RE.match(route)):
                return self._serve_collected(route)
            if method == "POST" and route == "/api/add-candidate":
                return self._api_add_candidate(body)
            if method == "GET" and route == "/api/day/current":
                return self._api_current()
            if method == "GET" and route == "/api/history":
                return self._api_history()
            if method == "POST" and route == "/api/select":
                return self._api_select(body)
            if method == "POST" and route == "/api/default-categories":
                return self._api_default_categories(body)
            if method == "POST" and route == "/api/categories":
                return self._api_edit_categories(body)
            if method == "POST" and route == "/api/start-generation":
                return self._api_start_generation(body)
            if method == "POST" and route == "/api/retry-generation":
                return self._api_retry_generation(body)
            if method == "POST" and route == "/api/reset-generation":
                return self._api_reset_generation(body)
            if method == "POST" and route == "/api/close-language":
                return self._api_close_language(body)
            if method == "POST" and route == "/api/finalize":
                return self._api_finalize(body)
            if method == "POST" and route == "/api/retry-telegram":
                return self._api_retry_telegram(body)
            if method == "POST" and route == "/api/re-render":
                return self._api_re_render()
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the hub
            logger.error("polls %s %s failed: %s", method, route, exc, exc_info=True)
            return _json_response(500, {"ok": False, "error": str(exc)})

        return 404, "text/plain; charset=utf-8", b"Not found"

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _today(self) -> str:
        from src.pipeline import local_day

        return local_day(self.settings.app.timezone)

    def _render(self) -> Path:
        from src import render

        return render.render_operator_page(
            settings=self.settings,
            db_path=self.db_path,
            day=self._today(),
            output_path=self.output_path,
            api_base=self.prefix,
            repo_path=self.repo_path,
        )

    def _serve_page(self) -> Response:
        try:
            path = self._render()
            return 200, "text/html; charset=utf-8", path.read_bytes()
        except Exception as exc:  # noqa: BLE001 - fall back to the last render
            logger.error("Rendering /polls failed: %s", exc, exc_info=True)
            if self.output_path.exists():
                return 200, "text/html; charset=utf-8", self.output_path.read_bytes()
            return _json_response(500, {"ok": False, "error": str(exc)})

    def _serve_collected(self, route: str) -> Response:
        """The full collected table — everything the run saw, not just the shortlist."""
        from src import render

        match = _COLLECTED_PAGE_RE.match(route)
        page = int(match.group(1)) if match else 1
        html = render.render_collected_page(
            settings=self.settings,
            db_path=self.db_path,
            day=self._today(),
            page=page,
            api_base=self.prefix,
            repo_path=self.repo_path,
        )
        return 200, "text/html; charset=utf-8", html.encode("utf-8")

    def _api_add_candidate(self, body: bytes) -> Response:
        """Promote one collected story onto today's shortlist by hand."""
        from src import db

        data = _parse_json(body)
        try:
            item_id = int(data.get("item_id"))
        except (TypeError, ValueError):
            return _json_response(400, {"ok": False, "error": "item_id must be an integer"})

        try:
            added = db.add_manual_candidate(self.db_path, item_id)
        except ValueError as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})

        return _json_response(200, {"ok": True, "item_id": item_id, "added": added})

    def _api_current(self) -> Response:
        from src import render

        state = render.build_state(self.settings, self.db_path, self._today())
        return _json_response(200, {"ok": True, **state})

    def _api_history(self) -> Response:
        from src import render

        history = render.build_history(self.settings, self.db_path, self._today())
        return _json_response(200, {"ok": True, "history": history})

    def _api_select(self, body: bytes) -> Response:
        from src import db

        data = _parse_json(body)
        slot = str(data.get("slot") or "").lower()
        if slot not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "slot must be 'ru' or 'en'"})

        raw_item = data.get("item_id")
        tone = data.get("tone")
        item_id = None

        if raw_item not in (None, "", 0):
            try:
                item_id = int(raw_item)
            except (TypeError, ValueError):
                return _json_response(400, {"ok": False, "error": "item_id must be an integer"})

            row = db.get_item(self.db_path, item_id)
            if row is None:
                return _json_response(400, {"ok": False, "error": f"unknown item {item_id}"})

            eligible = _slot_eligibility(row)
            if not eligible[slot]:
                return _json_response(
                    400,
                    {"ok": False, "error": f"item {item_id} is not eligible for the {slot.upper()} slot"},
                )
            if tone not in ("important", "funny"):
                return _json_response(
                    400, {"ok": False, "error": "tone must be 'important' or 'funny'"}
                )
        else:
            tone = None

        day = self._today()
        db.ensure_day(self.db_path, day)
        try:
            workflow = db.set_selection(self.db_path, day, slot, item_id, tone)
        except db.SelectionLocked as exc:
            return _json_response(409, {"ok": False, "error": str(exc)})

        return _json_response(200, {"ok": True, "workflow": workflow})

    def _api_default_categories(self, body: bytes) -> Response:
        """Toggle 'use default categories' for one slot, editable until it locks."""
        from src import db

        data = _parse_json(body)
        slot = str(data.get("slot") or "").lower()
        if slot not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "slot must be 'ru' or 'en'"})

        day = self._today()
        db.ensure_day(self.db_path, day)
        try:
            workflow = db.set_default_categories(
                self.db_path, day, slot, bool(data.get("enabled"))
            )
        except db.SelectionLocked as exc:
            return _json_response(409, {"ok": False, "error": str(exc)})

        return _json_response(200, {"ok": True, "workflow": workflow})

    def _api_edit_categories(self, body: bytes) -> Response:
        """Replace one echo's categories, in the database and in its prompt."""
        from src import workflow as wf
        from src.secrets import SecretsError, load_secrets

        data = _parse_json(body)
        language = str(data.get("language") or "").lower()
        if language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})

        raw = data.get("categories")
        if not isinstance(raw, list):
            return _json_response(400, {"ok": False, "error": "categories must be a list"})

        try:
            secrets = load_secrets(self.repo_path)
            result = wf.update_categories(
                self.settings, secrets, self.db_path, self._today(), language,
                [str(value) for value in raw],
            )
        except (wf.WorkflowError, SecretsError) as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})
        return _json_response(200, {"ok": True, **result})

    def _api_start_generation(self, body: bytes) -> Response:
        """Start one language (or every selected one when none is named)."""
        from src import db, workflow as wf
        from src.secrets import SecretsError, load_secrets

        data = _parse_json(body)
        language = str(data.get("language") or "").lower() or None
        if language and language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})

        try:
            secrets = load_secrets(self.repo_path)
            result = wf.start_generation(
                self.settings, secrets, self.db_path, self._today(),
                target_language=language,
            )
        except db.SelectionLocked as exc:
            return _json_response(409, {"ok": False, "error": str(exc)})
        except SecretsError as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})

        status = 200 if result.get("echoes") else 500
        return _json_response(status, result)

    def _api_close_language(self, body: bytes) -> Response:
        """Retire a finished poll so the language is free again today."""
        from src import db, workflow as wf

        data = _parse_json(body)
        language = str(data.get("language") or "").lower()
        if language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})

        try:
            result = wf.close_language(self.db_path, self._today(), language)
        except db.ResetRefused as exc:
            return _json_response(409, {"ok": False, "error": str(exc)})
        return _json_response(200, {"ok": True, **result})

    def _api_retry_generation(self, body: bytes) -> Response:
        from src import workflow as wf
        from src.secrets import SecretsError, load_secrets

        data = _parse_json(body)
        language = str(data.get("language") or "").lower()
        if language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})
        force_new = bool(data.get("force_new"))

        try:
            secrets = load_secrets(self.repo_path)
            result = wf.retry_language(
                self.settings, secrets, self.db_path, self._today(), language, force_new=force_new
            )
        except (wf.WorkflowError, SecretsError) as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})
        return _json_response(200, result)

    def _api_reset_generation(self, body: bytes) -> Response:
        """Discard one language's generation state so it can be started again."""
        from src import db

        data = _parse_json(body)
        language = str(data.get("language") or "").lower() or None
        if language and language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})

        # Reset is an operator tool: it never refuses, so there is no 409 here.
        result = db.reset_generation(self.db_path, self._today(), target_language=language)
        return _json_response(200, {"ok": True, **result})

    def _api_finalize(self, body: bytes) -> Response:
        from src import workflow as wf
        from src.secrets import SecretsError, load_secrets

        data = _parse_json(body)
        language = str(data.get("language") or "").lower()
        if language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})

        try:
            secrets = load_secrets(self.repo_path)
            result = wf.finalize(self.settings, secrets, self.db_path, self._today(), language)
        except (wf.WorkflowError, SecretsError) as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})
        return _json_response(200, result)

    def _api_retry_telegram(self, body: bytes) -> Response:
        from src import workflow as wf
        from src.secrets import SecretsError, load_secrets

        data = _parse_json(body)
        language = str(data.get("language") or "").lower()
        if language not in ("ru", "en"):
            return _json_response(400, {"ok": False, "error": "language must be 'ru' or 'en'"})

        try:
            secrets = load_secrets(self.repo_path)
            result = wf.retry_telegram(
                self.settings, secrets, self.db_path, self._today(), language
            )
        except (wf.WorkflowError, SecretsError) as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})
        return _json_response(200, {"ok": True, **result})

    def _api_re_render(self) -> Response:
        path = self._render()
        return _json_response(200, {"ok": True, "rendered": str(path)})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slot_eligibility(row: dict) -> dict[str, bool]:
    """Mirrors :meth:`src.models.NewsItem.eligible_for` for a database row."""
    language = row.get("source_language")
    has_translation = bool(row.get("title_en") and row.get("short_en"))
    return {
        "ru": language in ("ru", "he"),
        # Russian is translated on demand at Start, so it needs nothing here.
        "en": language in ("en", "ru") or (language == "he" and has_translation),
    }


def _json_response(status: int, payload: dict) -> Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def _parse_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _ensure_on_path(repo_path: Path) -> None:
    repo_str = str(repo_path)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
