"""Build plots/ from real logged traffic (metrics/*.jsonl) -- no synthetic data."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
METRICS = ROOT / "metrics"
PLOTS = ROOT / "plots"

def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def plot_soak_over_time() -> None:
    """Uses only metrics/final_verified_soak.jsonl -- one continuous run against
    the fully-fixed code, with no idle gaps between separate invocations.
    Concatenating multiple separate `load_gen` runs (each its own contiguous
    burst, with real idle time between them while other work happened) would
    otherwise draw a straight-line interpolation across that idle time and
    make it look like admitted traffic gradually decayed, which never
    happened -- a plot needs one uninterrupted soak, not a splice."""
    records = load(METRICS / "final_verified_soak.jsonl")
    if not records:
        print("no timestamped records, skipping soak plot")
        return
    t0 = min(r["t"] for r in records)
    bucket_s = 5
    buckets: dict[int, dict[str, int]] = {}
    for r in records:
        b = int((r["t"] - t0) // bucket_s)
        buckets.setdefault(b, {"admitted": 0, "shed": 0})
        errored = bool(r.get("errors")) or r.get("error") or (r.get("status") == 0)
        if errored:
            buckets[b]["shed"] += 1
        else:
            buckets[b]["admitted"] += 1
    xs = sorted(buckets)
    admitted = [buckets[x]["admitted"] for x in xs]
    shed = [buckets[x]["shed"] for x in xs]
    times = [x * bucket_s for x in xs]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(times, admitted, label="admitted (completed turn)", color="#2a9d8f")
    ax.plot(times, shed, label="shed / errored", color="#e76f51")
    ax.set_xlabel("seconds into soak")
    ax.set_ylabel(f"agent turns / {bucket_s}s bucket")
    ax.set_title("Soak: admitted vs shed agent turns over time (real traffic)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "soak.png", dpi=130)
    print(f"wrote {PLOTS / 'soak.png'} ({len(records)} records, {sum(shed)} shed)")

def plot_prompt_growth() -> None:
    records = [r for r in load(METRICS / "requests.jsonl") if r["kind"] == "interactive"]
    records += [r for r in load(METRICS / "all_requests_timed.jsonl") if r["kind"] == "interactive"]
    if not records:
        print("no interactive records, skipping prompt-growth plot")
        return
    by_steps: dict[int, list[int]] = {}
    for r in records:
        by_steps.setdefault(r["steps"], []).append(r["final_prompt_tokens_est"])
    fig, ax = plt.subplots(figsize=(7, 4.5))
    xs = sorted(by_steps)
    ax.boxplot([by_steps[x] for x in xs], tick_labels=[str(x) for x in xs])
    ax.axhline(2048, color="#e76f51", linestyle="--", label="max-model-len=2048")
    ax.set_xlabel("agent steps completed")
    ax.set_ylabel("estimated prompt tokens at final step")
    ax.set_title("Context growth: shared system+tool prefix + per-incident turns")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "prompt_growth.png", dpi=130)
    print(f"wrote {PLOTS / 'prompt_growth.png'} ({len(records)} incidents)")

def plot_wall_combined() -> None:
    """One figure from every concurrency sweep.

    sweep 1 (C=2..32) and the clean sweep (C=32,64,128: mutating tools off,
    KEDA paused, engine warmed) are solid; sweep 2 (C=64,128) ran while the
    agent could still scale/restart deployments, so it is hollow -- it agrees,
    which is the point of showing it."""
    path = METRICS / "exp_wall_all.json"
    if not path.exists():
        print("no exp_wall_all.json, skipping wall plot")
        return
    d = json.loads(path.read_text())
    sets = [
        ("sweep 1 (C=2..32)", d["sweep1_c2_32"], "o", "#2a9d8f", True),
        ("clean sweep (C=32..128)", d["sweep3_clean_c32_64_128"], "s", "#264653", True),
        ("sweep 2 (agent could mutate)", d["sweep2_c64_128"], "^", "#e76f51", False),
    ]
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
    for label, rows, mk, col, solid in sets:
        cs = [r["concurrency"] for r in rows]
        kw = dict(marker=mk, color=col, label=label, linestyle="-" if solid else "--",
                  markerfacecolor=col if solid else "none")
        ax[0].plot(cs, [r["gen_tokens_per_s"] for r in rows], **kw)
        ax[1].plot(cs, [r["step_latency_p50_s"] for r in rows], **kw)
        ax[2].plot(cs, [100 * r["kv_usage_peak"] for r in rows], **kw)
    ax[0].set_title("Throughput plateaus")
    ax[0].set_ylabel("generated tokens / s (engine)")
    ax[1].set_title("Latency grows with queueing")
    ax[1].set_ylabel("agent step latency p50 (s)")
    ax[2].set_title("KV never gets close to the shed line")
    ax[2].set_ylabel("peak KV usage (%)")
    ax[2].axhline(80, color="#c1121f", linestyle=":", label="KV_SATURATION 80%")
    ax[2].set_ylim(0, 100)
    for a in ax:
        a.set_xlabel("concurrent incidents")
        a.set_xscale("log", base=2)
        a.grid(alpha=0.3)
    ax[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(PLOTS / "wall.png", dpi=130)
    print(f"wrote {PLOTS / 'wall.png'}")

def plot_prefix_ab() -> None:
    """Same concurrency / prompt length / output length; only prefix sharing differs."""
    sp = METRICS / "exp_prefix" / "samples.csv"
    summ = METRICS / "exp_prefix" / "summary.json"
    if not (sp.exists() and summ.exists()):
        print("no exp_prefix data, skipping")
        return
    import csv

    rows = list(csv.DictReader(sp.read_text().splitlines()))
    res = {r["arm"]: r for r in json.loads(summ.read_text())}
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.2))
    for arm, col in (("shared", "#2a9d8f"), ("unique", "#e76f51")):
        seg = [r for r in rows if r["level"] == arm and r["kv_usage"] not in ("", "None")]
        if not seg:
            continue
        t0 = float(seg[0]["t"])
        ax[0].plot([float(r["t"]) - t0 for r in seg], [100 * float(r["kv_usage"]) for r in seg],
                   color=col, label=f"{arm} prefix (hit {100 * (res[arm].get('prefix_hit_rate') or 0):.0f}%)")
    ax[0].axhline(80, color="#c1121f", linestyle=":", label="KV_SATURATION 80%")
    ax[0].set_xlabel("seconds into arm")
    ax[0].set_ylabel("engine KV usage (%)")
    ax[0].set_title("Same 40 clients, same 1,429-token prompt")
    ax[0].set_ylim(0, 105)
    ax[0].legend(fontsize=8)
    labels = ["KV peak %", "preemptions", "kv_free sheds", "waiting peak"]
    def vals(a):
        r = res[a]
        return [100 * (r.get("kv_usage_peak") or 0), r.get("preemptions_delta") or 0,
                (r.get("shed_by_reason_end") or {}).get("kv_free", 0), r.get("waiting_peak") or 0]
    import numpy as np
    x = np.arange(len(labels))
    ax[1].bar(x - 0.2, vals("shared"), 0.4, color="#2a9d8f", label="shared")
    ax[1].bar(x + 0.2, vals("unique"), 0.4, color="#e76f51", label="unique")
    ax[1].set_xticks(x)
    ax[1].set_xticklabels(labels, fontsize=9)
    ax[1].set_title("What prefix sharing buys")
    ax[1].legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "prefix_ab.png", dpi=130)
    print(f"wrote {PLOTS / 'prefix_ab.png'}")

if __name__ == "__main__":
    PLOTS.mkdir(exist_ok=True)
    plot_soak_over_time()
    plot_prompt_growth()
    plot_wall_combined()
    plot_prefix_ab()
