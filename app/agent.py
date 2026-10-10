"""Tool-using SRE agent: think -> tool -> observe -> answer.

Every LLM turn goes through the control-plane gateway's OpenAI-compatible
`/v1/chat/completions` (control/gateway/serve.py) -- guardrails.inspect -> admit
(should_shed) -> place (Router.pick) -> queue (same-pod or Mooncake hop) -> engine.
No request in this file talks to vLLM directly.

The system prompt + tool schema is a fixed, shared prefix across every incident;
each incident's user turn and tool observations grow that conversation's own
context every step, the "multi-step decode with a lengthening prefill" shape.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import time
import urllib.error
import urllib.request
import uuid

from app.tools import TOOLS

MAX_STEPS = 6
MAX_TOKENS_PER_STEP = 256

# The five-phase structure (detect / triage / diagnose / remediate / report) is
# adapted from chaitanya14/k8s-incident-response-agent (Apache-2.0).
METHODOLOGY = """You are an SRE agent investigating a Kubernetes cluster that serves
LLM inference traffic (a gateway, a router, vLLM replicas, KEDA autoscaling, a
Mooncake KV-hop store). Follow this methodology, one phase per turn, in order:

1. DETECT   - k8s_get_pods, k8s_get_events, gateway_metrics: what's unhealthy?
2. TRIAGE   - classify_severity: how bad, how many pods, customer-facing?
3. DIAGNOSE - k8s_get_pod_logs, k8s_describe, mooncake_hops, lookup_runbook:
              form a root-cause hypothesis with evidence.
4. REMEDIATE - k8s_scale / k8s_rollout_restart, ONLY after stating intent.
               Never scale to zero or delete without saying so first.
5. REPORT   - get_incident_timeline, then create_incident_report, then stop.

At every turn, respond in EXACTLY one of these two forms and nothing else:

Thought: <one line: which phase you're in and why>
Action: <tool_name>
Action Input: {"arg": "value", ...}

or, only once you are done:

Thought: <one line>
Final Answer: <the incident report or final summary>
"""

def _tool_schema() -> str:
    lines = []
    for name, fn in sorted(TOOLS.items()):
        sig = inspect.signature(fn)
        doc = (fn.__doc__ or "").strip().splitlines()[0]
        lines.append(f"- {name}{sig}: {doc}")
    return "\n".join(lines)

TOOL_SCHEMA_TEXT = _tool_schema()
SYSTEM_PROMPT = f"{METHODOLOGY}\nTools:\n{TOOL_SCHEMA_TEXT}\n"
# Shared across every incident/turn on purpose -- this is the prefix the router's
# prefix-cache scoring and sticky routing are meant to exploit.
SHARED_PREFIX_HASH = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]

ACTION_RE = re.compile(r"Action:\s*(\w+)\s*\nAction Input:\s*(\{.*\})", re.DOTALL)
FINAL_RE = re.compile(r"Final Answer:\s*(.*)", re.DOTALL)

class GatewayError(RuntimeError):
    pass

def call_gateway(
    messages: list[dict],
    *,
    base_url: str,
    tenant: str,
    timeout_s: float,
    request_timeout_s: float = 30.0,
) -> dict:
    """POST one turn to the control-plane gateway. Raises GatewayError on non-2xx
    so the caller can see 429/503 shed responses instead of silently losing them."""
    payload = {
        "id": str(uuid.uuid4()),
        "model": "text",
        "messages": messages,
        "max_tokens": MAX_TOKENS_PER_STEP,
        "tenant": tenant,
        "timeout_s": timeout_s,
        "prefix_hash": SHARED_PREFIX_HASH,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=request_timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"gateway {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GatewayError(f"gateway unreachable: {exc}") from exc

def _assistant_text(resp: dict) -> str:
    choices = resp.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
    return str((msg or {}).get("content") or "")

def run_incident(
    description: str,
    *,
    base_url: str = "http://127.0.0.1:8080",
    tenant: str = "sre-agent",
    timeout_s: float = 30.0,
    max_steps: int = MAX_STEPS,
) -> dict:
    """Run one incident through the full think->tool->observe->answer loop.
    Returns the transcript plus per-step gateway metadata (kv_hop, status)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": description},
    ]
    transcript: list[dict] = []
    for step in range(1, max_steps + 1):
        t0 = time.perf_counter()
        try:
            resp = call_gateway(messages, base_url=base_url, tenant=tenant, timeout_s=timeout_s)
        except GatewayError as exc:
            transcript.append({"step": step, "error": str(exc)})
            break
        latency_s = time.perf_counter() - t0
        text = _assistant_text(resp)
        messages.append({"role": "assistant", "content": text})

        final = FINAL_RE.search(text)
        action = ACTION_RE.search(text)
        record = {
            "step": step,
            "latency_s": round(latency_s, 3),
            "kv_hop": resp.get("kv_hop"),
            "raw": text,
        }
        if final:
            record["final_answer"] = final.group(1).strip()
            transcript.append(record)
            break
        if action:
            name, raw_args = action.group(1), action.group(2)
            try:
                args = json.loads(raw_args)
                fn = TOOLS[name]
                result = fn(**args)
            except Exception as exc:  # tool errors are observations, not crashes
                result = f"tool error: {exc}"
            record["action"] = name
            record["action_input"] = raw_args
            record["observation"] = str(result)[:2000]
            messages.append({"role": "user", "content": f"Observation: {record['observation']}"})
        else:
            record["observation"] = "format error: expected Action: or Final Answer:"
            messages.append({"role": "user", "content": "Observation: " + record["observation"]})
        transcript.append(record)
    return {"description": description, "tenant": tenant, "transcript": transcript, "messages": messages}

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SRE agent over the control-plane gateway")
    p.add_argument("--incident", required=True)
    p.add_argument("--gateway", default="http://127.0.0.1:8080")
    p.add_argument("--tenant", default="sre-agent")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--max-steps", type=int, default=MAX_STEPS)
    p.add_argument("--json-out", default="")
    args = p.parse_args(argv)

    result = run_incident(
        args.incident,
        base_url=args.gateway,
        tenant=args.tenant,
        timeout_s=args.timeout_s,
        max_steps=args.max_steps,
    )
    for rec in result["transcript"]:
        if rec.get("error"):
            print(f"[step {rec['step']}] ERROR {rec['error']}")
            continue
        print(f"[step {rec['step']}] {rec['raw'].strip()[:400]}")
        if rec.get("observation"):
            print(f"  -> Observation: {rec['observation'][:400]}")
        if rec.get("kv_hop"):
            print(f"  -> kv_hop: {rec['kv_hop']}")
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(result, f, indent=2)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
