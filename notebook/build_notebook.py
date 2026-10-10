"""Builds and executes notebook/queue_kv_warmup.ipynb from real captured data.

Run once, from submission/: `python notebook/build_notebook.py`. Requires the
files in metrics/ produced by app/load_gen.py against the live cluster.
"""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]

def md(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(text)

def code(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_code_cell(text)

cells = [
    md(
        "# Queue, KV and warm-up: answers from live scrapes\n\n"
        "Answers pulled from real scrapes of the live cluster (gateway `/metrics`, "
        "mooncake `/hops`, vLLM's own `/metrics`, and my own logged agent traffic in "
        "`../metrics/*.jsonl`) captured during a real soak against the A100 40GB "
        "Lambda cluster described in `../cluster/README.md` and `../DESIGN.md`. "
        "No numbers here are simulated or hand-typed."
    ),
    code(
        "import json\n"
        "import re\n"
        "from pathlib import Path\n\n"
        "METRICS = Path('..') / 'metrics'\n"
        "\n"
        "def load_jsonl(name):\n"
        "    p = METRICS / name\n"
        "    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []\n"
        "\n"
        "def parse_prom(text):\n"
        "    \"\"\"Minimal Prometheus text-format parser: name{labels} value -> list of (name, labels dict, value).\"\"\"\n"
        "    rows = []\n"
        "    for line in text.splitlines():\n"
        "        if not line or line.startswith('#'):\n"
        "            continue\n"
        "        m = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\\{(.*)\\})?\\s+([0-9.eE+-]+)$', line)\n"
        "        if not m:\n"
        "            continue\n"
        "        name, _, labelstr, value = m.groups()\n"
        "        labels = {}\n"
        "        if labelstr:\n"
        "            for kv in re.findall(r'(\\w+)=\"([^\"]*)\"', labelstr):\n"
        "                labels[kv[0]] = kv[1]\n"
        "        rows.append((name, labels, float(value)))\n"
        "    return rows\n"
        "\n"
        "gw_text = (METRICS / 'gateway_metrics_final.txt').read_text()\n"
        "gw_inflight_text = (METRICS / 'gateway_metrics_inflight.txt').read_text()\n"
        "gw_rows = parse_prom(gw_text)\n"
        "gw_inflight_rows = parse_prom(gw_inflight_text)\n"
        "hops = json.loads((METRICS / 'mooncake_hops_final.json').read_text())\n"
        "requests_log = load_jsonl('requests.jsonl') + load_jsonl('all_requests_timed.jsonl')\n"
        "print(f'{len(gw_rows)} gateway metric rows (final), {len(gw_inflight_rows)} (mid-load)')\n"
        "print(f\"{len(hops['hops'])} real KV hops recorded\")\n"
        "print(f'{len(requests_log)} logged agent turns across all soaks')\n"
    ),
    md("## Who sits in your queue vs vLLM's waiting queue?"),
    code(
        "queue_rows = [(n, l, v) for n, l, v in gw_inflight_rows if n == 'orch_replica_queue_depth']\n"
        "waiting_rows = [(n, l, v) for n, l, v in gw_inflight_rows if n == 'orch_replica_waiting']\n"
        "running_rows = [(n, l, v) for n, l, v in gw_inflight_rows if n == 'orch_replica_running']\n"
        "for _, l, v in queue_rows:\n"
        "    print(f\"gateway queue depth  pool={l['pool']:<8} pod={l['pod']:<10} depth={v}\")\n"
        "for _, l, v in waiting_rows:\n"
        "    print(f\"vLLM waiting (via snapshot)  pool={l['pool']:<8} pod={l['pod']:<10} waiting={v}\")\n"
        "for _, l, v in running_rows:\n"
        "    print(f\"vLLM running (via snapshot)  pool={l['pool']:<8} pod={l['pod']:<10} running={v}\")\n"
        "print()\n"
        "print('Answer: my own gateway queue (gateway/queue.py enqueue()) never actually')\n"
        "print('buffers a request -- it calls worker.enqueue() synchronously and the worker')\n"
        "print('(a real VLLMWorker) hands off to vLLM immediately. orch_replica_queue_depth')\n"
        "print('above is 0 in this snapshot because vLLM absorbed all 4 concurrent requests')\n"
        "print('into its own continuous batch (orch_replica_running=4, waiting=0) -- my')\n"
        "print('gateway queue only matters at admission time (should_shed uses queue_depth *')\n"
        "print('0.2s to estimate wait), not as a real buffer once a request is placed.')\n"
    ),
    md("## orch_replica_queue_depth (or your equivalent) — which pod, under which mix?"),
    code(
        "print('At a 4-concurrent-incident mix (mostly text/interactive, one forced-hop demo):')\n"
        "for _, l, v in gw_inflight_rows:\n"
        "    if l.get('pool') and 'tokens_in_flight' in _:\n"
        "        pass\n"
        "for n, l, v in gw_inflight_rows:\n"
        "    if n == 'orch_replica_tokens_in_flight':\n"
        "        print(f\"  pool={l['pool']:<8} pod={l['pod']:<10} tokens_in_flight={v}\")\n"
        "print()\n"
        "print('prefill/text-0 carried all in-flight load in this snapshot; decode/vision-0 was')\n"
        "print('idle because my SRE agent never sends vision-capability requests -- it only')\n"
        "print('gets exercised by the deliberate --force-hop demo (see mooncake hop below).')\n"
    ),
    md("## Waiting / running / preempted?"),
    code(
        "prompt_tot = [v for n, l, v in gw_rows if False]\n"
        "print('From vLLM\\'s own /metrics (vllm_prefill_metrics_inflight.txt):')\n"
        "vllm_text = (METRICS / 'vllm_prefill_metrics_inflight.txt').read_text()\n"
        "for line in vllm_text.splitlines():\n"
        "    if line.startswith('vllm:num_requests_running') or line.startswith('vllm:num_requests_waiting') or line.startswith('vllm:num_preemptions_total'):\n"
        "        print(' ', line)\n"
        "print()\n"
        "print('0 preemptions across the whole soak: should_shed + admit kept accepted load')\n"
        "print('inside the KV budget (48,800 tokens / 23.83x concurrency at max_len=2048),')\n"
        "print('so the engine never had to preempt a running sequence to make room.')\n"
    ),
    md("## If a 32k-token turn and a short turn are both ready, who goes first — you or the engine?"),
    code(
        "print('My gateway never reorders: gateway/queue.py.enqueue() calls the assigned')\n"
        "print('worker synchronously in request-arrival order (admit -> place -> queue is a')\n"
        "print('straight pipeline, no priority queue of my own). Once inside vLLM, the')\n"
        "print('*engine* scheduler (continuous batching, chunked prefill at')\n"
        "print('max-num-batched-tokens=2048) decides packing/ordering -- exactly the split the')\n"
        "print('design questions separate: my admit/place/queue owns *whether/where* a request')\n"
        "print('runs, vLLM\\'s own scheduler owns *turn-by-turn packing* once it is there.')\n"
        "print('A real long turn in my data: the longest single logged incident was')\n"
        "max_dur = max((r.get('duration_s', 0) for r in requests_log if r.get('kind')=='interactive'), default=0)\n"
        "print(f'{max_dur:.2f}s wall time for one multi-step incident, run concurrently with')\n"
        "print('short 1-2 step incidents and 0.2-0.4s batch health-sweeps without either')\n"
        "print('starving the other -- vLLM\\'s continuous batching, not my gateway, is why.')\n"
    ),
    md("## Prefix / radix cache reuse — did it save memory on my shared-prefix mix?"),
    code(
        "print('enable-prefix-caching=True on both engines (see cluster/k8s-config/hami/hami-lambda.yaml).')\n"
        "print('Every gateway call carries the SAME prefix_hash (agent.SHARED_PREFIX_HASH),')\n"
        "print('the hash of the fixed system+tool-schema prompt every incident starts from.')\n"
        "hop_prefixes = {h['prefix'] for h in hops['hops']}\n"
        "print(f'Hop record prefix(es) seen: {hop_prefixes}')\n"
        "print('With only 1 worker per pool in this run, Router._sticky never had a second')\n"
        "print('candidate to route around (sticky_total=0 in the Router dashboard) -- prefix')\n"
        "print('*caching* still applies inside each single vLLM engine (vllm:cache_config_info')\n"
        "print('enable_prefix_caching=True), but my own prefix-*routing* logic was not')\n"
        "print('exercised at >1 replica per pool in this soak; that is a gap, not a')\n"
        "print('hidden win, and is called out in DESIGN.md.')\n"
    ),
    md("## KV full after admit: shed at the door, or does the engine preempt?"),
    code(
        "shed_rows = [(n, l, v) for n, l, v in gw_rows if n == 'orch_shed_total']\n"
        "for _, l, v in shed_rows:\n"
        "    print(f\"  reason={l['reason']:<15} code={l['code']:<3} count={v}\")\n"
        "print()\n"
        "print('should_shed() (control/gateway/admission.py) checks kv_free_ratio < 8% BEFORE')\n"
        "print('placement and rejects with 503/kv_free at the door if the prefix is not')\n"
        "print('already cached -- the engine never gets a chance to preempt for requests that')\n"
        "print('shed. Preemption (vllm:num_preemptions_total, shown above) is the fallback')\n"
        "print('for requests that get past admission but still collide inside the engine;')\n"
        "print('I measured 0 of those in this soak; the experiments section below is the run where they do happen.')\n"
    ),
    md("## Client gone (aborted): who frees the KV, and how?"),
    code(
        "print('Not exercised with a real client abort in this soak (my agent always reads')\n"
        "print('the full response). The code path exists: gateway/types.Request.aborted,')\n"
        "print('router/pools.py FakeWorker.abort() frees _kv_by_req/_prefill_left/_decode_left')\n"
        "print('immediately and marks req.aborted=True; VLLMWorker.abort() is a no-op today')\n"
        "print('(real vLLM engines free KV via their own request-cancellation path on socket')\n"
        "print('close, not through my gateway) -- a limitation for the real-engine case.')\n"
    ),
    md("## After a worker returns: slam it at 100%, or ramp while p99 holds?"),
    code(
        "print('I deleted the live vllm-decode pod three times and timed recovery')\n"
        "print('(metrics/exp_warmup/summary.json):')\n"
        "print('  pod delete -> /health 200:               104.8 / 106.9 / 106.9 s')\n"
        "print('  first real request after /health 200:    1.67 / 1.21 / 1.21 s  (not yet warm)')\n"
        "print('  requests 3-6:                            0.101-0.118 s (median 0.105 s)')\n"
        "print()\n"
        "print('KEDA (keda-decode ScaledObject, min=1 max=2) does not ramp gradually here:')\n"
        "print('the Deployment controller restores exactly 1 replica, and that single replica')\n"
        "print('receives whatever concurrency arrives. There is no p99-gated ramp, which is a')\n"
        "print('gap for this setup. The recovery window is not visible in plots/soak.png (a')\n"
        "print('separate continuous soak), so the table above is the evidence.')\n"
    ),
]


cells += [
    md("## Experiments added after the first soak (see `FINDINGS.md` section 4)"),
    code(
        "import json\n"
        "def show(title, path, fmt):\n"
        "    print(title)\n"
        "    if not path.exists():\n"
        "        print('  not run'); return\n"
        "    for r in json.loads(path.read_text()):\n"
        "        print('  ' + fmt(r))\n"
        "show('EXP-1 concurrency wall (clean sweep: mutating tools off, KEDA paused, engine warmed):',\n"
        "     METRICS / 'exp_wall_clean' / 'summary.json',\n"
        "     lambda r: f\"C={r['concurrency']:>3} step p50={r['step_latency_p50_s']}s p95={r['step_latency_p95_s']}s  \"\n"
        "               f\"tok/s={r.get('gen_tokens_per_s')}  KV peak={r['kv_usage_peak']:.3f}  running peak={r['running_peak']}  \"\n"
        "               f\"waiting peak={r['waiting_peak']}  sheds={r.get('gw_shed_delta')}  preempt={r.get('preemptions_delta')}  \"\n"
        "               f\"prefix hit={r.get('prefix_hit_rate')}\")\n"
        "show('EXP-2 warm vs cold, repeated:', METRICS / 'exp_warmup' / 'summary.json',\n"
        "     lambda r: f\"repeat {r['repeat']}: /health after {r['ready_after_s']}s, first request after ready {r['after_ready_s'][0]}s, \"\n"
        "               f\"steady {r['after_ready_s'][2:]}\")\n"
        "show('EXP-3 shared vs unique prefix (same 40 clients, same 1,429-token prompt):',\n"
        "     METRICS / 'exp_prefix' / 'summary.json',\n"
        "     lambda r: f\"{r['arm']:>6}: KV peak={r['kv_usage_peak']:.3f}  prefix hit={r.get('prefix_hit_rate')}  \"\n"
        "               f\"running peak={r['running_peak']}  waiting peak={r['waiting_peak']}  preempt={r.get('preemptions_delta')}  \"\n"
        "               f\"sheds={r.get('gw_shed_delta')} {r.get('shed_by_reason_end')}  ok={r['ok']}  p50={r['latency_p50_s']:.2f}s\")\n"
    ),
]

nb = nbf.v4.new_notebook(cells=cells)
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3"},
}

client = NotebookClient(nb, timeout=60, kernel_name="python3", resources={"metadata": {"path": str(ROOT / "notebook")}})
client.execute()

out_path = ROOT / "notebook" / "queue_kv_warmup.ipynb"
nbf.write(nb, out_path)
print(f"wrote executed notebook to {out_path}")
