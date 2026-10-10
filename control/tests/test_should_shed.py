from __future__ import annotations

from gateway.admission import should_shed
from gateway.types import Request, Snapshot

def _req(**kw) -> Request:
    base = dict(
        id="r0",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=32,
        max_new_tokens=8,
        prefix_hash=None,
        timeout_s=30.0,
        tenant="lab",
    )
    base.update(kw)
    return Request(**base)

def test_no_snapshot_is_never_shed() -> None:
    shed, status, reason, retry = should_shed(_req(), None)
    assert (shed, status, reason, retry) == (False, 200, "ok", None)

def test_kv_floor_with_new_prefix_is_shed() -> None:
    snap = Snapshot(pod_id="fleet", age_s=0.0, kv_free_ratio=0.05, prefix_tokens={})
    shed, status, reason, _ = should_shed(_req(prefix_hash="new"), snap)
    assert (shed, status, reason) == (True, 503, "kv_free")

def test_kv_floor_with_cached_prefix_is_accepted() -> None:
    snap = Snapshot(pod_id="fleet", age_s=0.0, kv_free_ratio=0.05, prefix_tokens={"seen": 100})
    shed, status, reason, _ = should_shed(_req(prefix_hash="seen", prompt_tokens=50), snap)
    assert (shed, status, reason) == (False, 200, "ok")

def test_expected_wait_over_half_timeout_is_shed() -> None:
    # queue_depth=100 * 0.2s/req = 20s expected wait > half of a 30s timeout
    snap = Snapshot(pod_id="fleet", age_s=0.0, queue_depth=100)
    shed, status, reason, retry = should_shed(_req(timeout_s=30.0), snap)
    assert (shed, status, reason, retry) == (True, 503, "timeout_queue", 2.0)

def test_short_queue_is_accepted() -> None:
    snap = Snapshot(pod_id="fleet", age_s=0.0, queue_depth=2, kv_free_ratio=0.5)
    shed, status, reason, _ = should_shed(_req(timeout_s=30.0), snap)
    assert (shed, status, reason) == (False, 200, "ok")
