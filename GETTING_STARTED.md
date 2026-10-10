# Getting Started (step by step, for someone who has never seen this repo)

This runs a real 2-worker vLLM cluster on a real GPU and a tool-using SRE
agent in front of it. Budget **~30 minutes** the first time. On a fresh
instance the four setup scripts took about 8 minutes in my run (vLLM install
~2.5 min, k3s + HAMi + KEDA + both engines ~4 min); the rest is launching the
instance, model downloads on a slow day, and slack. Cost: **~$2/hour** while
the GPU instance is running, so terminate it when you're done (last section).

You will end up with **three terminal windows** open at once. That's normal;
each one is called out below.

## 0. Prerequisites

- macOS or Linux, Python 3.10+, `git`, `ssh`.
- (Optional but recommended) `kubectl` — lets the agent's cluster-investigation
  tools work from your laptop instead of only from inside the GPU box.
- A [Lambda Cloud](https://cloud.lambda.ai) account with credits.
- An SSH keypair. If you don't have one:
  ```bash
  ssh-keygen -t ed25519 -C "you@example.com" -f ~/.ssh/id_ed25519 -N ""
  cat ~/.ssh/id_ed25519.pub
  ```
  Copy that `ssh-ed25519 ...` line — you'll paste it into Lambda in the next step.

## 1. Launch the GPU instance

1. Log into [cloud.lambda.ai](https://cloud.lambda.ai) → **SSH keys** → add
   the public key you just printed.
2. **Instances** → **Launch instance**.
3. GPU type: **1x A100 (40 GB SXM4)** (`gpu_1x_a100_sxm4`). If unavailable in
   your region, `gpu_1x_a100` (40GB PCIe) works too. **Do not** pick an
   ARM/Grace (GH200) instance — this stack targets x86 and hasn't been
   validated there.
4. Base image: the default **Lambda Stack** image (already has NVIDIA
   drivers/CUDA/Python — don't pick plain Ubuntu).
5. Filesystem: "Don't attach" is fine, no persistent storage needed.
6. Select the SSH key from step 1. Launch, wait for "Running", copy the
   **public IP**.

You're paying by the hour starting now — see step 11 for teardown.

## 2. Point this repo at your instance

```bash
cd cluster
cp .env.example .env
```
Edit `cluster/.env`:
```bash
export LAMBDA=ubuntu@YOUR_INSTANCE_IP
export LAMBDA_SSH_KEY=$HOME/.ssh/id_ed25519
```

Sync the code to the box:
```bash
bash setup/sync_to_lambda.sh
```
This syncs `cluster/` itself **and** `../control/gateway` + `../control/router`
(the actual control plane) into `~/serving` on the box, since the gateway pod
imports both packages from that same directory — this is a from-repo-root
concern, not just `cluster/`'s own files. Expect it to end with `Synced.` (a
warning about a missing `HUGGING_FACE_HUB_TOKEN` secret, if any, is harmless —
both models here are ungated).

## 3. Open the tunnel (Terminal A — leave this open the whole time)

```bash
cd cluster
bash setup/ssh.sh
```
This drops you into a shell **on the GPU box**, in `~/serving`, and forwards
everything you'll need to `127.0.0.1` on your laptop (gateway `:8080`, vLLM
engines `:8000`/`:8001`, Mooncake `:50051`, Open WebUI `:30030`, Grafana
`:31495`, the k3s API `:6443`). **Leave this terminal open for the rest of the
guide** — closing it drops every tunnel.

## 4. Bring up the cluster (still in Terminal A, on the GPU box)

```bash
bash setup/lambda_setup.sh       # ~5-10 min: venv + vLLM install
bash setup/lambda_cluster.sh     # ~5-10 min: k3s + HAMi + KEDA, deploys both engines
bash setup/smoke_sliced.sh       # expect: SLICED SMOKE PASS
bash setup/day2_observability.sh # ~3-5 min: Prometheus + Grafana + dashboards
```

`day2_observability.sh` prints a **Grafana admin password** near the end —
copy it, you'll need it in step 9. (Username is `admin`.)

**If `vllm-decode` crash-loops** with `ValueError: No available memory for
the cache blocks` — this repo's manifest already fixes the known cause (the
vision model's weights don't fit an 8GB KV budget on a 0.50-utilization 16GB
slice), so a fresh clone shouldn't hit this.

Confirm both engines are actually healthy (not just "Running"):
```bash
curl -sf http://127.0.0.1:8000/health && echo prefill-ok
curl -sf http://127.0.0.1:8001/health && echo decode-ok
```

## 5. Set up your laptop's Python environment (Terminal B — new terminal)

```bash
cd /path/to/this/repo   # the folder with app/, control/, cluster/
python3 -m venv .venv
source .venv/bin/activate
pip install -r control/requirements.txt -r app/requirements.txt
export PYTHONPATH="$PWD:$PWD/control"
```

(Optional, for the agent's real `kubectl`-backed tools) get a kubeconfig that
talks to the tunnel from step 3:
```bash
scp -i ~/.ssh/id_ed25519 ubuntu@YOUR_INSTANCE_IP:/etc/rancher/k3s/k3s.yaml ~/.kube/config-lambda
export KUBECONFIG=~/.kube/config-lambda
kubectl get pods   # should list mooncake-store, orch-serve, vllm-prefill, vllm-decode, open-webui
```

## 6. Smoke-test the control plane + a real completion

```bash
curl -s http://127.0.0.1:8080/v1/models
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"text","messages":[{"role":"user","content":"Say hi in five words."}],"max_tokens":32}'
```
You should get back a real completion from `Qwen/Qwen2.5-3B-Instruct` (check
`"model"` in the response), not an error.

## 7. Run the SRE agent

```bash
python -m app.agent --incident "Check vllm-prefill's status and recent events." --max-steps 4
```
You'll see `Thought / Action / Action Input / Observation` lines as the agent
investigates the real cluster and calls the real gateway for each step. If
you see `ERROR gateway 429 ... tenant tokens/min cap` — that's real admission
control working (10K tokens/min per tenant); wait ~60s or add `--tenant
someone-else` and try again.

## 8. Run a soak and build the plots/notebook

```bash
python -m app.load_gen --incidents 6 --workers 4 --max-steps 3 --duration-s 90 --force-hop
python -m app.make_plots            # writes plots/soak.png, plots/prompt_growth.png
python -m ipykernel install --user --name python3   # once
python notebook/build_notebook.py   # executes notebook/queue_kv_warmup.ipynb with real data
```

## 8b. Run the experiments (optional, ~15 minutes of GPU)

These are what `FINDINGS.md` is built from. Run them one at a time (they share
one GPU, so overlapping them distorts the numbers), from the repo root with the
same `PYTHONPATH`/`KUBECONFIG` as above:

```bash
# Pause KEDA so the replica count cannot change under the measurement
kubectl annotate scaledobject prefill-scaler autoscaling.keda.sh/paused-replicas=1 --overwrite
kubectl annotate scaledobject decode-scaler  autoscaling.keda.sh/paused-replicas=1 --overwrite

python -m app.experiments wall   --levels 32,64,128 --duration 45   # throughput / latency / KV vs concurrency
python -m app.experiments prefix --concurrency 40 --duration 40     # shared vs unique prefix, same load shape
python -m app.experiments warmup --repeats 3                        # kills the decode pod 3 times (~2 min each)
python -m app.make_plots                                            # -> plots/wall.png, plots/prefix_ab.png
```

Two things worth knowing before you run them:

- **The agent's cluster-changing tools are off by default.** `k8s_scale` and
  `k8s_rollout_restart` refuse unless `SRE_ALLOW_MUTATIONS=1`. Do not set it
  during a sweep: with 128 concurrent LLM-driven incidents the agent restarted
  and rescaled the very deployments being measured (`FINDINGS.md` P7).
- **Warm the engine first** if you have just restarted it (send ~6 requests),
  or the first level measures a cold start.

## 9. Look at Grafana (Terminal C, or your browser)

Open **http://127.0.0.1:31495**, log in `admin` / the password from step 4.
Dashboards → the **Serving /** dashboards — Overview, Gateway + admission, Router,
Mooncake KV, vLLM, HAMi, KEDA. Set the time range to "Last 15 minutes" and
re-run step 8 if panels look empty (they only show data once something has
actually called the gateway).

## 10. Run the tests

```bash
cd control/tests && python -m pytest -q
```
Expect `38 passed`.

## 11. When you're done: stop paying

**Terminate the Lambda instance** from the Lambda Cloud console (Instances →
your instance → Terminate). Nothing here needs to survive a restart — the
next run just repeats steps 1-4 on a fresh instance.
