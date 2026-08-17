#!/usr/bin/env bash
# Pre-flight configuration check.
#
#   bash scripts/check.sh            # offline checks
#   bash scripts/check.sh --remote   # also verify the Kvasir template (read-only)
#
# Same interpreter convention as scripts/run.sh: the shared ai-news environment
# by absolute path, overridable with PYTHON_BIN.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="${PYTHON_BIN:-/home/anton/miniconda3/envs/ai-news/bin/python}"

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" scripts/check_config.py "$@"
