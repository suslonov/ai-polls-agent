"""Credential loading.

Hard rule for this repository: every credential comes from the repository `.env`
file read through ``dotenv_values``. We never call ``load_dotenv()`` and never
read ``os.environ`` for secrets, so a stray shell export can't silently change
which Kvasir account or Telegram channel the pipeline writes to.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SecretsError(RuntimeError):
    """Raised when a required credential is missing from .env."""


class Secrets(BaseModel):
    """Immutable view of the repository .env file."""

    model_config = {"frozen": True}

    # LLMs
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_default_region: str = ""

    # Kvasir
    kvasir_user_sub: str = ""
    kvasir_user: str = ""
    kvasir_course_en: str = ""
    kvasir_course_ru: str = ""
    kvasir_template: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_channel_en: str = ""
    telegram_channel_ru: str = ""

    # ── Validation ────────────────────────────────────────────────────────────

    def require_llm(self) -> None:
        """Fail fast when the cron pipeline cannot run at all."""
        self._require({
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "GOOGLE_API_KEY": self.google_api_key,
        })

    def require_kvasir(self) -> None:
        """Validated lazily: only echo creation and finalization need these."""
        self._require({
            "AWS_ACCESS_KEY_ID": self.aws_access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.aws_secret_access_key,
            "KVASIR_USER_SUB": self.kvasir_user_sub,
            "KVASIR_TEMPLATE": self.kvasir_template,
        })

    def require_telegram(self) -> None:
        """Validated lazily: only the publish step needs these."""
        self._require({
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHANNEL_EN": self.telegram_channel_en,
            "TELEGRAM_CHANNEL_RU": self.telegram_channel_ru,
        })

    @property
    def kvasir_author_id(self) -> str:
        """Author id generated echoes belong to (``KVASIR_USER`` in .env).

        kv2_course reads it whenever donations are enabled, and it keeps the
        polls under the project's own author rather than the template's.
        """
        return self.kvasir_user.strip()

    def course_id_for(self, target_language: str) -> str:
        """Return the Kvasir course a generated echo belongs to."""
        course = self.kvasir_course_ru if target_language == "ru" else self.kvasir_course_en
        if not course:
            key = "KVASIR_COURSE_RU" if target_language == "ru" else "KVASIR_COURSE_EN"
            raise SecretsError(f"{key} is empty in .env")
        return course

    def telegram_channel_for(self, target_language: str) -> str:
        """Return the Telegram channel a published poll is announced in."""
        return self.telegram_channel_ru if target_language == "ru" else self.telegram_channel_en

    @staticmethod
    def _require(values: dict[str, str]) -> None:
        missing = sorted(name for name, value in values.items() if not str(value).strip())
        if missing:
            raise SecretsError(
                "Missing required value(s) in .env: " + ", ".join(missing)
            )


def load_secrets(repo_root: Path) -> Secrets:
    """Read and validate ``<repo_root>/.env``.

    ``dotenv_values`` understands the ``export FOO=bar`` form already used in
    this repository's .env, so both styles work.
    """
    env_path = Path(repo_root) / ".env"
    if not env_path.exists():
        raise SecretsError(
            f"{env_path} not found. Copy .env.example to .env and fill it in."
        )

    raw = {k: (v or "") for k, v in dotenv_values(env_path).items()}

    return Secrets(
        anthropic_api_key=raw.get("ANTHROPIC_API_KEY", ""),
        google_api_key=raw.get("GOOGLE_API_KEY", ""),
        aws_access_key_id=raw.get("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=raw.get("AWS_SECRET_ACCESS_KEY", ""),
        aws_session_token=raw.get("AWS_SESSION_TOKEN", ""),
        aws_default_region=raw.get("AWS_DEFAULT_REGION", ""),
        kvasir_user_sub=raw.get("KVASIR_USER_SUB", ""),
        kvasir_user=raw.get("KVASIR_USER", ""),
        kvasir_course_en=raw.get("KVASIR_COURSE_EN", ""),
        kvasir_course_ru=raw.get("KVASIR_COURSE_RU", ""),
        kvasir_template=raw.get("KVASIR_TEMPLATE", ""),
        telegram_bot_token=raw.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_channel_en=raw.get("TELEGRAM_CHANNEL_EN", ""),
        telegram_channel_ru=raw.get("TELEGRAM_CHANNEL_RU", ""),
    )


def redact(value: Optional[str], keep: int = 4) -> str:
    """Render a credential safely for logs (never log the raw value)."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"
