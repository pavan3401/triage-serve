# FINDINGS: what I learned, and how sure I am

Every claim below points at a file or a scrape. Each row names its **evidence**,
in plain terms:

- **scrape / log line / timer**: read directly off the running system.
- **arithmetic**: worked out from values read directly (inputs shown).
- **proxy**: a stand-in for the real quantity (stated).
- **not shown**: believed, but nothing here demonstrates it.
- **to run**: an experiment is defined for it in section 4.

## 1. Headline findings

| # | Finding | Kind of evidence | Where |
|---|---|---|---|
| F1 | The vision engine could not start at `--gpu-memory-utilization 0.50`: weights (7.15 GiB) + encoder cache consumed the 8 GiB budget, leaving no KV blocks. Crash-looped 5x, fixed at 0.80 (2.33 GiB / 67,856 tok). **This is a boot-time config failure, not a load-induced limit.** | scrape / log / event | vLLM init `ValueError`, DESIGN Part 6 |
| F2 | `kv_bytes_per_token` = 36,864 B (2 x 36 layers x 2 KV heads x 128 x 2 B). Measured 36,977 B/token (1.68 GiB / 48,800) matches within 0.3%. Identical for text and VL models (same backbone). | arithmetic + log line | config.json on both pods, vLLM startup log |
| F3 | "Ready" is not "warm". After `/health` returned 200 (104.8 / 106.9 / 106.9 s after pod delete, plus 115 s in an earlier single run), the **first** request took 1.67 / 1.21 / 1.21 s, vs a 0.105 s steady median (range 0.101-0.118): 11-16x. The first request ever sent to a freshly deployed pod showed the same (1.70 s). Only that one request is slow; the second is already warm. | timer, n=4 | `metrics/exp_warmup/summary.json` |
| F4 | KEDA `max=2` is unreachable on this cluster: both engines bind a fixed `hostPort`, so a second replica never schedules (`FailedScheduling: didn't have free ports`). | scrape / log / event | `kubectl describe pod` event |
| F5 | Under the agent workload the engine never ran out of KV: zero preemptions across 4.3M prompt tokens in the soaks and across every concurrency level up to 128. (It does preempt when prefix sharing is removed: F13.) | scrape | `vllm:num_preemptions_total`, `metrics/exp_wall_clean` |
| F6 | The only shed reason that fired live was `tenant_tokens` (61 in the final 240 s run, 104 cumulative). It came from three reused batch-sweep tenant names crossing 10K tokens/min, a property of my load generator, not a capacity limit. | scrape / log / event | `orch_shed_total`, `final_verified_soak.jsonl` |
| F7 | Agent context after 3-4 steps: p50 1,781 / p90 2,083 / max 2,267 tokens, already at the 2,048 `max-model-len`. **These are chars/4 over the JSON transcript**, so they overestimate real tokens. | proxy (chars/4) | `plots/prompt_growth.png` |
| F8 | The "hop" is bookkeeping. `VLLMWorker.enqueue` ignores `phase`, so a forced hop runs a second full inference on the decode engine; Mooncake records metadata only. | code read + 2 hop records | `router/pools.py`, `mooncake_hops_final.json` |
| F9 | Capability routing means text requests never hop by default (prefill == decode worker). | scrape / log / event | `Router._place_capability` |
| F10 | A brand-new instance reproduced the same KV pools to the token (48,800 / 67,856; 23.83x / 33.13x), Grafana landed on the pinned NodePort 31495 on first install, and all Helm installs succeeded first time. The four setup scripts ran in ~8 minutes. | log line + timer | `kubectl logs`, `~/bringup.log` |
| F11 | **Throughput plateaus at ~1,360-1,400 generated tok/s from 32 concurrent incidents onward** (1,358 / 1,401 / 1,363 at C=32/64/128); step latency p50 then grows with concurrency (0.84 / 1.97 / 4.10 s), i.e. extra clients become queueing delay, not throughput. Reproduced by three separate sweeps. | scrape | `metrics/exp_wall_all.json`, `plots/wall.png` |
| F12 | **The limiter under agent traffic is GPU compute, not KV.** KV peaked at 24 / 30 / 32%, with 0 sheds and 0 engine-side waiting, while GPU utilization sat at 86-87% mean (90% max) at all three levels. The engine process used 54-66% of *one* core, so it is not host-CPU-bound. Not isolated from the HAMi `gpucores: 55` cap on this slice. | scrape (nvidia-smi, ps) | `metrics/exp_wall_clean/host_samples.txt` |
| F13 | **Prefix sharing is what keeps KV low.** Same 40 clients, same 1,429-token prompt, same 64-token output; only prefix sharing differs. Shared: KV peak 4.4%, prefix hit 99%, 0 preemptions, 0 sheds. Unique: KV peak 99.3%, hit 10%, **26 preemptions, 112 sheds (110 `kv_free`)**, 19 waiting, p50 1.31 -> 2.41 s. The paper ceiling (23.83x) is the *unshared* case; this app sits ~20x below it because ~85% of its tokens are shared. | scrape | `metrics/exp_prefix/summary.json`, `plots/prefix_ab.png` |
| F14 | **The KV shed fired live** for the first time, but did not prevent preemption: 110 `kv_free` 503s and still 26 engine preemptions, because the shed reads a scrape-lagged KV value while admitted sequences keep growing. The `kv_free` label cannot tell `should_shed`'s 92% rule from the router's 80% saturation filter; KV was above both. | scrape | `metrics/exp_prefix/summary.json` |
| F15 | At C=128, Little's law (28.4 steps/s x 4.1 s) implies ~115 requests in flight, but the engine showed only 39 running and 0 waiting, so ~75 requests were held somewhere ahead of the engine's scheduler. **Where is not located** (gateway, API-server front end, or tunnel). | arithmetic; location not shown | `metrics/exp_wall_clean` |
| F16 | The gateway's front door dropped 16 of 1,552 requests at C=128 ("Remote end closed connection"); none at C <= 64. Cause not isolated (the stdlib HTTP server's small listen backlog is the leading suspect). | scrape | `exp_wall_clean/summary.json` |
| F17 | The guardrail fired live: 3 requests rejected `400 prompt_too_long` once an incident's transcript passed 8,000 characters. | scrape | `exp_wall_*/summary.json` |

## 2. Hypothesis vs result

(I stated which limiter I expected before measuring; this table checks each
assumption against the results.)

| Hypothesis (stated before running) | Result | Verdict |
|---|---|---|
| **H1** KV is the first limiter for this app on this slice | KV peaked at 24-32% up to 128 concurrent incidents; GPU compute saturated first (F11, F12). KV only binds when prefix sharing is removed (F13). | **Refuted for this app**; true for an unshared workload |
| **H2** Compute and interconnect never bind | Compute does bind: GPU 86-90% utilized at the throughput plateau (F12). Single GPU, so interconnect was never in play. | **Refuted for compute** (not isolated from the HAMi core cap) |
| **H3** Prefix-aware routing saves prefill | Prefix *caching* inside one engine is worth a lot (F13, 85-99% hit). Prefix-aware *routing* across workers was never exercised: one worker per pool. | Caching shown; **routing not shown** |
| **H4** A readiness gate is needed beyond `/health` | First request after ready is 11-16x slower, n=4 (F3) | **Supported** |
| **H5** `should_shed` / the KV filter would protect the engine | 110 `kv_free` sheds fired live (F14), but 26 preemptions still happened | **Partly**: sheds fire, protection is lagged |

## 3. Process findings: bugs I found in my own work

These are findings too: each was invisible until a different check exposed it.

| # | What was wrong | How found | Fix |
|---|---|---|---|
| P1 | `should_shed` existed only locally. The GPU box ran upstream's original `admission.py` for the whole first soak, so early "admission" numbers came from tenant cap only. | grep on the box while syncing another fix | Deployed; canonical soak re-run on the fixed code |
| P2 | `score_queue_depth` was weighted but absent from `PREFILL_SCORERS`/`DECODE_SCORERS`: queue depth never influenced placement. | Re-reading `router.py` to check which scorers were active | Added to both lists |
| P3 | `sync_to_lambda.sh` only synced `cluster/`, never `control/gateway` or `control/router`. A fresh clone would deploy a gateway pod with no gateway code. | Checking the docs against the scripts they describe | Script syncs both. **My first verification was incomplete**: I deleted the packages on a box that still had `app/` from an earlier sync, so a second gap (P4) survived until a truly fresh instance |
| P4 | The gateway imports `app.guardrails`, but the sync script never shipped `app/`. On a fresh instance `orch-serve` crash-looped with `ModuleNotFoundError: No module named 'app'`. | Bringing up a brand-new instance with only the repo's own scripts | Script now also syncs `app/__init__.py` and `app/guardrails.py` |
| P5 | `day2_observability.sh` runs `python3 -m observability.dashboards`, but the `observability/` package was never copied into `cluster/`. The script aborted before creating the dashboards ConfigMap or printing the Grafana password. | Same fresh bring-up | Package added to `cluster/`; manual recovery documented in `GETTING_STARTED.md` |
| P6 | **The gateway turned engine rejections into HTTP 200.** `VLLMWorker._post` returned the engine's 4xx error body as if it were a completion; the gateway wrapped it into `200 {"id":..}` and counted it completed. A 3,152-token prompt against a 2,048 limit looked like a success. | A load run whose "completed" count rose while the engine's own counters sat frozen | `EngineRejected` raised and mapped to the engine's status; new `orch_engine_rejected_total`; regression test; verified live (HTTP 400) |
| P7 | **The agent changed the cluster it was being measured on.** Under a 128-client sweep, `k8s_rollout_restart` ran against `vllm-decode` (Kubernetes `managedFields`: `kubectl-rollout` at 23:15:53; nobody ran it by hand) and `vllm-prefill` flapped 2 -> 0 -> 1 -> 2, killing the working engine mid-run (523 spurious 503s). The prefill flap is not attributable from the records; KEDA's threshold-1 trigger legitimately asked for 2. | Odd ReplicaSet hashes and `Scaled down ... from 2 to 0` events after a sweep | Mutating tools are off by default (`SRE_ALLOW_MUTATIONS=1` to enable); KEDA paused at 1 replica for measurement; sweep re-run clean |
| P8 | **My own experiment code had measurement bugs**, each caught by checking a number that looked wrong: shed clients re-submitted instantly (measured the load generator); `kv_usage = 0.0` was treated as missing; digit-suffixed "words" tokenized to 3,152 tokens, so the first prefix run measured only engine rejections and reported them as successes. | Frozen engine counters; implausible identical latencies | Backoff on shed; explicit None checks; prompts calibrated against the engine's `/tokenize` and refused if they would not fit |
| P9 | `soak.png` concatenated separate runs; matplotlib drew a line across real idle gaps, implying a traffic decay that never happened. | Looking at the plot | Plot now built from one continuous run |
| P10 | An early plot claim ("shows the 115 s outage") was not supported by the plotted run. | Plot gap investigation (P9) | Claim removed; the timing table is cited instead |

## 4. Experiments

All run on a fresh A100-40GB instance; harness in `app/experiments.py`, raw data
in `metrics/exp_*`, figures in `plots/`.

### EXP-1: the concurrency wall, with engine KV sampled at 1 Hz
- **Question:** at what concurrency does KV fill, shed, or preempt?
- **Method:** unique tenant per incident, engine metrics scraped every second.
  Three sweeps: C = 2, 8, 16, 32 (60 s); C = 64, 128 (60 s); and a **clean**
  re-run of C = 32, 64, 128 (45 s) with mutating tools off, KEDA paused, and the
  engine warmed, because the middle sweep had run while the agent could still
  restart deployments (P7).
- **Result (clean sweep):**

| C | step p50 / p95 (s) | gen tok/s | KV peak | running peak | GPU util (mean) | sheds | preempt |
|---|---|---|---|---|---|---|---|
| 32 | 0.84 / 1.85 | 1,358 | 23.8% | 26 | 87% | 0 | 0 |
| 64 | 1.97 / 2.98 | 1,401 | 29.8% | 41 | 87% | 0 | 0 |
| 128 | 4.10 / 5.11 | 1,363 | 32.1% | 39 | 86% | 0 | 0 |

  Earlier points: C = 2 / 8 / 16 gave 157 / 527 / 918 tok/s (KV 5 / 8 / 13%).
  Throughput climbs to C = 32 and is flat after. See F11, F12, F15, F16.

### EXP-2: warm vs cold, repeated
- **Method:** delete the decode pod; time to `/health` 200; then 6 immediate
  requests; 3 repeats, 3 warm requests before each kill.
- **Result:** ready in 104.8 / 106.9 / 106.9 s; first request 1.67 / 1.21 / 1.21 s;
  requests 3-6 median 0.105 s. See F3.

### EXP-3: is prefix sharing what keeps KV low?
- **Method:** C = 40, 40 s per arm, 1,429-token prompt, 64-token output;
  `shared` = identical prompt body plus a one-line question, `unique` = a fresh
  random word sequence per request. Prompt length verified against the engine's
  `/tokenize` before the run (an earlier attempt with an uncalibrated prompt
  measured nothing: P8).
- **Result:** see F13, F14 and `plots/prefix_ab.png`. Throughput in generated
  tokens/s is *not* comparable between arms: the shared arm's replies are short
  ("summarise in one line") while the unique arm's word-salad prompts make the
  model ramble to the 64-token cap.
