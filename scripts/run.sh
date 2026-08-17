#!/usr/bin/env bash
# Cron-ready wrapper for one collection pass.
#
#   bash scripts/run.sh              # full pass
#   bash scripts/run.sh --dry-run    # collect + filter only, no LLM calls
#
# Uses the shared ai-news conda environment by absolute path (same pattern as
# ai-news-agent/scripts/run_no_conda.sh) so cron does not need a conda hook.
# Override with PYTHON_BIN=/path/to/python.
#
# Credentials are read from the repository .env by the Python code itself, so
# this script deliberately never sources .env into the environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="${PYTHON_BIN:-/home/anton/miniconda3/envs/ai-news/bin/python}"

cd "$PROJECT_ROOT"

LOG_DIR="$("$PYTHON_BIN" -c "
import os, sys
sys.path.insert(0, '$PROJECT_ROOT')
from src.settings import load_settings
print(os.path.expanduser(load_settings().app.log_dir))
" 2>/dev/null || echo "$HOME/logs")"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/ai-polls-agent_$(date +%Y%m%d_%H%M%S).log"

# flock keeps two overlapping cron ticks from running the pipeline at once
# (src/scheduler_entry.py takes the same precaution independently).
LOCK_FILE="$LOG_DIR/ai-polls-agent.runlock"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting AI Polls Agent run" | tee "$LOG_FILE"

set +e
flock -n "$LOCK_FILE" "$PYTHON_BIN" -m src.main "$@" 2>&1 | tee -a "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
set -e

if [ "$STATUS" -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Run complete. Open /polls in ai-home-hub." | tee -a "$LOG_FILE"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Run FAILED (exit $STATUS). See $LOG_FILE" | tee -a "$LOG_FILE"
fi

exit "$STATUS"
