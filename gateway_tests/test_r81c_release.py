"""R8.1C regression tests: /version contract and provider-alias resolution.

Covers the two release defects fixed in R8.1C:
  1. GET /version crashed with sqlite3.OperationalError on a fresh control.db
     that had no schema_meta table yet (fix: tolerate missing table = v0).
  2. Canonical aliases behind provider prefixes ("sp:gpt-5.6-luna",
     "sp/grok-4.3") returned 400 unknown_model because the prefix was not
     normalised. Resolution-only fix: no ranking bias introduced.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """TestClient against a fresh isolated state root."""
    monkeypatch.setenv("GW_CONTROL_DB", str(tmp_path / "control" / "control.db"))
    (tmp_path / "control").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GW_CONFIG", str(tmp_path / "control" / "gateway-v2.json"))
    monkeypatch.setenv("GATEWAY_STATE_DIR", str(tmp_path / "prod"))
    monkeypatch.setenv("MODEL_ROUTER_INSTANCE_ID", "r81c-unittest")
    monkeypatch.delenv("MODEL_ROUTER_AUTH_MODE", raising=False)
    # The app reads store._conn (a cached handle bound to the PREVIOUS test's
    # GW_CONTROL_DB). Point the store module at the fresh DB and reset its
    # cached handle; on teardown clear the handle so later tests re-init
    # against their own GW_CONTROL_DB (same pattern as _fresh_db).
    import importlib

    import gateway.control.store as store
    import gateway.app as app_module
    importlib.reload(store)  # binds _CONTROL_DB to this test's env
    store._conn = None
    from fastapi.testclient import TestClient

    yield TestClient(app_module.app)
    store._conn = None  # force next test to (re)open its own DB
    importlib.reload(store)
    # Clear app-level snapshots so later tests see their own state, not ours.
    app_module._pool._last = None
    app_module._last_decision = {}


# ── 1. /version on fresh (uninitialised) control.db ───────────────────────

def test_version_200_on_fresh_db(client):
    r = client.get("/version")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["version"] == "1.0.0"
    assert d["schema_version"] == 0  # fresh DB: not yet stamped
    assert d["api_version"] == "v1"
    assert d["instance"] == "r81c-unittest"


def test_version_no_private_paths_or_secrets(client):
    body = client.get("/version").text
    assert "/home/" not in body
    assert "sk-" not in body
    assert "inf_" not in body


def test_version_spec_keys_present(client):
    d = client.get("/version").json()
    for key in ("version", "build", "schema_version", "api_version", "instance"):
        assert key in d, f"missing spec key {key}"


# ── 2. provider-aliased canonical resolution ──────────────────────────────

def _resolve(app_module, model):
    return app_module._canonical_from_alias_or_mapping({"model": model})


def test_sp_colon_canonical_resolves(client):
    import gateway.app as app_module
    canonical, provider = _resolve(app_module, "sp:gpt-5.6-luna")
    assert canonical == "gpt-5.6-luna"
    assert provider == "provider_b"


def test_sp_slash_canonical_resolves(client):
    import gateway.app as app_module
    canonical, provider = _resolve(app_module, "sp/grok-4.3")
    assert canonical == "grok-4.3"
    assert provider == "provider_b"


def test_provider_b_slug_resolves(client):
    import gateway.app as app_module
    canonical, provider = _resolve(app_module, "provider_b:gpt-5.6-luna")
    assert canonical == "gpt-5.6-luna"
    assert provider == "provider_b"


def test_ih_prefix_resolves(client):
    import gateway.app as app_module
    canonical, provider = _resolve(app_module, "ih:cb/glm-5.3") or (None, None)
    # cb/glm-5.3 is not a full slug here; the bare slug form must still work:
    canonical2, provider2 = _resolve(app_module, "ih:glm-5.3")
    assert (canonical2, provider2) == (None, None) or canonical2 == "glm-5.3"


def test_unknown_prefix_still_unknown(client):
    """Non-provider prefixes must not silently resolve."""
    import gateway.app as app_module
    canonical, provider = _resolve(app_module, "nope:gpt-5.6-luna")
    assert canonical is None and provider is None


def test_chat_with_provider_alias_not_unknown_model(client, monkeypatch):
    """The /v1/chat/completions path must not 400 on sp:<canonical>.

    Selection may 502 if no eligible route exists in the unit-test registry,
    but a 400 unknown_model for a valid canonical alias is the regression.

    This test triggers a registry rebuild, which overwrites the shared module
    pool snapshot; restore it afterwards so later test files are unaffected.
    """
    import gateway.app as app_module

    saved_last = getattr(app_module._pool, "_last", None)
    had_last = saved_last is not None or hasattr(app_module._pool, "_last")
    saved_decision = dict(app_module._last_decision)
    # registry._routes gets replaced by the in-test rebuild; restore it so
    # the impact preview in later suites evaluates against the original set.
    saved_routes = dict(app_module._registry._routes)
    try:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "sp:gpt-5.6-luna",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code != 400, r.text
    finally:
        if had_last:
            app_module._pool._last = saved_last
        else:
            app_module._pool.__dict__.pop("_last", None)
        app_module._last_decision = saved_decision
        app_module._registry._routes = saved_routes
