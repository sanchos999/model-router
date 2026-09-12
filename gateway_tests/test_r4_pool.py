"""R4 test suite — model pool + canonical lifecycle (spec R4 §15).

Deterministic fakes only; no live provider calls. Covers:
  * same canonical across providers (multi-provider identity)
  * different variants remain separate (mini/flash/pro/base)
  * case/vendor prefix normalization (cb/, cx/, cmc/moonshotai/...)
  * bad fuzzy merge prevented (no-merge guarantee for near-identical names)
  * lifecycle transitions (CORE→UNAVAILABLE→WATCH recovery, DEPRECATED→SUNSET)
  * deprecated/sunset exclusion from routing
  * dominated model exclusion (with/without evidence)
  * WATCH frontier handling (not weak, just unevidenced)
  * UNAVAILABLE recovery
  * active-session cache survives registry refresh (session_route_permitted)
  * discount >=80 gate in pool view
  * context fit (advertised/certified/safe)
  * provider parity (no provider-name prior in pool/ranking)
  * price FREE vs UNKNOWN (UNKNOWN never ranks as free)
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from gateway.canonical import CanonicalRegistry
from gateway.config import GatewayConfig, ProviderConfig
from gateway.lifecycle import (
    CORE, DEPRECATED, DISABLED, DOMINATED, SPECIALIST, FALLBACK_ONLY, SUNSET,
    UNAVAILABLE, WATCH,
    LifecycleRegistry,
)
from gateway.model_pool import ModelPool, session_route_permitted
from gateway.normalize import normalize, variant_of, family_of
from gateway.providers.base import ProviderModel
from gateway.registry import RouteRegistry
from gateway.selector import SelectionContext, Selector

from gateway_tests.test_g2_dynamic import FakeAdapter, CTX, _run


def _registry(models_by_provider: dict[str, list[tuple]]) -> tuple[RouteRegistry, CanonicalRegistry, dict[str, FakeAdapter]]:
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
    adapters = {"provider_a": FakeAdapter("provider_a"), "provider_b": FakeAdapter("provider_b")}
    for ad in adapters.values():
        reg.register_adapter(ad)
    for pname, models in models_by_provider.items():
        for m in models:
            adapters[pname].add_model(*m)
    return reg, canon, adapters


def _mk_pool(reg, canon, tmp_path):
    lc = LifecycleRegistry(path=str(tmp_path / "lifecycle.json"))
    pool = ModelPool(reg, canon, lc)
    return pool, lc


# ── 1. canonical normalization ─────────────────────────────────────────────

def test_same_canonical_across_providers():
    assert normalize("provider_a", "cb/gpt-5.6-luna") == normalize("provider_b", "gpt-5.6-luna")
    assert normalize("provider_a", "cb/gpt-5.6-luna") == "gpt-5.6-luna"
    assert normalize("provider_a", "cx/gpt-5.6-luna") == "gpt-5.6-luna"
    assert normalize("provider_b", "GPT-5.6-Luna") == "gpt-5.6-luna"


def test_case_and_vendor_prefix_normalization():
    assert normalize("provider_a", "cmc/moonshotai/Kimi-K3") == "kimi-k3"
    assert normalize("provider_a", "ali/kimi-k3") == "kimi-k3"
    assert normalize("provider_a", "cmc/zai-org/GLM-5.2") == "glm-5.2"
    assert normalize("provider_a", "ocg/qwen3.8-max") == "qwen3.8-max"
    # Provider B dash-alias maps through the alias table
    assert normalize("provider_b", "qwen-3-8-max") == "qwen3.8-max"
    # case-insensitive heads
    assert normalize("provider_a", "CB/DeepSeek-V4-Pro") == "deepseek-v4-pro"


def test_different_variants_remain_separate():
    assert normalize("provider_a", "cb/gpt-5.4") != normalize("provider_a", "cx/gpt-5.4-mini")
    assert normalize("provider_a", "cb/glm-5.3") != normalize("provider_a", "cbcn/glm-5.3-flash")
    assert normalize("provider_a", "ag/gemini-3.8-flash-high") == "gemini-3.8-flash-high"
    assert normalize("provider_a", "gemini-3.7-flash") == "gemini-3.7-flash"
    assert normalize("provider_a", "cb/gpt-5.5") != normalize("provider_a", "cb/gpt-5.6-terra")
    assert normalize("provider_a", "ocg/grok-4.5") != normalize("provider_a", "cmc/xai/grok-4.6")


def test_bad_fuzzy_merge_prevented():
    # These MUST NOT collapse into each other despite being near-identical.
    pairs = [
        ("gpt-5.4", "gpt-5.4-mini"),
        ("gpt-5.6-luna", "gpt-5.6-sol"),
        ("gpt-5.6-sol", "gpt-5.6-sol-pro"),
        ("glm-5.3", "glm-5.3-flash"),
        ("glm-5.2", "glm-5.3"),
        ("minimax-m3", "minimax-m3-preview"),
        ("kimi-k3", "kimi-k3-fast-api"),
        ("kimi-k2.6", "kimi-k2.7"),
    ]
    for a, b in pairs:
        assert normalize("provider_a", a) != normalize("provider_a", b), (a, b)
        assert normalize("provider_b", a) != normalize("provider_b", b), (a, b)
    # display/variant helpers keep variants distinguishable
    assert variant_of("gpt-5.4-mini") == "mini"
    assert variant_of("glm-5.3-flash") == "flash"
    assert variant_of("glm-5.3") == ""
    assert family_of("gpt-5.6-luna") == "gpt"
    assert family_of("kimi-k3") == "kimi"


# ── 2. pool build ──────────────────────────────────────────────────────────

def test_pool_groups_routes_by_canonical(tmp_path):
    reg, canon, _ = _registry({
        "provider_a": [
            ("cb/gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.02, 0.12, 0.90, "EXACT"),
            ("cx/gpt-5.6-luna", "gpt-5.6-luna", 272000, 0.005, 0.03, 0.975, "EXACT"),
        ],
        "provider_b": [
            ("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.04, 0.24, 0.80, "ESTIMATED_UPPER_BOUND"),
        ],
    })
    _run(reg.build(reg.adapters()))
    pool, _ = _mk_pool(reg, canon, tmp_path)
    out = pool.build()
    m = out["models"]["gpt-5.6-luna"]
    assert m["lifecycle"] in (CORE, WATCH, SPECIALIST, FALLBACK_ONLY)
    assert set(m["eligible_providers"]) == {"provider_a", "provider_b"}  # provider parity
    assert len(m["routes"]) == 3
    # best route by price among healthy/eligible — price only, provider never a key
    assert m["best_current_route"]["route"].endswith("gpt-5.6-luna")


def test_pool_marks_unavailable_without_eligible_routes(tmp_path):
    reg, canon, _ = _registry({
        "provider_b": [
            ("gpt-5.5", "gpt-5.5", 272000, 1.25, 7.5, 0.55, "ESTIMATED_UPPER_BOUND"),
        ],
    })
    pool, lc = _mk_pool(reg, canon, tmp_path)
    out = pool.build()
    assert out["models"]["gpt-5.5"]["lifecycle"] == UNAVAILABLE
    # persists a generated artifact
    lc.persist()
    assert (tmp_path / "lifecycle.json").exists()


# ── 3. lifecycle transitions ───────────────────────────────────────────────

def test_lifecycle_core_to_unavailable_and_recovery(tmp_path):
    reg, canon, _ = _registry({
        "provider_a": [("cb/kimi-k3", "kimi-k3", 1000000, 0.3, 1.5, 0.90, "EXACT")],
    })
    _run(reg.build(reg.adapters()))
    pool, lc = _mk_pool(reg, canon, tmp_path)
    lc.set_status("kimi-k3", CORE, reason="seed")
    out = pool.build()
    assert out["models"]["kimi-k3"]["lifecycle"] == CORE
    # route loses eligibility -> UNAVAILABLE (auto transition)
    adapters = {a.name: a for a in reg.adapters()}
    adapters["provider_a"]._models[0] = ProviderModel(
        provider="provider_a", provider_model_id="cb/kimi-k3", canonical_model="kimi-k3",
        context_length=1000000, input_price=0.3, output_price=1.5, discount=0.60,
        capabilities=CTX, certification_status="CERTIFIED",
    )
    _run(reg.build(reg.adapters()))
    out = pool.build()
    rec = lc.get("kimi-k3")
    assert rec.status == UNAVAILABLE
    assert any(t["to"] == UNAVAILABLE for t in out["summary"]["transitions"])
    # recovery: discount back above floor -> UNAVAILABLE -> WATCH (not CORE, no evidence)
    adapters["provider_a"]._models[0] = ProviderModel(
        provider="provider_a", provider_model_id="cb/kimi-k3", canonical_model="kimi-k3",
        context_length=1000000, input_price=0.3, output_price=1.5, discount=0.91,
        capabilities=CTX, certification_status="CERTIFIED",
    )
    _run(reg.build(reg.adapters()))
    pool.build()
    assert lc.get("kimi-k3").status == WATCH


def test_deprecated_then_sunset_transition(tmp_path):
    lc = LifecycleRegistry(path=str(tmp_path / "lc.json"))
    lc.set_status("gpt-5.4", CORE, reason="seed")
    deprecated_map = {"gpt-5.4": {"source": "provider", "reason": "provider marked deprecated"}}
    lc.apply_refresh(eligible_canonicals=set(), known_canonicals={"gpt-5.4"}, deprecated_map=deprecated_map)
    assert lc.get("gpt-5.4").status == DEPRECATED
    deprecated_map = {"gpt-5.4": {"source": "provider", "reason": "sunset", "sunset_date": "2026-12-01"}}
    lc.apply_refresh(eligible_canonicals=set(), known_canonicals={"gpt-5.4"}, deprecated_map=deprecated_map)
    rec = lc.get("gpt-5.4")
    assert rec.status == SUNSET
    assert rec.sunset_date == "2026-12-01"
    # history preserved, nothing deleted
    assert len(rec.history) == 2


def test_manual_disabled_never_auto_overridden(tmp_path):
    lc = LifecycleRegistry(path=str(tmp_path / "lc.json"))
    lc.set_status("minimax-m3", DISABLED, reason="manual ban", manual=True)
    lc.apply_refresh(
        eligible_canonicals={"minimax-m3"},
        known_canonicals={"minimax-m3"},
        deprecated_map={"minimax-m3": {"source": "provider"}},
    )
    assert lc.get("minimax-m3").status == DISABLED


def test_dominated_requires_full_evidence(tmp_path):
    lc = LifecycleRegistry(path=str(tmp_path / "lc.json"))
    # insufficient evidence -> rejected, stays WATCH
    assert lc.apply_dominance("kimi-k3", "glm-5.3", {"quality_gte": True}) is False
    assert lc.status("kimi-k3") == WATCH
    # full evidence with at least one strict improvement -> accepted
    ev = {
        "quality_gte": True, "context_gte": True, "reliability_gte": True, "cost_lte": True,
        "cost_strict": True,
    }
    assert lc.apply_dominance("kimi-k3", "glm-5.3", ev) is True
    assert lc.status("kimi-k3") == DOMINATED


# ── 4. selector lifecycle gate ─────────────────────────────────────────────

def test_selector_excludes_lifecycle_statuses():
    reg, canon, _ = _registry({
        "provider_a": [("cb/kimi-k3", "kimi-k3", 1000000, 0.3, 1.5, 0.90, "EXACT")],
    })
    lc = LifecycleRegistry(path="/tmp/r4-lc-selector.json")
    lc.set_status("kimi-k3", DEPRECATED, reason="test")
    sel = Selector(reg, canon, lc)
    ctx = SelectionContext(canonical_hint="kimi-k3", tier="T4", required_context=0,
                           capabilities_required=CTX, task_class="CRITICAL")
    steps, trace = sel.plan(ctx)
    assert steps == []
    assert any("lifecycle:DEPRECATED" in r["reason"] for r in trace["rejected_routes"])


def test_selector_without_lifecycle_still_works():
    reg, canon, _ = _registry({
        "provider_a": [("cb/kimi-k3", "kimi-k3", 1000000, 0.3, 1.5, 0.90, "EXACT")],
    })
    _run(reg.build(reg.adapters()))
    sel = Selector(reg, canon)  # no lifecycle wired (back-compat)
    ctx = SelectionContext(canonical_hint="kimi-k3", tier="T2", required_context=0,
                           capabilities_required=CTX, task_class="DEBUG")
    steps, _ = sel.plan(ctx)
    assert len(steps) == 1


# ── 5. cache safety (spec R4 §10) ─────────────────────────────────────────

def test_active_session_cache_survives_registry_refresh(tmp_path):
    lc = LifecycleRegistry(path=str(tmp_path / "lc.json"))
    # healthy, eligible, fits, quality floor ok, CORE -> keep
    ok, why = session_route_permitted(
        lc, "gpt-5.6-luna", health_ok=True, discount_ok=True,
        context_ok=True, quality_floor_ok=True,
    )
    assert ok is True and why == "ok"
    # WATCH (frontier ranking changed) — still permitted for warm session
    lc.set_status("gpt-5.6-luna", WATCH, reason="rank shuffle")
    ok, why = session_route_permitted(
        lc, "gpt-5.6-luna", health_ok=True, discount_ok=True,
        context_ok=True, quality_floor_ok=True,
    )
    assert ok is True
    # hard-excluded statuses break the warm session
    for st in (DOMINATED, DEPRECATED, SUNSET, DISABLED):
        lc.set_status("gpt-5.6-luna", st, reason="test",
                      manual=(st == DISABLED), deprecation_source="provider")
        ok, why = session_route_permitted(
            lc, "gpt-5.6-luna", health_ok=True, discount_ok=True,
            context_ok=True, quality_floor_ok=True,
        )
        assert ok is False and why == f"lifecycle:{st}"
    # route-level regressions still break the session
    lc.set_status("gpt-5.6-luna", CORE, reason="back", manual=True)
    for kw in ({"health_ok": False}, {"discount_ok": False}, {"context_ok": False}, {"quality_floor_ok": False}):
        args = dict(health_ok=True, discount_ok=True, context_ok=True, quality_floor_ok=True)
        args.update(kw)
        ok, why = session_route_permitted(lc, "gpt-5.6-luna", **args)
        assert ok is False and why == list(kw.keys())[0].replace("_ok","")


# ── 6. pool economics: discount/context/price semantics ───────────────────

def test_pool_discount_gate_and_price_states(tmp_path):
    reg, canon, _ = _registry({
        "provider_b": [
            ("gpt-5.6-luna", "gpt-5.6-luna", 1050000, 0.04, 0.24, 0.80, "ESTIMATED_UPPER_BOUND"),
            ("gpt-5.5", "gpt-5.5", 272000, 1.25, 7.5, 0.55, "ESTIMATED_UPPER_BOUND"),
            ("kimi-k3", "kimi-k3", 1000000, 0.0, 0.0, None, "UNKNOWN"),
        ],
    })
    _run(reg.build(reg.adapters()))
    pool, _ = _mk_pool(reg, canon, tmp_path)
    out = pool.build()
    luna = out["models"]["gpt-5.6-luna"]
    assert luna["routes"][0]["eligible"] is True        # exactly at floor 80
    assert luna["routes"][0]["status"] == "ELIGIBLE"
    g55 = out["models"]["gpt-5.5"]
    assert g55["routes"][0]["eligible"] is False        # 55% < floor
    kimi = out["models"]["kimi-k3"]
    # UNKNOWN price is never FREE: route is discount-eligible-None -> below floor
    assert kimi["routes"][0]["eligible"] is False


def test_pool_context_fields(tmp_path):
    reg, canon, _ = _registry({
        "provider_a": [("cb/kimi-k3", "kimi-k3", 1000000, 0.3, 1.5, 0.90, "EXACT")],
    })
    _run(reg.build(reg.adapters()))
    pool, _ = _mk_pool(reg, canon, tmp_path)
    out = pool.build()
    r = out["models"]["kimi-k3"]["routes"][0]
    assert r["advertised_context"] == 1000000
    assert r["certified_context"] == 1000000
    assert r["safe_context"] == 900000  # min(0.9*1e6, 1e6-8192) = 900000
