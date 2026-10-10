# Cluster: how the engine comes up

One Lambda GPU instance, `gpu_1x_a100_sxm4` (NVIDIA A100-SXM4-40GB, x86_64,
$1.99/hr) — chosen over A10 24GB because two HAMi-sliced 16 GiB workers plus a
3B/3B-VL model each need real KV-cache headroom (see `../DESIGN.md` Part 1);
over H100/8x-A100 because the goal was a budget where the limits are
reachable, not the fastest possible serving. Measured on this slice, compute
does saturate (GPU ~87% at ~1,370 tok/s), so a bigger GPU would raise the
plateau; that was not tested. Deliberately not one of Lambda's ARM/Grace (GH200) instances — those
have known compatibility gaps with this stack (community reports of
driver/CUDA-wheel mismatches), and
every script here (k3s's nvidia runtime, HAMi, vLLM's CUDA build) is
validated against x86 Lambda Stack images, so the ARM risk was not worth
taking.

The stack is upstream's (k3s + HAMi GPU slicing + Mooncake KV + KEDA +
Prometheus/Grafana; see the README's credits), brought up unmodified except for
the changes listed below.
Bring-up, in order, on the GPU box:

```
bash setup/lambda_setup.sh        # venv, vLLM
bash setup/lambda_cluster.sh      # k3s + HAMi + KEDA, then applies k8s-config/
bash setup/smoke_sliced.sh        # sanity check
bash setup/day2_observability.sh  # Prometheus + Grafana + DCGM + dashboards
```

On the Mac: `setup/sync_to_lambda.sh` rsyncs this tree to `~/serving` on the
box (that remote directory name is intentional — `k8s-config/gateway/orch-serve.yaml`
and `.../mooncake/mooncake.yaml` hostPath-mount `/home/ubuntu/serving`), then `setup/ssh.sh` tunnels the ports below.

## Topology

Two workers on one GPU, HAMi-sliced 16384 MiB each (`k8s-config/hami/hami-lambda.yaml`):

| Worker | Model | Port | Role |
|---|---|---|---|
| `vllm-prefill` | `Qwen/Qwen2.5-3B-Instruct` | 8000 (NodePort 30800) | text capability |
| `vllm-decode` | `Qwen/Qwen2.5-VL-3B-Instruct` | 8000 (NodePort 30801) | vision capability |

`gateway/orch-serve.yaml` sets `LAB_SPLIT=capability`, so `Router._place_capability`
sends every text request to `vllm-prefill` for **both** its prefill and decode
phase (`Handoff(prefill=picked, decode=picked)`) — same worker, no hop, KV
already there. That is the default, normal path for this text-only SRE agent.

To exercise an actual cross-worker hop, a
request can set `"kv_hop": true`, which sets `Request.force_phase=True` and
skips capability routing: prefill lands on `vllm-prefill` (text engine),
decode lands on `vllm-decode` (vision engine) — two different real GPU slices,
a real `router.mooncake` hop record, and the final tokens come from the vision
engine's text-mode completion. `app/load_gen.py --force-hop` fires exactly one
of these for the DESIGN.md proof; it is not the default traffic shape; most requests never hop.

**One real fix, documented not hidden:** the stock manifest sets
`--gpu-memory-utilization 0.50` on both workers (8192 MiB of the 16384 MiB
slice for weights+activations+KV). `Qwen2.5-VL-3B-Instruct`'s weights
(7.15 GiB) plus its vision encoder cache left **no** room for KV blocks —
`vllm-decode` crash-looped with `ValueError: No available memory for the
cache blocks.` (5 restarts, real logs in `../metrics/`). I raised decode's
`--gpu-memory-utilization` to `0.80`, verified via its own logs
(`Available KV cache memory: 2.33 GiB`, `GPU KV cache size: 67,856 tokens`,
`Maximum concurrency for 2,048 tokens: 33.13x`). `vllm-prefill` stays at 0.50
(`Available KV cache memory: 1.68 GiB`, `48,800 tokens`, `23.83x`).

## Ports (tunnel to `127.0.0.1`, never expose the engine publicly)

| Service | Port |
|---|---|
| Gateway (`orch-serve`) | 8080 |
| Open WebUI | 30030 |
| Mooncake store | 50051 |
| Grafana | 31495 (pinned in `k8s-config/observability/grafana-values.yaml`; the chart otherwise assigns a random NodePort) |
| vLLM prefill (text) direct | 30800 |
| vLLM decode (vision) direct | 30801 |
| k3s API (for local `kubectl`) | 6443 |
