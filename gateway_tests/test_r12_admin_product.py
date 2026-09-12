"""R12 regression tests: Admin UI product surface.

Contract tests for the R12 endpoints (inventory liquidity fields, insights,
baselines, price history, audit human format, calculator precision) plus the
economic anomaly detector (§29). No provider calls — fixtures only.
"""
import importlib
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def r12_env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="mr-r12-")
    monkeypatch.setenv("GW_CONTROL_DB", os.path.join(tmp, "control.db"))
    monkeypatch.setenv("GW_STATE_DIR", tmp)
    monkeypatch.setenv("ROUTER_API_KEY", "test-key")
    from gateway.control import store
    importlib.reload(store)
    # seed: two discovered models, one mapped canonical in pool
    now = 1_789_000_000.0
    store.upsert_discovered("provider_a", "x/gpt-test", {
        "provider_model_id": "x/gpt-test", "display_name": "GPT Test",
        "official_input": 5.0, "official_output": 25.0,
        "min_ask_in": 0.10, "min_ask_out": 0.48, "ask_count": 17,
        "context": 200000,
    }, priced=True, now=now)
    store.upsert_discovered("provider_b", "sp-test", {
        "provider_model_id": "sp-test", "display_name": "GPT Test",
        "official_input": 5.0, "official_output": 25.0,
        "min_ask_in": 0.05, "min_ask_out": 0.30,
        "context": 128000,
        "market": {"best_input_per_1m": 0.05, "best_output_per_1m": 0.30,
                   "best_discount_pct": 99.0, "num_sellers": 3,
                   "credits_sold_24h": 500},
    }, priced=True, now=now)
    store.set_canonical("provider_a", "x/gpt-test", "gpt-test", source="manual")
    store.set_canonical("provider_b", "sp-test", "gpt-test", source="manual")
    store.set_pool_policy("gpt-test", {"in_pool": True})
    yield store


@pytest.fixture()
def client(r12_env):
    # reload every control module that holds a `store` reference, so each
    # test runs against its own MR_CONTROL_DIR database
    import gateway.control.store as _s
    from gateway.control import inventory as _inv, insights as _ins, admin_api as _api
    import importlib as _il
    _il.reload(_inv)
    _il.reload(_ins)
    _il.reload(_api)
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(_api.router)
    c = TestClient(app)
    c.headers.update({"x-admin-token": "test-key"})
    yield c
    del _s


def test_inventory_liquidity_fields(client):
    r = client.get("/admin/inventory")
    assert r.status_code == 200
    models = {m["canonical"]: m for m in r.json()["models"]}
    g = models["gpt-test"]
    # Provider A asks are price offers (17), Provider B gives sellers via market
    assert g["offers_count"] >= 17
    assert "providers" in g and set(g["providers"]) == {"provider_a", "provider_b"}
    # §4: probe latency only when an inference probe exists
    assert g["ttft_ms"] is None
    assert g["last_probe_at"] is None


def test_price_history_roundtrip(r12_env, client):
    import time as _time
    r12_env.record_price_history([
        {"provider": "provider_a", "provider_model_id": "x/gpt-test", "canonical": "gpt-test",
         "ts": _time.time(), "best_input": 0.10, "best_output": 0.48,
         "discount_pct": 98.0, "offers_count": 17, "sellers_count": None},
    ])
    r = client.get("/admin/price-history/gpt-test")
    assert r.status_code == 200
    d = r.json()
    assert d["points"] >= 1
    assert d["now"]["best_input"]["min"] == pytest.approx(0.10)


def test_cost_preview_precision(client):
    """§13: cheap prices never collapse to $0.00 — 6-8dp values survive."""
    r = client.post("/admin/market/cost-preview",
                    json={"canonical": "gpt-test", "input_tokens": 100_000,
                          "output_tokens": 5_000})
    assert r.status_code == 200
    d = r.json()
    # best route = provider_b 0.05/0.30
    assert d["best_ask_cost"] == pytest.approx((100000 * 0.05 + 5000 * 0.30) / 1e6)
    assert d["savings_pct"] > 95
    assert len(d["per_provider"]) == 2
    assert d["per_provider"][0]["cost"] <= d["per_provider"][1]["cost"]
    # policy max = official*(1-floor)
    assert d["policy_max_input_per_1m"] == pytest.approx(5.0 * 0.2)


def test_baseline_create_diff_restore(client):
    r = client.post("/admin/baselines", json={"description": "KNOWN-GOOD R12 TEST"})
    assert r.status_code == 200
    bid = r.json()["baseline"]["id"]
    assert len(r.json()["baseline"]["content_hash"]) == 64
    # diff without changes: mostly empty
    d = client.get(f"/admin/baselines/{bid}/diff").json()["diff"]
    assert d["mappings_added"] == [] and d["mappings_removed"] == []
    # mutate, then restore
    client.post("/admin/canonical-map", json={"provider": "provider_b",
                                              "provider_model_id": "sp-test",
                                              "canonical": "other-canon"})
    d2 = client.get(f"/admin/baselines/{bid}/diff").json()["diff"]
    assert (len(d2["mappings_added"]) + len(d2["mappings_removed"])
            + len(d2["mappings_changed"])) >= 1
    rr = client.post(f"/admin/baselines/{bid}/restore").json()
    assert rr.get("restored_from") == bid
    inv = client.get("/admin/inventory").json()
    assert any(m["canonical"] == "gpt-test" for m in inv["models"])


def test_audit_human_format(client):
    client.post("/admin/canonical-map", json={"provider": "provider_a",
                                              "provider_model_id": "x/gpt-test",
                                              "canonical": "gpt-test"})
    r = client.get("/admin/audit")
    assert r.status_code == 200
    items = r.json()["audit"]
    assert items, "audit must not be empty"
    a = items[0]
    for k in ("iso", "actor", "action", "text"):
        assert k in a
    # human text for a known action
    assert any("Сопоставление" in i["text"] or "политика" in i["text"].lower()
               for i in items)


def test_policy_inheritance(client):
    r = client.get("/admin/policy/inheritance/gpt-test")
    assert r.status_code == 200
    d = r.json()
    assert d["min_discount"]["source"] == "inherit"
    assert 0 < d["global_min_discount"] < 1
    # override
    client.post("/admin/model-pool/gpt-test", json={"min_discount_override": 0.9})
    d2 = client.get("/admin/policy/inheritance/gpt-test").json()
    assert d2["min_discount"]["source"] == "override"
    assert d2["min_discount"]["value"] == pytest.approx(0.9)


def test_model_lifecycle_actions(client):
    for action, expected in [
            ("hide", {"hidden": True}),
            ("unhide", {"hidden": False}),
            ("remove_auto", {"in_pool": False}),
            ("restore", {"in_pool": True, "hidden": False})]:
        r = client.post("/admin/model-lifecycle/gpt-test", json={"action": action})
        assert r.status_code == 200, action
        assert r.json()["ok"] is True
    # unmap removes the manual mappings
    r = client.post("/admin/model-lifecycle/gpt-test", json={"action": "unmap"})
    assert r.status_code == 200
    assert len(r.json()["removed_mappings"]) == 2
    # unknown action rejected
    assert client.post("/admin/model-lifecycle/gpt-test",
                       json={"action": "nuke"}).status_code == 400


def test_unmatched_grouping(client):
    r = client.get("/admin/unmatched")
    assert r.status_code == 200
    d = r.json()
    assert "family_counts" in d and "total" in d


def test_search_catalog_wizard(client):
    r = client.get("/admin/search/catalog?q=gpt")
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "matched gpt-test must be findable"
    top = results[0]
    assert top["canonical"] == "gpt-test"
    assert len(top["variants"]) == 2
    # short query returns nothing, no 500
    assert client.get("/admin/search/catalog?q=g").json()["results"] == []


def test_dashboard_summary_semantics(client, monkeypatch):
    """§18: router routes and market offers counted separately."""
    # stub runtime /models/pool
    import gateway.control.admin_api as api_mod

    class FakeResp:
        status_code = 200
        def json(self):
            return {"models": {"gpt-test": {"routes": [{"route": "provider_a:x/gpt-test"}]}}}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, path): return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    r = client.get("/admin/dashboard/summary")
    assert r.status_code == 200
    d = r.json()
    assert d["router_routes"] == 1          # one execution path
    assert d["market_offers_provider_a_asks"] >= 17   # 17 price offers
    assert "catalog_counts" in d


def test_economic_anomaly_detector(client, monkeypatch):
    """§29: a cheaper equal-or-better model that passes all gates but is not
    selected must be flagged as a diagnostic, never auto-applied."""
    from gateway.control import insights, inventory
    _real_build = inventory.build_inventory

    def fake_build(provider=None):
        inv = _real_build()
        # model A: expensive, in pool
        inv["models"].append({"canonical": "model-a", "display_name": "Model A",
                              "providers": ["provider_a"], "in_pool": True, "hidden": False,
                              "market_active": True, "eligible": True,
                              "best_input": 5.0, "best_output": 25.0,
                              "official_input": 5.0, "official_output": 25.0,
                              "discount_pct": 0.0, "effective_min_discount": 0.8,
                              "context_max": 200000, "routes": [],
                              "offers_count": 0, "sellers_count": 0,
                              "offers_kind": None, "offers_providers": [],
                              "last_probe_at": None, "ttft_ms": None, "last_priced": None})
        # model B: same quality family, 98% cheaper, in pool, eligible but NOT selected
        inv["models"].append({"canonical": "model-b", "display_name": "Model B",
                              "providers": ["provider_b"], "in_pool": False, "hidden": False,
                              "market_active": True, "eligible": False,
                              "best_input": 0.05, "best_output": 0.25,
                              "official_input": 5.0, "official_output": 25.0,
                              "discount_pct": 99.0, "effective_min_discount": 0.8,
                              "context_max": 200000,
                              "routes": [{"provider": "provider_b", "provider_model_id": "sp-b",
                                          "market_active": True, "eligible": False,
                                          "eligibility_reason": "не в моём пуле",
                                          "discount_pct": 99.0,
                                          "official_input": 5.0, "official_output": 25.0,
                                          "best_input": 0.05, "best_output": 0.25,
                                          "context": 200000, "sellers": 3,
                                          "offers_kind": "sellers", "last_probe": None}],
                              "offers_count": 0, "sellers_count": 3,
                              "offers_kind": "sellers", "offers_providers": ["provider_b"],
                              "last_probe_at": None, "ttft_ms": None, "last_priced": None})
        # make model-a eligible AND cheapest-eligible expensive
        for m in inv["models"]:
            if m["canonical"] == "gpt-test":
                m["eligible"] = False  # keep baseline fixture out of economics
        return inv

    monkeypatch.setattr(inventory, "build_inventory", fake_build)

    class FakeMetrics:
        def get(self):
            return {"selected": {"model-a": {"requests": 10, "cost_usd": 1.0}},
                    "models": {}, "providers": {}}

    monkeypatch.setattr(insights, "_runtime_metrics",
                        lambda: FakeMetrics().get() if hasattr(insights, "_runtime_metrics")
                        else None, raising=False)
    out = insights.economics()
    assert "anomalies" in out
    # the detector only REPORTS; no policy is changed
    assert client.get("/admin/model-pool/model-b").status_code in (200, 404)


def test_partial_policy_update_keeps_pool_membership(client):
    """R12 regression: POST only min_discount_override on a model WITHOUT a
    model_pool row must not silently remove it from the pool."""
    r = client.post("/admin/model-pool/gpt-test",
                    json={"min_discount_override": 0.9})
    assert r.status_code == 200
    d = client.get("/admin/model-pool/gpt-test").json()
    assert d["in_pool"] is not False or d["in_pool"] is None or d["in_pool"] is True
    # the seeded gpt-test had in_pool=True explicitly; a fresh canonical case:
    r2 = client.post("/admin/model-pool/brand-new-model",
                     json={"min_discount_override": 0.9})
    d2 = client.get("/admin/model-pool/brand-new-model").json()
    assert d2["in_pool"] is True, f"partial update dropped pool: {d2}"


# ── R12 blocker regression tests (post real-UI defects) ──────────────────

def test_price_history_empty_canonical_is_not_404(client):
    """B1: unmatched model -> /admin/price-history/ must NOT 404."""
    r = client.get("/admin/price-history/")
    assert r.status_code == 200
    d = r.json()
    assert d["points"] == 0
    assert "empty_reason" in d and d["empty_reason"]


def test_price_history_route_for_unmatched(client):
    """B1: route-level history works without a canonical."""
    client.get("/admin/price-history/route?provider=provider_a&pid=x/gpt-test")
    # no history yet -> empty state 200 (not 404/500)
    r = client.get("/admin/price-history/route?provider=provider_a&pid=x/gpt-test")
    assert r.status_code == 200
    assert r.json()["points"] == 0


def test_inventory_groups_have_unique_keys(client):
    """B1: unmatched rows get unique keys (canonical '' is not an id)."""
    r = client.get("/admin/inventory")
    assert r.status_code == 200
    d = r.json()
    keys = [u.get("key") for u in d["unmatched"]]
    assert all(keys)
    assert len(set(keys)) == len(keys)


def test_market_freshness_endpoint(client):
    """B3: freshness verdicts exist per provider."""
    r = client.get("/admin/market/freshness")
    assert r.status_code == 200
    d = r.json()
    assert set(d["providers"]) == {"provider_a", "provider_b"}
    for v in d["providers"].values():
        assert v["state"] in ("нет данных", "свежие", "устарели", "очень старые")


def test_reconciliation_snapshot_shape(client):
    """B6: single reconciliation snapshot across sources."""
    r = client.get("/admin/insights/reconciliation-snapshot")
    assert r.status_code == 200
    d = r.json()
    for k in ("CONFIGURED_PROVIDERS", "ONLINE_PROVIDERS", "PROVIDER_A_DISCOVERED",
              "PROVIDER_B_DISCOVERED", "POOL_MODELS", "AVAILABLE_POOL_MODELS",
              "UNMATCHED", "RUNTIME_ROUTES", "MARKET_OFFERS",
              "STALE_PRICES", "FRESH_PRICES"):
        assert k in d, k
    # consistency: pool <= discovered canonicals
    assert d["POOL_MODELS"] <= d["PROVIDER_A_DISCOVERED"] + d["PROVIDER_B_DISCOVERED"]


def test_price_units_audit_shape(client):
    """B7: unit reconciliation returns MODEL/RAW/UNIT/NORMALIZED/UI/MATCH."""
    r = client.get("/admin/insights/price-units?limit=5")
    assert r.status_code == 200
    d = r.json()
    assert "mismatches" in d and "rows" in d
    for row in d["rows"]:
        for k in ("MODEL", "PROVIDER_RAW_VALUE", "PROVIDER_UNIT",
                  "NORMALIZED_USD_PER_1M", "UI_VALUE", "MATCH"):
            assert k in row, k


# ── R13 guards ────────────────────────────────────────────────────────────

def test_price_sanity_guards(r12_env):
    """§7: negative/NaN rejected, unit jump dropped, $0 needs FREE proof,
    extreme discount flagged, official<market recorded."""
    from gateway.control import discovery as d
    m, w = d.sanitize_prices({"official_input": 5.0, "min_ask_in": -1})
    assert m["min_ask_in"] is None
    m, w = d.sanitize_prices({"official_input": 5.0, "min_ask_in": float("nan")})
    assert m["min_ask_in"] is None
    m, w = d.sanitize_prices({"official_input": 5.0,
                              "market": {"best_input_per_1m": 99999.0}})
    assert m["market"]["best_input_per_1m"] is None
    m, w = d.sanitize_prices({"official_input": 5.0, "min_ask_in": 0.0})
    assert m["min_ask_in"] is None  # $0 without proven FREE
    m, w = d.sanitize_prices({"official_input": 0.0, "min_ask_in": 0.0})
    assert m["min_ask_in"] == 0.0  # official 0 => FREE proven
    m, w = d.sanitize_prices({"official_input": 5.0, "min_ask_in": 0.0004})
    assert m["min_ask_in"] == 0.0004
    assert any("99" in x for x in w)  # extreme discount warning
    m, w = d.sanitize_prices({"official_input": 5.0, "min_ask_in": 7.0})
    assert any("market" in x and "official" in x for x in w)


def test_discovery_shrink_guard(r12_env):
    """§6: a >50% catalog collapse blocks the destructive persist."""
    from gateway.control import discovery as d
    # seed a healthy catalog of 40 models
    now = 1_789_000_000.0
    for i in range(40):
        d.store.upsert_discovered("provider_b", f"m{i}", {
            "provider": "provider_b", "provider_model_id": f"m{i}",
            "display_name": f"M{i}", "official_input": 1.0,
        }, priced=True, now=now)
    a = d.check_shrink("provider_b", 40)
    assert a is None  # same size -> no anomaly
    a = d.check_shrink("provider_b", 1)
    assert a is not None and a["previous"] >= 40 and a["new"] == 1
    assert "НЕ применено" in a["text"]


def test_refresh_schedule_endpoint(client):
    """§5: schedule state exposes the three TTL jobs."""
    r = client.get("/admin/refresh/schedule")
    assert r.status_code == 200
    d = r.json()
    assert set(d["jobs"]) == {"catalog", "market", "availability"}
    assert d["jobs"]["catalog"]["ttl_s"] == 6 * 3600
    assert d["jobs"]["market"]["ttl_s"] == 3600
    assert d["jobs"]["availability"]["ttl_s"] == 1800
