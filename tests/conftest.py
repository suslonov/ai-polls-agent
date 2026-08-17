"""Shared fixtures and fakes for the test suite."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import db
from src.models import NewsItem, QuizDesign, Settings
from src.secrets import Secrets
from src.settings import load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    """Fail loudly instead of calling a real model API from a test.

    Each module does `from src.llm import claude_json`, so the name has to be
    patched per module. A test that wants model behaviour overrides its own
    module's symbol afterwards; anything else that reaches out is a bug.
    """
    from src import category_designer, prefilter, quiz_designer, selector

    def blocked(**kwargs):
        raise AssertionError(
            "a test tried to call a model API — monkeypatch the call in the test"
        )

    for module in (selector, quiz_designer, category_designer):
        monkeypatch.setattr(module, "claude_json", blocked, raising=False)
    monkeypatch.setattr(prefilter, "gemini_json", blocked, raising=False)


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state.db"
    db.init_db(path)
    return path


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Real settings.yaml with every filesystem path redirected into tmp_path."""
    loaded = load_settings(REPO_ROOT / "config" / "settings.yaml")

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    # Whatever the stable pages are called in settings.yaml — the tests never
    # hardcode the names, so a rename there needs no change here.
    for filename in (loaded.publishing.file_for("en"), loaded.publishing.file_for("ru")):
        (pages_dir / filename).write_text(
            "<html><body>\n<!-- POLLS:START -->\n\n<!-- POLLS:END -->\n</body></html>",
            encoding="utf-8",
        )

    return loaded.model_copy(
        update={
            "app": loaded.app.model_copy(
                update={
                    "db_path": str(tmp_path / "state.db"),
                    "render_path": str(tmp_path / "rendered" / "index.html"),
                    "log_dir": str(tmp_path / "logs"),
                }
            ),
            "publishing": loaded.publishing.model_copy(
                update={"html_dirs": [str(pages_dir)], "telegram_enabled": False}
            ),
        }
    )


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(
        anthropic_api_key="test-anthropic",
        google_api_key="test-google",
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        aws_default_region="us-east-1",
        kvasir_user_sub="sub-1234",
        kvasir_course_en="500",
        kvasir_course_ru="501",
        kvasir_template="9001",
        telegram_bot_token="123:ABC",
        telegram_channel_en="@poll_en",
        telegram_channel_ru="@poll_ru",
    )


def make_item(
    title: str = "A story about something",
    url: str = "https://example.com/news/story-1",
    language: str = "en",
    source_id: str = "toi_en",
    source_type: str = "rss",
    published_at: datetime | None = None,
    **overrides,
) -> NewsItem:
    """Build a NewsItem with sane defaults for tests."""
    from src.extraction import canonicalize_url, content_hash

    canonical = canonicalize_url(url)
    now = datetime.now(timezone.utc)
    fields = {
        "source_id": source_id,
        "source_name": source_id.upper(),
        "source_type": source_type,
        "source_language": language,
        "title_original": title,
        "title_en": title if language == "en" else None,
        "url": url,
        "canonical_url": canonical,
        "published_at": published_at or (now - timedelta(hours=2)),
        "fetched_at": now,
        "content_hash": content_hash(title, canonical),
        "first_seen_at": now,
        "last_seen_at": now,
    }
    fields.update(overrides)
    return NewsItem(**fields)


def seed_candidate(db_path: Path, **kwargs) -> int:
    """Insert one shortlisted candidate and return its id."""
    item = make_item(**kwargs)
    item_id = db.upsert_news_item(db_path, item)
    db.update_selection(db_path, item_id, rank=1, interesting=80, funny=20, topic="society", why="test")
    return item_id


def make_design(title: str = "Should the city ban scooters?") -> QuizDesign:
    return QuizDesign(
        title=title,
        description_text="The city council votes on a scooter ban this week.",
        news_summary_for_prompt="The council will vote on banning e-scooters downtown.",
        yes_no_question="Should e-scooters be banned downtown?",
        picture_suggestions=["Overhead photo of a scooter lane", "Close crop of a parked scooter"],
    )


class FakeKvasirClient:
    """In-memory stand-in for :class:`src.kvasir_client.KvasirClient`.

    Records every Lambda payload and S3 operation so tests can assert on the
    exact call sequence (for example: exactly two ``component_update`` calls).
    """

    def __init__(self, template: dict | None = None, next_component_id: int = 4242):
        from src.models import KvasirConfig

        self.region = "us-east-1"
        self.config = KvasirConfig(aws_region="us-east-1")
        self.calls: list[dict] = []
        self.s3_objects: dict[str, str] = {}
        self.copies: list[tuple[str, str]] = []
        self.puts: list[tuple[str, str]] = []
        self.component_updates: list[dict] = []
        self.next_component_id = next_component_id
        self.scrolls: list[dict] = []
        self.components: dict[int, dict] = {}
        # The poll courses, both owned by the author in the `secrets` fixture.
        self.author_id = 16
        self.courses: dict[int, dict] = {
            500: {"id": 500, "author_id": self.author_id, "language": "en",
                  "status": "free", "title": "Daily news quizzes"},
            501: {"id": 501, "author_id": self.author_id, "language": "ru",
                  "status": "free", "title": "Ежедневные квизы по новостям"},
        }

        self.template = template or {
            "id": 9001,
            "course_id": 500,
            "type": "echo",
            "language": "en",
            "title": "Template echo",
            "status": "raw",
            "total_adviser": 3,
            "voice": "alloy",
            "dependency": 0,
            "assets": {
                "text": {"region": "us-east-1", "name": "9001", "ext": "txt"},
                "title_picture": {"region": "us-east-1", "name": "9001", "ext": "jpg"},
            },
            "details": {
                "conv_type": "poll",
                "llm_model": "claude",
                "theme": "default",
                "effort": "minimal",
                "public": False,
                "description": "template description",
            },
        }
        self.components[9001] = self.template
        self.s3_objects["500/title_picture/9001.jpg"] = "<binary image>"
        template_key = "500/text/9001.txt"
        # Mirrors the deployed template: the CATEGORIES marker sits inside a JSON
        # array and carries the party list as its DEFAULT payload.
        self.s3_objects[template_key] = (
            '<general>\n"locale": {{LOCALE}}\n"categories": [{{CATEGORIES DEFAULT={\n'
            '"ru": ["Ликуд", "НДИ", "ШАС", "Демократы", "Оцма Йехудит"],\n'
            '"en": ["Likud", "Yisrael Beiteinu", "Shas", "The Democrats", "Otzma Yehudit"]}}}]\n'
            "</general>\n\n{{NEWS_SUMMARY}}\n\n{{PERSONA}}\n"
        )

    # ── Lambda ────────────────────────────────────────────────────────────────

    def invoke_kv2_course(self, payload: dict) -> dict:
        self.calls.append({"function": "kv2_course", "payload": payload})
        from src.kvasir_client import KvasirError

        action = payload.get("action")
        if action == "get_component":
            return dict(self.components.get(int(payload["component_id"]), {}))
        if action == "get_course":
            course = self.courses.get(int(payload["course_id"]))
            if course is None:
                raise KvasirError("kv2_course/get_course: HTTP 404: Course not found")
            return {"course": dict(course)}
        if action == "component_update":
            record = payload["component_record"]
            self._assert_lambda_contract(record)
            self._assert_course_access(record)
            self.component_updates.append(json.loads(json.dumps(record, default=str)))
            component_id = record.get("id") or self.next_component_id
            if not record.get("id"):
                self.next_component_id += 1
            stored = dict(record)
            stored["id"] = int(component_id)
            self.components[int(component_id)] = stored
            return {"component_id": int(component_id)}
        raise AssertionError(f"unexpected kv2_course action {action!r}")

    def _assert_course_access(self, record: dict) -> None:
        """Mirror kv2_course's asymmetric access check.

        Creating a component (no ``id``) accepts any ``course_id`` at all;
        updating one runs ``check_component_access``, which joins the component
        to its course's author. A course we do not author therefore fails on the
        *second* call, after the component already exists — the 404 that left an
        orphan draft in production.
        """
        from src.kvasir_client import KvasirError

        if not record.get("id"):
            return
        component = self.components.get(int(record["id"])) or {}
        course = self.courses.get(int(component.get("course_id") or record.get("course_id") or 0))
        if not course or str(course.get("author_id")) != str(self.author_id):
            raise KvasirError(
                "kv2_course/component_update: HTTP 404: "
                "Course not found or you are not the author"
            )

    @staticmethod
    def _assert_lambda_contract(record: dict) -> None:
        """Reproduce the field accesses kv2_course performs unguarded.

        The real Lambda indexes these directly, so a missing key is an HTTP 500
        rather than a validation error. Mirroring that here keeps the fake
        honest — a record the Lambda would reject must fail in tests too.
        """
        from src.kvasir_client import KvasirError

        for required in ("course_id", "type", "language", "title", "assets", "details"):
            if required not in record:
                raise KvasirError(
                    f"kv2_course/component_update: HTTP 500: missing {required!r}"
                )
        details = record.get("details") or {}
        if details.get("allow_donations", False) and "author_id" not in record:
            # update_component(): author_record = db.get_author_record(record["author_id"])
            raise KvasirError("kv2_course/component_update: HTTP 500: KeyError 'author_id'")

    def invoke_kv2_text(self, payload: dict) -> dict:
        self.calls.append({"function": "kv2_text", "payload": payload})
        if payload.get("action") == "list_scrolls":
            return {"scrolls": list(self.scrolls), "has_more": False}
        raise AssertionError(f"unexpected kv2_text action {payload.get('action')!r}")

    # ── Convenience wrappers mirroring the real client ────────────────────────

    def get_component(self, component_id, siblings: bool = False) -> dict:
        return self.invoke_kv2_course(
            {"action": "get_component", "component_id": int(component_id),
             "siblings": siblings, "editor": True}
        )

    def get_course(self, course_id) -> dict:
        body = self.invoke_kv2_course({"action": "get_course", "course_id": int(course_id)})
        course = body.get("course")
        return course if isinstance(course, dict) else body

    def component_update(self, component_record: dict) -> dict:
        return self.invoke_kv2_course(
            {"action": "component_update", "component_record": component_record}
        )

    def list_scrolls(self, component_id, limit: int = 50, offset: int = 0) -> list[dict]:
        body = self.invoke_kv2_text(
            {"action": "list_scrolls", "component_id": int(component_id),
             "limit": limit, "offset": offset}
        )
        return body.get("scrolls", [])

    @staticmethod
    def language_postfix(language: str) -> str:
        lang = (language or "en").strip().lower()
        return "" if lang in ("", "en") else f".{lang}"

    def text_key(self, course_id, name, language: str, ext: str = "txt") -> str:
        return f"{course_id}/text/{name}{self.language_postfix(language)}.{ext}"

    def copy_object(self, source_key: str, destination_key: str) -> None:
        if source_key not in self.s3_objects:
            # The real client wraps a NoSuchKey into KvasirError.
            from src.kvasir_client import KvasirError

            raise KvasirError(f"S3 copy {source_key} -> {destination_key} failed: NoSuchKey")
        self.copies.append((source_key, destination_key))
        self.s3_objects[destination_key] = self.s3_objects[source_key]

    def get_text(self, key: str) -> str:
        return self.s3_objects[key]

    def put_text(self, key: str, body: str) -> None:
        self.puts.append((key, body))
        self.s3_objects[key] = body

    def editor_url(self, component_id) -> str:
        return f"https://quizly.pub/echo-edit?id={component_id}"

    def scroll_quiz_url(self, component_id, scroll_id: str) -> str:
        return f"https://quizly.pub/scroll-quiz?id={component_id}#{scroll_id}"
