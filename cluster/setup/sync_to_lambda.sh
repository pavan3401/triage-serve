#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

if [[ -z "${LAMBDA:-}" || -z "${LAMBDA_SSH_KEY:-}" || ! -f "$LAMBDA_SSH_KEY" ]]; then
  echo "Set LAMBDA and LAMBDA_SSH_KEY in .env" >&2
  exit 1
fi

echo "Sync Mac → $LAMBDA:~/serving/"
ssh -i "$LAMBDA_SSH_KEY" -o StrictHostKeyChecking=accept-new "$LAMBDA" "mkdir -p ~/serving"
rsync -avz -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.env' \
  ./ \
  "$LAMBDA:~/serving/"

# gateway/ and router/ (the actual control plane) live in ../control, not here
# -- orch-serve.yaml runs `python -m gateway.serve` from this same ~/serving
# hostPath mount, so both packages must land there too, or the gateway pod
# fails at import time with no gateway/router on the box at all.
CONTROL="$ROOT/../control"
if [[ -d "$CONTROL/gateway" && -d "$CONTROL/router" ]]; then
  rsync -avz -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" \
    --exclude '__pycache__' --exclude '.pytest_cache' \
    "$CONTROL/gateway/" "$LAMBDA:~/serving/gateway/"
  rsync -avz -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" \
    --exclude '__pycache__' --exclude '.pytest_cache' \
    "$CONTROL/router/" "$LAMBDA:~/serving/router/"
  # gateway/serve.py does `from app.guardrails import inspect`, so the
  # (small) app package it needs must be on the box as well -- found the
  # hard way: on a fresh instance the gateway pod crash-looped with
  # `ModuleNotFoundError: No module named 'app'`.
  APP="$ROOT/../app"
  if [[ -f "$APP/guardrails.py" ]]; then
    ssh -i "$LAMBDA_SSH_KEY" -o StrictHostKeyChecking=accept-new "$LAMBDA" "mkdir -p ~/serving/app"
    rsync -avz -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" \
      "$APP/__init__.py" "$APP/guardrails.py" "$LAMBDA:~/serving/app/"
  else
    echo "WARNING: ../app/guardrails.py not found -- gateway pod will fail to import." >&2
  fi
else
  echo "WARNING: ../control/gateway or ../control/router not found -- the" >&2
  echo "orch-serve pod on the box will have no control-plane code to import." >&2
fi
echo "Synced."
