#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
bash "$ROOT/setup/lambda_k3s_hami.sh"
bash "$ROOT/setup/lambda_apply_slices.sh"
