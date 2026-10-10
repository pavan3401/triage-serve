from __future__ import annotations

from gateway.admission import Gateway
from gateway.metrics import METRICS
from gateway.types import EngineRejected, Request
from router.kvbus import KVBus
from router.pools import FakeWorker
from router.router import Router

class RejectingWorker(FakeWorker):
    def enqueue(self, req, phase="both"):
        raise EngineRejected(400, '{"error":"maximum context length is 2048 tokens"}')

def _req() -> Request:
    return Request(id="r", arrival_t=0.0, priority=1, prompt_tokens=32,
                   max_new_tokens=8, prefix_hash=None, timeout_s=30.0, tenant="t")

def test_engine_4xx_is_not_reported_as_success() -> None:
    bus = KVBus()
    w = RejectingWorker("w0", bus=bus)
    gw = Gateway(Router([w], [w]))
    resp = gw.handle(_req())
    assert resp.status == 400
    assert resp.error == "engine_rejected"
    assert METRICS.completed_total == 0
    assert METRICS.engine_rejected_total[400] == 1
    assert "orch_engine_rejected_total{code=\"400\"} 1" in METRICS.render()
