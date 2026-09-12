"""G3 Context Manager tests — pure + endpoint-level, no live provider calls.

Covers:
  * soft/hard compression triggers as fraction of safe_context
  * anti-thrash (soft suppressed until new context >= max(100k, 20% safe))
  * hard bypasses anti-thrash
  * target_tokens bounds (22-30% of safe_context)
  * cache-switch policy branches (WARM protected, LIKELY_WARM half-threshold,
    unhealthy warm free switch, cold free switch, unknown savings conservative)
  * /v1/compress decision endpoint (400 on bad input, decision on good)
  * /v1/compress/record bookkeeping + /context/events exposure
  * observe hook (_observe_and_decide) sets privacy-safe routing headers

Run: PYTHONPATH=.. python3 -m pytest gateway_tests/test_g3_context.py -q
"""
from __future__ import annotations

import asyncio
import time

import pytest

from gateway.context_manager import (
    ANTI_THRASH_MIN_TOKENS,
    ContextManager,
    evaluate_cache_switch,
    route_safe_context,
)
from gateway.app import _observe_and_decide
from gateway.selector import PlanStep


class _FakeRequest:
    def __init__(self, payload: dict):
        self._p = payload

    async def json(self) -> dict:
        return self._p


def _post(endpoint, payload: dict) -> dict:
    return asyncio.run(endpoint(_FakeRequest(payload)))


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------

def test_safe_context_formula():
    assert route_safe_context(1_000_000) == min(900_000, 1_000_000 - 8192)
    assert route_safe_context(0) == 0


def test_soft_trigger_at_65pct_and_below():
    cm = ContextManager()
    safe = 1_000_000
    should, kind, _ = cm.should_compress("s1", int(safe * 0.64), safe)
    assert not should and kind == "none"
    should, kind, _ = cm.should_compress("s1", int(safe * 0.66), safe)
    assert should and kind == "soft"


def test_hard_trigger_at_78pct():
    cm = ContextManager()
    safe = 1_000_000
    should, kind, _ = cm.should_compress("s1", int(safe * 0.79), safe)
    assert should and kind == "hard"


def test_anti_thrash_suppresses_second_soft():
    cm = ContextManager()
    safe = 1_000_000
    cm.record_compression("s2", before_tokens=500_000, after_tokens=200_000,
                          summary_tokens=1000, retained_tail_tokens=0,
                          compressor_canonical="c", compressor_provider="p",
                          compressor_route_key="p:m", compressor_kind="session",
                          trigger_kind="soft", trigger_threshold=325_000,
                          compressor_api_ms=1.0, total_compression_ms=1.0,
                          cost_usd=0.0, cost_state="UNKNOWN")
    # Only +160k new context since last compression -> below required 200k -> suppressed.
    should, kind, reason = cm.should_compress("s2", 660_000, safe)
    assert not should and kind == "anti_thrash_skipped"
    # +200k new context (>= max(100k, 20% safe)) below hard -> soft allowed.
    should, kind, _ = cm.should_compress("s2", 700_000, safe)
    assert should and kind == "soft"


def test_hard_bypasses_anti_thrash():
    cm = ContextManager()
    safe = 1_000_000
    cm.record_compression("s3", before_tokens=int(safe * 0.80), after_tokens=int(safe * 0.25),
                          summary_tokens=1000, retained_tail_tokens=0,
                          compressor_canonical="c", compressor_provider="p",
                          compressor_route_key="p:m", compressor_kind="session",
                          trigger_kind="hard", trigger_threshold=int(safe * 0.78),
                          compressor_api_ms=1.0, total_compression_ms=1.0,
                          cost_usd=0.0, cost_state="UNKNOWN")
    should, kind, _ = cm.should_compress("s3", int(safe * 0.81), safe)
    assert should and kind == "hard"


def test_target_tokens_bounds():
    cm = ContextManager()
    lo, hi = cm.target_tokens(1_000_000)
    assert lo == 220_000 and hi == 250_000  # G3.1: 22-25% target (was 0.30)


def test_no_safe_context_is_noop():
    cm = ContextManager()
    should, kind, reason = cm.should_compress("s4", 500_000, 0)
    assert not should and kind == "none" and reason == "no_safe_context"


# ---------------------------------------------------------------------------
# Cache-switch policy
# ---------------------------------------------------------------------------

def _switch(warm, cold, state, unhealthy=False):
    return evaluate_cache_switch(
        warm_route_saving_usd=warm, cold_route_saving_usd=cold,
        warm_cache_state=state, warm_unhealthy=unhealthy)


def test_switch_warm_protected():
    d = _switch(0.010, 0.005, "WARM")  # 50% saving but < $0.002? no: 0.005 >= 0.002 and 50% >= 20%
    # delta=0.005 >= 0.002 and pct=50% >= 20% -> switches.
    assert d.switch and d.reason == "warm_with_big_saving"
    d2 = _switch(0.010, 0.0095, "WARM")  # pct 5% < 20% -> protected
    assert not d2.switch and d2.reason == "warm_protected"


def test_switch_warm_unhealthy_free():
    d = _switch(0.010, 0.0095, "WARM", unhealthy=True)
    assert d.switch and d.reason == "warm_unhealthy"


def test_switch_likely_warm_half_threshold():
    # pct=10% < 20% but >= 10% (half) and delta 0.001 < 0.002 but >= 0.001 (half) -> switch.
    d = _switch(0.010, 0.0090, "LIKELY_WARM")
    assert d.switch and d.reason == "likely_warm_with_saving"


def test_switch_cold_free_on_any_saving():
    d = _switch(0.010, 0.0099, "COLD")
    assert d.switch and d.reason == "cold_free_switch"
    d2 = _switch(0.009, 0.010, "COLD")
    assert not d2.switch and d2.reason == "no_positive_saving"


def test_switch_unknown_savings_conservative():
    d = _switch(None, 0.001, "COLD")
    assert not d.switch and d.reason == "unknown_savings"
    d2 = _switch(None, None, "WARM", unhealthy=True)
    assert d2.switch and d2.reason == "unknown_savings"


# ---------------------------------------------------------------------------
# observe hook + endpoints
# ---------------------------------------------------------------------------

def _step(canonical="gpt-5.6-luna", provider="provider_a", slug="cx/gpt-5.6-luna", safe=1_000_000):
    return PlanStep(canonical=canonical, provider=provider, provider_model_id=slug,
                    context_length=1_100_000, discount=0.88, quality_score=0.81,
                    reason="test", safe_context_limit=safe)


def test_observe_and_decide_headers():
    hdrs: dict = {}
    out = _observe_and_decide(
        {"x-hermes-session-id": "sess-1"}, {}, _step(),
        {"prompt_tokens": int(1_000_000 * 0.66), "completion_tokens": 10}, hdrs)
    assert not out["should_compress"] or out["trigger"] == "soft"
    assert hdrs["x-gateway-ctx-compression"] in ("none", "soft")
    assert "x-gateway-ctx-utilization" in hdrs
    # Hard trigger path.
    hdrs2: dict = {}
    out2 = _observe_and_decide(
        {"x-hermes-session-id": "sess-2"}, {}, _step(),
        {"prompt_tokens": int(1_000_000 * 0.80), "completion_tokens": 0}, hdrs2)
    assert out2["should_compress"] and out2["trigger"] == "hard"
    assert hdrs2["x-gateway-ctx-compression"] == "hard"


def test_compress_decision_endpoint_validation():
    from gateway.app import compress_decision
    r = _post(compress_decision, {"session_id": "", "context_tokens": 0})
    assert r.status_code == 400 and r.body and b"invalid_input" in r.body
    d = _post(compress_decision, {"session_id": "ep-1", "context_tokens": 790_000, "safe_context": 1_000_000})
    assert d["should_compress"] and d["trigger"] == "hard"
    assert d["safe_context"] == 1_000_000
    assert d["target_tokens"]["min"] == 220_000


def test_compress_record_and_events():
    from gateway.app import compress_record, context_events
    rec = _post(compress_record, {
        "session_id": "ep-rec", "before_tokens": 700_000, "after_tokens": 240_000,
        "summary_tokens": 5_000, "retained_tail_tokens": 2_000,
        "compressor_canonical": "gpt-5.6-luna", "compressor_provider": "provider_a",
        "compressor_route_key": "provider_a:cx/gpt-5.6-luna", "compressor_kind": "session",
        "trigger_kind": "soft", "trigger_threshold": 650_000,
        "compressor_api_ms": 120.0, "total_compression_ms": 140.0,
        "cost_usd": 0.01, "cost_state": "EXACT",
    })
    assert rec["ok"] is True and rec["event_id"]
    evs = asyncio.run(context_events(10))["events"]
    assert any(e["event_id"] == rec["event_id"] and e["session_hash"].startswith("sha256:") for e in evs)
    # No raw prompt fields in telemetry events.
    assert all(not any(k in e for k in ("prompt", "messages", "content")) for e in evs)


def test_recorded_compression_resets_anti_thrash_window():
    cm = ContextManager()
    safe = 1_000_000
    cm.record_compression("s5", before_tokens=500_000, after_tokens=200_000,
                          summary_tokens=1000, retained_tail_tokens=0,
                          compressor_canonical="c", compressor_provider="p",
                          compressor_route_key="p:m", compressor_kind="session",
                          trigger_kind="soft", trigger_threshold=325_000,
                          compressor_api_ms=1.0, total_compression_ms=1.0,
                          cost_usd=0.0, cost_state="UNKNOWN")
    should, kind, reason = cm.should_compress("s5", 650_000, safe)
    assert not should and kind == "anti_thrash_skipped"
    # After >= required new tokens (200k) and below hard -> soft fires again.
    required = max(ANTI_THRASH_MIN_TOKENS, int(safe * 0.20))
    should, kind, _ = cm.should_compress("s5", 500_000 + required, safe)
    assert should and kind == "soft"
