"""Cron entry point: exactly one pipeline pass, with an overlap lock.

    python -m src.scheduler_entry

Exits non-zero on an unrecoverable failure so cron mail (or the wrapper script)
surfaces it.
"""

from __future__ import annotations

import fcntl
import logging
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import pipeline  # noqa: E402
from src.secrets import SecretsError, load_secrets  # noqa: E402
from src.settings import load_settings, load_sources, resolve_path  # noqa: E402

logger = logging.getLogger("scheduler")

LOCK_NAME = "ai-polls-agent.lock"


def _configure_logging(log_dir: Path) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        handlers.append(logging.FileHandler(log_dir / f"polls_run_{stamp}.log", encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - logging must never block a run
        print(f"Could not open log directory {log_dir}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def main() -> int:
    settings = load_settings()
    log_dir = resolve_path(settings.app.log_dir)
    _configure_logging(log_dir)

    db_path = resolve_path(settings.app.db_path)
    lock_path = db_path.parent / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # A slow run must never be overtaken by the next cron tick.
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another run is already in progress (%s) — exiting", lock_path)
        lock_file.close()
        return 0

    try:
        secrets = load_secrets(_REPO_ROOT)
        sources = load_sources()
        logger.info("Starting scheduled collection pass …")
        stats = pipeline.run_pipeline(settings, sources, secrets, db_path)

        logger.info(
            "Finished: collected=%d prefiltered=%d final=%d errors=%d",
            stats.collected, stats.prefiltered, stats.final_candidates, len(stats.errors),
        )
        for error in stats.errors:
            logger.warning("Source/step error: %s", error)

        if not stats.final_candidates:
            logger.error("Run produced no candidates")
            return 1
        return 0

    except SecretsError as exc:
        logger.error("Configuration problem: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        return 1
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
