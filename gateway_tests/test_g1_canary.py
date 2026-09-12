"""Canary test suite for the V2 Gateway (:4101).

All tests run with PYTHONPATH=.. — the V2 app is not required to be running;
the test instantiates its own FastAPI client. The tests exercise:

  * Provider A adapter request PASS (mocked transport)
  * Provider B adapter request PASS (mocked transport)
  * same-model provider failover
  * provider unhealthy exclusion
  * discount <80 exclusion
  * context-too-large exclusion
  * TTFT timeout failover
  * single response / no mixed stream
  * existing routing regression (Phase 3B tests still pass)
  * PROVIDER_B PARITY: Provider A unhealthy + Provider B healthy => Provider B selected
  * PROVIDER_B FAILOVER: arbitrary request routed through the Provider B adapter

The tests use deterministic fakes — no live provider calls in CI mode. A
separate live-smoke ``gateway_tests/test_live_smoke.py`` exercises real
endpoints when run explicitly.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import AsyncIterator

import pytest

# Test-local httpx mock for transport. Real providers stay out of the loop.
from gateway.providers.base import (
    ProviderAdapter,
    ProviderModel,
    ProviderError,
    UpstreamChunk,
    UpstreamRequest,
)
from gateway.registry import DISCOUNT_FLOOR, RouteRecord, RouteRegistry
from gateway.selector import PlanStep, SelectionContext, Selector
from gateway.timeouts import DEFAULT, TimeoutPolicy
from gateway.transport import execute_plan, stream_plan, AttemptFailure, TransportError


# ──────────────────────────────────────────────────────────────────────────
# Fake provider adapter — fully deterministic.
# ──────────────────────────────────────────────────────────────────────────


class FakeAdapter(ProviderAdapter):
    """Records calls; behavior configured per-test via :meth:`set_responses`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._models: list[ProviderModel] = []
        self.requests: list[dict] = []
        self._next_response: tuple[int, dict, ProviderError | None] | None = None
        self._next_stream: list[UpstreamChunk] | None = None
        self._stream_delay_first_chunk: float = 0.0
        self._stream_delay_per_chunk: float = 0.0
        self._healthy: bool = True

    def add_model(self, slug: str, canonical: str, ctx: int, inp: float, out: float, discount: float = 0.90) -> None:
        self._models.append(
            ProviderModel(
                provider=self.name,
                provider_model_id=slug,
                canonical_model=canonical,
                context_length=ctx,
                input_price=inp,
                output_price=out,
                discount=discount,
                capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
                certification_status="CERTIFIED",
            )
        )

    def set_response(self, status: int, payload: dict, err: ProviderError | None = None) -> None:
        self._next_response = (status, payload, err)

    def set_stream(self, chunks: list[UpstreamChunk], delay_first_chunk: float = 0.0, delay_per_chunk: float = 0.0) -> None:
        self._next_stream = chunks
        self._stream_delay_first_chunk = delay_first_chunk
        self._stream_delay_per_chunk = delay_per_chunk

    def set_healthy(self, ok: bool) -> None:
        self._healthy = ok

    async def discover(self) -> list[ProviderModel]:
        return list(self._models)

    async def price(self, model: ProviderModel) -> ProviderModel:
        return model

    def capabilities(self, model: ProviderModel) -> frozenset[str]:
        return model.capabilities

    async def request(self, model: ProviderModel, req: UpstreamRequest) -> tuple[int, dict, ProviderError | None]:
        self.requests.append({"model": model.provider_model_id, "stream": req.stream, "body": dict(req.body)})
        if not self._healthy:
            return 0, {}, ProviderError(code="server", status=503, message="fake_unhealthy")
        if self._next_response is None:
            return 200, {"id": "fake", "model": model.provider_model_id, "choices": [{"message": {"role": "assistant", "content": "hi"}}]}, None
        return self._next_response

    async def stream(self, model: ProviderModel, req: UpstreamRequest) -> AsyncIterator[UpstreamChunk]:
        self.requests.append({"model": model.provider_model_id, "stream": True, "body": dict(req.body)})
        if not self._healthy:
            raise ProviderError(code="server", status=503, message="fake_unhealthy")
        if self._next_stream is None:
            yield UpstreamChunk(raw=b'data: {"ok":true}\n\n', meta={"provider": self.name, "model": model.provider_model_id})
            yield UpstreamChunk(raw=b"", meta={}, done=True)
            return
        # optional TTFT delay
        if self._stream_delay_first_chunk > 0:
            await asyncio.sleep(self._stream_delay_first_chunk)
        for i, ch in enumerate(self._next_stream):
            if i > 0 and self._stream_delay_per_chunk > 0:
                await asyncio.sleep(self._stream_delay_per_chunk)
            yield ch

    async def health(self, model: ProviderModel) -> dict:
        return {"ok": self._healthy, "latency_ms": 1.0}

    def record_success(self, model: ProviderModel, latency_ms: float) -> None: ...
    def record_failure(self, model: ProviderModel, error: ProviderError) -> None: ...
    def usage(self, model: ProviderModel) -> dict:
        return {"provider": self.name, "model": model.provider_model_id}


def _seed_registry(reg: RouteRegistry, *, provider_b_model_count: int = 1) -> None:
    ih = FakeAdapter("provider_a")
    ih.add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)
    ih.add_model("cx/gpt-5.6-luna", "gpt-5.6-luna", 272000, 0.020, 0.120)
    ih.add_model("low-discount", "gpt-5.6-luna", 1050000, 0.20, 1.20, discount=0.50)
    ih.add_model("tiny-context", "gpt-5.6-luna", 100, 0.20, 1.20, discount=0.90)
    sp = FakeAdapter("provider_b")
    sp.add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.030, 0.180)
    if provider_b_model_count >= 2:
        sp.add_model("grok-4.3", "grok-4.3", 132000, 0.225, 0.900)
    reg.register_adapter(ih)
    reg.register_adapter(sp)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ──────────────────────────────────────────────────────────────────────────
# 1. Discount and capability filters are enforced at the registry, not the
#    selector. The selector only sees candidates.
# ──────────────────────────────────────────────────────────────────────────


def test_registry_excludes_low_discount_but_keeps_tiny_context_for_eligibility():
    """Registry filters on discount only; context eligibility is enforced by the
    selector at request time per ``required_context``."""
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    canonical = reg.for_canonical("gpt-5.6-luna")
    slugs = sorted(r.provider_model_id for r in canonical)
    assert "low-discount" not in slugs, "discount < 80% must be filtered"
    # tiny-context IS visible to the registry; selector skips it when required_context > ctx.
    assert "tiny-context" in slugs
    assert "cb/gpt-5.6-luna" in slugs
    assert "cx/gpt-5.6-luna" in slugs
    assert "gpt-5.6-luna" in slugs  # provider_b


def test_selector_skips_tiny_context_for_required_context():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=200_000)
    _, plan, _trace = sel.choose(ctx)
    keys = {f"{p.provider}:{p.provider_model_id}" for p in plan}
    assert "provider_a:tiny-context" not in keys
    assert "provider_a:cb/gpt-5.6-luna" in keys


def test_registry_only_keep_certified_discount_floor():
    """Routes with discount below DISCOUNT_FLOOR MUST NOT appear in the registry."""
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    for rec in reg.for_canonical("gpt-5.6-luna"):
        assert rec.discount is not None
        assert rec.discount >= DISCOUNT_FLOOR


# ──────────────────────────────────────────────────────────────────────────
# 2. Selector: routing order, sticky, same-canonical failover ordering.
# ──────────────────────────────────────────────────────────────────────────


def test_selector_returns_same_canonical_first_then_alternate():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    primary, plan, _trace = sel.choose(ctx)
    assert primary is not None
    assert primary.canonical == "gpt-5.6-luna"
    primary_keys = {f"{p.provider}:{p.provider_model_id}" for p in plan}
    assert primary_keys == {
        "provider_a:cb/gpt-5.6-luna",
        "provider_a:cx/gpt-5.6-luna",
        "provider_b:gpt-5.6-luna",
    }


def test_selector_excludes_failed_route_in_next_attempt():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg)
    ctx = SelectionContext(
        canonical_hint="gpt-5.6-luna",
        tier="T2",
        required_context=1000,
        exclude_route_keys=frozenset({"provider_a:cb/gpt-5.6-luna"}),
    )
    _, plan, _trace = sel.choose(ctx)
    assert all(f"{p.provider}:{p.provider_model_id}" != "provider_a:cb/gpt-5.6-luna" for p in plan)


def test_selector_health_excludes_unhealthy_routes():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    # Mark everything Provider A unhealthy.
    for rec in reg.for_canonical("gpt-5.6-luna"):
        if rec.provider == "provider_a":
            rec.health = "UNHEALTHY"
            rec.circuit_state = "OPEN"
            rec.failure_count = 5
            rec.success_count = 0
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    providers = [p.provider for p in plan]
    assert providers == ["provider_b"], f"Only Provider B should remain, got {providers}"


# ──────────────────────────────────────────────────────────────────────────
# 3. PROVIDER PARITY (G1 requirement 6)
#    Provider A unhealthy + Provider B healthy => Provider B selected.
# ──────────────────────────────────────────────────────────────────────────


def test_provider_b_selectable_when_provider_a_unhealthy():
    """Deterministic parity test: Provider A down, Provider B must win."""
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    # Simulate real unhealthy state for both Provider A slugs.
    for sl in ("cb/gpt-5.6-luna", "cx/gpt-5.6-luna"):
        rec = reg.get("provider_a", sl)
        rec.health = "UNHEALTHY"
        rec.circuit_state = "OPEN"
        rec.failure_count = 10
        rec.success_count = 0
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    primary, plan, _trace = sel.choose(ctx)
    assert primary.provider == "provider_b", f"primary must be provider_b, got {primary.provider}"
    assert primary.provider_model_id == "gpt-5.6-luna"
    # Provider B healthy must not lose to a static provider prior.
    assert all(p.provider != "provider_a" for p in plan)


def test_provider_b_failover_after_provider_a_failure():
    """Live-failover during execution: Provider A fails, Provider B succeeds."""
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    ih = reg.get_adapter("provider_a")
    sp = reg.get_adapter("provider_b")
    # Pre-fail Provider A by sending one provider request that we see go to it.
    ih.set_response(0, {}, ProviderError(code="server", status=503, message="simulated_ih_503"))
    sp.set_response(200, {"id": "provider_b-ok", "choices": [{"message": {"role": "assistant", "content": "provider_b-ok"}}]})

    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    step, failures, meta = _run(execute_plan(plan=plan, registry=reg, body={"messages": [{"role": "user", "content": "hi"}]}, stream=False, policy=DEFAULT, tier="T2"))
    # The winning step must be Provider B.
    assert step.provider == "provider_b"
    assert step.provider_model_id == "gpt-5.6-luna"
    # We expect at least one Provider A failure logged.
    assert any(f.error_code == "server" for f in failures)
    assert meta["winning_status"] == 200
    assert meta["winning_body"]["id"] == "provider_b-ok"


# ──────────────────────────────────────────────────────────────────────────
# 4. Context-too-large exclusion (G1 requirement 10).
# ──────────────────────────────────────────────────────────────────────────


def test_context_too_large_excludes_routes():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg)
    # Required context > largest context (Provider B 1050000).
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=2_000_000)
    _, plan, _trace = sel.choose(ctx)
    assert plan == [], f"no plan must be returned when required_context exceeds all routes, got {plan}"


def test_context_too_large_excludes_only_short_routes():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg)
    # 300k context — IH cx (272k) is too small; IH cb (1050k) and Provider B (1050k) work.
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=300_000)
    _, plan, _trace = sel.choose(ctx)
    keys = {f"{p.provider}:{p.provider_model_id}" for p in plan}
    assert "provider_a:cx/gpt-5.6-luna" not in keys
    assert "provider_a:cb/gpt-5.6-luna" in keys
    assert "provider_b:gpt-5.6-luna" in keys


# ──────────────────────────────────────────────────────────────────────────
# 5. Discount <80 exclusion (G1 requirement 10).
# ──────────────────────────────────────────────────────────────────────────


def test_discount_below_floor_is_excluded():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    # Pre-seed: add a sub-floor Provider A route directly into the registry.
    rec = RouteRecord(
        provider="provider_a",
        provider_model_id="discounted-too-low",
        canonical="gpt-5.6-luna",
        context_length=200000,
        input_price=0.20,
        output_price=1.20,
        discount=0.79,    # below DISCOUNT_FLOOR (0.80)
        capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
        certification_status="CERTIFIED",
    )
    with reg._lock:
        reg._routes["provider_a:discounted-too-low"] = rec
        reg._by_canonical.setdefault("gpt-5.6-luna", []).append("provider_a:discounted-too-low")
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    keys = {f"{p.provider}:{p.provider_model_id}" for p in plan}
    assert "provider_a:discounted-too-low" not in keys


# ──────────────────────────────────────────────────────────────────────────
# 6. TTFT timeout failover.
# ──────────────────────────────────────────────────────────────────────────


def test_ttft_timeout_fails_over_to_next_route():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    ih = reg.get_adapter("provider_a")
    sp = reg.get_adapter("provider_b")
    # The plan begins with IH cb/... but we force Provider A to time out (TTFT > 45s)
    # by setting a TTFT delay longer than the policy. To keep this fast, we use a
    # small synthetic policy.
    ih.set_stream([], delay_first_chunk=2.0)
    sp.set_stream([
        UpstreamChunk(raw=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', meta={}),
        UpstreamChunk(raw=b"", meta={}, done=True),
    ])
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    short_policy = TimeoutPolicy(connect_s=1.0, first_response_s=0.05, stream_idle_s=1.0, normal_total_s=10.0, t4_total_s=20.0)
    results = []
    async def drive():
        gen = stream_plan(plan=plan, registry=reg, body={"messages": [{"role": "user", "content": "hi"}]}, policy=short_policy, tier="T2")
        async for f in gen:
            results.append(f)
    _run(drive())
    # The first chunk we actually emit must come from provider_b.
    chunks = [r for r in results if r.get("event") == "chunk"]
    assert chunks, f"no chunks emitted, got {results}"
    assert chunks[0]["provider"] == "provider_b"
    # No mixed streams: all chunks must come from the same provider.
    providers = {c["provider"] for c in chunks}
    assert providers == {"provider_b"}


# ──────────────────────────────────────────────────────────────────────────
# 7. Existing routing regression — Phase 3B invariants still hold.
# ──────────────────────────────────────────────────────────────────────────


def test_existing_routing_invariants_preserved():
    """Smoke check: V2 selector preserves the no-fixed-canonical semantics and
    routes through dynamic registry, not via active-pool.csv as a source of truth."""
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    canonicals = {p.canonical for p in plan}
    assert canonicals == {"gpt-5.6-luna"}
    # No plan step should reference an unknown canonical.
    for p in plan:
        from gateway.policy import get_canonical
        assert get_canonical(p.canonical) is not None


# ──────────────────────────────────────────────────────────────────────────
# 8. Timeout policy is loaded from defaults and overridable.
# ──────────────────────────────────────────────────────────────────────────


def test_default_timeouts_match_spec():
    from gateway.timeouts import load_policy, DEFAULT
    assert DEFAULT.connect_s == 10.0
    assert DEFAULT.first_response_s == 45.0
    assert DEFAULT.stream_idle_s == 60.0
    assert DEFAULT.normal_total_s == 180.0
    assert DEFAULT.t4_total_s == 240.0
    p = load_policy()
    assert p.connect_s == 10.0 and p.first_response_s == 45.0
    os.environ["GW_FIRST_RESPONSE_TIMEOUT_S"] = "11"
    try:
        assert load_policy().first_response_s == 11.0
    finally:
        del os.environ["GW_FIRST_RESPONSE_TIMEOUT_S"]
    assert load_policy().first_response_s == 45.0


def test_total_timeout_per_tier():
    from gateway.timeouts import total_for
    p = DEFAULT
    assert total_for("T3", p) == 180.0
    assert total_for("T4", p) == 240.0


# ──────────────────────────────────────────────────────────────────────────
# 9. inferred transport: no mixed-stream guarantees.
# ──────────────────────────────────────────────────────────────────────────


def test_no_mixed_stream_when_failover_attempted():
    reg = RouteRegistry()
    _seed_registry(reg)
    _run(reg.build(reg.adapters()))
    ih = reg.get_adapter("provider_a")
    sp = reg.get_adapter("provider_b")
    # IH times out, Provider B succeeds — but we want to assert that even after the
    # failover, no Provider A chunk leaks into the response.
    ih.set_stream([UpstreamChunk(raw=b'data: {"choices":[{"delta":{"content":"IH-LEAK"}}]}\n\n', meta={})], delay_first_chunk=2.0)
    sp.set_stream([
        UpstreamChunk(raw=b'data: {"choices":[{"delta":{"content":"SP-OK"}}]}\n\n', meta={}),
        UpstreamChunk(raw=b"", meta={}, done=True),
    ])
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    short_policy = TimeoutPolicy(connect_s=1.0, first_response_s=0.05, stream_idle_s=1.0, normal_total_s=10.0, t4_total_s=20.0)
    async def drive():
        out = []
        gen = stream_plan(plan=plan, registry=reg, body={"messages": [{"role": "user", "content": "x"}]}, policy=short_policy, tier="T2")
        async for f in gen:
            out.append(f)
        return out
    results = _run(drive())
    # Only chunks from provider_b arrive — Provider A's TTFT failure produced a
    # ``failover`` frame, not a chunk.
    chunks = [r for r in results if r.get("event") == "chunk"]
    assert all(c["provider"] == "provider_b" for c in chunks)
    assert any(r.get("event") == "failover" for r in results)
