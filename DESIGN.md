# DESIGN: an SRE agent served through a GPU control plane

Everything below was measured on the running system, not estimated. File
pointers and pasted scrapes are real. Where something was not exercised, I say
so.

The document is organised as a design review of a GPU serving system, in nine
parts: 0 what is served, 1 capacity on paper, 2 cluster design, 3 guard and
admit, 4 place, 5 queue, 6 hop (KV movement) and warm-up, 7 wiring, 8 proof.
"Upstream" means the lab code the control plane started from (see the
README's credits).

## Part 0: What is being served

**A tool-using SRE incident-response agent.** A user turn ("investigate X")
starts a loop: think, call a tool, observe, answer (`app/agent.py`). The tools
are a mix of real cluster introspection (`k8s_get_pods`, `k8s_get_events`,
`k8s_get_pod_logs`, `k8s_describe`, `k8s_scale`, `k8s_rollout_restart`, all
real `kubectl` calls against the same k3s cluster the agent's own LLM calls run
on) and SRE reasoning tools ported from `k8s-incident-response-agent`
(`classify_severity`, `lookup_runbook` against a 15-entry runbook JSON,
`get_incident_timeline`, `create_incident_report`).

The system prompt plus tool schema (`app/agent.py:SYSTEM_PROMPT`) is a fixed,
shared prefix, hashed once (`SHARED_PREFIX_HASH`) and sent on every call. Each
incident's own conversation grows every step: the "multi-step decode with a
lengthening prefill" shape typical of agent workloads. Interactive traffic is
one incident's live loop. Batch traffic is the periodic "cluster health sweep"
single-turn call in `app/load_gen.py`.

Every request, whether an agent step or a health sweep, goes through
`POST /v1/chat/completions` on the control-plane gateway
(`control/gateway/serve.py`), never straight to vLLM.

## Part 1: Capacity on paper

GPU: **NVIDIA A100-SXM4-40GB** (Lambda `gpu_1x_a100_sxm4`), driver 580.105.08.
Each worker is a HAMi slice of that one card, not the whole 40 GiB, so the
arithmetic is done against the slice.

| | prefill/text (`Qwen/Qwen2.5-3B-Instruct`) | decode/vision (`Qwen/Qwen2.5-VL-3B-Instruct`) |
|---|---|---|
| HAMi slice (visible HBM) | 16384 MiB | 16384 MiB |
| `--gpu-memory-utilization` | 0.50 | **0.80** (raised from the stock 0.50, see Part 6) |
| Weights (bf16, measured) | 5.79 GiB | 7.15 GiB |
| Available KV cache memory (measured) | **1.68 GiB** | **2.33 GiB** |
| GPU KV cache size (measured) | **48,800 tokens** (3050 blocks x 16) | **67,856 tokens** |
| vLLM's own "Maximum concurrency for 2,048 tokens" | **23.83x** | **33.13x** |

`kv_bytes_per_token` for `Qwen2.5-3B-Instruct` (config.json:
`hidden_size=2048, num_attention_heads=16 -> head_dim=128, num_key_value_heads=2`
(GQA), `num_hidden_layers=36`, bf16):

```
kv_bytes_per_token = 2 (K,V) x num_layers x num_kv_heads x head_dim x bytes/elem
                   = 2 x 36 x 2 x 128 x 2  = 36,864 bytes  (~36 KiB/token)
```

Cross-check against the measurement: `1.68 GiB / 48,800 tokens = 36,977
bytes/token`, which matches the formula to within block rounding. GQA (2 KV
heads instead of 16) is why this is small; without it the cost would be 8x
higher per token and concurrency would drop to about 3x on the same slice.

**Did switching models change bytes per token?** The two engines run different
models, so I checked. `Qwen2.5-VL-3B-Instruct`'s own `config.json`, pulled from
the decode pod, has `hidden_size=2048, num_attention_heads=16,
num_key_value_heads=2, num_hidden_layers=36`: the same text-decoder
architecture as `Qwen2.5-3B-Instruct` (the VL model reuses that backbone and
adds a vision encoder). So `kv_bytes_per_token` is the same 36,864 on both
engines, and the decode-side measurement agrees:
`2.33 GiB / 67,856 tokens = 36,978 bytes/token`. What differs between the two
engines is the weights (5.79 vs 7.15 GiB) and the vision encoder's activation
and cache overhead. That is why decode needed `gpu-memory-utilization=0.80`
instead of `0.50` to reach a usable KV budget (Part 6).

`max_concurrent_seqs ~ (slice_HBM - weights - activations) / (kv_bytes_per_token x max_len)`
at `max_len=2048` (the deployed `--max-model-len`) gives **23.83x** for the text
engine. That is vLLM's own number, and it matches the formula.

**At the length the app actually sends:** across 1,093 completed incident turns
(`metrics/requests.jsonl`, `metrics/all_requests_timed.jsonl`), the estimated
prompt tokens at the final step were **p50 = 1,781, p90 = 2,083, max = 2,267**
after only 3-4 agent steps (`plots/prompt_growth.png`). That is already at the
2,048 `max-model-len` ceiling, so the agent's step budget is capped by
`max-model-len`, not the other way round. Using p90 = 2,083 instead of 2,048
gives `48,800 / 2,083 = 23.4x`, barely different.

**Which limiter did I expect first, and was I right?** I expected **KV**, given
a deliberately small 16 GiB slice. **I was wrong for this app**, and the
experiments show why (`FINDINGS.md` F11-F13, H1):

- KV budget decides whether an engine can *start* (the boot-time OOM in
  Part 6), but under agent load KV peaked at only **24-32%** with 128
  concurrent incidents, and the engine never preempted.
- What saturated was **GPU compute**: throughput plateaus at ~1,370 generated
  tok/s from 32 concurrent incidents on, with GPU utilization at 86-87%. Extra
  clients become queueing delay (step p50 0.84 -> 1.97 -> 4.10 s at
  C = 32 / 64 / 128).
- KV is low because **the system and tool prefix is shared**. The 23.83x paper
  ceiling assumes every sequence stores its own 2,048 tokens. With the same
  load shape and no sharing, KV hits 99% and the engine preempts (EXP-3). So
  the formula above is the *unshared* bound, and this workload runs about 20x
  below it.
- Not isolated: whether the compute plateau is the A100 itself or the HAMi
  `gpucores: 55` cap on this slice. Interconnect never came into play (one
  GPU).

## Part 2: Cluster design

- **GPU**: 1x A100-SXM4-40GB, x86 (deliberately not one of Lambda's ARM/Grace
  GH200 instances, which have known compatibility gaps with this stack; every
  script here targets x86 Lambda Stack). $1.99/hr. Chosen over an A10 24GB
  because two HAMi-sliced 16 GiB workers plus a 3B and a 3B-VL model each need
  real KV headroom (a 24 GB card sliced two ways leaves ~12 GB slices, tighter
  than what already failed once at 16 GB, see Part 6). Chosen over an H100 or
  8x A100 because the aim was a budget where the limits are reachable, not
  peak serving. On this slice compute does saturate (GPU ~87% at ~1,370
  generated tok/s from 32 concurrent incidents), so a larger GPU would raise
  the plateau; I did not test that.
- **Model**: `Qwen/Qwen2.5-3B-Instruct` (text). Its weights (5.79 GiB) leave
  real room for KV on a 16 GiB slice, and GQA (2 KV heads) keeps
  `kv_bytes_per_token` low at 36 KiB.
- **Topology**: two workers, one GPU, HAMi-sliced 16384 MiB each
  (`cluster/k8s-config/hami/hami-lambda.yaml`). This is a **capability split**
  (text vs vision), not a same-model prefill/decode split.
  `cluster/k8s-config/gateway/orch-serve.yaml` sets `LAB_SPLIT=capability`, so
  `Router._place_capability` sends every text request to the `vllm-prefill` pod
  for *both* its prefill and decode phase (`Handoff(prefill=picked,
  decode=picked)`: same worker, no hop, KV already there). The cross-worker hop
  path is exercised deliberately (Part 6), not as the default traffic shape.
- **Concurrency**: `--max-model-len 2048`, chunked prefill enabled
  (`max-num-batched-tokens=2048`), default `--max-num-seqs`. I did not raise
  `max-num-seqs`: the running batch peaked at 41 sequences (`FINDINGS.md`
  EXP-1), so the default was never the constraint; compute saturated first
  (Part 1).
- **Hop backend**: `control/router/mooncake.py`, an HTTP store that records
  `{src, dst, req_id, tokens, prefix, backend}` per hop. **This is
  bookkeeping, not a tensor transfer.** When a hop happens (Part 6), the
  *decode* engine independently completes the request from scratch; nothing is
  physically copied between GPU slices.
- **Overflow**: `control/router/overflow.py`'s `Overflow(local=gw)` has no
  `OVERFLOW_BASE_URL` in this deployment, so overflow is a no-op and every
  503/529 stays local. No hosted overflow model was run.
- **Scaling**: KEDA `prefill-scaler` / `decode-scaler`, `min=1 max=2`, trigger
  `prometheus`. Under the heaviest soak, `prefill-scaler` went `READY=True` and
  KEDA created second `vllm-prefill`/`vllm-decode` pods, but both stayed
  **Pending** with `0/1 nodes are available: 1 node(s) didn't have free ports
  for the requested pod ports`. `hami-lambda.yaml` binds each container to a
  fixed `hostPort` (8000/8001) for direct-to-node access; on a **single-node**
  k3s cluster a second replica can never bind the same hostPort, so `max=2` is
  unreachable here regardless of GPU memory. If I scaled for real, the pool to
  add is still **decode slots** first (`control/router/planner.py:plan()`
  watches `decode_slots_used > 0.7 x decode_slots` and
  `uncached_prefill_tokens_in_flight > 0.7 x prefill_token_budget`, i.e. KV and
  token pressure per pool rather than a generic "traffic is up" signal). On a
  single node that would first require dropping `hostPort` in favour of the
  NodePort Services that already exist (`vllm-prefill`/`vllm-decode` on
  30800/30801), or adding a second node.

## Part 3: Guardrails, admit, stay vs leave

`app/guardrails.py:inspect(payload) -> Guard` (unchanged from upstream)
validates the model allowlist, clamps `max_tokens`, and checks prompt length
and image-vs-text shape. It runs before anything touches the router.

`control/gateway/admission.py:should_shed(req, snap) -> (shed, code, reason,
retry_after_s)` (new in this project, tested in
`control/tests/test_should_shed.py`) is a pure predicate over an aggregate
fleet `Snapshot`. Two checks:

1. `expected_wait_s = queue_depth x 0.2 s`. If that is more than half the
   request's `timeout_s`, shed `503 timeout_queue`.
2. If `kv_free_ratio < 8%` **and** the request's prefix is not already cached
   on that snapshot, shed `503 kv_free`. A request reusing a cached prefix is
   let through even under KV pressure.

It never retries or reroutes; that is `Router.pick`'s job, a second,
placement-time line of defence (`no_eligible_pod`, per-worker `timeout_queue`
and `kv_free` once a specific worker is being evaluated). Tenant protection
(`Gateway._over_cap`, 10,000 tokens/min/tenant, `429`) runs before
`should_shed`, so per-tenant limits are applied before fleet capacity limits.

## Part 4: Place

`control/router/router.py:Router.pick(pool, scorers, req, *, policy)`.
Policies: `random`, `least_loaded`, a `p2c`-style top-k random pick over
weighted scores, and `prefix_then_load` (sticky routing when prefix overlap is
at least 80% and a worker's in-flight tokens are under a sticky threshold).
Prefill and decode use different scorers (`PREFILL_SCORERS` includes
`score_prefix_cache`; `DECODE_SCORERS` does not).

**Found and fixed late in the project:** `score_queue_depth` was defined and
weighted (`WEIGHTS`) in the upstream code but never included in
`PREFILL_SCORERS`/`DECODE_SCORERS`, so queue depth had no effect on `pick`'s
scoring. I added it to both lists. `control/tests/test_epp_scorers.py` still
passes (38 tests overall), so no tested behaviour changed; an input that was
silently inert now counts. `_filter` treats an unhealthy or unknown worker as
ineligible rather than idle, falling back to unknown workers only when *all*
workers are unknown.

**Gap:** with exactly one worker per pool in every run, `pick` never had to
choose between candidates; `orch_sticky_total` and
`orch_pick_unknown_snapshot_total` both stayed at 0. The policy logic is
unit-tested (`control/tests/test_prefix_sticky.py`, `test_epp_scorers.py`), but
this deployment did not exercise multi-replica selection. That would need KEDA
to actually scale a pool to 2, which the hostPort limit prevents (Part 2).

## Part 5: Queue

`notebook/queue_kv_warmup.ipynb` is the executed, real-data walkthrough.
Headline findings:

- `control/gateway/queue.py:enqueue()` never buffers. It calls
  `worker.enqueue()` synchronously, and the real buffering and ordering happen
  inside vLLM's own continuous batching. `orch_replica_queue_depth` matters
  only as an *input to admission* (`should_shed`'s wait estimate), not as a
  queue once a request is placed.
- `vllm:num_preemptions_total` = **0** across every soak (4.3M cumulative
  prefill tokens, 216K generation tokens on the text engine). Accepted load
  stayed inside the measured KV budget.
- Shed counts from the continuous 240 s verification soak
  (`metrics/final_verified_soak.jsonl`):
  `orch_shed_total{reason="tenant_tokens",code="429"} = 61` out of 2,060
  gateway requests (1,999 completed). `plots/soak.png` is built from this run
  and shows shedding ramp up once the rotating batch-sweep tenants cross their
  10K tokens/min window, while admitted throughput stays healthy (8-14
  turns/5 s). The tenant cap was the only shed reason triggered by the agent
  workload; a real `kv_free` shed appeared only in the unshared-prefix
  experiment (below).
- **PagedAttention vs prefix cache: which saved memory?** The prefix cache, and
  there is a number for it. Block-table paging is always on and packs each
  sequence tightly, but it cannot make 40 sequences cheaper than 40 sequences.
  Sharing can. With the same 40 clients and 1,429-token prompt, KV peaked at
  **4.4%** when the prefix was shared (99% prefix-cache hit) and **99.3%** when
  it was not (10% hit), with 26 preemptions and 112 sheds (`FINDINGS.md` F13,
  `plots/prefix_ab.png`).
- **KV full after admit: shed at the door, or preempt?** Both happened, in that
  order, in the unshared arm: 110 `kv_free` sheds at the gateway and 26
  preemptions in the engine. The shed reads a scrape-lagged KV value, so
  already-admitted sequences keep growing and the engine still preempts. The
  door reduces preemption; it does not eliminate it (F14).
- Client-abort KV freeing was not exercised with a real aborted client:
  `FakeWorker.abort()` frees KV immediately, but `VLLMWorker.abort()` is a
  no-op today because the gateway holds no cancellable handle into a real vLLM
  request.

## Part 6: Hop, and is the replica warm?

**Default behaviour (same worker, do nothing).** Because
`LAB_SPLIT=capability`, every text request's prefill and decode phase land on
the same `vllm-prefill` pod (`Handoff(prefill=picked, decode=picked)`), so
`queue.enqueue()` takes the `handoff.decode is handoff.prefill` branch and
never calls `bus.transfer()`. This is the "KV is already there, do nothing"
case, and it is what over 99% of the real traffic did.

**Deliberate hop demo (different workers).** `app/load_gen.py --force-hop`
sends one request with `"kv_hop": true`, which sets `Request.force_phase=True`
and skips capability routing: prefill lands on the text engine, decode lands on
the vision engine, two different real GPU slices. The captured hop record
(`metrics/mooncake_hops_final.json`):

```json
{"src": "text-0", "dst": "vision-0", "req_id": "7ccad608-...", "tokens": 16,
 "backend": "mooncake", "prefix": "05e541972fcc9ed8"}
```

Caveat: because `VLLMWorker.enqueue()` always runs a full completion
regardless of `phase`, this hop costs a **second real inference** on the vision
engine (whose answer is what is returned) rather than a true KV-tensor
handoff. The Mooncake record proves the routing and accounting decision, not a
physical KV copy.

**Is the replica actually warm?** I killed the live `vllm-decode` pod and
measured recovery, then repeated it three times:

| Event | Measured |
|---|---|
| pod delete -> `/health` returns 200 | **104.8 / 106.9 / 106.9 s** (115 s in an earlier single run) |
| first real `/v1/chat/completions` after `/health` is green | **1.67 / 1.21 / 1.21 s** |
| requests 3-6 after that | **0.101-0.118 s** (median 0.105) |
| very first request to a freshly *deployed* pod | 1.70 s (same effect) |

Only the first request pays; the second is already warm. That is roughly
11-16x slower than steady state, on four observations
(`metrics/exp_warmup/summary.json`). "Ready" and "warm" are different states,
which is direct evidence against treating "weights are on the GPU" as the
readiness criterion.

This measurement hit `vllm-decode`'s own NodePort (`30801`) directly, on
purpose, to isolate the engine's warm-up latency from gateway overhead. That is
an instrumentation choice; `app/agent.py` never calls a worker directly.
`plots/soak.png` is a separate, later, continuous soak against the text engine
only and does not cover this decode-pod-kill window, so the table above, not
the plot, is the evidence for this claim.

**A real incident this surfaced (KV protection, not just warm-up).** The stock
manifest set `--gpu-memory-utilization 0.50` on both workers.
`Qwen2.5-VL-3B-Instruct`'s weights (7.15 GiB) plus its vision encoder cache
left **no** room for KV blocks on the 16 GiB slice, and `vllm-decode`
crash-looped 5 times with:

```
ValueError: No available memory for the cache blocks. Try increasing
`gpu_memory_utilization` when initializing the engine.
```

Fix: raise decode's `--gpu-memory-utilization` to `0.80`
(`cluster/k8s-config/hami/hami-lambda.yaml`), verified in its own logs
(`Available KV cache memory: 2.33 GiB`, `GPU KV cache size: 67,856 tokens`).
This is the single deliberate deviation from upstream's stock manifest.

## Part 7: Wiring

```
app/agent.py (think/tool/observe/answer)
        | POST /v1/chat/completions  (app/guardrails.py:inspect)
control/gateway/serve.py
        | should_shed (tenant cap, then fleet KV/queue check)
        | Router.pick (place)
        | gateway/queue.py:enqueue (same pod | mooncake transfer)
control/router/pools.py:VLLMWorker -> real vLLM /v1/chat/completions
        |
overflow gate (no-op in this deployment, no OVERFLOW_BASE_URL configured)
```

I confirmed the path with `curl http://127.0.0.1:8080/v1/models` and a real
chat completion through the tunnel before any agent code ran, so the engine was
known-good before the app was debugged. `metrics/gateway_metrics_final.txt` is
a later, cumulative snapshot taken after the verification soak.

## Part 8: Proof

Real soak: 5-8 concurrent SRE-agent incidents (3-4 steps each, unique tenant
per incident) plus a periodic single-turn "cluster health" batch sweep every
1.5-4 s. Across the whole project (development soaks plus the continuous 240 s
verification run, `metrics/final_verified_soak.jsonl`): **1,093 logged
interactive incidents** and **~3,200 underlying gateway requests**, of which
the verification run alone accounts for **2,060 requests / 1,999 completed /
61 `tenant_tokens` sheds**. The incidents are about this cluster itself
(`vllm-decode restarted`, `gateway shed tenant_tokens`, `kv_free pressure`,
`mooncake hops`), so the app and the thing being load-tested are the same
system.

**What is shared vs unique.** The system prompt plus full tool schema
(`app/agent.py:SYSTEM_PROMPT`, hashed once as `SHARED_PREFIX_HASH`) is
identical and sent on every call from every incident. Each incident's own
user turn and growing tool-observation history is unique.

**What gets stopped, and where:**

- Guardrails: 3 live `400 prompt_too_long` once an incident's transcript passed
  8,000 characters; the check runs before the tenant cap every time
  (`gateway/serve.py:prepare_chat`). Beyond the guard, the *engine* rejects
  anything over `max-model-len`; those now surface as `400 engine_rejected`
  (`orch_engine_rejected_total`) instead of a fake 200 (FINDINGS P6).
- Admit (tenant cap): **61** `429 tenant_tokens` sheds in the verification soak
  (104 cumulative across the project).
- Admit / place (KV): **110** `503 kv_free` sheds in the unshared-prefix arm
  (EXP-3), plus 2 `no_eligible_pod`; zero under the normal agent workload
  because KV never got near the threshold.
- Place: 0 `no_eligible_pod` sheds under the agent workload; both pools always
  had exactly one healthy worker.
- Queue / engine: 0 preemptions under the agent workload; **26** when prefix
  sharing was removed (EXP-3); 0 `slice_oom`.

**Preventing work that will time out.** `should_shed`'s `timeout_queue`
branch, before placement.

**Protecting KV.** Two places, deliberately not conflated: (a) `should_shed`'s
`kv_free < 8%` check before a request is placed, and (b)
`--gpu-memory-utilization` at engine boot, which is what actually failed in the
real incident. Admission-time protection cannot fix a slice that cannot
initialise its KV pool at all.

**Prioritising interactive traffic.** Not implemented as a distinct priority
lane. Every request is `priority=1` by default
(`gateway/types.request_from_openai`). The `p99_spread` shed reason exists in
`SHED_REASONS`, but nothing in this traffic drove p99 to 4x p50 with a growing
queue, so it was never exercised.

**Stopping one tenant from owning the GPU.** `Gateway._over_cap`, 10K
tokens/min/tenant. It was the only admission control the agent workload
triggered (104 times cumulative, 61 in the verification soak).

**Hop, and what is not copied.** Part 6: only on a deliberately forced
request, and GPU KV tensors are never physically copied, only hop metadata.

**Eviction.** `control/router/kv_eviction.py` (`prefix_protect` policy,
unit-tested in `control/tests/test_kv_eviction.py`) drops old unique prefixes
before shared ones under KV pressure. Not exercised live
(`orch_kv_evict_total` stayed 0) because KV pressure was never sustained long
enough under the agent workload.

**Engine scheduler vs admit/place/queue.** The gateway decides whether and
where; vLLM's own scheduler (continuous batching, chunked prefill at
`max-num-batched-tokens=2048`) decides turn-by-turn packing once a request
arrives. Concurrent short and long incidents completed without starving each
other (see `notebook/queue_kv_warmup.ipynb`).

**What limited concurrency on this GPU for this app.** GPU compute, measured:
throughput is flat at ~1,370 generated tok/s from 32 concurrent incidents while
GPU utilization is 86-87%, KV never exceeds 32%, and nothing is shed or
preempted (`plots/wall.png`, `FINDINGS.md` F11-F12). KV *would* be the limiter
for an unshared workload: 40 clients with unique prompts drive it to 99% and
cause 26 preemptions (F13). Open: ~75 requests at C = 128 wait somewhere ahead
of the engine's scheduler, and I did not locate where (F15).

**Four production alerts:**

1. `orch_replica_kv_free_ratio < 0.15` sustained: a leading indicator before
   `kv_free` sheds start (the shed floor is 0.08; alert above it).
2. `rate(orch_shed_total{reason="tenant_tokens"}[5m]) > 0` for a single tenant:
   the one signal that actually fired (104 times cumulative); it separates
   "one noisy tenant" from real capacity loss.
3. `rate(vllm:num_preemptions_total[5m]) > 0`: any preemption means admission
   let in more than the engine could hold. It should be 0, as it was under the
   agent workload.
4. Time to first fast request after a pod restart above 5 s (the measured
   leftover warm-up was 1.2 s on a healthy restart): catches the "ready is not
   warm" gap from Part 6 before a user hits it.

**If I scale, which pool?** Decode-slot pressure, per
`control/router/planner.py`'s own thresholds (Part 2), rather than adding a
same-size replica blindly.

**At 10x traffic, what changes, and which three moves are wrong?** The right
move is to scale the decode pool first (KEDA `decode-scaler`, `max=2`) and
recompute the KV math for the new concurrency *before* raising any per-engine
limit. At 23.83-33.13x concurrency on a deliberately small slice, 10x traffic
means real KV contention, not just "more boxes." Three moves that look
reasonable and are not: (1) raising `--max-model-len` to fit longer agent
contexts without recomputing `max_concurrent_seqs`, which trades concurrency
directly for context length on a fixed slice; (2) assuming the Mooncake hop
path adds throughput, when it is bookkeeping and forcing more hops means
*more* real inference (Part 6); (3) raising `gpu-memory-utilization` further,
when decode is already at 0.80 after a real OOM at 0.50 and there is little
headroom left on a 16 GiB slice before activation memory is squeezed.
