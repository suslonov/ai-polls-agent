#!/usr/bin/env bash
# Run the test suite in the shared ai-news environment.
#
#   bash scripts/test.sh [pytest args...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="${PYTHON_BIN:-/home/anton/miniconda3/envs/ai-news/bin/python}"

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m pytest "$@"
