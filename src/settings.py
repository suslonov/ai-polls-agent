"""Load config/settings.yaml and config/sources.yaml.

Configuration is non-secret and comes from YAML only; credentials come from
`.env` via :mod:`src.secrets`. Nothing here reads environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from src.models import Settings, SourceConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    """Return the repository root directory."""
    return _REPO_ROOT


def load_settings(path: Optional[Path] = None) -> Settings:
    """Parse settings.yaml into a validated :class:`Settings`."""
    settings_path = Path(path) if path else _REPO_ROOT / "config" / "settings.yaml"
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    return Settings(**raw)


def load_sources(path: Optional[Path] = None) -> list[SourceConfig]:
    """Parse sources.yaml into validated :class:`SourceConfig` objects."""
    sources_path = Path(path) if path else _REPO_ROOT / "config" / "sources.yaml"
    raw = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    return [SourceConfig(**entry) for entry in raw.get("sources", [])]


def resolve_path(path_str: str, root: Optional[Path] = None) -> Path:
    """Expand ``~`` and resolve; relative paths are anchored to the repo root."""
    base = root or _REPO_ROOT
    expanded = Path(os.path.expanduser(str(path_str).strip()))
    if expanded.is_absolute():
        return expanded
    return (base / expanded).resolve()
