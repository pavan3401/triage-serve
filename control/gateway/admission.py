from __future__ import annotations

import time
import urllib.error

from gateway.queue import enqueue
from gateway.metrics import METRICS
from router.router import Router
from gateway.types import EngineRejected, Request, Response, Shed, SliceOOM, Snapshot

WINDOW_S = 60.0

KV_FLOOR = 0.08
QUEUE_WAIT_PER_REQ_S = 0.2

def should_shed(req: Request, snap: Snapshot | None) -> tuple[bool, int, str, float | None]:
    """Pure admission predicate — Part 3 of DESIGN.md.

    Answers "is the server too busy to safely accept this?" from an aggregate
    fleet `snap` alone. It never touches a specific worker, never retries, and
    never reroutes — that is `Router.pick`'s job (a second, placement-time
    line of defense: "no eligible pod" / per-worker `timeout_queue` / `kv_free`
    once a worker is actually selected). Returns (shed?, status_code, reason,
    retry_after_seconds).
    """
    if snap is None:
        return False, 200, "ok", None
    if snap.queue_depth is not None:
        expected_wait_s = float(snap.queue_depth) * QUEUE_WAIT_PER_REQ_S
        if expected_wait_s > req.timeout_s / 2:
            return True, 503, "timeout_queue", 2.0
    if snap.kv_free_ratio is not None and snap.kv_free_ratio < KV_FLOOR:
        cached = (snap.prefix_tokens or {}).get(req.prefix_hash or "", 0)
        if not req.prefix_hash or cached <= 0:
            return True, 503, "kv_free", 2.0
    return False, 200, "ok", None

def _fleet_snapshot(router: Router) -> Snapshot | None:
    """Worst-case aggregate snapshot across every worker in both pools."""
    snaps = [w.snapshot() for w in list(router.prefill) + list(router.decode)]
    snaps = [s for s in snaps if s is not None]
    if not snaps:
        return None
    kv_free = [s.kv_free_ratio for s in snaps if s.kv_free_ratio is not None]
    queue_depth = [s.queue_depth for s in snaps if s.queue_depth is not None]
    return Snapshot(
        pod_id="fleet",
        age_s=0.0,
        kv_free_ratio=min(kv_free) if kv_free else None,
        queue_depth=max(queue_depth) if queue_depth else None,
        prefix_tokens=snaps[0].prefix_tokens,
    )

class Gateway:

    def __init__(self, router: Router, tokens_per_min: int = 10_000) -> None:
        self.router = router
        self.tokens_per_min = tokens_per_min
        self._window: dict[str, tuple[float, int]] = {}

    def refresh_metrics(self) -> None:
        METRICS.refresh_from_router(self.router)

    def handle(self, req: Request) -> Response:
        t0 = time.perf_counter()
        METRICS.inc_requests()
        if self._over_cap(req):
            METRICS.inc_shed("tenant_tokens", 429)
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Shed(
                status=429,
                error="rate_limit_error",
                reason="tenant_tokens",
                message="tenant tokens/min cap",
            ).to_response()

        shed, status, reason, retry_after = should_shed(req, _fleet_snapshot(self.router))
        if shed:
            METRICS.inc_shed(reason, status)
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Shed(
                status=status,
                error="server_is_overloaded",
                reason=reason,
                retry_after_s=retry_after,
                message="fleet admission check",
            ).to_response()

        result = self.router.place(req)
        if isinstance(result, Shed):
            METRICS.inc_shed(result.reason, result.status)
            self.refresh_metrics()
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return result.to_response()
        try:
            hop, completion = enqueue(result, req)
        except SliceOOM:
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Response(
                status=503,
                error="slice_oom",
                body={"error": {"type": "slice_oom", "message": "gpumem slice exceeded"}},
            )
        except EngineRejected as exc:
            METRICS.inc_engine_rejected(exc.code)
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Response(
                status=exc.code,
                error="engine_rejected",
                reason="engine_rejected",
                body={"error": {"type": "engine_rejected", "message": exc.body[:300]}},
            )
        except (OSError, TimeoutError, urllib.error.URLError):
            METRICS.inc_shed("no_eligible_pod", 503)
            METRICS.observe_duration("gateway", time.perf_counter() - t0)
            return Shed(
                status=503,
                error="server_is_overloaded",
                reason="no_eligible_pod",
                message="worker unreachable",
            ).to_response()
        if req.evict_after and hop:
            bus = getattr(result.prefill, "bus", None) or getattr(result.decode, "bus", None)
            if bus is not None and hasattr(bus, "evict"):
                bus.evict(hop.get("src") or result.prefill.id, req.prefix_hash)
                bus.evict(hop.get("dst") or result.decode.id, req.prefix_hash)
        METRICS.inc_completed()
        METRICS.inc_place(req.capability or "text")
        self.refresh_metrics()
        METRICS.observe_duration("gateway", time.perf_counter() - t0)
        body: dict = {"id": req.id}
        if hop:
            body["kv_hop"] = hop
        if isinstance(completion, dict):
            for key in ("choices", "model", "usage"):
                if key in completion:
                    body[key] = completion[key]
            if completion.get("id"):
                body["completion_id"] = completion["id"]
        return Response(status=200, handoff=result, body=body)

    def _over_cap(self, req: Request) -> bool:
        cost = int(req.prompt_tokens) + int(req.max_new_tokens)
        start, used = self._window.get(req.tenant, (req.arrival_t, 0))
        if req.arrival_t - start >= WINDOW_S:
            start, used = req.arrival_t, 0
        if used + cost > self.tokens_per_min:
            return True
        self._window[req.tenant] = (start, used + cost)
        return False
