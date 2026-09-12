"""R15: Production Guards + Observability tests.

Covers the §12 acceptance list:
- expensive selected vs cheaper equal-quality -> anomaly
- cheaper model rejected by quality -> NO false anomaly
- price x10 -> spike warning
- catalog 400->1 -> preserve old registry (shrink + schema drift)
- provider latency degradation
- stale price
- budget warning
- shadow candidate no production call
- canary rollback
- readiness green/yellow/red
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "control.db")
    monkeypatch.setenv("GW_CONTROL_DB", db)
    monkeypatch.setenv("GW_RUNTIME_URL", "http://127.0.0.1:1")  # unreachable
    from gateway.control import store, inventory, insights, admin_api
    importlib.reload(store)
    # store is reloaded; other modules hold stale refs — reload the chain
    importlib.reload(inventory)
    importlib.reload(insights)
    importlib.reload(admin_api)
    importlib.reload(importlib.import_module("gateway.control.observability"))
    importlib.reload(importlib.import_module("gateway.control.routing_explain"))
    yield store
    try:
        store._CONTROL_DB = db  # keep isolated
    except Exception:
        pass


@pytest.fixture()
def client(env):
    from gateway.control import admin_api, routing_explain
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(admin_api.router)
    app.include_router(routing_explain.router)
    return TestClient(app)


def _add_decision(env, *, plan_prices, tier="T3", task_class="CODING"):
    env.insert_decision({
        "ts": time.time(), "task_class": task_class, "tier": tier,
        "canonical": plan_prices[0]["canonical"],
        "provider": "provider_a", "provider_model_id": "x",
        "plan_len": len(plan_prices), "reason": "primary:cheapest",
        "trace": {"plan_prices": plan_prices,
                  "candidate_canonicals": [p["canonical"] for p in plan_prices]},
    })


# ── §1: economic anomaly ────────────────────────────────────────────────

def _pp(canon, cost, quality, in_price=1.0):
    return {"canonical": canon, "route": f"provider_a:{canon}",
            "reason": "primary", "input_price": in_price, "output_price": 1.0,
            "price_state": "EXACT", "discount": 0.9,
            "quality_score": quality, "expected_cost_usd": cost}


def test_expensive_selected_vs_cheaper_equal_quality_is_anomaly(client, env):
    # selected: $0.30, alternative of EQUAL quality: $0.05 → ratio 6 ≥ 3
    _add_decision(env, plan_prices=[
        _pp("gpt-5.6-luna", 0.30, 0.80),
        _pp("glm-5.3", 0.05, 0.80),
    ])
    from gateway.control import observability as obs
    res = obs.economic_anomalies()
    assert res["anomalies"], "anomaly must fire for expensive equal-quality pick"
    a = res["anomalies"][0]
    assert a["selected"]["canonical"] == "gpt-5.6-luna"
    assert a["alternative"]["canonical"] == "glm-5.3"
    assert a["ratio"] >= 3


def test_cheaper_model_rejected_by_quality_NO_false_anomaly(client, env):
    # alternative is cheaper but WEAKER quality (0.3 < 0.75 - 0.05) → no anomaly
    _add_decision(env, plan_prices=[
        _pp("gpt-5.6-luna", 0.30, 0.75),
        _pp("cheap-small", 0.01, 0.30),
    ])
    from gateway.control import observability as obs
    res = obs.economic_anomalies()
    assert not res["anomalies"], "weaker-quality cheaper model is not an anomaly"


def test_small_difference_not_anomaly(client, env):
    # ratio < 3 → silence (ordinary economics)
    _add_decision(env, plan_prices=[
        _pp("gpt-5.6-luna", 0.06, 0.80),
        _pp("glm-5.3", 0.05, 0.80),
    ])
    from gateway.control import observability as obs
    assert not obs.economic_anomalies()["anomalies"]


# ── §2: price spike ─────────────────────────────────────────────────────

def test_price_x10_spike_warning(client, env):
    now = time.time()
    env.insert_decision({"ts": now, "task_class": "X", "tier": "T1",
                         "canonical": "spiky", "provider": "p", "provider_model_id": "m",
                         "plan_len": 1, "reason": "r", "trace": {}})
    # pool membership for pool_canonicals
    c = sqlite3.connect(env.db_path())
    c.execute("CREATE TABLE IF NOT EXISTS model_pool (canonical TEXT PRIMARY KEY,"
              " in_pool INTEGER DEFAULT 1, hidden INTEGER DEFAULT 0,"
              " min_discount_override REAL, max_input_price REAL, max_output_price REAL,"
              " preferred_providers TEXT DEFAULT '[]', banned_providers TEXT DEFAULT '[]',"
              " auto_routing INTEGER DEFAULT 1, lifecycle_override TEXT, notes TEXT,"
              " updated_at REAL)")
    c.execute("INSERT OR REPLACE INTO model_pool (canonical, in_pool, updated_at)"
              " VALUES ('spiky', 1, ?)", (now,))
    c.commit()
    c.close()
    rows = []
    for i in range(6):
        rows.append({"provider": "provider_b", "provider_model_id": f"m{i}",
                     "canonical": "spiky", "ts": now - 7200 - i * 600,
                     "best_input": 0.50, "best_output": 1.0})
    env.record_price_history(rows)
    env.record_price_history([{"provider": "provider_b", "provider_model_id": "m9",
                               "canonical": "spiky", "ts": now - 120,
                               "best_input": 5.00, "best_output": 10.0}])
    from gateway.control import observability as obs
    spikes = obs.price_spikes()
    assert any(s["canonical"] == "spiky" for s in spikes), "x10 spike must warn"
    sp = [s for s in spikes if s["canonical"] == "spiky"][0]
    assert sp["multiplier"] >= 5


def test_normal_volatility_no_spike(client, env):
    now = time.time()
    c = sqlite3.connect(env.db_path())
    c.execute("CREATE TABLE IF NOT EXISTS model_pool (canonical TEXT PRIMARY KEY,"
              " in_pool INTEGER DEFAULT 1, hidden INTEGER DEFAULT 0,"
              " min_discount_override REAL, max_input_price REAL, max_output_price REAL,"
              " preferred_providers TEXT DEFAULT '[]', banned_providers TEXT DEFAULT '[]',"
              " auto_routing INTEGER DEFAULT 1, lifecycle_override TEXT, notes TEXT,"
              " updated_at REAL)")
    c.execute("INSERT OR REPLACE INTO model_pool (canonical, in_pool, updated_at)"
              " VALUES ('calm', 1, ?)", (now,))
    c.commit()
    c.close()
    rows = []
    for i in range(6):
        rows.append({"provider": "provider_b", "provider_model_id": f"m{i}",
                     "canonical": "calm", "ts": now - 7200 - i * 600,
                     "best_input": 0.50, "best_output": 1.0})
    env.record_price_history(rows)
    env.record_price_history([{"provider": "provider_b", "provider_model_id": "m9",
                               "canonical": "calm", "ts": now - 120,
                               "best_input": 0.55, "best_output": 1.0}])  # +10%
    from gateway.control import observability as obs
    assert not any(s["canonical"] == "calm" for s in obs.price_spikes()), \
        "10% move is ordinary volatility, not a spike"


# ── §8: catalog collapse + schema drift ──────────────────────────────────

def test_catalog_400_to_1_preserves_registry(client, env):
    from gateway.control import discovery
    models = [{"provider_model_id": f"m{i}", "official_input": 1.0,
               "official_output": 2.0} for i in range(400)]
    r1 = discovery.persist_discovery("provider_a", models)
    assert r1["total"] == 400
    # first accepted fetch seeds the fingerprint
    discovery.check_schema_drift("provider_a", models)
    # collapse to 1 model
    shrink = discovery.check_shrink("provider_a", 1)
    assert shrink, "shrink guard must fire on 400->1"
    # drift: pricing field vanished for everyone
    broken = [{"provider_model_id": "m0"}]
    drift = discovery.check_schema_drift("provider_a", broken)
    assert drift, "schema drift must fire when pricing disappears"
    # the registry survives
    rows = env.list_discovered(provider="provider_a", include_missing=True)
    assert len(rows) == 400, "old registry must be preserved"
    alerts = env.list_fingerprint_alerts()
    assert alerts and alerts[0]["provider"] == "provider_a"


# ── §4: provider degradation ────────────────────────────────────────────

def test_provider_latency_degradation(client, env):
    from gateway.control import observability as obs
    metrics = {"providers": {"provider_a": {
        "requests": 100, "successes": 95, "failures": 5, "timeouts": 0,
        "ttft_p50_ms": 3000, "ttft_p95_ms": 18000, "total_p95_ms": 25000}}}
    provs = obs.provider_degradation(metrics)
    ih = [p for p in provs if p["provider"] == "provider_a"][0]
    assert ih["status"] == "degraded"
    assert "TTFT" in ih["reason"]
    assert ih["status_ru"] == "Деградация"


def test_provider_stale_price_no_data(client, env):
    from gateway.control import observability as obs
    # provider with metrics but zero discovery freshness → no_data + reason
    provs = obs.provider_degradation({"providers": {"provider_a": {
        "requests": 10, "successes": 10}}})
    ih = [p for p in provs if p["provider"] == "provider_a"][0]
    assert ih["status"] == "no_data"
    assert "каталог" in ih["reason"]


def test_provider_down(client, env):
    from gateway.control import observability as obs
    provs = obs.provider_degradation({"providers": {"provider_b": {
        "requests": 30, "successes": 0, "failures": 30, "timeouts": 10}}})
    sp = [p for p in provs if p["provider"] == "provider_b"][0]
    assert sp["status"] == "down"
    assert sp["status_ru"] == "Недоступен"


# ── §3: budgets ─────────────────────────────────────────────────────────

def test_budget_warning(client, env, monkeypatch):
    from gateway.control import observability as obs
    # configure a budget via active config path (kv-free): monkeypatch _cfg
    monkeypatch.setattr(obs, "_kv", lambda k: "0.1" if k == "r15.daily_warning_budget_usd" else None)
    env.append_spend(0.20, "provider_a:x")
    res = obs.budgets()
    assert res["today_usd"] >= 0.20
    assert any(a["object"] == "дневной бюджет" for a in res["alerts"]), \
        "daily budget warning must fire"


def test_budget_disabled_by_default(client, env):
    from gateway.control import observability as obs
    env.append_spend(5.0, "provider_a:x")
    res = obs.budgets()
    assert res["daily_budget_usd"] is None
    assert not res["alerts"], "no budgets configured -> no warnings"


# ── §6: shadow candidates ───────────────────────────────────────────────

POOL_DDL = ("CREATE TABLE IF NOT EXISTS model_pool (canonical TEXT PRIMARY KEY,"
            " in_pool INTEGER DEFAULT 1, hidden INTEGER DEFAULT 0,"
            " min_discount_override REAL, max_input_price REAL, max_output_price REAL,"
            " preferred_providers TEXT DEFAULT '[]', banned_providers TEXT DEFAULT '[]',"
            " auto_routing INTEGER DEFAULT 1, lifecycle_override TEXT, notes TEXT,"
            " updated_at REAL)")


def _pool_row(env, canonical):
    c = sqlite3.connect(env.db_path())
    c.execute(POOL_DDL)
    c.execute("INSERT OR REPLACE INTO model_pool (canonical, in_pool, updated_at)"
              " VALUES (?, 1, ?)", (canonical, time.time()))
    c.commit()
    c.close()


def test_shadow_candidate_no_production_call(client, env):
    from gateway.control import integration
    # candidate marker excludes routes from production filtering
    class R:
        canonical = "shadow-model"
        provider = "provider_a"
    routes = [R()]
    def policy(canonical):
        return {"lifecycle_override": "CANDIDATE" if canonical == "shadow-model" else None,
                "restricted_providers": []}
    out = integration.filter_routes_by_model_policy(routes, policy)
    assert out == [], "candidate routes must not receive production traffic"
    # control-plane stats still computable
    _pool_row(env, "shadow-model")
    env.set_candidate("shadow-model", True)
    from gateway.control import observability as obs
    res = obs.shadow_candidates()
    assert any(c["canonical"] == "shadow-model" for c in res["candidates"])


def test_shadow_candidate_dry_run_stats(client, env):
    _pool_row(env, "shadow-model")
    env.set_candidate("shadow-model", True)
    # decisions where the candidate would have been cheaper
    for _ in range(3):
        _add_decision(env, plan_prices=[
            _pp("gpt-5.6-luna", 0.30, 0.80, in_price=3.0),
            _pp("other", 0.20, 0.78, in_price=2.0),
        ])
    from gateway.control import observability as obs
    res = obs.shadow_candidates()
    cand = [c for c in res["candidates"] if c["canonical"] == "shadow-model"][0]
    assert cand["decisions_examined"] == 3


# ── §7: canary rollback ─────────────────────────────────────────────────

def test_canary_rollback(client, env, monkeypatch):
    from gateway.control import observability as obs
    env.set_kv("canary", json.dumps({
        "enabled": True, "model": "glm-5.3", "traffic_pct": 100,
        "started_at": time.time(), "duration_s": 3600,
        "rollback": {"error_rate": 0.1, "ttft_ms": 5000, "cost_multiplier": 2.0}}))
    # fake runtime metrics — 50% errors breaches the 10% threshold
    class FakeMetrics:
        @staticmethod
        def canary_snapshot(model):
            return {"requests": 20, "successes": 10, "error_rate": 0.5,
                    "ttft_p95_ms": 8000, "cost_per_success": 0.01}
    import gateway.metrics as metrics_mod
    monkeypatch.setattr(metrics_mod.Metrics, "canary_snapshot",
                        lambda self, model: FakeMetrics.canary_snapshot(model))
    # canary_evaluate imports _metrics from gateway.app — patch there
    import gateway.app as app_mod
    monkeypatch.setattr(app_mod, "_metrics", FakeMetrics(), raising=False)
    res = obs.canary_evaluate()
    assert res["rolled_back"], "threshold breach must auto-rollback"
    st = obs.canary_status()
    assert st["enabled"] is False, "canary disabled after rollback"
    events = env.list_system_events(kind="canary")
    assert events, "rollback must land in system events history"


def test_canary_disabled_by_default(client, env):
    from gateway.control import observability as obs
    assert obs.canary_status()["enabled"] is False


def test_canary_apply_hint(client, env, monkeypatch):
    from gateway.control import integration
    env.set_kv("canary", json.dumps({
        "enabled": True, "model": "glm-5.3", "traffic_pct": 100,
        "task_classes": ["CODING"], "started_at": time.time(), "duration_s": 3600}))
    monkeypatch.setattr("random.random", lambda: 0.0)  # always under 100%
    class Ctx:
        pass
    res = integration.apply_canary(Ctx(), "CODING")
    assert res.get("canonical_hint") == "glm-5.3"
    # non-matching class → no hint
    assert not integration.apply_canary(Ctx(), "SIMPLE")


# ── §9: readiness ───────────────────────────────────────────────────────

def test_readiness_green_when_healthy(client, env, monkeypatch):
    from gateway.control import observability as obs
    now = time.time()
    for prov in ("provider_a", "provider_b"):
        env.set_kv(f"fp:{prov}", "x")
    # fresh discovery rows (latest_discovery_meta reads discovered_models)
    for i in range(3):
        env.upsert_discovered("provider_a", f"m{i}", {"provider_model_id": f"m{i}",
                                      "official_input": 1.0}, priced=True, now=now - 60)
        env.upsert_discovered("provider_b", f"s{i}", {"provider_model_id": f"s{i}",
                                      "official_input": 1.0}, priced=True, now=now - 60)
    # known-good baseline exists
    env.set_known_good("base-x", "test")
    monkeypatch.setattr(obs, "model_degradation", lambda **kw: [])
    res = obs.readiness(runtime_health={"ok": True}, control_ok=True,
                        runtime_metrics={})
    assert res["level"] == "GREEN", f"expected GREEN, got {res}"


def test_readiness_red_when_runtime_down(client, env):
    from gateway.control import observability as obs
    res = obs.readiness(runtime_health={"ok": False}, control_ok=True)
    assert res["level"] == "RED"
    assert any(c["name"] == "runtime" for c in res["criticals"])


def test_readiness_yellow_on_stale_catalog(client, env):
    from gateway.control import observability as obs
    res = obs.readiness(runtime_health={"ok": True}, control_ok=True,
                        runtime_metrics={})
    assert res["level"] == "YELLOW", f"stale catalogs → YELLOW, got {res['level']}"
    assert res["level_ru"] == "ЖЁЛТЫЙ"


# ── §10/§11: alert center + system events ───────────────────────────────

def test_alert_center_categories_and_ack(client, env):
    from gateway.control import observability as obs
    # stale-price provider + spike model + budget
    env.append_spend(1.0, "x")
    alerts = obs.alert_center(runtime_metrics={"providers": {"provider_a": {
        "requests": 5, "successes": 5}}})
    assert isinstance(alerts["alerts"], list)
    kinds = {a["kind"] for a in alerts["alerts"]}
    assert "provider" in kinds
    # ack hides it
    key = [a for a in alerts["alerts"] if a["kind"] == "provider"][0]["alert_key"]
    env.ack_alert(key)
    alerts2 = obs.alert_center(runtime_metrics={"providers": {"provider_a": {
        "requests": 5, "successes": 5}}})
    assert all(a["alert_key"] != key or a["acked"] for a in alerts2["alerts"])
    unacked = [a for a in alerts2["alerts"] if not a["acked"] and a["alert_key"] == key]
    assert not unacked


def test_system_events_separate_from_revisions(client, env):
    env.insert_system_event({"kind": "schema", "severity": "bad",
                             "object": "provider_b", "reason": "drift",
                             "recommended": "check"})
    events = env.list_system_events(kind="schema")
    assert len(events) == 1
    # revision log untouched by system events
    revs = env.list_revisions() if hasattr(env, "list_revisions") else []
    assert not any(r.get("kind") == "schema" for r in revs)


# ── API surface ─────────────────────────────────────────────────────────

def test_obs_endpoints_200(client, env):
    for ep in ("obs/readiness", "obs/alerts", "obs/providers", "obs/models",
               "obs/budgets", "obs/economic-anomalies", "obs/shadow", "obs/canary"):
        r = client.get(f"/admin/{ep}")
        assert r.status_code == 200, f"{ep}: {r.status_code}"
    # ack endpoint
    r = client.post("/admin/obs/alerts/price:m1/ack")
    assert r.status_code == 200
    # canary stop
    r = client.post("/admin/obs/canary/config", json={"stop": True})
    assert r.status_code == 200
    assert r.json()["canary"].get("enabled") is False


def test_canary_config_validation(client, env):
    r = client.post("/admin/obs/canary/config", json={"traffic_pct": 5})
    assert r.status_code == 400, "model is required"
    r = client.post("/admin/obs/canary/config",
                    json={"model": "glm-5.3", "traffic_pct": 200})
    assert r.status_code == 200
    assert r.json()["canary"]["traffic_pct"] == 100.0, "pct clamped to 100"
