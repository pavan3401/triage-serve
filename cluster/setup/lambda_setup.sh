#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== GPU =="
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

echo "== venv =="
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt
pip install "vllm>=0.28"

echo "== verify =="
python -c "
import torch, vllm
print('torch', torch.__version__)
print('cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')
print('vllm', vllm.__version__)
"
echo "Setup done. Next: bash setup/lambda_cluster.sh"
