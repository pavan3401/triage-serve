"""SRE agent tools.

`classify_severity`, `lookup_runbook`, `get_incident_timeline` and
`create_incident_report` are adapted from the custom MCP server in
https://github.com/chaitanya14/k8s-incident-response-agent (Apache-2.0, the same
license as this repository). The logic is preserved; the MCP wrapper is removed
and some output fields are trimmed, so these are modified versions, not
verbatim copies. `app/data/runbooks.json` is copied unchanged.

The `k8s_*`, `gateway_metrics` and `mooncake_hops` tools are new here: real
`kubectl`/HTTP calls against the same cluster the agent's own LLM calls run on.
The two cluster-changing tools are off unless SRE_ALLOW_MUTATIONS=1.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# --- ported: SRE reasoning tools (from k8s-incident-response-agent) -----------------

def classify_severity(
    affected_pods: int,
    total_pods: int,
    is_customer_facing: bool,
    error_rate_percent: float = 0.0,
    duration_minutes: int = 0,
    has_data_loss: bool = False,
    affected_namespaces: list[str] | None = None,
) -> str:
    """Classify incident severity (P1-P4) from blast radius, customer impact, error
    rate, and duration. Call during TRIAGE, after DETECT has gathered initial signals."""
    affected_namespaces = affected_namespaces or []
    impact_ratio = affected_pods / max(total_pods, 1)

    if (
        has_data_loss
        or (is_customer_facing and impact_ratio > 0.5)
        or error_rate_percent > 50
        or (is_customer_facing and error_rate_percent > 30 and duration_minutes > 15)
    ):
        severity, sla, escalate = "P1", "15 minutes", True
    elif (
        (is_customer_facing and impact_ratio > 0.25)
        or error_rate_percent > 25
        or (is_customer_facing and duration_minutes > 30)
        or (impact_ratio > 0.5 and not is_customer_facing)
    ):
        severity, sla, escalate = "P2", "30 minutes", True
    elif (
        (impact_ratio > 0.1)
        or (is_customer_facing and affected_pods > 0)
        or len(affected_namespaces) > 1
        or error_rate_percent > 5
    ):
        severity, sla, escalate = "P3", "4 hours", False
    else:
        severity, sla, escalate = "P4", "24 hours", False

    return json.dumps(
        {
            "severity": severity,
            "response_sla": sla,
            "escalation_required": escalate,
            "justification": {
                "impact_ratio": f"{impact_ratio:.1%} of pods affected ({affected_pods}/{total_pods})",
                "customer_facing": is_customer_facing,
                "error_rate": f"{error_rate_percent}%",
                "duration": f"{duration_minutes} minutes",
                "data_loss": has_data_loss,
                "namespaces_affected": affected_namespaces or ["unknown"],
            },
        },
        indent=2,
    )

def _load_runbooks() -> list[dict]:
    with open(os.path.join(DATA_DIR, "runbooks.json")) as f:
        return json.load(f)

def lookup_runbook(symptoms: list[str], resource_type: str = "", namespace: str = "") -> str:
    """Match observed symptoms (e.g. ['CrashLoopBackOff']) to the embedded runbook
    knowledge base. Call during DIAGNOSE after gathering evidence from logs/events/metrics."""
    runbooks = _load_runbooks()
    symptoms_lower = [s.lower() for s in symptoms]
    matches = []
    for rb in runbooks:
        rb_symptoms = [s.lower() for s in rb.get("symptoms", [])]
        matched = [s for s in symptoms_lower if any(rs in s or s in rs for rs in rb_symptoms)]
        if not matched:
            continue
        confidence = len(matched) / max(len(symptoms_lower), 1)
        if resource_type and resource_type.lower() in [rt.lower() for rt in rb.get("resource_types", [])]:
            confidence = min(confidence + 0.15, 1.0)
        matches.append(
            {
                "runbook_id": rb["id"],
                "title": rb["title"],
                "confidence": round(confidence, 2),
                "symptoms_matched": matched,
                "remediation_steps": rb.get("remediation_steps", []),
                "related_metrics": rb.get("related_metrics", []),
            }
        )
    matches.sort(key=lambda m: m["confidence"], reverse=True)
    return json.dumps({"matches_found": len(matches), "runbooks": matches[:5]}, indent=2)

def get_incident_timeline(events: list[dict], start_time: str = "", end_time: str = "") -> str:
    """Sort, dedupe, and phase-annotate raw events into a timeline. Call during
    DIAGNOSE for root cause analysis, and again during REPORT."""
    if not events:
        return json.dumps({"error": "No events provided", "timeline": []})
    normalized = []
    for e in events:
        sev = str(e.get("severity", "info")).lower()
        normalized.append(
            {
                "timestamp": e.get("timestamp", ""),
                "source": e.get("source", "unknown"),
                "message": e.get("message", ""),
                "severity": sev if sev in ("info", "warning", "error", "critical") else "info",
            }
        )
    normalized.sort(key=lambda e: e["timestamp"])
    if start_time:
        normalized = [e for e in normalized if e["timestamp"] >= start_time]
    if end_time:
        normalized = [e for e in normalized if e["timestamp"] <= end_time]
    deduped = []
    for e in normalized:
        if deduped and deduped[-1]["message"] == e["message"] and deduped[-1]["source"] == e["source"]:
            continue
        deduped.append(e)
    key_events = [e for e in deduped if e["severity"] in ("error", "critical")]
    duration = "unknown"
    if len(deduped) >= 2:
        try:
            first = datetime.fromisoformat(deduped[0]["timestamp"].replace("Z", "+00:00"))
            last = datetime.fromisoformat(deduped[-1]["timestamp"].replace("Z", "+00:00"))
            minutes = int((last - first).total_seconds() / 60)
            duration = f"{minutes} minutes" if minutes > 0 else f"{int((last-first).total_seconds())} seconds"
        except (ValueError, TypeError):
            pass
    return json.dumps(
        {"timeline": deduped, "total_events": len(deduped), "duration": duration, "key_events": key_events},
        indent=2,
    )

def create_incident_report(
    incident_id: str,
    title: str,
    severity: str,
    summary: str,
    affected_resources: list[str],
    root_cause: str,
    remediation_steps: list[str],
    timeline_events: list[dict],
    status: str = "investigating",
) -> str:
    """Generate a structured Markdown incident report. Call during REPORT, last."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timeline_table = "\n".join(
        f"| {e.get('timestamp', 'N/A')} | {e.get('description', e.get('message', 'N/A'))} |"
        for e in timeline_events
    ) or "| N/A | No events recorded |"
    remediation = "\n".join(f"{i+1}. {s}" for i, s in enumerate(remediation_steps))
    resources = "\n".join(f"- `{r}`" for r in affected_resources)
    return (
        f"# Incident Report: {incident_id}\n\n## {title}\n\n"
        f"| Field | Value |\n|---|---|\n| Severity | {severity} | \n| Status | {status} | "
        f"\n| Generated | {now} |\n\n## Summary\n\n{summary}\n\n## Affected Resources\n\n{resources}\n\n"
        f"## Timeline\n\n| Timestamp | Event |\n|---|---|\n{timeline_table}\n\n"
        f"## Root Cause\n\n{root_cause}\n\n## Remediation Steps\n\n{remediation}\n"
    )

# --- new: real tools against the same Lambda k3s cluster ---------------------------

def _kubectl(args: list[str], timeout_s: float = 10.0) -> str:
    try:
        out = subprocess.run(
            ["kubectl", *args], capture_output=True, text=True, timeout=timeout_s, check=False
        )
        return (out.stdout or out.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"kubectl error: {exc}"

def _mutations_allowed() -> bool:
    """Cluster-changing tools are OFF unless SRE_ALLOW_MUTATIONS=1.

    Found under load: with 64-128 concurrent LLM-driven incidents, the agent's
    `k8s_rollout_restart` / `k8s_scale` calls changed the very deployments the
    sweep was measuring (a decode rollout restart, and prefill replica flaps),
    so the system under test was being modified by its own client. A small
    model improvising remediation against its own serving cluster is not
    something to leave switched on by default."""
    return os.environ.get("SRE_ALLOW_MUTATIONS", "") == "1"

_REFUSED = ("refused: this agent runs read-only by default. Describe the change "
            "you would make instead of calling it (set SRE_ALLOW_MUTATIONS=1 to enable).")

def k8s_get_pods(namespace: str) -> str:
    """List pods and their status/restarts in a namespace. Use during DETECT."""
    return _kubectl(["get", "pods", "-n", namespace, "-o", "wide"])

def k8s_get_events(namespace: str) -> str:
    """Recent events in a namespace, newest last. Use during DETECT."""
    return _kubectl(["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"])

def k8s_get_pod_logs(pod: str, namespace: str, tail_lines: int = 50) -> str:
    """Tail logs for a pod. Use during DIAGNOSE."""
    return _kubectl(["logs", pod, "-n", namespace, f"--tail={tail_lines}"])

def k8s_describe(resource: str, namespace: str) -> str:
    """`kubectl describe` a resource, e.g. resource='deployment/vllm-decode'. DIAGNOSE."""
    return _kubectl(["describe", resource, "-n", namespace])

def k8s_scale(deployment: str, namespace: str, replicas: int) -> str:
    """Scale a deployment. REMEDIATE — state intent before calling this."""
    if not _mutations_allowed():
        return _REFUSED
    return _kubectl(["scale", f"deployment/{deployment}", "-n", namespace, f"--replicas={replicas}"])

def k8s_rollout_restart(deployment: str, namespace: str) -> str:
    """Rolling-restart a deployment. REMEDIATE — state intent before calling this."""
    if not _mutations_allowed():
        return _REFUSED
    return _kubectl(["rollout", "restart", f"deployment/{deployment}", "-n", namespace])

def gateway_metrics(gateway_url: str = "http://127.0.0.1:8080") -> str:
    """Scrape the control-plane gateway's own /metrics (shed reasons, queue depth,
    completed/requests). Use during DETECT/DIAGNOSE to see admission/routing behavior."""
    try:
        with urllib.request.urlopen(f"{gateway_url}/metrics", timeout=5) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        return f"gateway /metrics unreachable: {exc}"

def mooncake_hops(mooncake_url: str = "http://127.0.0.1:50051") -> str:
    """Fetch the KV-hop record store (src, dst, tokens, prefix per hop). Use during
    DIAGNOSE to see whether decode is warm or recomputing prefill after a hop."""
    try:
        with urllib.request.urlopen(f"{mooncake_url}/hops", timeout=5) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        return f"mooncake /hops unreachable: {exc}"

TOOLS = {
    "classify_severity": classify_severity,
    "lookup_runbook": lookup_runbook,
    "get_incident_timeline": get_incident_timeline,
    "create_incident_report": create_incident_report,
    "k8s_get_pods": k8s_get_pods,
    "k8s_get_events": k8s_get_events,
    "k8s_get_pod_logs": k8s_get_pod_logs,
    "k8s_describe": k8s_describe,
    "k8s_scale": k8s_scale,
    "k8s_rollout_restart": k8s_rollout_restart,
    "gateway_metrics": gateway_metrics,
    "mooncake_hops": mooncake_hops,
}
