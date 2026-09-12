"""G2 test suite — dynamic model + provider routing (:4101).

Deterministic fakes only; no live provider calls. Covers (spec G2 §22):
  * canonical/provider separation (LEVEL A vs LEVEL B)
  * dynamic catalog registry (no-restart discovery add/remove)
  * discount gate (>=80, per-provider config)
  * health TTL decay + circuit recovery
  * same-canonical failover ordering (route B before alternate canonical)
  * Provider B parity scenarios A–E (no provider-name prior)
  * unknown frontier model handling (QUALITY_UNKNOWN_FRONTIER)
  * context eligibility (route-level safe_context)
  * quality-first selection
  * provider share metrics
  * decision explainability
  * no prompt logging (telemetry never carries raw content)
"""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from gateway.canonical import CanonicalRegistry
from gateway.classifier import classify, tier_for_class, capabilities_for_class
from gateway.config import GatewayConfig, ProviderConfig
from gateway.metrics import Metrics
from gateway.providers.base import ProviderAdapter, ProviderModel, ProviderError, UpstreamChunk, UpstreamRequest
from gateway.registry import RouteRegistry
from gateway.selector import SelectionContext, Selector, safe_context


def _run(coro):
    return asyncio.run(coro)


class FakeAdapter(ProviderAdapter):
    def __init__(self, name: str) -> None:
        self.name = name
        self._models: list[ProviderModel] = []
        self.requests: list[dict] = []

    def add_model(self, slug: str, canonical: str, ctx: int, inp: float, out: float, discount: float = 0.90,
                  price_state: str = "EXACT") -> None:
        self._models.append(ProviderModel(
            provider=self.name, provider_model_id=slug, canonical_model=canonical,
            context_length=ctx, input_price=inp, output_price=out, discount=discount,
            price_state=price_state,
            capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
            certification_status="CERTIFIED",
        ))

    async def discover(self) -> list[ProviderModel]:
        return list(self._models)

    async def price(self, model: ProviderModel) -> ProviderModel:
        return model

    def capabilities(self, model: ProviderModel) -> frozenset[str]:
        return model.capabilities

    async def request(self, model: ProviderModel, req: UpstreamRequest):
        self.requests.append({"model": model.provider_model_id})
        return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, None

    async def stream(self, model: ProviderModel, req: UpstreamRequest):
        self.requests.append({"model": model.provider_model_id, "stream": True})
        yield UpstreamChunk(raw=b'data: {"ok":true}\n\n', meta={})
        yield UpstreamChunk(raw=b"", meta={}, done=True)

    async def health(self, model: ProviderModel) -> dict:
        return {"ok": True}

    def record_success(self, model, latency_ms): ...
    def record_failure(self, model, error): ...
    def usage(self, model) -> dict:
        return {}


def _mk_registry(models_by_provider: dict[str, list[tuple]]) -> tuple[RouteRegistry, CanonicalRegistry, dict[str, FakeAdapter]]:
    cfg = GatewayConfig(
        min_discount=0.80,
        quality_first=True,
        providers={
            "provider_a": ProviderConfig("provider_a", True, 0.80),
            "provider_b": ProviderConfig("provider_b", True, 0.80),
        },
    )
    reg = RouteRegistry(config=cfg)
    canon = CanonicalRegistry()
    canon.rebuild()
    adapters: dict[str, FakeAdapter] = {
        "provider_a": FakeAdapter("provider_a"),
        "provider_b": FakeAdapter("provider_b"),
    }
    for ad in adapters.values():
        reg.register_adapter(ad)
    for pname, models in models_by_provider.items():
        for m in models:
            adapters[pname].add_model(*m)
    return reg, canon, adapters


CTX = frozenset({"text", "streaming"})


def _sel_ctx(canonical: str, *, tier: str = "T4", req_ctx: int = 0, **kw) -> SelectionContext:
    return SelectionContext(canonical_hint=canonical, tier=tier, required_context=req_ctx,
                            capabilities_required=CTX, task_class="CRITICAL", **kw)


# ── 1. canonical / provider separation ────────────────────────────────────

def test_level_a_candidate_selection_uses_quality_not_provider():
    """T4 task with quality floor: unknown-quality frontier models are not
    candidates; verified ones are. Provider names play no role."""
    reg, canon, _ = _mk_registry({})
    ctx = SelectionContext(tier="T4", task_class="CRITICAL", required_context=0, capabilities_required=CTX)
    accepted, rejected, _ = Selector(reg, canon).candidate_canonicals(ctx)
    names = set(accepted)
    assert "gpt-5.6-luna" in names and "grok-4.3" in names          # VERIFIED
    assert "gpt-5.6-sol" not in names and "gpt-6-astra" not in names  # UNKNOWN frontier


def test_unknown_frontier_is_rejected_for_high_tiers_but_not_artificially_weak():
    reg, canon, _ = _mk_registry({})
    sel = Selector(reg, canon)
    ctx = _sel_ctx("gpt-6-astra")
    accepted, rejected, _ = sel.candidate_canonicals(ctx)
    assert rejected and "quality_unknown" in rejected[0][1]
    # explicit-policy escape (canary / benchmark / fallback policy) works:
    ctx2 = _sel_ctx("gpt-6-astra", allow_unknown_quality=True)
    accepted2, _, _ = sel.candidate_canonicals(ctx2)
    assert accepted2 == ["gpt-6-astra"]


def test_unknown_frontier_profile_status_is_quality_unknown_frontier():
    reg, canon, _ = _mk_registry({})
    prof = canon.get("gpt-5.6-sol")
    assert prof is not None
    assert prof.confidence == "UNKNOWN"
    assert prof.quality_frontier is True


# ── 2. route registry / discount gate ─────────────────────────────────────

def test_registry_discount_gate_per_provider():
    reg, _, adapters = _mk_registry({
        "provider_a": [("cb/sol", "gpt-5.6-sol", 200000, 0.10, 0.50, 0.85)],
        "provider_b": [("gpt-5.6-sol", "gpt-5.6-sol", 200000, 0.12, 0.60, 0.79)],
    })
    _run(reg.build(reg.adapters()))
    routes = reg.for_canonical("gpt-5.6-sol")
    providers = {r.provider for r in routes}
    assert providers == {"provider_a"}  # provider_b 79% excluded by gate


def test_registry_provider_b_appears_when_discount_recovers():
    reg, _, adapters = _mk_registry({
        "provider_a": [("cb/sol", "gpt-5.6-sol", 200000, 0.10, 0.50, 0.85)],
        "provider_b": [("gpt-5.6-sol", "gpt-5.6-sol", 200000, 0.12, 0.60, 0.79)],
    })
    _run(reg.build(reg.adapters()))
    assert reg.get("provider_b", "gpt-5.6-sol") is None
    # tomorrow: Sol appears at >=80 on Provider B — rebuild discovers it (§11).
    # ProviderModel is frozen in production; reseed via dataclasses.replace()
    # instead of mutating in place — keeps the production domain immutable.
    old = adapters["provider_b"]._models[0]
    adapters["provider_b"]._models[0] = dataclasses.replace(old, discount=0.85)
    _run(reg.build(reg.adapters()))
    assert reg.get("provider_b", "gpt-5.6-sol") is not None
    # and when it disappears again, it is dropped without restart
    adapters["provider_b"]._models.clear()
    _run(reg.build(reg.adapters()))
    assert reg.get("provider_b", "gpt-5.6-sol") is None


def test_disabled_provider_is_not_built():
    cfg = GatewayConfig(min_discount=0.80, providers={
        "provider_a": ProviderConfig("provider_a", True, 0.80),
        "provider_b": ProviderConfig("provider_b", False, 0.80),
    })
    reg = RouteRegistry(config=cfg)
    ih = FakeAdapter("provider_a"); ih.add_model("cb/luna", "gpt-5.6-luna", 1000000, 0.02, 0.12)
    sp = FakeAdapter("provider_b"); sp.add_model("gpt-5.6-luna", "gpt-5.6-luna", 1000000, 0.03, 0.18)
    reg.register_adapter(ih); reg.register_adapter(sp)
    _run(reg.build(reg.adapters()))
    assert reg.get("provider_a", "cb/luna") is not None
    assert reg.get("provider_b", "gpt-5.6-luna") is None  # disabled


# ── 3. Provider B parity scenarios (spec G2 §12) ────────────────────────────

def _seed_parity(reg: RouteRegistry, adapters: dict[str, FakeAdapter]) -> None:
    adapters["provider_a"].add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)
    adapters["provider_b"].add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.015, 0.090)
    _run(reg.build(reg.adapters()))


def test_parity_A_provider_b_wins_when_cheaper():
    """A: both healthy, Provider B cheaper → Provider B CAN WIN."""
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    sel = Selector(reg, canon)
    primary, plan, trace = sel.choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_b"
    # plan retains same-canonical failover
    assert [p.provider for p in plan] == ["provider_b", "provider_a"]


def test_parity_B_provider_b_must_win_when_provider_a_unhealthy():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    for rec in reg.for_canonical("gpt-5.6-luna"):
        if rec.provider == "provider_a":
            rec.health = "DOWN"
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_b"
    assert all(p.provider == "provider_b" for p in plan)


def test_parity_C_provider_a_wins_when_provider_b_unhealthy():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    for rec in reg.for_canonical("gpt-5.6-luna"):
        if rec.provider == "provider_b":
            rec.health = "DOWN"
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_a"


def test_parity_D_provider_b_discount_below_80_is_excluded():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    # ProviderModel is frozen in production; reseed via dataclasses.replace().
    old = adapters["provider_b"]._models[0]
    adapters["provider_b"]._models[0] = dataclasses.replace(old, discount=0.75)
    _run(reg.build(reg.adapters()))
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_a"
    assert any(r["route"] == "provider_b:gpt-5.6-luna" and r["reason"].startswith("discount")
               for r in trace["rejected_routes"])


def test_parity_E_provider_b_recovers_after_ttl_and_is_selectable_again():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    # failed: circuit opens
    rec = reg.get("provider_b", "gpt-5.6-luna")
    rec.circuit_state = "OPEN"
    # Place failure in the past so ttl_failure_s=30 has already elapsed when
    # apply_health_ttl runs (no time-travel / no sleep in tests).
    rec.last_failure_ts = time.time() - 60
    rec.last_success_ts = time.time() - 1000
    primary, _, _ = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_a"
    # failure TTL expires → HALF_OPEN → selectable again (short TTL for test)
    reg.apply_health_ttl(ttl_success_s=300.0, ttl_failure_s=30.0)
    primary, _, _ = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_b"


def test_no_provider_name_prior_anywhere_in_ranking():
    """With identical metrics except price, the cheaper route wins regardless
    of which provider it belongs to; swapping prices swaps the winner."""
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    sel = Selector(reg, canon)
    for rec in reg.for_canonical("gpt-5.6-luna"):
        rec.health = "HEALTHY"; rec.success_count = 5
    primary, _, _ = sel.choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_b"  # cheaper
    for rec in reg.for_canonical("gpt-5.6-luna"):
        pass
    # flip prices
    sp = reg.get("provider_b", "gpt-5.6-luna"); ih = reg.get("provider_a", "cb/gpt-5.6-luna")
    sp.input_price, sp.output_price, ih.input_price, ih.output_price = ih.input_price, ih.output_price, sp.input_price, sp.output_price
    primary, _, _ = sel.choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_a"  # now IH cheaper → IH wins


# ── 4. same-canonical failover ordering ───────────────────────────────────

def test_same_canonical_failover_before_alternate_canonical():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    adapters["provider_a"].add_model("cb/glm-5.3", "glm-5.3", 1000000, 0.10, 0.40)
    _run(reg.build(reg.adapters()))
    ctx = _sel_ctx("gpt-5.6-luna", exclude_route_keys=frozenset({"provider_b:gpt-5.6-luna"}))
    primary, plan, trace = Selector(reg, canon).choose(ctx)
    assert [p.provider for p in plan][:2] == ["provider_a", "provider_a"]  # same canonical routes first
    assert plan[-1].canonical == "glm-5.3"  # alternate canonical LAST


def test_context_eligibility_route_level_safe_context():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    adapters["provider_a"].add_model("cx/gpt-5.6-luna", "gpt-5.6-luna", 272000, 0.02, 0.12)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg, canon)
    # 500k required → cx (272k) ineligible, 1M routes eligible; NOT a failure/circuit event
    primary, plan, trace = sel.choose(_sel_ctx("gpt-5.6-luna", req_ctx=500_000))
    routes = {f"{p.provider}:{p.provider_model_id}" for p in plan}
    assert "provider_a:cx/gpt-5.6-luna" not in routes
    assert "provider_b:gpt-5.6-luna" in routes
    assert all(p.context_eligible for p in plan)
    rec = reg.get("provider_a", "cx/gpt-5.6-luna")
    assert rec.circuit_state != "OPEN" and rec.health not in ("DOWN", "UNHEALTHY")
    # required context exceeds every route → empty plan, still no circuit events
    primary, plan, trace = sel.choose(_sel_ctx("gpt-5.6-luna", req_ctx=2_000_000))
    assert plan == []
    assert all("context" in r["reason"] for r in trace["rejected_routes"])


def test_safe_context_formula():
    assert safe_context(1_000_000, 8192) == 900_000        # ratio bound
    assert safe_context(10_000, 8192) == 1_808             # reserved bound


# ── 5. health TTL ─────────────────────────────────────────────────────────

def test_health_ttl_decay():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    rec = reg.get("provider_b", "gpt-5.6-luna")
    rec.health = "HEALTHY"
    rec.last_success_ts = time.time() - 3600  # older than TTL
    rec.last_failure_ts = 0.0
    reg.apply_health_ttl(ttl_success_s=300.0, ttl_failure_s=45.0)
    assert rec.health == "UNKNOWN"
    # recent failure poisons after success decay
    rec.health = "HEALTHY"
    rec.last_failure_ts = time.time() - 3600 + 299  # after last success
    reg.apply_health_ttl(ttl_success_s=300.0, ttl_failure_s=45.0)
    assert rec.health == "DEGRADED"


# ── 6. quality-first selection ────────────────────────────────────────────

def test_quality_first_no_cheap_weak_model_for_hard_tiers():
    reg, canon, adapters = _mk_registry({})
    adapters["provider_a"].add_model("cb/minimax-m3", "minimax-m3", 1000000, 0.001, 0.001)  # very cheap
    adapters["provider_a"].add_model("cb/luna", "gpt-5.6-luna", 1000000, 0.02, 0.12)
    _run(reg.build(reg.adapters()))
    sel = Selector(reg, canon)
    ctx = SelectionContext(tier="T4", task_class="CRITICAL", required_context=0, capabilities_required=CTX)
    primary, plan, trace = sel.choose(ctx)
    assert primary.canonical == "gpt-5.6-luna"
    assert all(p.canonical != "minimax-m3" for p in plan[:1])


def test_tier_floor_gates():
    from gateway.selector import _quality_floor
    assert _quality_floor("T4") == 0.75
    assert _quality_floor("T3") == 0.55
    assert _quality_floor("T2") == 0.40
    assert _quality_floor("T1") == 0.0


# ── 7. classifier ─────────────────────────────────────────────────────────

def test_classifier_classes_and_tiers():
    assert classify("найди в документации pathlib назначение") == "SEARCH"
    assert classify("исправь баг в модуле auth") == "NORMAL_CODING"
    assert classify("сколько будет 2+2") == "SIMPLE"
    assert tier_for_class("SEARCH") == "T1"
    assert tier_for_class("NORMAL_CODING") == "T2"
    assert tier_for_class("ARCHITECTURE") == "T3"
    assert tier_for_class("CRITICAL") == "T4"
    caps = capabilities_for_class("NORMAL_CODING")
    assert "text" in caps and "tool_call" in caps


# ── 8. metrics & provider share ───────────────────────────────────────────

def test_metrics_provider_share_counts():
    m = Metrics()
    m.record_selected(canonical="gpt-5.6-luna", provider="provider_b", slug="gpt-5.6-luna")
    m.record_attempt(canonical="gpt-5.6-luna", provider="provider_b", slug="gpt-5.6-luna",
                     error_code=None, latency_ms=100.0, ttft_ms=50.0,
                     tokens_in=100, tokens_out=50, cost_usd=0.001)
    m.record_attempt(canonical="gpt-5.6-luna", provider="provider_a", slug="cb/gpt-5.6-luna",
                     error_code="timeout", latency_ms=200.0, failover=True)
    share = m.provider_share()
    assert share["provider_b"]["successes"] == 1
    assert share["provider_b"]["selected_requests"] == 1
    assert share["provider_a"]["timeouts"] == 1
    assert share["provider_a"]["failover_count"] == 1
    snap = m.route_metrics()
    assert snap["provider_b:gpt-5.6-luna"]["success"] == 1
    assert snap["provider_b:gpt-5.6-luna"]["cost_per_success"] is not None


# ── 9. decision explainability & privacy ──────────────────────────────────

def test_decision_trace_has_no_prompt_content():
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    adapters["provider_a"].add_model("cb/glm-5.3", "glm-5.3", 1000000, 0.10, 0.40)
    _run(reg.build(reg.adapters()))
    # Mark the provider_a luna route unhealthy so the trace contains a
    # populated ``rejected_routes`` entry — exercises the privacy check on
    # a real rejection reason, not just an empty list.
    rec = reg.get("provider_a", "cb/gpt-5.6-luna")
    rec.health = "UNHEALTHY"
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    blob = repr(trace)
    for forbidden in ("messages", "content", "prompt", "api_key", "Authorization"):
        assert forbidden not in blob
    assert trace["task_class"] == "CRITICAL"
    assert trace["rejected_routes"] and all("reason" in r for r in trace["rejected_routes"])


def test_dynamic_registry_adds_route_without_restart():
    """§11: catalog adds Sol on Provider B → route appears after refresh only."""
    reg, canon, adapters = _mk_registry({})
    _seed_parity(reg, adapters)
    assert reg.get("provider_b", "gpt-5.6-sol") is None
    adapters["provider_b"].add_model("gpt-5.6-sol", "gpt-5.6-sol", 200000, 0.12, 0.60, 0.85)
    _run(reg.build(reg.adapters()))  # background refresh does exactly this
    assert reg.get("provider_b", "gpt-5.6-sol") is not None


def test_canonical_identity_has_no_provider_ids():
    """Canonical registry entries must not embed provider-specific slugs."""
    reg, canon, _ = _mk_registry({})
    for prof in canon.all().values():
        for pid in ("cb/", "cx/", "cmc/", "ocg/", "ih:", "sp:"):
            assert not prof.canonical_id.startswith(pid)


# ── G2.4: Provider B price semantics ─────────────────────────────────────────

def test_provider_b_prompt_completion_parsing():
    """Provider B catalog pricing.prompt/completion are OFFICIAL USD/token prices.
    They must be parsed into USD/Mtok and marked ESTIMATED_UPPER_BOUND
    (paid price = official * (1 - floor) upper bound; min80 is a floor,
    not the exact discount — measured actual 88-90%)."""
    from gateway.providers.provider_b import _entry_to_model
    entry = {
        "id": "gpt-5.6-luna",
        "context_length": 1050000,
        "pricing": {"prompt": "0.0000002000", "completion": "0.0000012000"},
    }
    pm = _entry_to_model("provider_b", entry, "https://provider-b.example/min80/v1", 0.80)
    assert pm is not None
    assert pm.input_price == pytest.approx(0.2 * 0.20)   # 0.04 USD/Mtok
    assert pm.output_price == pytest.approx(1.2 * 0.20)  # 0.24 USD/Mtok
    assert pm.price_state == "ESTIMATED_UPPER_BOUND"
    assert pm.metadata["official_in_per_mtok"] == pytest.approx(0.2)
    assert pm.metadata["official_out_per_mtok"] == pytest.approx(1.2)


def test_price_unit_conversion():
    """USD/token string 0.0000002 == 0.2 USD/Mtok exactly."""
    from gateway.providers.provider_b import _entry_to_model
    pm = _entry_to_model(
        "provider_b",
        {"id": "gpt-5.6-luna", "pricing": {"prompt": "0.0000002", "completion": "0.0000012"}},
        "https://x/min80/v1", 0.80,
    )
    assert pm.input_price == pytest.approx(0.04)
    assert pm.output_price == pytest.approx(0.24)


def test_provider_b_unknown_price_not_free():
    """Provider B route with unparseable price must be UNKNOWN (sentinel 1e9 in
    ranking) and NEVER rank as free 0. Priced Provider A wins."""
    reg, canon, adapters = _mk_registry({})
    adapters["provider_a"].add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)
    adapters["provider_b"].add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.0, 0.0, price_state="UNKNOWN")
    _run(reg.build(reg.adapters()))
    from gateway.selector import _route_cost
    sp = reg.get("provider_b", "gpt-5.6-luna")
    ih = reg.get("provider_a", "cb/gpt-5.6-luna")
    assert sp.price_state == "UNKNOWN"
    cost_sp, state_sp = _route_cost(sp)
    assert state_sp == "UNKNOWN" and cost_sp == 1e9
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_a"
    assert any("cost=UNKNOWN" in r["reason"] for r in trace["eligible_routes"] if "provider_b" in r["route"])


def test_cheaper_provider_b_wins_same_metrics():
    """Same canonical, both healthy, identical reliability/latency:
    Provider B EXACT-priced 0.10 < Provider A 0.14 → Provider B primary."""
    reg, canon, adapters = _mk_registry({})
    adapters["provider_a"].add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)
    adapters["provider_b"].add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.010, 0.090)
    _run(reg.build(reg.adapters()))
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_b"
    assert [p.provider for p in plan] == ["provider_b", "provider_a"]


def test_estimated_price_not_reported_as_exact():
    """An ESTIMATED_UPPER_BOUND route must print cost<=X ESTIMATED_UPPER_BOUND
    in the plan reason — never bare cost=X (which implies EXACT)."""
    reg, canon, adapters = _mk_registry({})
    adapters["provider_a"].add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)
    adapters["provider_b"].add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.030, 0.180)
    _run(reg.build(reg.adapters()))
    from gateway.registry import RouteRecord
    sp = reg.get("provider_b", "gpt-5.6-luna")
    sp.price_state = "ESTIMATED_UPPER_BOUND"
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    reasons = {p.provider: p.reason for p in plan}
    assert "ESTIMATED_UPPER_BOUND" in reasons["provider_b"]
    assert "EXACT" in reasons["provider_a"]


def test_decision_trace_cost_matches_ranking():
    """The trace reason string must carry the same cost value the ranking used."""
    reg, canon, adapters = _mk_registry({})
    adapters["provider_a"].add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)
    adapters["provider_b"].add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.0, 0.0)
    _run(reg.build(reg.adapters()))
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    ih_reason = next(p.reason for p in plan if p.provider == "provider_a")
    assert "cost=0.1400 EXACT" in ih_reason
    sp_reason = next((p.reason for p in plan if p.provider == "provider_b"), None)
    if sp_reason is not None:
        assert "cost=UNKNOWN" in sp_reason and "0.0000" not in sp_reason


def test_exact_effective_price_preferred_over_floor_estimate():
    """A route with observed actual cost_per_success (EXACT) beats the same
    price-sum estimated from the catalog floor."""
    reg, canon, adapters = _mk_registry({})
    adapters["provider_a"].add_model("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.030, 0.180)  # sum 0.21
    adapters["provider_b"].add_model("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.020, 0.120)      # sum 0.14
    _run(reg.build(reg.adapters()))
    # Provider A accumulated real billing: actual 0.05/success << provider_b estimate 0.14
    ih = reg.get("provider_a", "cb/gpt-5.6-luna")
    ih.cost_per_success = 0.05
    primary, plan, trace = Selector(reg, canon).choose(_sel_ctx("gpt-5.6-luna"))
    assert primary.provider == "provider_a"
