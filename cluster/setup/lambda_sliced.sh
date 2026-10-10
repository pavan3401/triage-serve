#!/usr/bin/env bash
set -euo pipefail

TEXT_MODEL="${TEXT_MODEL:-Qwen/Qwen2.5-3B-Instruct}"
VISION_MODEL="${VISION_MODEL:-Qwen/Qwen2.5-VL-3B-Instruct}"
PREFILL_PORT="${PREFILL_PORT:-8000}"
DECODE_PORT="${DECODE_PORT:-8001}"
MOONCAKE_PORT="${MOONCAKE_PORT:-50051}"
MAX_LEN="${MAX_MODEL_LEN:-2048}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MEM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ' || echo 81920)"
if [[ -z "${UTIL_PREFILL:-}" || -z "${UTIL_DECODE:-}" ]]; then
  if [[ "${MEM_MIB:-0}" -le 50000 ]]; then
    UTIL_PREFILL="${UTIL_PREFILL:-0.40}"
    UTIL_DECODE="${UTIL_DECODE:-0.42}"
    echo "GPU ${MEM_MIB} MiB → two slices ${UTIL_PREFILL} + ${UTIL_DECODE} (7B must fit twice)."
  else
    UTIL_PREFILL="${UTIL_PREFILL:-0.34}"
    UTIL_DECODE="${UTIL_DECODE:-0.40}"
    echo "GPU ${MEM_MIB} MiB → two slices ${UTIL_PREFILL} + ${UTIL_DECODE} (D5-shaped, 1p+1d)."
  fi
fi

mkdir -p /tmp
echo "== mooncake store :${MOONCAKE_PORT} =="
python -c "from router.mooncake import main; main(port=int('${MOONCAKE_PORT}'))" >/tmp/mooncake.log 2>&1 &
echo $! >/tmp/mooncake.pid

wait_http() {
  local url="$1" n=0
  until curl -sf "$url" >/dev/null 2>&1; do
    n=$((n + 1))
    if [[ "$n" -gt 90 ]]; then
      echo "timeout waiting for $url"
      return 1
    fi
    sleep 2
  done
}

serve() {
  local name="$1" port="$2" util="$3" model="$4"
  echo "== vllm ${name} :${port} util=${util} model=${model} =="
  vllm serve "$model" \
    --host 0.0.0.0 \
    --port "$port" \
    --enable-prefix-caching \
    --scheduling-policy priority \
    --max-model-len "$MAX_LEN" \
    --gpu-memory-utilization "$util" \
    --tensor-parallel-size "${TENSOR_PARALLEL_SIZE:-1}" \
    --pipeline-parallel-size "${PIPELINE_PARALLEL_SIZE:-1}" \
    --data-parallel-size "${DATA_PARALLEL_SIZE:-1}" \
    ${SPECULATIVE_CONFIG:+--speculative-config "$SPECULATIVE_CONFIG"} \
    >/tmp/vllm-${name}.log 2>&1 &
  echo $! >/tmp/vllm-${name}.pid
}

serve text "$PREFILL_PORT" "$UTIL_PREFILL" "$TEXT_MODEL"
wait_http "http://127.0.0.1:${PREFILL_PORT}/v1/models"
serve vision "$DECODE_PORT" "$UTIL_DECODE" "$VISION_MODEL"
wait_http "http://127.0.0.1:${DECODE_PORT}/v1/models"
wait_http "http://127.0.0.1:${MOONCAKE_PORT}/health" || true

echo
echo "PREFILL_URLS=http://127.0.0.1:${PREFILL_PORT}   # text ${TEXT_MODEL}"
echo "DECODE_URLS=http://127.0.0.1:${DECODE_PORT}    # vision ${VISION_MODEL}"
echo "LAB_SPLIT=capability"
echo "LAB_TOPOLOGY=disaggregated"
echo "MOONCAKE_URL=http://127.0.0.1:${MOONCAKE_PORT}"
echo "nvidia-smi should show two vLLM processes on one GPU."
echo "Ctrl-C stops this shell — kill the PIDs in /tmp/vllm-*.pid /tmp/mooncake.pid"

wait
