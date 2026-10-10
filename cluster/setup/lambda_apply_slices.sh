#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

MEM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ' || echo 40960)"
MANIFEST="$ROOT/k8s-config/hami/hami-lambda.yaml"
if [[ "${MEM_MIB:-0}" -gt 50000 ]]; then
  echo "GPU ${MEM_MIB} MiB → D5-shaped slices 22528 / 27648"
  sed -e 's/nvidia.com\/gpumem: "16384"/nvidia.com\/gpumem: "22528"/' \
      -e '0,/nvidia.com\/gpumem: "22528"/! s/nvidia.com\/gpumem: "16384"/nvidia.com\/gpumem: "27648"/' \
      "$MANIFEST" | kubectl apply -f -
else
  echo "GPU ${MEM_MIB} MiB → two × 16384 MiB slices"
  kubectl apply -f "$MANIFEST"
fi

kubectl apply -f "$ROOT/k8s-config/gateway/orch-serve.yaml"
kubectl apply -f "$ROOT/k8s-config/router/keda-prefill.yaml"
kubectl apply -f "$ROOT/k8s-config/router/keda-decode.yaml"
kubectl apply -f "$ROOT/k8s-config/ui/open-webui.yaml"

echo "== waiting for workloads =="
kubectl rollout status deploy/vllm-prefill --timeout=30m || true
kubectl rollout status deploy/vllm-decode --timeout=30m || true
kubectl rollout status deploy/mooncake-store --timeout=3m || true
kubectl rollout status deploy/orch-serve --timeout=5m || true
kubectl rollout status deploy/open-webui --timeout=5m || true

echo
kubectl get deploy,svc,scaledobject
echo
echo "PREFILL_URLS=http://127.0.0.1:8000"
echo "DECODE_URLS=http://127.0.0.1:8001"
echo "LAB_TOPOLOGY=disaggregated"
echo "KV_BACKEND=mooncake"
echo "MOONCAKE_URL=http://127.0.0.1:50051"
echo "ORCH=http://127.0.0.1:8080"
echo "WEBUI=http://127.0.0.1:30030   ChatGPT-like UI → orch-serve → local vLLM / Superlinked on 503"
echo "Next: bash setup/smoke_sliced.sh"
