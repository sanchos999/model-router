"""R9 Admin UI / API contract tests.

Verifies that EVERY endpoint the Admin UI (gateway/control/serve_control.py
PAGE JS) calls actually exists as a registered FastAPI route and returns a
usable (2xx, or explicitly-handled error shape) response — no 404/405/422/500
from a UI-initiated request on the happy path.

Run: cd <model-router checkout> && PYTHONPATH=. python3 -m pytest gateway_tests/test_r9_admin_ui_contract.py -q
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROUTER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROUTER_ROOT))


# Every fetch() URL the Admin UI JS can issue, derived from the PAGE source.
UI_ENDPOINTS = [
    ("GET", "/admin/healthz"),
    ("GET", "/admin/router/metrics-summary"),
    ("GET", "/admin/runtime/health"),
    ("GET", "/admin/models/pool"),
    ("GET", "/admin/providers"),
    ("POST", "/admin/providers"),
    ("POST", "/admin/providers/{name}/enable"),
    ("POST", "/admin/providers/{name}/disable"),
    ("DELETE", "/admin/providers/{name}"),
    ("GET", "/admin/config/active"),
    ("POST", "/admin/config/revisions"),
    ("GET", "/admin/config/revisions"),
    ("POST", "/admin/config/impact-preview"),
    ("POST", "/admin/config/revisions/{rid}/apply"),
    ("POST", "/admin/config/revisions/{rid}/rollback"),
    ("POST", "/admin/config/revisions/{rid}/validate"),
    ("POST", "/admin/config/revisions/{rid}/simulate"),
    ("GET", "/admin/overrides"),
    ("POST", "/admin/overrides"),
    ("DELETE", "/admin/overrides/{oid}"),
    ("POST", "/admin/simulate"),
    ("GET", "/admin/audit"),
    ("GET", "/healthz"),
    ("GET", "/admin-ui/"),
]


def _page_source() -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "gateway" / "control" / "static" / "admin"
    return (root / "index.html").read_text(encoding="utf-8") + "\n" + (root / "app.js").read_text(encoding="utf-8")


@pytest.fixture()
def control_env(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    monkeypatch.setenv("GW_CONTROL_DB", str(db))
    monkeypatch.setenv("GW_CONFIG", str(tmp_path / "gateway-v2.json"))
    import gateway.control.store as store
    importlib.reload(store)
    import gateway.control.revisions as revisions
    importlib.reload(revisions)
    import gateway.control.overrides as overrides
    importlib.reload(overrides)
    yield {"store": store, "revisions": revisions, "overrides": overrides,
           "tmp": tmp_path}


@pytest.fixture()
def serve_app(control_env, monkeypatch):
    """Full control-plane app (admin router + UI) like the real :4111 process,
    with runtime proxy pointed at a stub HTTP server."""
    # stub runtime router
    from fastapi import FastAPI

    runtime = FastAPI()

    @runtime.get("/health")
    async def _h():
        return {"ok": True, "service": "model-router", "instance_id": "stub",
                "uptime_s": 10.0, "registry": {"routes": 3, "canonicals": 2,
                                                "providers": ["provider_a", "provider_b"]}}

    @runtime.get("/provider/share")
    async def _ps():
        return {"providers": {"provider_a": {"requests": 4, "successes": 4,
                                           "failures": 0, "cost_usd": 0.0002,
                                           "cost_per_success": 5e-05,
                                           "total_p50_ms": 900.0,
                                           "total_p95_ms": 1500.0,
                                           "eligible_routes": 7}},
                "eligible_routes": {"provider_a": 7}}

    @runtime.get("/models/pool")
    async def _mp():
        return {"summary": {"canonical_total": 2, "eligible_canonicals": 1,
                            "counts": {"CORE": 1, "WATCH": 1, "UNAVAILABLE": 0}},
                "models": {"gpt-5.6-luna": {"canonical": "gpt-5.6-luna",
                                            "lifecycle": "CORE",
                                            "quality_status": "PROVISIONAL",
                                            "quality_score": 0.81,
                                            "eligible_providers": ["provider_a"],
                                            "best_current_route": {"route": "ih:cb/gpt-5.6-luna"}}}}

    import threading
    import socket
    import uvicorn
    _s = socket.socket()
    _s.bind(("127.0.0.1", 0))
    port = _s.getsockname()[1]
    _s.close()
    t = threading.Thread(
        target=uvicorn.run, args=(runtime,), kwargs={"host": "127.0.0.1",
                                                     "port": port, "log_level": "error"},
        daemon=True)
    t.start()
    import time
    for _ in range(50):
        try:
            import httpx
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    monkeypatch.setenv("GW_RUNTIME_URL", f"http://127.0.0.1:{port}")

    import gateway.control.admin_api as admin_api
    importlib.reload(admin_api)
    import gateway.control.serve_control as serve_control
    importlib.reload(serve_control)
    yield serve_control.app


@pytest.fixture()
def client(serve_app):
    return TestClient(serve_app)


# ── §1 endpoint registration vs UI calls ───────────────────────────────────

def test_every_ui_endpoint_is_registered(client):
    """No UI fetch() may hit an unregistered path (404) or wrong method (405)."""
    openapi = client.get("/openapi.json").json()
    registered = {(m.upper(), p) for p, ms in openapi["paths"].items()
                  for m in ms}
    for method, path in UI_ENDPOINTS:
        assert (method, path) in registered, \
            f"UI calls {method} {path} but it is not registered"


def test_ui_source_uses_no_unregistered_direct_paths():
    """All /admin, /healthz fetch URLs in the PAGE source are in the contract
    set — catches drift when the JS is edited. Dynamic concatenated paths
    (e.g. '/admin/models/'+canonical+'/policy') are checked as prefixes."""
    from fastapi.testclient import TestClient
    import gateway.control.serve_control as sc
    src = _page_source()
    called = {c.split("?")[0] for c in re.findall(r"M\('([^']+)'", src)}
    allowed = {p.split("?")[0] for _, p in UI_ENDPOINTS
               if not p.startswith("/admin-ui")}
    openapi = TestClient(sc.app).get("/openapi.json").json()
    registered = set(openapi["paths"])
    for c in called:
        if c in allowed:
            continue
        # dynamic prefix (JS concatenates the rest): OK if some registered
        # route starts with it (e.g. '/admin/models/' + canonical + '/policy')
        if any(r.startswith(c) for r in registered):
            continue
        raise AssertionError(f"UI fetches {c!r} which is not in the contract set")


def test_no_ui_call_to_removed_paths():
    """Legacy broken paths must not reappear in the UI."""
    src = _page_source()
    assert "'/router/metrics-summary'" not in src
    assert "'/models/pool'" not in src


# ── §2 happy-path responses for every tab ──────────────────────────────────

def test_dashboard_endpoints_ok(client):
    for path in ("/admin/healthz", "/admin/router/metrics-summary",
                 "/admin/runtime/health"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text)
    ms = client.get("/admin/router/metrics-summary").json()
    assert "provider_a" in ms["providers"], ms
    v = ms["providers"]["provider_a"]
    for k in ("requests", "successes", "failures", "success_rate", "p50_ms",
              "p95_ms", "cost_est"):
        assert k in v
    assert ms["errors"] == []
    rh = client.get("/admin/runtime/health").json()
    assert rh["ok"] is True and rh["data"]["service"] == "model-router"


def test_dashboard_reports_runtime_errors_not_empty_tables(client,
                                                           monkeypatch):
    """Runtime router down -> explicit error with endpoint + status, not an
    empty providers table."""
    import gateway.control.admin_api as admin_api
    monkeypatch.setattr(admin_api, "RUNTIME_BASE_URL", "http://127.0.0.1:1")
    r = client.get("/admin/router/metrics-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["providers"] == {}
    assert body["errors"], "expected explicit error entries"
    e = body["errors"][0]
    assert e["endpoint"].startswith("http://") and e["error"]
    rh = client.get("/admin/runtime/health").json()
    assert rh["ok"] is False and rh["error"]


def test_models_pool_ok(client):
    r = client.get("/admin/models/pool")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert "models" in body and "summary" in body


def test_providers_lists_config_providers(client):
    r = client.get("/admin/providers")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["providers"]]
    assert "provider_a" in names and "provider_b" in names, names


def test_routing_policy_read_and_revise(client):
    r = client.get("/admin/config/active")
    assert r.status_code == 200
    assert r.json()["config"]["min_discount"] is not None
    r = client.post("/admin/config/revisions",
                    json={"config": {"min_discount": 0.82}, "reason": "ui test"})
    assert r.status_code == 200, r.text
    rid = r.json()["revision"]["revision_id"]
    r = client.post(f"/admin/config/revisions/{rid}/validate")
    assert r.status_code == 200
    r = client.post(f"/admin/config/revisions/{rid}/apply")
    assert r.status_code == 200, r.text
    assert r.json()["revision"]["status"] == "APPLIED"
    assert client.get("/admin/config/active").json()["config"]["min_discount"] == 0.82


def test_tier_policy_flow(client):
    r = client.post("/admin/config/revisions",
                    json={"config": {"quality_floors": {"T4": 0.8}},
                          "reason": "tier test"})
    assert r.status_code == 200
    rid = r.json()["revision"]["revision_id"]
    assert client.post(f"/admin/config/revisions/{rid}/apply").status_code == 200
    floors = client.get("/admin/config/active").json()["config"]["quality_floors"]
    assert floors["T4"] == 0.8


def test_overrides_crud(client):
    r = client.post("/admin/overrides",
                    json={"kind": "FORCE_CANONICAL", "target": "gpt-5.6-luna",
                          "reason": "ui test", "ttl_s": 600})
    assert r.status_code == 200, r.text
    oid = r.json()["override"]["override_id"]
    assert any(o["override_id"] == oid
               for o in client.get("/admin/overrides").json()["overrides"])
    assert client.delete(f"/admin/overrides/{oid}").status_code == 200
    assert not any(o["override_id"] == oid
                   for o in client.get("/admin/overrides").json()["overrides"])
    # invalid override -> 422 with detail (not 500)
    r = client.post("/admin/overrides", json={"kind": "BOGUS", "target": "x",
                                              "reason": "r"})
    assert r.status_code == 422


def test_simulator_returns_decision(client, monkeypatch):
    """Simulator must return a structured decision without production
    inference. Registry build is stubbed to avoid network calls."""
    import gateway.control.admin_api as admin_api

    class _FakeRoute:
        canonical = "gpt-5.6-luna"
        provider = "provider_a"
        provider_model_id = "cb/gpt-5.6-luna"
        reason = "stub"

    class _FakePrimary:
        canonical = "gpt-5.6-luna"
        provider = "provider_a"
        provider_model_id = "cb/gpt-5.6-luna"
        reason = "stub"

    def _fake_choose(ctx):
        return _FakePrimary(), [_FakeRoute()], {"candidate_canonicals": ["gpt-5.6-luna"],
                                                "rejected_canonicals": [],
                                                "rejected_routes": [],
                                                "cache_economics": {},
                                                "hint_fallback": False}

    async def _fake_build(force=False):
        return 1

    monkeypatch.setattr(admin_api, "_ensure_registry_built", _fake_build)
    import gateway.app as gapp
    monkeypatch.setattr(gapp, "_selector", type("S", (), {"choose": staticmethod(_fake_choose)})())
    monkeypatch.setattr(gapp, "_registry", type("R", (), {"all": staticmethod(lambda: [object()])})())
    r = client.post("/admin/simulate", json={"mode": "AUTO",
                                             "task_class": "NORMAL_CODING",
                                             "context_tokens": 20000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["winner"] is not None
    assert body["winner"]["canonical"] == "gpt-5.6-luna"
    assert "plan" in body and "hard_gates" in body


def test_audit_and_revisions_list(client):
    assert client.get("/admin/audit").status_code == 200
    assert client.get("/admin/config/revisions").status_code == 200
    # audit rows exist after a revision was made
    client.post("/admin/config/revisions",
                json={"config": {"min_discount": 0.81}, "reason": "audit test"})
    assert len(client.get("/admin/audit").json()["audit"]) > 0


def test_admin_ui_page_served(client):
    r = client.get("/admin-ui/")
    assert r.status_code == 200
    assert "Model Router" in r.text
    assert "/admin-ui/app.js" in r.text


def test_healthz_root(client):
    assert client.get("/healthz").status_code == 200


# ── §3 error surfaces ──────────────────────────────────────────────────────

def test_unknown_revision_returns_404_not_500(client):
    assert client.post("/admin/config/revisions/rev-nope/validate").status_code == 404
    assert client.post("/admin/config/revisions/rev-nope/apply").status_code == 404


def test_unknown_provider_toggle_returns_404(client):
    assert client.post("/admin/providers/ghost/enable").status_code == 404
    assert client.post("/admin/providers/ghost/disable").status_code == 404


def test_invalid_revision_payload_returns_400(client):
    r = client.post("/admin/config/revisions", json={"config": {}, "reason": ""})
    assert r.status_code == 400
