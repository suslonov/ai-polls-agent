"""Pydantic models for the AI Polls Agent pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

SourceLanguage = Literal["he", "ru", "en"]
TargetLanguage = Literal["ru", "en"]
Tone = Literal["important", "funny"]


# ── Configuration ─────────────────────────────────────────────────────────────


class SourceType(str, Enum):
    rss = "rss"
    site_index = "site_index"
    telegram_public = "telegram_public"


class SourceConfig(BaseModel):
    """One configured news source."""

    id: str
    enabled: bool = True
    language: SourceLanguage
    type: SourceType
    name: str
    urls: list[str] = Field(default_factory=list)
    priority: int = 2
    authoritative: bool = True
    tags: list[str] = Field(default_factory=list)

    # site_index only
    link_pattern: Optional[str] = None
    min_title_len: int = 20


class AppConfig(BaseModel):
    timezone: str = "Asia/Jerusalem"
    db_path: str
    render_path: str
    log_dir: str = "~/logs"
    history_days_in_ui: int = 30
    user_agent: str = "ai-polls-agent/1.0"


class ScheduleConfig(BaseModel):
    local_time: str = ""


class CollectionConfig(BaseModel):
    lookback_hours: int = 30
    max_items_per_source: int = 30
    max_candidates_before_prefilter: int = 250
    prefilter_keep: int = 45
    final_candidates_min: int = 10
    final_candidates_max: int = 20
    max_article_chars_for_selector: int = 800
    max_article_chars_for_enrichment: int = 9000
    max_article_chars_for_prompt: int = 6000
    prefilter_batch_size: int = 40
    http_timeout_seconds: int = 20


class ModelsConfig(BaseModel):
    prefilter: str
    prefilter_max_output_tokens: int = 8192
    selector: str
    selector_max_tokens: int = 8192
    quiz_designer: str
    quiz_designer_max_tokens: int = 2048


class KvasirConfig(BaseModel):
    aws_region: str = ""
    kv2_course_lambda_name: str = "kv2_course"
    kv2_text_lambda_name: str = "kv2_text"
    courses_bucket: str = "kv-courses"
    initial_component_status: str = "raw"
    echo_editor_base_url: str = "https://quizly.pub/echo-edit?id="
    scroll_quiz_base_url: str = "https://quizly.pub/scroll-quiz"


class PublishingConfig(BaseModel):
    html_dirs: list[str] = Field(default_factory=list)
    today_file_en: str = "daily-israel-polls-en.html"
    today_file_ru: str = "daily-israel-polls-ru.html"
    public_url_en: str = "https://kvasir.pub/daily-israel-polls-en"
    public_url_ru: str = "https://kvasir.pub/daily-israel-polls-ru"
    max_entries_per_page: int = 60
    telegram_enabled: bool = False

    def file_for(self, target_language: str) -> str:
        return self.today_file_ru if target_language == "ru" else self.today_file_en

    def public_url_for(self, target_language: str) -> str:
        return self.public_url_ru if target_language == "ru" else self.public_url_en


class CategoryGenerationConfig(BaseModel):
    """Bounds for the per-poll participant categories."""

    target_count: int = 8
    min_count: int = 4
    cap: int = 10
    max_count: int = 12
    max_chars: int = 90
    near_duplicate_threshold: float = 0.6
    max_tokens: int = 1500


class PartiesConfig(BaseModel):
    """Political parties, usable as categories only on political stories."""

    category_template: dict[str, str] = Field(default_factory=dict)
    undecided: dict[str, str] = Field(default_factory=dict)
    max_in_poll: int = 5
    defaults: dict[str, list[str]] = Field(default_factory=dict)

    def format_party(self, party: str, language: str) -> str:
        """Render a bare party name as a participant identity."""
        template = self.category_template.get(language) or self.category_template.get("en")
        if not template:
            return party
        return template.format(party=party)

    def undecided_for(self, language: str) -> str:
        return self.undecided.get(language) or self.undecided.get("en") or ""


class Settings(BaseModel):
    """Everything from config/settings.yaml."""

    app: AppConfig
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    models: ModelsConfig
    kvasir: KvasirConfig = Field(default_factory=KvasirConfig)
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)
    category_generation: CategoryGenerationConfig = Field(default_factory=CategoryGenerationConfig)
    parties: PartiesConfig = Field(default_factory=PartiesConfig)
    political_keywords: dict[str, list[str]] = Field(default_factory=dict)
    stakeholder_fallbacks: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    funny_fallbacks: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    personas: dict[str, dict[str, str]] = Field(default_factory=dict)

    def persona_for(self, tone: str, target_language: str) -> str:
        by_tone = self.personas.get(tone) or {}
        text = by_tone.get(target_language) or by_tone.get("en") or ""
        return text.strip()

    def party_defaults_for(self, target_language: str) -> list[str]:
        """Configured party names for a language (fallback for the template's own list)."""
        return self.parties.defaults.get(target_language) or self.parties.defaults.get("en") or []

    def political_keywords_for(self, target_language: str) -> list[str]:
        keywords = list(self.political_keywords.get(target_language) or [])
        if target_language != "en":
            keywords += list(self.political_keywords.get("en") or [])
        return keywords

    def fallback_pool(self, tone: str, domain: str, target_language: str) -> list[str]:
        """Last-resort categories for a tone and topical domain."""
        library = self.funny_fallbacks if tone == "funny" else self.stakeholder_fallbacks
        by_domain = library.get(domain) or library.get("general") or {}
        return by_domain.get(target_language) or by_domain.get("en") or []


# ── News items ────────────────────────────────────────────────────────────────


class NewsItem(BaseModel):
    """A normalized story, identical in shape across all collectors."""

    id: Optional[int] = None
    run_id: Optional[int] = None

    source_id: str
    source_name: str
    source_type: str
    source_language: SourceLanguage

    title_original: str
    title_en: Optional[str] = None

    url: str
    canonical_url: str
    discovery_url: Optional[str] = None

    published_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

    dek_original: Optional[str] = None
    snippet_original: Optional[str] = None
    full_text: Optional[str] = None

    short_en: Optional[str] = None

    content_hash: str = ""
    duplicate_group: Optional[str] = None

    prefilter_keep: Optional[bool] = None
    prefilter_relevance_score: Optional[float] = None
    prefilter_interesting_score: Optional[float] = None
    prefilter_funny_score: Optional[float] = None

    selector_rank: Optional[int] = None
    selector_interesting_score: Optional[float] = None
    selector_funny_score: Optional[float] = None
    why_candidate: Optional[str] = None
    topic: Optional[str] = None
    final_candidate: bool = False

    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("title_original")
    @classmethod
    def _title_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title_original must not be empty")
        return v.strip()

    def text_for_prompt(self, max_chars: int) -> str:
        """Best available body text, capped."""
        body = self.full_text or self.snippet_original or self.dek_original or ""
        return body[:max_chars].strip()

    def eligible_for(self, slot: str) -> bool:
        """Slot eligibility.

        Hebrew stories feed both slots — the quiz is written in the target
        language from the Hebrew source either way. The EN slot additionally
        requires the English rendering, because the operator picks from it and
        the English quiz is designed against it; the RU slot does not, since
        the designer works from the Hebrew original.
        """
        if slot == "ru":
            return self.source_language in ("ru", "he")
        if slot == "en":
            if self.source_language == "en":
                return True
            if self.source_language == "he":
                return bool(self.title_en and self.short_en)
        return False


# ── Workflow ──────────────────────────────────────────────────────────────────


class WorkflowStatus(str, Enum):
    ready = "ready"
    generating = "generating"
    editing = "editing"
    partially_finalized = "partially_finalized"
    finalized = "finalized"
    generation_failed = "generation_failed"


class EchoStatus(str, Enum):
    creating = "creating"
    editing = "editing"
    ready_to_publish = "ready_to_publish"
    published = "published"
    error = "error"


class RunStatus(str, Enum):
    running = "running"
    complete = "complete"
    failed = "failed"


class PublishStatus(str, Enum):
    pending = "pending"
    page_published = "page_published"
    page_published_telegram_failed = "page_published_telegram_failed"
    complete = "complete"
    failed = "failed"


# ── LLM responses ─────────────────────────────────────────────────────────────


class PrefilterVerdict(BaseModel):
    """One item of the cheap-model prefilter response."""

    id: int
    keep: bool = False
    israel_relevance: float = 0.0
    interesting_score: float = 0.0
    funny_score: float = 0.0
    topic: str = ""
    story_group_hint: str = ""
    reason: str = ""


class SelectorPick(BaseModel):
    """One item of the Claude final-selection response."""

    id: int
    rank: int = 0
    interesting_score: float = 0.0
    funny_score: float = 0.0
    topic: str = ""
    why_candidate: str = ""


class QuizDesign(BaseModel):
    """Strict JSON contract of the quiz-designer step."""

    title: str
    description_text: str
    news_summary_for_prompt: str
    yes_no_question: str
    greeting: str = ""
    picture_suggestions: list[str] = Field(default_factory=list)

    @field_validator("title", "description_text", "news_summary_for_prompt", "yes_no_question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()


class RunStats(BaseModel):
    """Counters for one cron pass."""

    run_id: Optional[int] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    collected: int = 0
    deterministic_dropped: int = 0
    deduped: int = 0
    prefiltered: int = 0
    final_candidates: int = 0
    errors: list[str] = Field(default_factory=list)
