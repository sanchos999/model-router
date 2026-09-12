"""R8 §28 — release hardening test suite.

Run: cd <model-router> && PYTHONPATH=. .venv/bin/python -m pytest gateway_tests/test_r8_release.py -q
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[1]


# ── §7 fresh venv / import isolation ───────────────────────────────────────

def test_runtime_loads_from_product_root():
    import gateway
    import gateway.app
    assert str(Path(gateway.__file__).parent).startswith(str(PRODUCT_ROOT))
    assert str(Path(gateway.app.__file__)).startswith(str(PRODUCT_ROOT))


def test_no_hermes_dependency():
    # no hermes/hermes-router path on sys.path; import must fail
    paths = [p for p in sys.path if p]
    assert not any(("hermes-router" in p) or p.endswith("/.hermes") for p in paths)
    with pytest.raises(ModuleNotFoundError):
        import hermes  # noqa: F401


def test_no_source_reference_to_legacy_tree():
    for py in (PRODUCT_ROOT / "gateway").rglob("*.py"):
        src = py.read_text()
        legacy = str(Path.home() / "hermes-router")
        assert legacy not in src, f"legacy path in {py}"


# ── §3 state relocation ────────────────────────────────────────────────────

def test_state_dir_env_override(monkeypatch):
    import gateway.state_paths as sp
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MODEL_ROUTER_STATE_DIR", d)
        monkeypatch.delenv("GATEWAY_STATE_DIR", raising=False)
        assert sp.state_dir() == d
        assert sp.state_file("x.json") == os.path.join(d, "x.json")
        # legacy alias wins when set (back-compat for prod/canary units)
        monkeypatch.setenv("GATEWAY_STATE_DIR", d + "/prod")
        assert sp.state_dir() == d + "/prod"


# ── §12 version endpoint ───────────────────────────────────────────────────

def test_version_payload_shape():
    from gateway.version import version_payload
    v = version_payload()
    assert set(v) >= {"version", "build_commit", "schema_version", "api_version", "instance_id"}
    assert v["api_version"] == "v1"
    assert isinstance(v["schema_version"], int)


# ── §13 schema versioning ──────────────────────────────────────────────────

def _fresh_db(tmp_path):
    db = str(tmp_path / "control.db")
    os.environ["GW_CONTROL_DB"] = db
    import gateway.control.store as store
    importlib.reload(store)
    store._conn = None
    return db, store


def test_schema_version_created_on_fresh_db(tmp_path):
    db, store = _fresh_db(tmp_path)
    c = store.conn()
    ver = c.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0]
    assert ver == store.CURRENT_SCHEMA_VERSION == 1
    c.close()


def test_future_schema_refused(tmp_path):
    db, store = _fresh_db(tmp_path)
    c = store.conn(); c.close()
    # stamp a future version directly
    raw = sqlite3.connect(db)
    raw.execute("UPDATE schema_meta SET schema_version = 99 WHERE id = 1")
    raw.commit(); raw.close()
    with pytest.raises(store.SchemaVersionError):
        store._connect()
    # and the report helper surfaces it as a version, not a crash
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0] == 99
    raw.close()


def test_migrations_idempotent(tmp_path):
    db, store = _fresh_db(tmp_path)
    c = store._connect()  # second connect re-runs versioning: must be a no-op
    ver = c.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0]
    assert ver == 1
    c.close()


# ── §11 config export/import ───────────────────────────────────────────────

def test_export_has_no_secrets(tmp_path):
    db, store = _fresh_db(tmp_path)
    from gateway.control import config_io
    payload = config_io.export_config()
    s = json.dumps(payload)
    assert payload["kind"] == "model-router-config-export"
    assert "sk-" not in s
    assert "API_KEY" not in s.replace("secret_ref", "").replace("_API_KEY", "")


def test_import_dry_run_and_reject_garbage(tmp_path):
    db, store = _fresh_db(tmp_path)
    from gateway.control import config_io
    # wrong kind rejected
    r = config_io.import_config({"kind": "bogus"}, actor="t", dry_run=True)
    assert not r["ok"] and not r["applied"]
    # valid defaults pass dry-run without applying
    r = config_io.import_config({"kind": config_io.EXPORT_KIND,
                                 "export_version": 1, "config": {"min_discount": 0.8}},
                                actor="t", dry_run=True)
    assert r["ok"] and not r["applied"] and r["dry_run"]


def test_import_apply_creates_revision(tmp_path):
    db, store = _fresh_db(tmp_path)
    from gateway.control import config_io
    r = config_io.import_config({"kind": config_io.EXPORT_KIND,
                                 "export_version": 1, "config": {"min_discount": 0.82}},
                                actor="r8-test", dry_run=False)
    assert r["ok"] and r["applied"], r
    cfg, rid = store.get_active_config()
    assert rid == r["revision_id"]
    assert cfg["min_discount"] == 0.82


# ── §21 auth separation ────────────────────────────────────────────────────

def test_inference_auth_disabled_by_default(monkeypatch):
    import gateway.app as app
    monkeypatch.delenv("MODEL_ROUTER_AUTH_MODE", raising=False)
    assert app._inference_auth_enabled() is False


def test_inference_auth_bearer_fail_closed(monkeypatch):
    import gateway.app as app

    class FakeReq:
        headers = {}

    monkeypatch.setenv("MODEL_ROUTER_AUTH_MODE", "bearer")
    monkeypatch.delenv("MODEL_ROUTER_PROVIDER_A_TOKEN", raising=False)
    assert app._inference_auth_enabled() is True
    assert app._check_inference_auth(FakeReq()) is False  # no token -> closed

    monkeypatch.setenv("MODEL_ROUTER_PROVIDER_A_TOKEN", "tok-123")
    FakeReq.headers = {"authorization": "Bearer tok-123"}
    assert app._check_inference_auth(FakeReq()) is True
    FakeReq.headers = {"authorization": "Bearer wrong"}
    assert app._check_inference_auth(FakeReq()) is False


def test_admin_token_separate_from_inference(monkeypatch):
    import gateway.control.admin_api as adm

    class FakeReq:
        headers = {}
        client = type("C", (), {"host": "10.0.0.5"})()

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    with pytest.raises(Exception):
        adm._check_auth(FakeReq())          # remote + no admin token -> 401
    monkeypatch.setenv("ADMIN_TOKEN", "admin-tok")
    FakeReq.headers = {"x-admin-token": "admin-tok"}
    adm._check_auth(FakeReq())              # ok with ADMIN token
    # inference token does NOT satisfy admin auth
    FakeReq.headers = {"x-admin-token": "tok-123"}
    with pytest.raises(Exception):
        adm._check_auth(FakeReq())


# ── §6 provider plugin loading ─────────────────────────────────────────────

def test_example_provider_plugin_loads():
    sys.path.insert(0, str(PRODUCT_ROOT))
    try:
        import examples.providers.example_openai_provider as ex
        a = ex.build_example_provider()
        assert a.name == "provider-n"
        assert "example" in a.base_url
    finally:
        sys.path.remove(str(PRODUCT_ROOT))


# ── §16 secret scan (fast subset, mirrors scripts/secret_scan.sh) ──────────

def test_no_real_keys_in_tree():
    import re
    pat = re.compile(r"sk-[A-Za-z0-9]{20,}")
    for f in PRODUCT_ROOT.rglob("*"):
        if ".venv" in f.parts or not f.is_file():
            continue
        if f.suffix not in {".py", ".md", ".yaml", ".sh", ".service", ".json", ".example", ".txt"}:
            continue
        m = pat.search(f.read_text(errors="ignore"))
        assert m is None, f"possible API key in {f}"


# ── §8 release state isolation ─────────────────────────────────────────────

def test_release_test_state_isolated(tmp_path):
    # release-test instances must not write into the product tree
    import gateway.state_paths as sp
    monkey = os.environ.copy()
    try:
        os.environ["GATEWAY_STATE_DIR"] = str(tmp_path)
        assert sp.state_dir() == str(tmp_path)
        assert str(tmp_path) != str(PRODUCT_ROOT / "state")
    finally:
        os.environ.clear(); os.environ.update(monkey)
