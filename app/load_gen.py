"""Drive the live cluster with app-shaped traffic (DESIGN.md Part 8) and log everything.

Two workloads, an interactive vs batch split:
  - interactive: concurrent SRE incidents, each a full multi-step agent loop
    (growing, unique conversation; shared system+tool-schema prefix)
  - batch: a lower-rate background "cluster health sweep" (single-turn, longer
    tolerance) sharing the same prefix

Every request is logged to metrics/requests.jsonl (arrival, latency, status,
kv_hop, tenant) so plots/ and notebook/ can be built from real, not simulated,
data.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.agent import SHARED_PREFIX_HASH, call_gateway, run_incident, GatewayError

ROOT = Path(__file__).resolve().parents[1]
LOG_LOCK = threading.Lock()

INCIDENTS = [
    "vllm-decode has restarted recently. Check its status and recent events, and summarize whether it's healthy now.",
    "The gateway's shed counters show tenant_tokens rejections. Investigate which tenant and whether it's expected.",
    "Check gateway_metrics for kv_free shed reasons and describe the vllm-prefill deployment to see current KV pressure.",
    "A user reports slow responses. Check mooncake_hops and gateway_metrics to see if requests are hopping between workers.",
    "Do a routine health sweep: list pods, check events, and classify overall cluster severity.",
]

def _log(path: Path, record: dict) -> None:
    record.setdefault("t", round(time.time(), 3))
    with LOG_LOCK:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")

def run_one_incident(i: int, *, gateway: str, log_path: Path, max_steps: int) -> None:
    tenant = f"incident-{i}-{uuid.uuid4().hex[:6]}"
    incident = INCIDENTS[i % len(INCIDENTS)]
    t0 = time.time()
    result = run_incident(incident, base_url=gateway, tenant=tenant, timeout_s=45.0, max_steps=max_steps)
    _log(
        log_path,
        {
            "kind": "interactive",
            "tenant": tenant,
            "incident": incident,
            "steps": len(result["transcript"]),
            "final_prompt_tokens_est": len(json.dumps(result["messages"])) // 4,
            "duration_s": round(time.time() - t0, 3),
            "hops": [r.get("kv_hop") for r in result["transcript"] if r.get("kv_hop")],
            "errors": [r.get("error") for r in result["transcript"] if r.get("error")],
        },
    )

def run_health_sweep(*, gateway: str, log_path: Path, tenant: str) -> None:
    messages = [
        {"role": "system", "content": "You are a cluster health summarizer."},
        {"role": "user", "content": "One sentence: is the cluster currently healthy?"},
    ]
    t0 = time.time()
    try:
        resp = call_gateway(messages, base_url=gateway, tenant=tenant, timeout_s=60.0)
        status, err = 200, None
    except GatewayError as exc:
        resp, status, err = {}, 0, str(exc)
    _log(
        log_path,
        {
            "kind": "batch",
            "tenant": tenant,
            "duration_s": round(time.time() - t0, 3),
            "status": status,
            "error": err,
            "kv_hop": resp.get("kv_hop") if isinstance(resp, dict) else None,
        },
    )

def force_hop_demo(*, gateway: str, log_path: Path) -> dict:
    """One deliberate forced-phase request: prefill on the text engine, decode on
    the vision engine -- a real cross-GPU-slice hop for the DESIGN.md Part 6 proof."""
    payload = {
        "id": str(uuid.uuid4()),
        "model": "text",
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": 8,
        "tenant": "hop-demo",
        "timeout_s": 30.0,
        "prefix_hash": SHARED_PREFIX_HASH,
        "kv_hop": True,
    }
    req = urllib.request.Request(
        f"{gateway.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    _log(log_path, {"kind": "hop_demo", "response": body})
    return body

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Agent-shaped soak against the live gateway")
    p.add_argument("--gateway", default="http://127.0.0.1:8080")
    p.add_argument("--incidents", type=int, default=8, help="concurrent interactive incidents")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-steps", type=int, default=3)
    p.add_argument("--batch-every-s", type=float, default=3.0)
    p.add_argument("--duration-s", type=float, default=90.0)
    p.add_argument("--log", default=str(ROOT / "metrics" / "requests.jsonl"))
    p.add_argument("--force-hop", action="store_true")
    args = p.parse_args(argv)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if args.force_hop:
        print("hop demo:", force_hop_demo(gateway=args.gateway, log_path=log_path))

    stop_at = time.time() + args.duration_s
    batch_stop = threading.Event()

    def batch_loop() -> None:
        n = 0
        while not batch_stop.is_set():
            run_health_sweep(gateway=args.gateway, log_path=log_path, tenant=f"batch-sweep-{n % 3}")
            n += 1
            batch_stop.wait(args.batch_every_s)

    batch_thread = threading.Thread(target=batch_loop, daemon=True)
    batch_thread.start()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = []
        i = 0
        while time.time() < stop_at:
            if len(futs) < args.incidents:
                futs.append(
                    pool.submit(run_one_incident, i, gateway=args.gateway, log_path=log_path, max_steps=args.max_steps)
                )
                i += 1
            done = [f for f in futs if f.done()]
            for f in done:
                f.result()
                futs.remove(f)
            time.sleep(0.5)
        for f in as_completed(futs):
            f.result()

    batch_stop.set()
    batch_thread.join(timeout=5)
    print(f"soak done, log at {log_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
