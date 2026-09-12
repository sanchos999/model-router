"""R14: Router Control Center — explainability + decision journal + task/tier
policy + recovery-point protection. Backend contract tests (no network)."""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GW_CONTROL_DB", str(tmp_path / "control.db"))
    monkeypatch.setenv("MODEL_ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GW_CONFIG", str(tmp_path / "gw.json"))
    from gateway.control import store
    store._CONTROL_DB = str(tmp_path / "control.db")
    store._conn = None
    from gateway.control.serve_control import app
    with TestClient(app) as c:
        yield c


# ── §10/§39: effective configuration ──────────────────────────────────────

def test_routing_effective_returns_source_and_default(client):
    r = client.get("/admin/routing/effective")
    assert r.status_code == 200
    params = {p["key"]: p for p in r.json()["params"]}
    md = params["min_discount"]
    assert md["value"] == 0.8
    assert md["default"] == 0.8
    assert md["source"]  # непустой человекочитаемый источник
    # timeouts приходят из кода/env и помечены не-configurable
    to = params.get("pre_first_failover_s")
    if to:
        assert to["configurable"] is False or to["source"] == "code"
    # каждый параметр несёт человекочитаемое описание
    for p_ in r.json()["params"]:
        assert p_.get("ru") or p_.get("hint")


def test_routing_pipeline_stage_order_matches_code(client):
    r = client.get("/admin/routing/pipeline")
    assert r.status_code == 200
    stages = [s["title"] for s in r.json()["stages"]]
    # кодовая правда: LEVEL A (качество) до LEVEL B (жёсткие фильтры)
    a = stages.index("Кандидаты-модели (LEVEL A)")
    b = stages.index("Жёсткие фильтры маршрута (LEVEL B)")
    assert a < b
    assert "Экономика кэша" in stages
    assert "Failover транспорта" in stages
    for st in r.json()["stages"]:
        assert st.get("ru") or st.get("detail")  # каждый блок объяснён


# ── §13: decision journal ─────────────────────────────────────────────────

def test_decision_journal_roundtrip(client):
    from gateway.control import store
    did = store.insert_decision({
        "ts": time.time(), "task_class": "SEARCH", "tier": "T1",
        "canonical": "glm-5.3", "provider": "provider_a",
        "provider_model_id": "cb/glm-5.3", "plan_len": 3,
        "reason": "primary:UNKNOWN:cost=0.5800 EXACT",
        "trace": {"required_context": 8231, "cache_state": "COLD"},
    })
    assert did
    rows = store.list_decisions(limit=10)
    assert any(r["id"] == did for r in rows)
    one = store.get_decision(did)
    assert one["task_class"] == "SEARCH"
    # by-id endpoint
    r = client.get(f"/admin/routing/decisions/{did}")
    assert r.status_code == 200
    assert r.json()["canonical"] == "glm-5.3"
    assert "reason_ru" in r.json()
    # privacy: no prompt fields ever
    assert "prompt" not in r.json() and "messages" not in r.json()


def test_distribution_counts_journal(client):
    from gateway.control import store
    for cls, tier in (("SEARCH", "T1"), ("NORMAL_CODING", "T2"), ("CRITICAL", "T4")):
        store.insert_decision({
            "ts": time.time(), "task_class": cls, "tier": tier,
            "canonical": "glm-5.3", "provider": "provider_a",
            "provider_model_id": "cb/glm-5.3", "plan_len": 1, "reason": "x",
            "trace": {}})
    r = client.get("/admin/routing/distribution?hours=24")
    assert r.status_code == 200
    d = r.json()
    assert d["sample_count"] >= 3
    assert d["by_tier"]["T1"]["requests"] >= 1
    crit = [x for x in d["by_task_class"] if x["name"] == "CRITICAL"]
    assert crit and crit[0]["requests"] >= 1
    ih = [x for x in d["by_provider"] if x["name"] == "provider_a"]
    assert ih and ih[0]["requests"] >= 3
    # honest pct
    total = sum(v["requests"] for v in d["by_tier"].values())
    assert total == d["sample_count"]


# ── §14–§17: task classes + tier policy ───────────────────────────────────

def test_task_classes_code_truth(client):
    r = client.get("/admin/taCHANGE_ME")
    assert r.status_code == 200
    d = r.json()
    classes = {c["class"]: c for c in d["classes"]}
    assert len(classes) == 13
    assert classes["CRITICAL"]["tier"] == "T4"
    assert classes["NORMAL_CODING"]["tier"] == "T2"
    assert classes["NORMAL_CODING"]["capabilities"] == ["streaming", "text", "tool_call"]
    assert classes["SIMPLE"]["source"] == "code"


def test_tier_for_class_config_override():
    from gateway.config import GatewayConfig
    from gateway.classifier import tier_for_class
    cfg = GatewayConfig()
    assert tier_for_class("NORMAL_CODING") == "T2"
    cfg.apply_fields({"task_classes": {"NORMAL_CODING": {"tier": "T3"}}})
    assert tier_for_class("NORMAL_CODING", config=cfg) == "T3"
    # disable → SIMPLE fallback (T1), never a hard failure
    cfg2 = GatewayConfig()
    cfg2.apply_fields({"task_classes": {"RESEARCH": {"enabled": False}}})
    assert tier_for_class("RESEARCH", config=cfg2) == "T1"


def test_task_classes_config_override_visible(client):
    from gateway.control import revisions as rev
    rev.create_draft({"task_classes": {"RESEARCH": {"tier": "T4"}}},
                     actor="t", reason="t")
    # find applied revision — apply the draft
    rid = rev.store.list_revisions(1)[0]["revision_id"]
    out = rev.apply_revision(rid, actor="t")
    assert out.get("applied")
    r = client.get("/admin/taCHANGE_ME")
    c = {x["class"]: x for x in r.json()["classes"]}["RESEARCH"]
    assert c["tier"] == "T4"
    assert c["source"] == "config"


def test_task_classes_validation_rejects_unknown_class():
    from gateway.control.revisions import validate_config
    errs = validate_config({"task_classes": {"NOT_A_CLASS": {"tier": "T2"}}})
    assert any("unknown class" in e for e in errs)
    errs = validate_config({"task_classes": {"SIMPLE": {"tier": "T9"}}})
    assert any("T1..T4" in e for e in errs)


# ── §33/§34: recovery point protection ────────────────────────────────────

def test_primary_known_good_set_and_delete_protection(client):
    # create two recovery points
    a = client.post("/admin/baselines", json={"name": "A"}).json()["baseline"]
    b = client.post("/admin/baselines", json={"name": "B"}).json()["baseline"]
    # set A primary
    r = client.post("/admin/baselines/known-good/set", json={"baseline_id": a["id"]})
    assert r.status_code == 200
    # deleting the ACTIVE primary must fail
    r = client.post(f"/admin/baselines/{a['id']}/delete")
    assert r.status_code == 409
    # switch primary to B, then A becomes deletable
    client.post("/admin/baselines/known-good/set", json={"baseline_id": b["id"]})
    r = client.post(f"/admin/baselines/{a['id']}/delete")
    assert r.status_code == 200
    # B (primary) still protected
    r = client.post(f"/admin/baselines/{b['id']}/delete")
    assert r.status_code == 409
    # list marks the primary
    lst = client.get("/admin/baselines").json()
    assert lst["primary_known_good"]["baseline_id"] == b["id"]
    ids = [x["id"] for x in lst["baselines"]]
    assert a["id"] not in ids and b["id"] in ids


def test_factory_default_never_deletable(client):
    from gateway.control import store
    row = store.create_baseline("FACTORY_DEFAULT", "factory", {})
    r = client.post(f"/admin/baselines/{row['id']}/delete")
    assert r.status_code == 409


# ── §6/§7: refresh + availability UX data ─────────────────────────────────

def test_cost_preview_savings_usd_and_honest_unknown(client, monkeypatch):
    """Калькулятор: savings_usd присутствует; при отсутствии цен — null, не NaN."""
    from gateway.control import admin_api, inventory as _inv

    fake = {"models": [{
        "canonical": "x", "display_name": "X",
        "best_input": None, "best_output": None,
        "official_input": None, "official_output": None,
        "effective_min_discount": 0.8, "routes": [],
    }]}
    monkeypatch.setattr(_inv, "build_inventory", lambda **k: fake)
    out = admin_api.cost_preview_raw("x", 1000, 100)
    assert out["best_ask_cost"] is None
    assert out["official_cost"] is None
    assert out["savings_usd"] is None
    assert out["savings_pct"] is None  # no NaN path


def test_overrides_real_kinds_only(client):
    """§20: UI kinds = backend kinds; unknown kind rejected."""
    r = client.post("/admin/overrides", json={
        "kind": "pin_canonical", "target": "gpt-5.6-luna",
        "ttl_s": 600, "reason": "t"})
    assert r.status_code in (400, 422)
    r = client.post("/admin/overrides", json={
        "kind": "DISABLE_MODEL", "target": "gpt-5.6-luna",
        "ttl_s": 600, "reason": "тест"})
    assert r.status_code == 200
    ov = r.json()["override"]
    assert ov["kind"] == "DISABLE_MODEL"
    assert ov["expires_at"] is not None


def test_persistent_override_requires_explicit_flag(client):
    r = client.post("/admin/overrides", json={
        "kind": "DISABLE_PROVIDER", "target": "provider_b", "reason": "t"})
    assert r.status_code in (400, 422)  # no ttl, no persistent
    r = client.post("/admin/overrides", json={
        "kind": "DISABLE_PROVIDER", "target": "provider_b",
        "persistent": True, "reason": "t"})
    assert r.status_code == 200
    assert r.json()["override"]["expires_at"] is None


# ── §25/§27: audit coverage ───────────────────────────────────────────────

def test_audit_russian_render_covers_r14_actions(client):
    from gateway.control import store
    store.audit("t", "baseline.delete", "bid", {"kind": "KNOWN_GOOD"})
    store.audit("t", "baseline.known_good", "bid", {})
    store.audit("t", "market.refresh", "provider_a", {"prices_changed": 3})
    store.audit("t", "availability.check", "pool", {"ok": 18, "failed": 1})
    r = client.get("/admin/audit?limit=10")
    texts = [a["text"] for a in r.json()["audit"]]
    assert any("Удалена точка" in t for t in texts)
    assert any("основная рабочая" in t for t in texts)
    assert any("обновлены" in t.lower() and "цены" in t.lower() for t in texts)
    assert any("Проверка доступности" in t for t in texts)
