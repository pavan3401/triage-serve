#!/usr/bin/env bash
set -euo pipefail

TEXT="${PREFILL_URLS:-http://127.0.0.1:8000}"
VISION="${DECODE_URLS:-http://127.0.0.1:8001}"
TEXT="${TEXT%%,*}"
VISION="${VISION%%,*}"
TEXT="${TEXT//YOUR_LAMBDA_IP/127.0.0.1}"
VISION="${VISION//YOUR_LAMBDA_IP/127.0.0.1}"

echo "== two models =="
curl -sf "${TEXT}/v1/models" >/dev/null
curl -sf "${VISION}/v1/models" >/dev/null
echo "text ${TEXT}  vision ${VISION}"

echo "== nvidia-smi (expect two compute procs) =="
nvidia-smi --query-compute-apps=pid,used_gpu_memory --format=csv || true

export PREFILL_URLS="$TEXT"
export DECODE_URLS="$VISION"
export LAB_SPLIT=capability
export LAB_TOPOLOGY=disaggregated
export TEXT_MODEL="${TEXT_MODEL:-Qwen/Qwen2.5-3B-Instruct}"
export VISION_MODEL="${VISION_MODEL:-Qwen/Qwen2.5-VL-3B-Instruct}"
export TRACE_PATH="${TRACE_PATH:-traces/requests.jsonl}"

python - <<'PY'
from router.overflow import Overflow
from router.router import Router
from gateway.admission import Gateway
from gateway.metrics import METRICS
from router.pools import build_pools
from gateway.types import Request

METRICS.reset()
text_pool, vision_pool = build_pools()
assert text_pool[0] is not vision_pool[0]
gw = Gateway(Router(text_pool, vision_pool), tokens_per_min=10_000_000)
overflow = Overflow(local=gw)

def ping(name: str, capability: str) -> None:
    resp = overflow.send(
        Request(
            id=name,
            arrival_t=0.0,
            priority=1,
            prompt_tokens=16,
            max_new_tokens=4,
            prefix_hash="lab",
            timeout_s=60.0,
            tenant="lab",
            capability=capability,
        )
    )
    hop = resp.body.get("kv_hop")
    print(f"{capability} via={resp.via} status={resp.status} worker={getattr(resp.handoff.prefill if resp.handoff else None, 'id', None)} kv_hop={hop}")
    if resp.status != 200:
        raise SystemExit(f"{capability} failed status={resp.status}")
    if hop:
        raise SystemExit("capability split must not Mooncake-hop across models")

ping("text-ping", "text")
ping("vision-ping", "vision")
if METRICS.kv_transfer_total != 0:
    raise SystemExit("kv_transfer should stay 0 across different models")
print("SLICED SMOKE PASS")
PY
