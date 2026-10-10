#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${OVERFLOW_BASE_URL:-}" ]]; then
  echo "OVERFLOW_BASE_URL unset — skip"
  exit 0
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export TRACE_PATH="${TRACE_PATH:-$ROOT/traces/requests.jsonl}"
exec python "$ROOT/setup/overflow_smoke.py"
