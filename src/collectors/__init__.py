"""Source collectors. Every collector returns normalized NewsItem objects."""

from src.collectors import rss, site_index, telegram_public
from src.collectors.base import CollectorResult, collect_source

__all__ = ["CollectorResult", "collect_source", "rss", "site_index", "telegram_public"]
