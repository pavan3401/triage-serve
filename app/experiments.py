"""Controlled experiments against the live gateway.

Why this file exists: the first soak ran one fixed load and never changed a
variable, so it could not say what *limited* the system. These experiments
change one thing at a time and sample the engine's own metrics every second,
so every claim in FINDINGS.md points at a number from here.

  python -m app.experiments wall   --levels 2,8,16,32 --duration 60
  python -m app.experiments warmup --repeats 3
  python -m app.experiments prefix --concurrency 40 --duration 40

Outputs go to metrics/exp_*/ (raw CSV/JSON) and plots/ (figures).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app.agent import GatewayError, run_incident
from app.load_gen import INCIDENTS

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "http://127.0.0.1:8080"
TEXT_ENGINE = "http://127.0.0.1:8000"
VISION_ENGINE = "http://127.0.0.1:8001"

_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)$")

def _get(url: str, timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

def parse_prom(text: str | None) -> dict[str, float]:
    """Prometheus text -> {'name{labels}': value}. Missing scrape -> {}."""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        m = _LINE.match(line.strip())
        if m:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
    return out

def _sum(d: dict[str, float], prefix: str) -> float:
    return sum(v for k, v in d.items() if k.startswith(prefix))

def _one(d: dict[str, float], name: str) -> float | None:
    for k, v in d.items():
        if k.startswith(name):
            return v
    return None

def _first_present(d: dict[str, float], *names: str) -> float | None:
    # not `a or b`: a KV reading of 0.0 is real data, not "missing"
    for n in names:
        v = _one(d, n)
        if v is not None:
            return v
    return None

def sample() -> dict:
    gw = parse_prom(_get(f"{GATEWAY}/metrics"))
    en = parse_prom(_get(f"{TEXT_ENGINE}/metrics"))
    shed = {
        k.split('reason="')[1].split('"')[0]: v
        for k, v in gw.items()
        if k.startswith("orch_shed_total{") and 'reason="' in k
    }
    return {
        "t": round(time.time(), 3),
        "gw_requests": gw.get("orch_requests_total"),
        "gw_completed": gw.get("orch_completed_total"),
        "gw_shed_total": sum(shed.values()) if shed else 0.0,
        "gw_shed_by_reason": json.dumps(shed),
        "kv_usage": _first_present(en, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
        "running": _one(en, "vllm:num_requests_running"),
        "waiting": _one(en, "vllm:num_requests_waiting"),
        "preemptions": _one(en, "vllm:num_preemptions_total"),
        "prompt_tokens": _one(en, "vllm:prompt_tokens_total"),
        "gen_tokens": _one(en, "vllm:generation_tokens_total"),
        "prefix_hits": _one(en, "vllm:prefix_cache_hits_total"),
        "prefix_queries": _one(en, "vllm:prefix_cache_queries_total"),
    }

def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]

def _fnum(x: object) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0

# --- experiment 1: the concurrency wall, with the engine's KV usage sampled -------

def run_level(c: int, duration_s: float, label: dict) -> dict:
    stop_at = time.time() + duration_s
    lock = threading.Lock()
    step_lat: list[float] = []
    incidents = {"done": 0, "errored": 0}
    errs: dict[str, int] = {}

    def worker(i: int) -> None:
        n = 0
        while time.time() < stop_at:
            tenant = f"wall-c{c}-w{i}-{n}-{uuid.uuid4().hex[:4]}"
            n += 1
            res = run_incident(
                INCIDENTS[(i + n) % len(INCIDENTS)],
                base_url=GATEWAY, tenant=tenant, timeout_s=45.0, max_steps=3,
            )
            with lock:
                bad = False
                for rec in res["transcript"]:
                    if rec.get("error"):
                        bad = True
                        key = rec["error"][:40]
                        errs[key] = errs.get(key, 0) + 1
                    elif "latency_s" in rec:
                        step_lat.append(rec["latency_s"])
                incidents["errored" if bad else "done"] += 1
            if bad:
                # a real client honours Retry-After; without this a shed
                # thread re-submits instantly and the sweep measures the
                # load generator's tight loop, not the system
                time.sleep(1.0)

    label["level"] = c
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(c)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_s + 90)
    label["level"] = None
    return {
        "concurrency": c,
        "wall_s": round(time.time() - t0, 1),
        "steps_ok": len(step_lat),
        "incidents_done": incidents["done"],
        "incidents_errored": incidents["errored"],
        "step_latency_p50_s": _pct(step_lat, 0.50),
        "step_latency_p95_s": _pct(step_lat, 0.95),
        "step_latency_p99_s": _pct(step_lat, 0.99),
        "error_kinds": errs,
    }

def cmd_wall(args: argparse.Namespace) -> None:
    out = ROOT / "metrics" / "exp_wall"
    out.mkdir(parents=True, exist_ok=True)
    label: dict = {"level": None}
    rows: list[dict] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            s = sample()
            s["level"] = label["level"]
            rows.append(s)
            stop.wait(1.0)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    time.sleep(2.0)  # idle baseline before the first level
    summaries = []
    for c in [int(x) for x in args.levels.split(",")]:
        print(f"[wall] concurrency={c} for {args.duration}s", flush=True)
        first = len(rows)
        summ = run_level(c, args.duration, label)
        seg = [r for r in rows[first:] if r["level"] == c]
        kv = [r["kv_usage"] for r in seg if r["kv_usage"] is not None]
        run = [r["running"] for r in seg if r["running"] is not None]
        wait = [r["waiting"] for r in seg if r["waiting"] is not None]
        if seg:
            a, b = seg[0], seg[-1]
            summ["gw_requests_delta"] = _fnum(b["gw_requests"]) - _fnum(a["gw_requests"])
            summ["gw_shed_delta"] = _fnum(b["gw_shed_total"]) - _fnum(a["gw_shed_total"])
            summ["preemptions_delta"] = _fnum(b["preemptions"]) - _fnum(a["preemptions"])
            summ["gen_tokens_per_s"] = round(
                (_fnum(b["gen_tokens"]) - _fnum(a["gen_tokens"])) / max(1e-6, b["t"] - a["t"]), 1
            )
            dq = _fnum(b["prefix_queries"]) - _fnum(a["prefix_queries"])
            summ["prefix_hit_rate"] = (
                round((_fnum(b["prefix_hits"]) - _fnum(a["prefix_hits"])) / dq, 3) if dq > 0 else None
            )
        summ["kv_usage_peak"] = max(kv) if kv else None
        summ["kv_usage_mean"] = round(sum(kv) / len(kv), 3) if kv else None
        summ["running_peak"] = max(run) if run else None
        summ["waiting_peak"] = max(wait) if wait else None
        summaries.append(summ)
        print("   ", json.dumps(summ), flush=True)
        time.sleep(args.cooldown)  # let the engine drain between levels
    stop.set()
    th.join(timeout=3)

    (out / "summary.json").write_text(json.dumps(summaries, indent=2))
    if rows:
        with open(out / "samples.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    plot_wall(summaries, rows)
    print(f"[wall] wrote {out}")

def plot_wall(summaries: list[dict], rows: list[dict]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    (ROOT / "plots").mkdir(exist_ok=True)
    cs = [s["concurrency"] for s in summaries]
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
    ax[0].plot(cs, [s["step_latency_p50_s"] or 0 for s in summaries], "o-", label="p50")
    ax[0].plot(cs, [s["step_latency_p95_s"] or 0 for s in summaries], "s-", label="p95")
    ax[0].set_xlabel("concurrent incidents")
    ax[0].set_ylabel("agent step latency (s)")
    ax[0].set_title("Latency vs concurrency")
    ax[0].legend()
    a2 = ax[1]
    a2.plot(cs, [(s["kv_usage_peak"] or 0) * 100 for s in summaries], "o-", color="#2a9d8f", label="peak KV %")
    a2.axhline(80, color="#e76f51", linestyle="--", label="KV_SATURATION shed line (80%)")
    a2.set_xlabel("concurrent incidents")
    a2.set_ylabel("KV cache usage (%)")
    a2.set_title("What actually fills the KV pool")
    a2.legend()
    fig.tight_layout()
    fig.savefig(ROOT / "plots" / "wall.png", dpi=130)

    ts = [r for r in rows if r.get("kv_usage") is not None]
    if ts:
        t0 = ts[0]["t"]
        fig, a = plt.subplots(figsize=(10, 3.6))
        a.plot([r["t"] - t0 for r in ts], [(r["kv_usage"] or 0) * 100 for r in ts], color="#2a9d8f")
        a.set_xlabel("seconds")
        a.set_ylabel("KV usage %")
        a.set_title("Engine KV usage over the whole sweep (1 Hz)")
        fig.tight_layout()
        fig.savefig(ROOT / "plots" / "kv_timeline.png", dpi=130)

# --- experiment 2: warm vs cold, repeated -------------------------------------

def _timed_completion(url: str) -> float:
    body = json.dumps(
        {"model": "vision", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 4}
    ).encode()
    req = urllib.request.Request(
        f"{url}/v1/chat/completions", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
    return time.time() - t0

def cmd_warmup(args: argparse.Namespace) -> None:
    out = ROOT / "metrics" / "exp_warmup"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for n in range(args.repeats):
        warm_before = [round(_timed_completion(VISION_ENGINE), 3) for _ in range(3)]
        t0 = time.time()
        subprocess.run(
            ["kubectl", "delete", "pod", "-l", "app=vllm-decode", "--grace-period=0", "--force"],
            capture_output=True, text=True, check=False,
        )
        ready_s = None
        while time.time() - t0 < args.timeout:
            h = _get(f"{VISION_ENGINE}/health", timeout=2.0)
            if h is not None:
                ready_s = round(time.time() - t0, 1)
                break
            time.sleep(2)
        after = []
        if ready_s is not None:
            for _ in range(6):
                try:
                    after.append(round(_timed_completion(VISION_ENGINE), 3))
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    after.append(f"error: {exc}")
        rec = {"repeat": n + 1, "warm_before_s": warm_before, "ready_after_s": ready_s, "after_ready_s": after}
        results.append(rec)
        print("[warmup]", json.dumps(rec), flush=True)
    (out / "summary.json").write_text(json.dumps(results, indent=2))
    firsts = [r["after_ready_s"][0] for r in results if r["after_ready_s"] and isinstance(r["after_ready_s"][0], float)]
    warms = [x for r in results for x in r["after_ready_s"][2:] if isinstance(x, float)]
    if firsts and warms:
        print(f"[warmup] first-after-ready: {firsts}  steady: median {sorted(warms)[len(warms)//2]}")

# --- experiment 3: is prefix sharing what keeps KV low? -----------------------
#
# The wall sweep showed KV peaking at ~30% with ~40 sequences running, far below
# the paper ceiling (23.83x of 2,048-token sequences). My explanation: the
# system+tool prefix is shared, so its blocks are stored once. This arm tests
# that directly: same concurrency, same prompt length, same output length --
# only whether the prompt prefix is shared changes.

_WORDS = ("pod node kube gpu kv cache queue token shed route hop warm slice pool "
          "metric alert replica engine decode prefill batch admit place lease "
          "memory block page fault drain probe scale limit window tenant "
          "latency tail budget stall spill evict ghost ramp").split()

def _prompt(n_chars: int, seed: int | None) -> str:
    import random

    rng = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < n_chars:
        w = rng.choice(_WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)[:n_chars]

def _count_tokens(text: str) -> int | None:
    body = json.dumps({"model": "Qwen/Qwen2.5-3B-Instruct",
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(f"{TEXT_ENGINE}/tokenize", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return int(json.loads(r.read().decode()).get("count"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None

def _post_chat(tenant: str, prompt: str, max_tokens: int) -> tuple[int, float]:
    body = json.dumps(
        {"model": "text", "messages": [{"role": "user", "content": prompt}],
         "max_tokens": max_tokens, "tenant": tenant, "timeout_s": 60.0}
    ).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            r.read()
            return r.status, time.time() - t0
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.time() - t0
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, time.time() - t0

def run_prefix_arm(arm: str, c: int, duration_s: float, n_chars: int, label: dict) -> dict:
    stop_at = time.time() + duration_s
    lock = threading.Lock()
    lat: list[float] = []
    codes: dict[int, int] = {}
    shared_body = _prompt(n_chars, seed=7)

    def worker(i: int) -> None:
        n = 0
        while time.time() < stop_at:
            n += 1
            if arm == "shared":
                prompt = shared_body + f"\nQuestion {i}-{n}: summarise in one line."
            else:
                prompt = _prompt(n_chars, seed=None)
            code, dt = _post_chat(f"pfx-{arm}-{i}-{n}-{uuid.uuid4().hex[:4]}", prompt, 64)
            with lock:
                codes[code] = codes.get(code, 0) + 1
                if code == 200:
                    lat.append(dt)
            if code != 200:
                time.sleep(1.0)

    label["level"] = f"{arm}"
    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(c)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=duration_s + 90)
    label["level"] = None
    return {"arm": arm, "concurrency": c, "prompt_chars": n_chars,
            "wall_s": round(time.time() - t0, 1), "ok": len(lat), "status_codes": codes,
            "latency_p50_s": _pct(lat, 0.5), "latency_p95_s": _pct(lat, 0.95)}

def cmd_prefix(args: argparse.Namespace) -> None:
    out = ROOT / "metrics" / "exp_prefix"
    out.mkdir(parents=True, exist_ok=True)
    label: dict = {"level": None}
    rows: list[dict] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            s = sample()
            s["level"] = label["level"]
            rows.append(s)
            stop.wait(1.0)

    n_tok = _count_tokens(_prompt(args.chars, seed=None))
    print(f"[prefix] prompt = {args.chars} chars = {n_tok} tokens (engine limit 2048, +64 output)", flush=True)
    if n_tok is None or n_tok + 64 + 40 > 2048:
        raise SystemExit("prompt does not fit max-model-len; lower --chars (otherwise the engine "
                         "rejects every request and the run measures nothing)")
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    time.sleep(2.0)
    results = []
    for arm in args.arms.split(","):
        print(f"[prefix] arm={arm} C={args.concurrency} for {args.duration}s", flush=True)
        first = len(rows)
        res = run_prefix_arm(arm, args.concurrency, args.duration, args.chars, label)
        seg = [r for r in rows[first:] if r["level"] == arm]
        kv = [r["kv_usage"] for r in seg if r["kv_usage"] is not None]
        run = [r["running"] for r in seg if r["running"] is not None]
        wait = [r["waiting"] for r in seg if r["waiting"] is not None]
        if seg:
            a, b = seg[0], seg[-1]
            res["gw_shed_delta"] = _fnum(b["gw_shed_total"]) - _fnum(a["gw_shed_total"])
            res["shed_by_reason_end"] = json.loads(b["gw_shed_by_reason"] or "{}")
            res["preemptions_delta"] = _fnum(b["preemptions"]) - _fnum(a["preemptions"])
            res["gen_tokens_per_s"] = round(
                (_fnum(b["gen_tokens"]) - _fnum(a["gen_tokens"])) / max(1e-6, b["t"] - a["t"]), 1)
            dq = _fnum(b["prefix_queries"]) - _fnum(a["prefix_queries"])
            res["prefix_hit_rate"] = (
                round((_fnum(b["prefix_hits"]) - _fnum(a["prefix_hits"])) / dq, 3) if dq > 0 else None)
        res["kv_usage_peak"] = max(kv) if kv else None
        res["running_peak"] = max(run) if run else None
        res["waiting_peak"] = max(wait) if wait else None
        results.append(res)
        print("   ", json.dumps(res), flush=True)
        time.sleep(args.cooldown)
    stop.set()
    th.join(timeout=3)
    (out / "summary.json").write_text(json.dumps(results, indent=2))
    if rows:
        with open(out / "samples.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"[prefix] wrote {out}")

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("wall")
    k.add_argument("--levels", default="2,8,16,32")
    k.add_argument("--duration", type=float, default=60.0)
    k.add_argument("--cooldown", type=float, default=15.0)
    k.set_defaults(fn=cmd_wall)
    x = sub.add_parser("prefix")
    x.add_argument("--concurrency", type=int, default=40)
    x.add_argument("--duration", type=float, default=40.0)
    x.add_argument("--cooldown", type=float, default=15.0)
    x.add_argument("--chars", type=int, default=7000)
    x.add_argument("--arms", default="shared,unique")
    x.set_defaults(fn=cmd_prefix)
    w = sub.add_parser("warmup")
    w.add_argument("--repeats", type=int, default=3)
    w.add_argument("--timeout", type=float, default=400.0)
    w.set_defaults(fn=cmd_warmup)
    args = p.parse_args(argv)
    args.fn(args)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
