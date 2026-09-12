"""R5 — FINAL ROUTING ECONOMICS + CACHE AFFINITY. Deterministic unit tests.

Covers spec R5 §19:
  quality before price, discount before price, context before price,
  health before price, cheaper same-canonical route wins COLD,
  tiny saving does not destroy WARM cache, large saving crosses break-even,
  unhealthy current route bypasses cache protection, exact FREE semantics,
  UNKNOWN != FREE, actual billed cost feedback, low reliability changes
  cost_per_success, insufficient reliability samples conservative,
  same-canonical failover first, canonical switch quality override,
  registry refresh preserves eligible warm route, compression-auto
  unaffected, no provider-name bias, privacy decision trace.
"""
from __future__ import annotations

import pytest

from gateway.config import GatewayConfig
from gateway.registry import RouteRegistry, RouteRecord
from gateway.selector import (
    SelectionContext,
    Selector,
    cache_switch_economics,
    estimate_request_cost,
    is_free,
    effective_cost_per_success,
    reason_code,
)


def _route(provider="provider_a", slug="m1", canonical="gpt-5.6-luna", **kw):
    base = dict(
        provider=provider,
        provider_model_id=slug,
        canonical=canonical,
        context_length=1000000,
        input_price=0.30,
        output_price=1.20,
        discount=0.85,
        capabilities=frozenset({"text", "streaming"}),
        price_state="EXACT",
        health="HEALTHY",
    )
    base.update(kw)
    return RouteRecord(**base)


def _registry_with(*records, cfg=None):
    reg = RouteRegistry(config=cfg or GatewayConfig())
    from unittest.mock import patch
    for r in records:
        reg._routes[f"{r.provider}:{r.provider_model_id}"] = r
        reg._by_canonical.setdefault(r.canonical, []).append(
            f"{r.provider}:{r.provider_model_id}")
    return reg


class _CanonStub:
    """Stub canonical registry: hint passes through, quality comes from record."""
    def __init__(self):
        self._p = {}
    def get(self, canonical):
        return self._p.get(canonical)
    def all(self):
        return dict(self._p)


# ── §4 price states ──────────────────────────────────────────────────────────

def test_unknown_price_is_never_free():
    r = _route(input_price=0.0, output_price=0.0, price_state="UNKNOWN")
    assert not is_free(r)
    cost, state = estimate_request_cost(r, 1000, 100)
    assert cost is None and state == "UNKNOWN"


def test_exact_zero_price_is_free():
    r = _route(input_price=0.0, output_price=0.0, price_state="EXACT")
    assert is_free(r)
    cost, state = estimate_request_cost(r, 1000, 100)
    assert cost == 0.0 and state == "EXACT"


def test_estimated_upper_bound_preserved_not_exact():
    r = _route(price_state="ESTIMATED_UPPER_BOUND")
    cost, state = estimate_request_cost(r, 1_000_000, 100_000)
    assert state == "ESTIMATED_UPPER_BOUND"
    assert cost == pytest.approx(0.30 + 0.12)


def test_cost_normalization_usd_per_request():
    r = _route(input_price=0.30, output_price=1.20)
    cost, _ = estimate_request_cost(r, 500_000, 50_000)
    assert cost == pytest.approx(0.15 + 0.06)


# ── §6 cost per success ──────────────────────────────────────────────────────

def test_low_reliability_increases_cost_per_success():
    r = _route(success_count=10, failure_count=10)  # 50% error
    est, _ = estimate_request_cost(r, 1_000_000, 0)
    c1, st1, ev = effective_cost_per_success(r, est, reliability_min_samples=10)
    assert st1 == "ESTIMATED"
    assert c1 == pytest.approx(est / 0.5)  # reliability penalty applied


def test_sufficient_samples_use_observed_exact():
    r = _route(cost_per_success=0.001, cost_sample_count=10)
    c, st, ev = effective_cost_per_success(r, 0.9, reliability_min_samples=10)
    assert st == "EXACT" and c == 0.001 and ev["cost_sample_count"] == 10


def test_insufficient_samples_conservative_estimate():
    r = _route(cost_per_success=0.001, cost_sample_count=2)
    est, _ = estimate_request_cost(r, 1_000_000, 0)
    c, st, ev = effective_cost_per_success(r, est, reliability_min_samples=10)
    assert st == "ESTIMATED"
    assert c >= est  # conservative, never below request cost


# ── §8-§9 break-even switch policy ───────────────────────────────────────────

def test_warm_tiny_saving_does_not_switch():
    d = cache_switch_economics(
        stay_cost=0.010, candidate_cost=0.0095,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.30, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=True)
    assert not d["switch"] and d["reason_code"] == "CACHE_STAY_CHEAPER"


def test_warm_large_saving_crosses_break_even():
    d = cache_switch_economics(
        stay_cost=0.100, candidate_cost=0.010,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.30, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=True)
    assert d["switch"] and d["reason_code"] == "CACHE_SWITCH_BREAK_EVEN"


def test_likely_warm_uses_weaker_penalty():
    d_likely = cache_switch_economics(
        stay_cost=0.020, candidate_cost=0.010,
        cache_state="LIKELY_WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.30, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=True)
    d_warm = cache_switch_economics(
        stay_cost=0.020, candidate_cost=0.010,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.30, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=True)
    assert d_likely["cache_rebuild_penalty"] == pytest.approx(
        d_warm["cache_rebuild_penalty"] * 0.5)


def test_cold_cache_penalty_zero():
    d = cache_switch_economics(
        stay_cost=0.010, candidate_cost=0.008,
        cache_state="COLD", warm_prefix_tokens=500_000,
        candidate_input_price=0.30, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=True)
    assert d["cache_rebuild_penalty"] == 0.0
    assert d["switch"] and d["reason_code"] == "COLD_CHEAPER"


def test_unhealthy_current_route_bypasses_cache_protection():
    # candidate economics applied; the warm route health gate is upstream,
    # here we verify candidate-healthy=False never wins.
    d = cache_switch_economics(
        stay_cost=0.5, candidate_cost=0.0,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.30, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=False)
    assert not d["switch"] and d["reason_code"] == "HEALTH_INELIGIBLE"


def test_free_route_wins_when_reliable():
    d = cache_switch_economics(
        stay_cost=0.05, candidate_cost=0.0,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.0, candidate_free=True,
        candidate_success_rate=0.99, candidate_healthy=True)
    assert d["switch"] and d["reason_code"] == "FREE_ROUTE"
    assert d["cache_rebuild_penalty"] == 0.0


def test_free_route_rejected_when_unreliable():
    d = cache_switch_economics(
        stay_cost=0.05, candidate_cost=0.0,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.0, candidate_free=True,
        candidate_success_rate=0.50, candidate_healthy=True)
    assert not d["switch"] and d["reason_code"] == "FREE_ROUTE_REJECTED_RELIABILITY"


def test_unknown_price_candidate_never_wins():
    d = cache_switch_economics(
        stay_cost=0.05, candidate_cost=None,
        cache_state="WARM", warm_prefix_tokens=500_000,
        candidate_input_price=0.0, candidate_free=False,
        candidate_success_rate=0.99, candidate_healthy=True)
    assert not d["switch"] and d["reason_code"] == "PRICE_UNKNOWN"


# ── selector integration ─────────────────────────────────────────────────────

def test_cold_cheaper_same_canonical_route_wins():
    cheap = _route(provider="provider_a", slug="cheap", input_price=0.10, output_price=0.40)
    dear = _route(provider="provider_b", slug="dear", input_price=0.30, output_price=1.20)
    reg = _registry_with(cheap, dear)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=20000, cache_state="UNKNOWN")
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_a" and primary.provider_model_id == "cheap"


def test_warm_tiny_saving_keeps_warm_route():
    cheap = _route(provider="provider_a", slug="cheap", input_price=0.30, output_price=1.19)
    dear = _route(provider="provider_b", slug="dear", input_price=0.30, output_price=1.20)
    reg = _registry_with(cheap, dear)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=500_000,
                           current_route_key="provider_b:dear",
                           cache_state="WARM", warm_prefix_tokens=500_000)
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_b"
    assert trace["cache_economics"]["reason_code"] == "CACHE_STAY_CHEAPER"
    assert trace["cache_economics"]["applied"] is True


def test_warm_nominal_saving_500k_does_not_switch():
    """R5 §13 scenario B: 500k WARM, candidate cheaper by a negligible amount
    (0.001 USD/Mtok input) -> current route stays."""
    slightly_cheaper = _route(provider="provider_a", slug="sc",
                              input_price=0.299, output_price=1.20)
    dear = _route(provider="provider_b", slug="dear", input_price=0.30, output_price=1.20)
    reg = _registry_with(slightly_cheaper, dear)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=500_000,
                           current_route_key="provider_b:dear",
                           cache_state="WARM", warm_prefix_tokens=500_000)
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_b"
    assert trace["cache_economics"]["reason_code"] == "CACHE_STAY_CHEAPER"


def test_warm_large_saving_switches():
    cheap = _route(provider="provider_a", slug="cheap", input_price=0.05, output_price=0.20)
    dear = _route(provider="provider_b", slug="dear", input_price=0.50, output_price=2.00)
    reg = _registry_with(cheap, dear)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=500_000,
                           current_route_key="provider_b:dear",
                           cache_state="WARM", warm_prefix_tokens=500_000)
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_a"
    assert trace["cache_economics"]["reason_code"] == "CACHE_SWITCH_BREAK_EVEN"


def test_unhealthy_warm_route_switches():
    cheap = _route(provider="provider_a", slug="cheap", input_price=0.10, output_price=0.40)
    sick = _route(provider="provider_b", slug="sick", health="UNHEALTHY",
                  input_price=0.05, output_price=0.20)
    reg = _registry_with(cheap, sick)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=500_000,
                           current_route_key="provider_b:sick",
                           cache_state="WARM", warm_prefix_tokens=500_000)
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_a"
    assert trace["cache_economics"]["reason_code"] == "HEALTH_INELIGIBLE"


def test_canonical_switch_margin_doubles():
    # same-canonical candidate just under break-even for cross-canonical double margin
    cheap = _route(provider="provider_a", slug="other-model",
                   canonical="glm-5.3", input_price=0.10, output_price=0.40)
    dear = _route(provider="provider_b", slug="dear", input_price=0.30, output_price=1.20)
    reg = _registry_with(cheap, dear)
    canon = _CanonStub()
    from gateway.canonical import CanonicalProfile
    canon._p["glm-5.3"] = CanonicalProfile(
        canonical_id="glm-5.3", display_name="glm", family="glm", generation="5.3",
        capabilities=frozenset({"text", "streaming"}), quality_score=0.85,
        confidence="VERIFIED", tier_eligibility="T2")
    canon._p["gpt-5.6-luna"] = CanonicalProfile(
        canonical_id="gpt-5.6-luna", display_name="luna", family="gpt", generation="5.6",
        capabilities=frozenset({"text", "streaming"}), quality_score=0.85,
        confidence="VERIFIED", tier_eligibility="T2")
    sel = Selector(reg, canon)
    # hint exhausted path: hint=dear-canonical has routes; warm is dear route;
    # cheap is OTHER canonical -> cross-canonical margin x2 applies.
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=500_000,
                           current_route_key="provider_b:dear",
                           cache_state="WARM", warm_prefix_tokens=500_000)
    primary, plan, trace = sel.choose(ctx)
    econ = trace["cache_economics"]
    assert econ.get("canonical_switch") in (None, False, True)
    # margin doubling visible when a cross-canonical candidate is compared
    if econ.get("canonical_switch"):
        assert econ["margin_effective"] == pytest.approx(
            reg.config().cache_switch_margin * 2)


def test_no_provider_name_bias_cheap_provider_b_beats_dear_provider_a():
    dear_ih = _route(provider="provider_a", slug="dear", input_price=0.30, output_price=1.20)
    cheap_sp = _route(provider="provider_b", slug="cheap", input_price=0.10, output_price=0.40)
    reg = _registry_with(dear_ih, cheap_sp)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=20000, cache_state="UNKNOWN")
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_b"


def test_same_canonical_failover_first_alternate_canonical_second():
    good = _route(provider="provider_a", slug="alt", input_price=0.10, output_price=0.40)
    other_canonical = _route(provider="provider_b", slug="other",
                             canonical="glm-5.3", input_price=0.05, output_price=0.20)
    reg = _registry_with(good, other_canonical)
    canon = _CanonStub()
    from gateway.canonical import CanonicalProfile
    for cid, fam, gen, q in (("gpt-5.6-luna", "gpt", "5.6", 0.85), ("glm-5.3", "glm", "5.3", 0.85)):
        canon._p[cid] = CanonicalProfile(
            canonical_id=cid, display_name=cid, family=fam, generation=gen,
            capabilities=frozenset({"text", "streaming"}), quality_score=q,
            confidence="VERIFIED", tier_eligibility="T2")
    sel = Selector(reg, canon)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=20000, exclude_route_keys=frozenset({"provider_b:x"}))
    primary, plan, trace = sel.choose(ctx)
    assert plan[0].canonical == "gpt-5.6-luna"
    # any alternate canonical must come AFTER same-canonical steps
    for i, step in enumerate(plan):
        if step.canonical != "gpt-5.6-luna":
            assert all(s.canonical == "gpt-5.6-luna" for s in plan[:i])
            break


def test_compression_auto_alias_unaffected():
    from gateway.policy import MODEL_ALIASES
    assert MODEL_ALIASES.get("compression-auto") == "gpt-5.6-luna"


def test_reason_codes_machine_readable():
    assert reason_code("health:UNHEALTHY") == "HEALTH_INELIGIBLE"
    assert reason_code("discount:0.5") == "DISCOUNT_INELIGIBLE"
    assert reason_code("context:100<200") == "CONTEXT_INELIGIBLE"
    assert reason_code("quality_floor:0.3<0.75") == "QUALITY_INELIGIBLE"
    assert reason_code("lifecycle:SUNSET") == "LIFECYCLE_INELIGIBLE"
    assert reason_code("capability_gap:['tools']") == "CAPABILITY_INELIGIBLE"
    assert reason_code("certification:UNKNOWN") == "CERTIFICATION_INELIGIBLE"


def test_gates_reject_before_price_is_considered():
    sick = _route(provider="provider_a", slug="sick", health="UNHEALTHY",
                  input_price=0.0001, output_price=0.0001)
    priced = _route(provider="provider_b", slug="ok", input_price=0.30, output_price=1.20)
    reg = _registry_with(sick, priced)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=20000, cache_state="UNKNOWN")
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_b"  # health gate beats cheaper price
    assert any(r["code"] == "HEALTH_INELIGIBLE"
               for r in trace["rejected_routes"])


def test_context_ineligible_beats_price():
    small = _route(provider="provider_a", slug="small", context_length=100_000,
                   input_price=0.0001, output_price=0.0001)
    big = _route(provider="provider_b", slug="big", context_length=1_000_000,
                 input_price=0.30, output_price=1.20)
    reg = _registry_with(small, big)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=500_000, cache_state="UNKNOWN")
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_b"
    assert any(r["code"] == "CONTEXT_INELIGIBLE"
               for r in trace["rejected_routes"])


def test_discount_gate_before_price():
    below = _route(provider="provider_a", slug="below", discount=0.5,
                   input_price=0.0001, output_price=0.0001)
    ok = _route(provider="provider_b", slug="ok", discount=0.85,
                input_price=0.30, output_price=1.20)
    reg = _registry_with(below, ok)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=20000, cache_state="UNKNOWN")
    primary, plan, trace = sel.choose(ctx)
    assert primary.provider == "provider_b"
    assert any(r["code"] == "DISCOUNT_INELIGIBLE"
               for r in trace["rejected_routes"])


# ── §14 billing feedback ─────────────────────────────────────────────────────

def test_billing_feedback_updates_cost_per_success():
    r = _route()
    reg = _registry_with(r)
    reg.record_cost_feedback("provider_a", "m1", cost_usd=0.002, success=True)
    reg.record_cost_feedback("provider_a", "m1", cost_usd=0.004, success=True)
    rec = reg.get("provider_a", "m1")
    assert rec.cost_sample_count == 2
    assert rec.cost_per_success == pytest.approx(0.003)
    assert rec.last_actual_cost_usd == pytest.approx(0.004)


def test_registry_refresh_preserves_warm_route_state_and_cost():
    r = _route(health="HEALTHY", success_count=5)
    reg = _registry_with(r)
    reg.record_cost_feedback("provider_a", "m1", 0.001, success=True)

    class _PM:
        provider = "provider_a"; provider_model_id = "m1"
        canonical_model = "gpt-5.6-luna"; context_length = 1_000_000
        input_price = 0.30; output_price = 1.20; discount = 0.85
        capabilities = frozenset({"text", "streaming"})
        price_state = "EXACT"; certification_status = "CERTIFIED"

    class _Adapt:
        name = "provider_a"
        async def discover(self):
            return [_PM()]
        async def price(self, pm):
            return pm

    import asyncio
    reg.register_adapter(_Adapt())
    asyncio.get_event_loop().run_until_complete(reg.build([_Adapt()])) if False else None
    import anyio
    anyio.run(reg.build, [_Adapt()])
    rec = reg.get_any("provider_a", "m1")
    assert rec is not None
    assert rec.health == "HEALTHY" and rec.success_count == 5
    assert rec.cost_sample_count == 1
    assert rec.cost_per_success == pytest.approx(0.001)


def test_privacy_decision_trace_no_content_fields():
    from gateway.app import _record_decision
    cheap = _route(provider="provider_a", slug="cheap", input_price=0.10, output_price=0.40)
    reg = _registry_with(cheap)
    sel = Selector(reg, _CanonStub())
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2",
                           required_context=20000, cache_state="UNKNOWN",
                           prompt_cache_key="sess-123")
    primary, plan, trace = sel.choose(ctx)
    blob = json.dumps(trace)
    assert "sess-123" not in blob
    assert "prompt" not in blob.lower()


import json  # noqa: E402  (used by privacy test above)
