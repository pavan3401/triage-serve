from __future__ import annotations

import router.router as epp_mod
from router.router import DECODE_SCORERS, PREFILL_SCORERS, Router, score_prefix_cache
from router.kvbus import KVBus
from router.pools import FakeWorker
from gateway.types import Request, STALE_S

def _req(**kw) -> Request:
    base = dict(
        id="r0",
        arrival_t=0.0,
        priority=1,
        prompt_tokens=32,
        max_new_tokens=8,
        prefix_hash="h0",
        timeout_s=30.0,
        tenant="lab",
    )
    base.update(kw)
    return Request(**base)

def test_decode_pick_does_not_call_prefix_scorer(monkeypatch) -> None:
    calls = {"n": 0}
    real = epp_mod.score_prefix_cache

    def spy(snap, req):
        calls["n"] += 1
        return real(snap, req)

    monkeypatch.setattr(epp_mod, "score_prefix_cache", spy)
    bus = KVBus()
    prefill = [FakeWorker("p0", bus=bus)]
    decode = [FakeWorker("d0", bus=bus)]
    epp = Router(prefill, decode)
    epp.pick(decode, [epp_mod.score_active_request, epp_mod.score_kv_utilisation], _req())
    assert calls["n"] == 0
    assert score_prefix_cache not in DECODE_SCORERS
    assert epp_mod.score_prefix_cache not in DECODE_SCORERS

    epp.place(_req(id="r1", prefix_hash="h1"))
    assert epp_mod.score_prefix_cache not in epp.last_decode_scorers
    assert score_prefix_cache in PREFILL_SCORERS

def test_stale_empty_snapshot_not_chosen_if_healthy_pod_exists() -> None:
    bus = KVBus()
    stale = FakeWorker("stale-empty", bus=bus)
    stale.age_override = STALE_S + 10
    fresh = FakeWorker("fresh", bus=bus)
    fresh.enqueue(
        Request(
            id="warm",
            arrival_t=0.0,
            priority=1,
            prompt_tokens=128,
            max_new_tokens=0,
            prefix_hash="other",
            timeout_s=30.0,
            tenant="lab",
        ),
        phase="prefill",
    )
    epp = Router([stale, fresh], [fresh], policy="max")
    picked = epp.pick(epp.prefill, PREFILL_SCORERS, _req(prefix_hash="no-match"))
    assert picked is fresh
    assert stale.snapshot().tokens_in_flight == 0
    assert stale.snapshot().age_s > STALE_S
