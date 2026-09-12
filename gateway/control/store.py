"""Control plane persistent store (spec R6 §C/§D/§N).

Single SQLite database (state/control.db). Holds:
  config_active    — currently applied mutable policy (config_json + revision)
  config_revisions — full revision history (DRAFT..APPLIED/ROLLED_BACK)
  overrides        — temporary/persistent routing overrides with TTL
  providers        — provider registry (adapter type, base_url, secret_ref)
  model_policy     — per-canonical admin policy (lifecycle override, restrictions)
  audit_log        — who changed what, when, why (no secrets, no prompts)

Secrets are NEVER stored here: providers.secret_ref holds an environment
variable NAME, resolved at request time by the adapter.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

# R11: RLock — store functions call audit() (which takes the same lock)
# from inside locked sections; a plain Lock deadlocked the control plane.
_lock = threading.RLock()

# R8 §3: control DB lives in the product data root by default;
# GW_CONTROL_DB overrides (legacy). Control state is shared across
# instances (prod/canary) by design — see state_paths.py.
from gateway.state_paths import state_dir as _shared_state_dir

_CONTROL_DB = os.environ.get(
    "GW_CONTROL_DB",
    os.path.join(_shared_state_dir(), "control.db"),
)
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS config_active (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL,
    revision_id TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS config_revisions (
    revision_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    previous_revision TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    applied_at REAL
);
CREATE TABLE IF NOT EXISTS overrides (
    override_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    persistent INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS providers (
    name TEXT PRIMARY KEY,
    adapter_type TEXT NOT NULL,
    base_url TEXT,
    secret_ref TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    catalog_refresh_interval_s REAL,
    min_discount_override REAL,
    health_policy_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS model_policy (
    canonical TEXT PRIMARY KEY,
    lifecycle_override TEXT,
    restricted_providers_json TEXT NOT NULL DEFAULT '[]',
    tier_restriction TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '{}',
    revision_id TEXT
);
-- R14 §13: privacy-safe routing decision journal (task class, tier, winner,
-- reasons only — never prompt content). Written by the runtime instance.
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    instance TEXT NOT NULL DEFAULT 'prod',
    task_class TEXT,
    tier TEXT,
    canonical TEXT,
    provider TEXT,
    provider_model_id TEXT,
    plan_len INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    trace_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log(ts);
-- R15 §11: system events history — SEPARATE from the revision log.
-- Anomalies, alerts, refresh results, drift: everything that happened to
-- the system without being a configuration change.
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    object TEXT,
    reason TEXT,
    recommended TEXT,
    fields_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_system_events_ts ON system_events(ts);
-- R15 §8: provider schema-drift fingerprints recorded at refresh time.
CREATE TABLE IF NOT EXISTS fingerprint_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    prev_fingerprint TEXT,
    reason TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warn'
);
CREATE INDEX IF NOT EXISTS idx_fp_alerts_ts ON fingerprint_alerts(ts);
-- R15 §10: acknowledged alerts (alert_key = kind:object).
CREATE TABLE IF NOT EXISTS alert_acks (
    alert_key TEXT PRIMARY KEY,
    acked_at REAL NOT NULL,
    acked_by TEXT NOT NULL
);
-- R14 §33: which KNOWN_GOOD baseline is the primary working recovery point.
CREATE TABLE IF NOT EXISTS known_good_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    baseline_id TEXT NOT NULL,
    set_at REAL NOT NULL,
    set_by TEXT NOT NULL
);

"""


def db_path() -> str:
    return _CONTROL_DB


# R8 §13: state schema versioning. Bump when the layout changes and add a
# migration below. A DB stamped with a FUTURE version makes the service
# refuse to start (safe, non-destructive) instead of writing garbage.
CURRENT_SCHEMA_VERSION = 1
SCHEMA_META_TABLE = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);
"""
_MIGRATIONS: dict[int, str] = {
    # version -> SQL applied on top of the previous version (idempotent)
}


def _read_schema_version(c: sqlite3.Connection) -> int:
    try:
        row = c.execute(
            "SELECT schema_version FROM schema_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # fresh / not-yet-initialised DB: schema_meta is created lazily by
        # _apply_schema_versioning(); until then treat as version 0 (R8.1C).
        return 0
    return int(row[0]) if row else 0


def db_schema_version() -> int:
    # separate short-lived connection: conn() returns a shared handle that
    # must not be closed here
    c = sqlite3.connect(_CONTROL_DB, timeout=10.0)
    try:
        c.execute("PRAGMA busy_timeout=10000")
        return _read_schema_version(c)
    finally:
        c.close()


class SchemaVersionError(RuntimeError):
    """control.db was written by a NEWER product version — refuse safely."""


def _apply_schema_versioning(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA_META_TABLE)
    v = _read_schema_version(c)
    if v == 0:
        # fresh (or pre-versioning) DB: existing tables were created by the
        # executescript(SCHEMA) above, which IS schema version 1.
        c.execute(
            "INSERT OR REPLACE INTO schema_meta (id, schema_version, applied_at) VALUES (1, ?, ?)",
            (CURRENT_SCHEMA_VERSION, time.time()),
        )
    elif v > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"control.db schema_version={v} is newer than supported "
            f"{CURRENT_SCHEMA_VERSION}; upgrade Model Router before using this state"
        )
    for target in sorted(_MIGRATIONS):
        if v < target <= CURRENT_SCHEMA_VERSION:
            c.executescript(_MIGRATIONS[target])
            c.execute(
                "INSERT OR REPLACE INTO schema_meta (id, schema_version, applied_at) VALUES (1, ?, ?)",
                (target, time.time()),
            )
    c.commit()


def _connect() -> sqlite3.Connection:
    Path(_CONTROL_DB).parent.mkdir(parents=True, exist_ok=True)
    # R7 §2: control.db is shared by control (:4111) and both inference
    # instances (:4100/:4101) — WAL + busy_timeout make cross-process
    # read/write concurrency safe (short transactions, atomic apply).
    conn = sqlite3.connect(_CONTROL_DB, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass  # best-effort; rollback journal still safe with busy_timeout
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _apply_schema_versioning(conn)
    conn.commit()
    return conn


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        return _conn


def audit(actor: str, action: str, entity: str, fields: dict | None = None,
          revision_id: str | None = None) -> None:
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO audit_log (ts, actor, action, entity, fields_json, revision_id)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), actor, action, entity,
             json.dumps(fields or {}), revision_id))
        c.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── active config ──────────────────────────────────────────────────────────

def get_active_config() -> tuple[dict, str | None]:
    """Returns (config_dict, revision_id). Empty dict = never applied."""
    row = conn().execute("SELECT config_json, revision_id FROM config_active WHERE id=1").fetchone()
    if row is None:
        return {}, None
    return json.loads(row["config_json"]), row["revision_id"]


def set_active_config(config: dict, revision_id: str) -> None:
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO config_active (id, config_json, revision_id, updated_at) VALUES (1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,"
            " revision_id=excluded.revision_id, updated_at=excluded.updated_at",
            (json.dumps(config), revision_id, time.time()))
        c.commit()


# ── revisions ──────────────────────────────────────────────────────────────

def insert_revision(rev: dict) -> None:
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO config_revisions (revision_id, created_at, actor, reason,"
            " diff_json, config_json, previous_revision, status, warnings_json, applied_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rev["revision_id"], rev["created_at"], rev["actor"], rev["reason"],
             json.dumps(rev["diff"]), json.dumps(rev["config"]),
             rev.get("previous_revision"), rev["status"],
             json.dumps(rev.get("warnings") or []), rev.get("applied_at")))
        c.commit()


def update_revision_status(revision_id: str, status: str,
                           warnings: list | None = None, applied_at: float | None = None) -> None:
    c = conn()
    with _lock:
        c.execute(
            "UPDATE config_revisions SET status=?, warnings_json=?, applied_at=? WHERE revision_id=?",
            (status, json.dumps(warnings or []), applied_at, revision_id))
        c.commit()


def get_revision(revision_id: str) -> dict | None:
    row = conn().execute(
        "SELECT * FROM config_revisions WHERE revision_id=?", (revision_id,)).fetchone()
    if row is None:
        return None
    return _revision_row(row)


def _revision_row(row) -> dict:
    return {
        "revision_id": row["revision_id"],
        "created_at": row["created_at"],
        "actor": row["actor"],
        "reason": row["reason"],
        "diff": json.loads(row["diff_json"]),
        "config": json.loads(row["config_json"]),
        "previous_revision": row["previous_revision"],
        "status": row["status"],
        "warnings": json.loads(row["warnings_json"]),
        "applied_at": row["applied_at"],
    }


def list_revisions(limit: int = 100) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM config_revisions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_revision_row(r) for r in rows]


# ── overrides ──────────────────────────────────────────────────────────────

def insert_override(ov: dict) -> None:
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO overrides (override_id, kind, target, params_json, reason, actor,"
            " created_at, expires_at, persistent, enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ov["override_id"], ov["kind"], ov["target"], json.dumps(ov.get("params") or {}),
             ov["reason"], ov["actor"], ov["created_at"], ov.get("expires_at"),
             1 if ov.get("persistent") else 0, 1))
        c.commit()


def list_overrides(active_only: bool = True) -> list[dict]:
    rows = conn().execute("SELECT * FROM overrides ORDER BY created_at DESC").fetchall()
    now = time.time()
    out = []
    for r in rows:
        if active_only and not r["enabled"]:
            continue
        if active_only and r["expires_at"] is not None and r["expires_at"] <= now:
            continue
        out.append(_override_row(r))
    return out


def _override_row(r) -> dict:
    return {
        "override_id": r["override_id"], "kind": r["kind"], "target": r["target"],
        "params": json.loads(r["params_json"]), "reason": r["reason"], "actor": r["actor"],
        "created_at": r["created_at"], "expires_at": r["expires_at"],
        "persistent": bool(r["persistent"]), "enabled": bool(r["enabled"]),
    }


def get_override(override_id: str) -> dict | None:
    row = conn().execute("SELECT * FROM overrides WHERE override_id=?", (override_id,)).fetchone()
    return _override_row(row) if row else None


def disable_override(override_id: str) -> bool:
    c = conn()
    with _lock:
        cur = c.execute("UPDATE overrides SET enabled=0 WHERE override_id=?", (override_id,))
        c.commit()
    return cur.rowcount > 0


# ── providers ──────────────────────────────────────────────────────────────

def upsert_provider(name: str, adapter_type: str, base_url: str | None,
                    secret_ref: str | None, enabled: bool = True,
                    catalog_refresh_interval_s: float | None = None,
                    min_discount_override: float | None = None,
                    health_policy: dict | None = None) -> dict:
    now = time.time()
    c = conn()
    with _lock:
        c.execute(
            "INSERT INTO providers (name, adapter_type, base_url, secret_ref, enabled,"
            " catalog_refresh_interval_s, min_discount_override, health_policy_json,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET adapter_type=excluded.adapter_type,"
            " base_url=excluded.base_url, secret_ref=excluded.secret_ref,"
            " enabled=excluded.enabled, catalog_refresh_interval_s=excluded.catalog_refresh_interval_s,"
            " min_discount_override=excluded.min_discount_override,"
            " health_policy_json=excluded.health_policy_json, updated_at=excluded.updated_at",
            (name, adapter_type, base_url, secret_ref, 1 if enabled else 0,
             catalog_refresh_interval_s, min_discount_override,
             json.dumps(health_policy or {}), now, now))
        c.commit()
    audit("admin-api", "provider.upsert", name, {"adapter_type": adapter_type, "enabled": enabled})
    return get_provider(name)


def get_provider(name: str) -> dict | None:
    row = conn().execute("SELECT * FROM providers WHERE name=?", (name,)).fetchone()
    return _provider_row(row) if row else None


def _provider_row(r) -> dict:
    secret_ref = r["secret_ref"]
    return {
        "name": r["name"], "adapter_type": r["adapter_type"], "base_url": r["base_url"],
        # SECURITY: never return the secret itself — presence only.
        "secret_ref": secret_ref,
        "secret_configured": bool(secret_ref) and bool(os.environ.get(secret_ref)),
        "enabled": bool(r["enabled"]), "archived": bool(r["archived"]),
        "catalog_refresh_interval_s": r["catalog_refresh_interval_s"],
        "min_discount_override": r["min_discount_override"],
        "health_policy": json.loads(r["health_policy_json"]),
        "updated_at": r["updated_at"],
    }


def list_providers(include_archived: bool = False) -> list[dict]:
    q = "SELECT * FROM providers" + ("" if include_archived else " WHERE archived=0")
    rows = conn().execute(q + " ORDER BY name").fetchall()
    return [_provider_row(r) for r in rows]


def set_provider_enabled(name: str, enabled: bool) -> bool:
    c = conn()
    with _lock:
        cur = c.execute("UPDATE providers SET enabled=?, updated_at=? WHERE name=?",
                        (1 if enabled else 0, time.time(), name))
        c.commit()
    if cur.rowcount:
        audit("admin-api", "provider.enable" if enabled else "provider.disable", name, {})
    return cur.rowcount > 0


def archive_provider(name: str) -> bool:
    c = conn()
    with _lock:
        cur = c.execute("UPDATE providers SET archived=1, enabled=0, updated_at=? WHERE name=?",
                        (time.time(), name))
        c.commit()
    if cur.rowcount:
        audit("admin-api", "provider.archive", name, {})
    return cur.rowcount > 0


# ── model policy ───────────────────────────────────────────────────────────

def set_model_policy(canonical: str, lifecycle_override: str | None = None,
                     restricted_providers: list[str] | None = None,
                     tier_restriction: str | None = None) -> dict:
    now = time.time()
    c = conn()
    with _lock:
        row = c.execute("SELECT * FROM model_policy WHERE canonical=?", (canonical,)).fetchone()
        lo = lifecycle_override if lifecycle_override is not None else (row["lifecycle_override"] if row else None)
        rp = restricted_providers if restricted_providers is not None else (
            json.loads(row["restricted_providers_json"]) if row else [])
        tr = tier_restriction if tier_restriction is not None else (row["tier_restriction"] if row else None)
        c.execute(
            "INSERT INTO model_policy (canonical, lifecycle_override, restricted_providers_json,"
            " tier_restriction, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(canonical) DO UPDATE SET lifecycle_override=excluded.lifecycle_override,"
            " restricted_providers_json=excluded.restricted_providers_json,"
            " tier_restriction=excluded.tier_restriction, updated_at=excluded.updated_at",
            (canonical, lo, json.dumps(rp), tr, now))
        c.commit()
    audit("admin-api", "model_policy.set", canonical, {"lifecycle": lo})
    return get_model_policy(canonical)


def get_model_policy(canonical: str) -> dict:
    row = conn().execute("SELECT * FROM model_policy WHERE canonical=?", (canonical,)).fetchone()
    if row is None:
        return {"canonical": canonical, "lifecycle_override": None,
                "restricted_providers": [], "tier_restriction": None}
    return {"canonical": canonical, "lifecycle_override": row["lifecycle_override"],
            "restricted_providers": json.loads(row["restricted_providers_json"]),
            "tier_restriction": row["tier_restriction"], "updated_at": row["updated_at"]}


def list_model_policy() -> list[dict]:
    rows = conn().execute("SELECT * FROM model_policy ORDER BY canonical").fetchall()
    return [get_model_policy(r["canonical"]) for r in rows]


# ── audit read ─────────────────────────────────────────────────────────────

def list_audit(limit: int = 200) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
             "entity": r["entity"], "fields": json.loads(r["fields_json"]),
             "revision_id": r["revision_id"]} for r in rows]


# ── availability evidence (UI spec §F/§G) ──────────────────────────────────
# Runtime health evidence from control-plane probes. Never mutates policy.

AVAILABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS availability_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_model_id TEXT NOT NULL,
    checked_at REAL NOT NULL,
    ok INTEGER NOT NULL,
    probe TEXT NOT NULL DEFAULT 'models',
    http_status INTEGER,
    error_code TEXT,
    error TEXT,
    latency_ms INTEGER,
    models_found INTEGER,
    reply TEXT,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_avail_route_time
    ON availability_checks (provider, provider_model_id, checked_at DESC);
"""


def record_availability(result: dict) -> None:
    c = conn()
    with _lock:
        c.executescript(AVAILABILITY_SCHEMA)
        c.execute(
            "INSERT INTO availability_checks (provider, provider_model_id, checked_at,"
            " ok, probe, http_status, error_code, error, latency_ms, models_found,"
            " reply, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (result.get("provider"), result.get("provider_model_id"),
             result.get("checked_at") or time.time(),
             1 if result.get("ok") else 0, result.get("probe") or "models",
             result.get("http_status"), result.get("error_code"),
             (result.get("error") or "")[:500] or None,
             result.get("latency_ms"), result.get("models_found"),
             result.get("reply"), json.dumps(result)[:4000]))
        c.commit()


def _avail_row(r) -> dict:
    d = json.loads(r["result_json"]) if r["result_json"] else {}
    d.setdefault("ok", bool(r["ok"]))
    d.setdefault("checked_at", r["checked_at"])
    d.setdefault("latency_ms", r["latency_ms"])
    d.setdefault("error_code", r["error_code"])
    return d


def last_availability(provider: str, provider_model_id: str) -> dict | None:
    c = conn()
    try:
        row = c.execute(
            "SELECT * FROM availability_checks WHERE provider=? AND provider_model_id=?"
            " ORDER BY checked_at DESC LIMIT 1",
            (provider, provider_model_id)).fetchone()
    except sqlite3.OperationalError:
        return None  # table not created yet — no evidence
    return _avail_row(row) if row else None


def availability_for_provider(provider: str, limit: int = 500) -> list[dict]:
    """Latest check per route of a provider."""
    c = conn()
    try:
        rows = c.execute(
            "SELECT a.* FROM availability_checks a JOIN ("
            "  SELECT provider, provider_model_id, MAX(checked_at) AS m"
            "  FROM availability_checks WHERE provider=? GROUP BY provider, provider_model_id"
            ") b ON a.provider=b.provider AND a.provider_model_id=b.provider_model_id"
            " AND a.checked_at=b.m WHERE a.provider=? LIMIT ?",
            (provider, provider, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_avail_row(r) for r in rows]


# ── R11: raw discovery inventory (DISCOVERY != ELIGIBILITY) ────────────────

DISCOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovered_models (
    provider TEXT NOT NULL,
    provider_model_id TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    last_priced REAL,
    missing_since REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (provider, provider_model_id)
);
CREATE TABLE IF NOT EXISTS canonical_map (
    provider TEXT NOT NULL,
    provider_model_id TEXT NOT NULL,
    canonical TEXT NOT NULL,
    display_name TEXT,
    source TEXT NOT NULL DEFAULT 'manual',   -- builtin | manual | automatch
    created_at REAL NOT NULL,
    PRIMARY KEY (provider, provider_model_id)
);
CREATE TABLE IF NOT EXISTS model_pool (
    canonical TEXT PRIMARY KEY,
    in_pool INTEGER NOT NULL DEFAULT 1,      -- my-pool membership
    hidden INTEGER NOT NULL DEFAULT 0,
    min_discount_override REAL,              -- NULL = inherit global
    max_input_price REAL,
    max_output_price REAL,
    preferred_providers TEXT NOT NULL DEFAULT '[]',
    banned_providers TEXT NOT NULL DEFAULT '[]',
    auto_routing INTEGER NOT NULL DEFAULT 1,
    lifecycle_override TEXT,
    notes TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    provider TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disc_missing ON discovered_models (provider, missing_since);
"""


def _ensure_discovery_schema(c) -> None:
    c.executescript(DISCOVERY_SCHEMA)
    c.commit()


def upsert_discovered(provider: str, pid: str, raw: dict, *, priced: bool,
                      now: float) -> bool:
    """Insert/update a raw catalog row. Returns True when newly discovered."""
    c = conn()
    with _lock:
        _ensure_discovery_schema(c)
        row = c.execute("SELECT first_seen FROM discovered_models"
                        " WHERE provider=? AND provider_model_id=?",
                        (provider, pid)).fetchone()
        c.execute(
            "INSERT INTO discovered_models (provider, provider_model_id, first_seen,"
            " last_seen, last_priced, missing_since, raw_json) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(provider, provider_model_id) DO UPDATE SET"
            " last_seen=excluded.last_seen, last_priced=excluded.last_priced,"
            " missing_since=NULL, raw_json=excluded.raw_json",
            (provider, pid, now, now, now if priced else None,
             None, json.dumps(raw)[:6000]))
        c.commit()
    return row is None


def mark_missing(provider: str, seen: set[str], now: float) -> int:
    """Catalog rows that vanished from the live feed (kept, flagged)."""
    c = conn()
    with _lock:
        _ensure_discovery_schema(c)
        rows = c.execute("SELECT provider_model_id FROM discovered_models"
                         " WHERE provider=? AND missing_since IS NULL",
                         (provider,)).fetchall()
        gone = [r["provider_model_id"] for r in rows if r[0] not in seen]
        for pid in gone:
            c.execute("UPDATE discovered_models SET missing_since=?"
                      " WHERE provider=? AND provider_model_id=?",
                      (now, provider, pid))
        c.commit()
    return len(gone)


def latest_discovery_meta(provider: str) -> dict | None:
    """R12-B3: freshest catalog/pricing timestamps for a provider."""
    c = conn()
    try:
        r = c.execute(
            "SELECT MAX(last_seen) AS last_seen, MAX(last_priced) AS last_priced,"
            " MAX(first_seen) AS first_seen, COUNT(*) AS n"
            " FROM discovered_models WHERE provider=?", (provider,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None or r["n"] == 0:
        return None
    market_ts = None
    try:
        m = c.execute("SELECT MAX(fetched_at) AS f FROM market_snapshots"
                      " WHERE provider=?", (provider,)).fetchone()
        market_ts = m["f"] if m else None
    except sqlite3.OperationalError:
        pass
    return {"last_seen": r["last_seen"], "last_priced": r["last_priced"],
            "market_updated_at": market_ts, "models": r["n"]}


def list_discovered(provider: str | None = None,
                    include_missing: bool = True) -> list[dict]:
    c = conn()
    try:
        if provider:
            rows = c.execute("SELECT * FROM discovered_models WHERE provider=?"
                             " ORDER BY provider, provider_model_id",
                             (provider,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM discovered_models"
                             " ORDER BY provider, provider_model_id").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        if not include_missing and r["missing_since"]:
            continue
        d = json.loads(r["raw_json"]) if r["raw_json"] else {}
        d.update({"provider": r["provider"], "provider_model_id": r["provider_model_id"],
                  "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                  "last_priced": r["last_priced"],
                  "missing": bool(r["missing_since"])})
        out.append(d)
    return out


# ── canonical mapping (dynamic, no code changes for new models) ────────────

def builtin_canonical_map() -> dict[tuple[str, str], str]:
    """Seed from the in-code CANONICAL_MAPPING (R4 evidence-backed set)."""
    from ..policy import CANONICAL_MAPPING
    return {(m.provider, m.provider_model_id): m.canonical for m in CANONICAL_MAPPING
            if m.canonical and m.provider and m.provider_model_id}


def seed_builtin_map() -> int:
    c = conn()
    n = 0
    with _lock:
        _ensure_discovery_schema(c)
        for (p, pid), canonical in builtin_canonical_map().items():
            row = c.execute("SELECT 1 FROM canonical_map WHERE provider=? AND"
                            " provider_model_id=?", (p, pid)).fetchone()
            if row is None:
                c.execute("INSERT INTO canonical_map (provider, provider_model_id,"
                          " canonical, source, created_at) VALUES (?,?,?,?,?)",
                          (p, pid, canonical, "builtin", time.time()))
                n += 1
        c.commit()
    return n


def get_canonical_for(provider: str, pid: str) -> str | None:
    c = conn()
    try:
        row = c.execute("SELECT canonical FROM canonical_map WHERE provider=?"
                        " AND provider_model_id=?", (provider, pid)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and (row["canonical"] or "").strip():
        return row["canonical"]
    v = builtin_canonical_map().get((provider, pid))
    return v or None


def set_canonical(provider: str, pid: str, canonical: str | None,
                  source: str = "manual") -> bool:
    """Map or unmap a provider model to a canonical. canonical=None (or an
    empty/whitespace string, R12 fix) removes the mapping."""
    c = conn()
    with _lock:
        _ensure_discovery_schema(c)
        if not (canonical or "").strip():
            cur = c.execute("DELETE FROM canonical_map WHERE provider=? AND"
                            " provider_model_id=?", (provider, pid))
            c.commit()
            audit("admin-api", "canonical_map.delete", f"{provider}:{pid}", {})
            return cur.rowcount > 0
        c.execute(
            "INSERT INTO canonical_map (provider, provider_model_id, canonical,"
            " display_name, source, created_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(provider, provider_model_id) DO UPDATE SET"
            " canonical=excluded.canonical, source=excluded.source",
            (provider, pid, canonical, None, source, time.time()))
        c.commit()
    audit("admin-api", "canonical_map.set", f"{provider}:{pid}", {"canonical": canonical})
    return True


def list_canonical_map() -> list[dict]:
    c = conn()
    try:
        rows = c.execute("SELECT * FROM canonical_map ORDER BY canonical, provider").fetchall()
    except sqlite3.OperationalError:
        rows = []
    return [{"provider": r["provider"], "provider_model_id": r["provider_model_id"],
             "canonical": r["canonical"], "source": r["source"],
             "created_at": r["created_at"]} for r in rows]


# ── my-pool / per-model policy ─────────────────────────────────────────────

def get_pool_policy(canonical: str) -> dict:
    c = conn()
    try:
        r = c.execute("SELECT * FROM model_pool WHERE canonical=?", (canonical,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r is None:
        return {"canonical": canonical, "in_pool": None, "hidden": False,
                "min_discount_override": None, "max_input_price": None,
                "max_output_price": None, "preferred_providers": [],
                "banned_providers": [], "auto_routing": True,
                "lifecycle_override": None}
    return {"canonical": canonical, "in_pool": bool(r["in_pool"]),
            "hidden": bool(r["hidden"]),
            "min_discount_override": r["min_discount_override"],
            "max_input_price": r["max_input_price"],
            "max_output_price": r["max_output_price"],
            "preferred_providers": json.loads(r["preferred_providers"]),
            "banned_providers": json.loads(r["banned_providers"]),
            "auto_routing": bool(r["auto_routing"]),
            "lifecycle_override": r["lifecycle_override"],
            "updated_at": r["updated_at"]}


def set_pool_policy(canonical: str, fields: dict) -> dict:
    cur = get_pool_policy(canonical)
    # R12 fix: a partial update (e.g. only min_discount_override) on a model
    # WITHOUT an existing row must NOT silently drop it from the pool.
    # in_pool=None means "no row yet" -> preserve the implicit default (True)
    # unless the caller explicitly passes in_pool.
    if cur.get("in_pool") is None and "in_pool" not in fields:
        cur["in_pool"] = True
    cur.update({k: v for k, v in fields.items()
                if k in ("in_pool", "hidden", "min_discount_override",
                         "max_input_price", "max_output_price",
                         "preferred_providers", "banned_providers",
                         "auto_routing", "lifecycle_override", "notes")})
    c = conn()
    with _lock:
        _ensure_discovery_schema(c)
        c.execute(
            "INSERT INTO model_pool (canonical, in_pool, hidden, min_discount_override,"
            " max_input_price, max_output_price, preferred_providers, banned_providers,"
            " auto_routing, lifecycle_override, notes, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(canonical) DO UPDATE SET in_pool=excluded.in_pool,"
            " hidden=excluded.hidden, min_discount_override=excluded.min_discount_override,"
            " max_input_price=excluded.max_input_price,"
            " max_output_price=excluded.max_output_price,"
            " preferred_providers=excluded.preferred_providers,"
            " banned_providers=excluded.banned_providers,"
            " auto_routing=excluded.auto_routing,"
            " lifecycle_override=excluded.lifecycle_override, notes=excluded.notes,"
            " updated_at=excluded.updated_at",
            (canonical, 1 if cur["in_pool"] else 0, 1 if cur["hidden"] else 0,
             cur["min_discount_override"], cur["max_input_price"],
             cur["max_output_price"], json.dumps(cur["preferred_providers"]),
             json.dumps(cur["banned_providers"]), 1 if cur["auto_routing"] else 0,
             cur["lifecycle_override"], cur.get("notes"), time.time()))
        c.commit()
    audit("admin-api", "model_pool.set", canonical,
          {k: cur[k] for k in ("in_pool", "hidden", "min_discount_override")})
    return cur


def list_pool_policies() -> list[dict]:
    c = conn()
    try:
        rows = c.execute("SELECT canonical FROM model_pool").fetchall()
    except sqlite3.OperationalError:
        rows = []
    return [get_pool_policy(r["canonical"]) for r in rows]


# ── R12: price history + golden baselines ─────────────────────────────────

R12_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_model_id TEXT NOT NULL,
    canonical TEXT,
    ts REAL NOT NULL,
    best_input REAL, best_output REAL,
    official_input REAL, official_output REAL,
    discount_pct REAL,
    offers_count INTEGER, sellers_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ph_route_ts ON price_history (provider, provider_model_id, ts);
CREATE INDEX IF NOT EXISTS idx_ph_canon_ts ON price_history (canonical, ts);
CREATE TABLE IF NOT EXISTS baselines (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- FACTORY_DEFAULT | KNOWN_GOOD
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    description TEXT
);
"""

_PRICE_HISTORY_PRUNE_S = 35 * 86400.0  # keep ~30d aggregates + margin


def _ensure_r12_schema(c) -> None:
    c.executescript(R12_SCHEMA)
    c.commit()


def record_price_history(rows: list[dict]) -> int:
    """Append price snapshots (one per route per discovery refresh)."""
    if not rows:
        return 0
    c = conn()
    with _lock:
        _ensure_r12_schema(c)
        c.executemany(
            "INSERT INTO price_history (provider, provider_model_id, canonical, ts,"
            " best_input, best_output, official_input, official_output, discount_pct,"
            " offers_count, sellers_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(r.get("provider"), r.get("provider_model_id"), r.get("canonical"),
              r.get("ts"), r.get("best_input"), r.get("best_output"),
              r.get("official_input"), r.get("official_output"),
              r.get("discount_pct"), r.get("offers_count"), r.get("sellers_count"))
             for r in rows])
        c.execute("DELETE FROM price_history WHERE ts < ?", (time.time() - _PRICE_HISTORY_PRUNE_S,))
        c.commit()
    return len(rows)


def price_history(canonical: str | None = None, provider: str | None = None,
                  pid: str | None = None, since: float | None = None) -> list[dict]:
    c = conn()
    try:
        q = "SELECT * FROM price_history WHERE 1=1"
        args: list = []
        if canonical:
            q += " AND canonical=?"; args.append(canonical)
        if provider:
            q += " AND provider=?"; args.append(provider)
        if pid:
            q += " AND provider_model_id=?"; args.append(pid)
        if since is not None:
            q += " AND ts>=?"; args.append(since)
        q += " ORDER BY ts DESC LIMIT 5000"
        rows = c.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{k: r[k] for k in r.keys()} for r in rows]


def _agg(rows: list[dict], field: str) -> dict | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    if not vals:
        return None
    return {"min": round(min(vals), 6), "avg": round(sum(vals) / len(vals), 6),
            "max": round(max(vals), 6), "samples": len(vals)}


def price_history_aggregates(canonical: str) -> dict:
    """Current / 1h / 24h / 7d / 30d min-avg-max for best price & discount."""
    now = time.time()
    all_rows = price_history(canonical=canonical, since=now - 31 * 86400)
    windows = {"now": (0, 900), "h1": (900, 3600), "d1": (3600, 86400),
               "d7": (86400, 7 * 86400), "d30": (7 * 86400, 31 * 86400)}
    out: dict = {"canonical": canonical, "points": len(all_rows)}
    for name, (lo, hi) in windows.items():
        wr = [r for r in all_rows if lo <= now - r["ts"] < hi]
        out[name] = {
            "best_input": _agg(wr, "best_input"),
            "best_output": _agg(wr, "best_output"),
            "discount_pct": _agg(wr, "discount_pct"),
            "offers": _agg(wr, "offers_count"),
            "ts_range": [min((r["ts"] for r in wr), default=None),
                         max((r["ts"] for r in wr), default=None)],
        }
    latest = all_rows[0] if all_rows else None
    out["latest"] = {k: latest.get(k) for k in
                     ("ts", "best_input", "best_output", "discount_pct",
                      "offers_count", "sellers_count", "provider")} if latest else None
    return out


def _canonical_json_hash(obj) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def create_baseline(kind: str, description: str, payload: dict,
                    source_commit: str | None = None) -> dict:
    assert kind in ("FACTORY_DEFAULT", "KNOWN_GOOD")
    c = conn()
    bid = f"base-{kind.lower().replace('_', '')}-{uuid.uuid4().hex[:8]}"
    if source_commit:
        payload = {"meta": {"source_commit": source_commit,
                            "created_with": "model-router R13"},
                   **payload}
    row = {"id": bid, "kind": kind, "payload": payload,
           "content_hash": _canonical_json_hash(payload),
           "schema_version": CURRENT_SCHEMA_VERSION,
           "created_at": time.time(), "description": description}
    with _lock:
        _ensure_r12_schema(c)
        # single FACTORY_DEFAULT / latest KNOWN_GOOD semantics: keep all rows
        c.execute(
            "INSERT INTO baselines (id, kind, payload_json, content_hash,"
            " schema_version, created_at, description) VALUES (?,?,?,?,?,?,?)",
            (row["id"], kind, json.dumps(payload), row["content_hash"],
             row["schema_version"], row["created_at"], description))
        c.commit()
    audit("admin-api", "baseline.create", bid, {"kind": kind, "hash": row["content_hash"][:16]})
    return row


def get_baseline(baseline_id: str) -> dict | None:
    c = conn()
    try:
        r = c.execute("SELECT * FROM baselines WHERE id=?", (baseline_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None:
        return None
    return {"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload_json"]),
            "content_hash": r["content_hash"], "schema_version": r["schema_version"],
            "created_at": r["created_at"], "description": r["description"]}


def list_baselines(kind: str | None = None) -> list[dict]:
    c = conn()
    try:
        if kind:
            rows = c.execute("SELECT * FROM baselines WHERE kind=? ORDER BY created_at DESC",
                             (kind,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM baselines ORDER BY created_at DESC").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"id": r["id"], "kind": r["kind"], "content_hash": r["content_hash"],
             "schema_version": r["schema_version"], "created_at": r["created_at"],
             "description": r["description"],
             "payload_keys": sorted((json.loads(r["payload_json"]) or {}).keys())}
            for r in rows]


# ── R14: routing decision journal ─────────────────────────────────────────

def insert_decision(row: dict) -> int | None:
    """Append one privacy-safe decision record. Never raises for bad rows."""
    c = conn()
    try:
        with _lock:
            cur = c.execute(
                "INSERT INTO decision_log (ts, instance, task_class, tier, canonical,"
                " provider, provider_model_id, plan_len, reason, trace_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row.get("ts") or time.time(), row.get("instance") or "prod",
                 row.get("task_class"), row.get("tier"), row.get("canonical"),
                 row.get("provider"), row.get("provider_model_id"),
                 int(row.get("plan_len") or 0), row.get("reason") or "",
                 json.dumps(row.get("trace") or {}, ensure_ascii=False)[:20000]))
            c.commit()
            return cur.lastrowid
    except sqlite3.OperationalError:
        return None


def list_decisions(limit: int = 200, hours: float | None = None) -> list[dict]:
    c = conn()
    try:
        q = "SELECT * FROM decision_log"
        args: list = []
        if hours:
            q += " WHERE ts >= ?"
            args.append(time.time() - hours * 3600.0)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = c.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["trace"] = json.loads(d.pop("trace_json") or "{}")
        except Exception:
            d["trace"] = {}
        out.append(d)
    return out


def get_decision(decision_id: int) -> dict | None:
    c = conn()
    try:
        r = c.execute("SELECT * FROM decision_log WHERE id=?", (int(decision_id),)).fetchone()
    except (sqlite3.OperationalError, ValueError, TypeError):
        return None
    if r is None:
        return None
    d = dict(r)
    try:
        d["trace"] = json.loads(d.pop("trace_json") or "{}")
    except Exception:
        d["trace"] = {}
    return d


def decisions_count(hours: float | None = None) -> int:
    c = conn()
    try:
        if hours:
            r = c.execute("SELECT COUNT(*) FROM decision_log WHERE ts >= ?",
                          (time.time() - hours * 3600.0,)).fetchone()
        else:
            r = c.execute("SELECT COUNT(*) FROM decision_log").fetchone()
        return int(r[0]) if r else 0
    except sqlite3.OperationalError:
        return 0


# ── R15: system events / fingerprints / acks / spend / candidates ──────────

def insert_system_event(row: dict) -> int | None:
    """§11: system events (anomalies, alerts, refresh results). Never raises."""
    c = conn()
    try:
        with _lock:
            cur = c.execute(
                "INSERT INTO system_events (ts, kind, severity, object, reason,"
                " recommended, fields_json) VALUES (?,?,?,?,?,?,?)",
                (row.get("ts") or time.time(), row.get("kind") or "system",
                 row.get("severity") or "info", row.get("object") or "",
                 row.get("reason") or "", row.get("recommended") or "",
                 json.dumps(row.get("fields") or {}, ensure_ascii=False)[:8000]))
            c.commit()
            return cur.lastrowid
    except sqlite3.Error:
        return None


def list_system_events(limit: int = 100, kind: str | None = None,
                       severity: str | None = None, hours: float | None = None) -> list[dict]:
    c = conn()
    try:
        q = "SELECT * FROM system_events WHERE 1=1"
        args: list = []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if severity:
            q += " AND severity=?"; args.append(severity)
        if hours:
            q += " AND ts >= ?"; args.append(time.time() - hours * 3600.0)
        q += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
        rows = c.execute(q, args).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["fields"] = json.loads(d.pop("fields_json") or "{}")
        except Exception:
            d["fields"] = {}
        out.append(d)
    return out


def insert_fingerprint_alert(provider: str, fingerprint: str, prev: str | None,
                             reason: str, severity: str = "warn") -> int | None:
    c = conn()
    try:
        with _lock:
            cur = c.execute(
                "INSERT INTO fingerprint_alerts (ts, provider, fingerprint,"
                " prev_fingerprint, reason, severity) VALUES (?,?,?,?,?,?)",
                (time.time(), provider, fingerprint, prev, reason, severity))
            c.commit()
            return cur.lastrowid
    except sqlite3.Error:
        return None


def list_fingerprint_alerts(limit: int = 50) -> list[dict]:
    c = conn()
    try:
        rows = c.execute("SELECT * FROM fingerprint_alerts ORDER BY id DESC LIMIT ?",
                         (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def ack_alert(alert_key: str, actor: str = "admin") -> bool:
    c = conn()
    try:
        with _lock:
            c.execute("INSERT OR REPLACE INTO alert_acks (alert_key, acked_at, acked_by)"
                      " VALUES (?,?,?)", (alert_key, time.time(), actor))
            c.commit()
        return True
    except sqlite3.Error:
        return False


def list_alert_acks() -> list[dict]:
    c = conn()
    try:
        rows = c.execute("SELECT * FROM alert_acks").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def pool_canonicals() -> list[str]:
    c = conn()
    try:
        rows = c.execute("SELECT DISTINCT canonical FROM model_pool"
                         " WHERE in_pool=1 AND canonical != ''").fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def spend_totals() -> dict:
    """Day/month spend buckets. Runtime billing observer appends to the
    spend ledger (kv-free table); absent evidence = 0, never fabricated."""
    c = conn()
    out = {"today_usd": 0.0, "month_usd": 0.0}
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS spend_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            usd REAL NOT NULL,
            route TEXT
        )""")
        c.commit()
        now = time.time()
        day_start = now - (now % 86400)
        lt = time.localtime(now)
        month_start = now - ((lt.tm_mday - 1) * 86400 + (now % 86400))
        t = c.execute("SELECT COALESCE(SUM(usd),0) FROM spend_ledger WHERE ts>=?",
                      (day_start,)).fetchone()[0]
        m = c.execute("SELECT COALESCE(SUM(usd),0) FROM spend_ledger WHERE ts>=?",
                      (month_start,)).fetchone()[0]
        out = {"today_usd": float(t or 0), "month_usd": float(m or 0)}
    except sqlite3.Error:
        pass
    return out


def append_spend(usd: float, route: str = "") -> None:
    """Best-effort spend ledger append (runtime billing observer)."""
    if usd is None or usd < 0:
        return
    c = conn()
    try:
        with _lock:
            c.execute("CREATE TABLE IF NOT EXISTS spend_ledger ("
                      " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                      " ts REAL NOT NULL, usd REAL NOT NULL, route TEXT)")
            c.execute("INSERT INTO spend_ledger (ts, usd, route) VALUES (?,?,?)",
                      (time.time(), float(usd), route))
            c.execute("DELETE FROM spend_ledger WHERE ts < ?",
                      (time.time() - 45 * 86400,))
            c.commit()
    except sqlite3.Error:
        pass


def list_candidates() -> list[dict]:
    """Shadow-candidate models (pool row with candidate marker). Joins the
    canonical quality profile + best market price for dry-run evaluation."""
    c = conn()
    out = []
    try:
        rows = c.execute("SELECT canonical FROM model_pool"
                         " WHERE lifecycle_override='CANDIDATE' AND canonical != ''").fetchall()
    except sqlite3.Error:
        return []
    canon_ids = [r[0] for r in rows]
    for canon in canon_ids:
        prof = {}
        try:
            from gateway.quality import QualityRegistry
            _qr = QualityRegistry()
            _qr.load()
            qp = _qr.profiles().get(canon)
            if qp is not None:
                prof = {"quality_score": qp.quality_score,
                        "confidence": qp.confidence}
        except Exception:
            prof = {}
        best_in = None
        try:
            hist = price_history(canonical=canon, since=time.time() - 3600)
            prices = [h.get("best_input") for h in hist if h.get("best_input")]
            if prices:
                best_in = min(prices)
        except Exception:
            pass
        out.append({"canonical": canon,
                    "display_name": canon,
                    "profile": prof,
                    "best_input": best_in})
    return out


def set_candidate(canonical: str, candidate: bool) -> bool:
    """§6: mark a pool model as a shadow candidate (lifecycle marker,
    NOT a routing change — candidate rows stay out of auto-routing)."""
    c = conn()
    try:
        with _lock:
            c.execute(
                "UPDATE model_pool SET lifecycle_override=?, updated_at=?"
                " WHERE canonical=?",
                ("CANDIDATE" if candidate else None, time.time(), canonical))
            c.commit()
        return True
    except sqlite3.Error:
        return False


# ── R14 §33: primary known-good pointer ───────────────────────────────────

def get_known_good() -> dict | None:
    c = conn()
    try:
        r = c.execute("SELECT baseline_id, set_at, set_by FROM known_good_pointer"
                      " WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return {"baseline_id": r["baseline_id"], "set_at": r["set_at"], "set_by": r["set_by"]} if r else None


def set_known_good(baseline_id: str, actor: str = "admin-api") -> None:
    c = conn()
    with _lock:
        c.execute("INSERT OR REPLACE INTO known_good_pointer (id, baseline_id, set_at, set_by)"
                  " VALUES (1, ?, ?, ?)", (baseline_id, time.time(), actor))
        c.commit()
    audit(actor, "baseline.known_good", baseline_id, {})


def delete_baseline(baseline_id: str, actor: str = "admin-api") -> bool:
    """Delete a user recovery point. FACTORY_DEFAULT and the active primary
    known-good are protected and can never be deleted."""
    row = get_baseline(baseline_id)
    if row is None:
        return False
    if row["kind"] == "FACTORY_DEFAULT":
        raise ValueError("factory default cannot be deleted")
    kg = get_known_good()
    if kg and kg["baseline_id"] == baseline_id:
        raise ValueError("active primary known-good cannot be deleted")
    c = conn()
    with _lock:
        c.execute("DELETE FROM baselines WHERE id = ?", (baseline_id,))
        c.commit()
    audit(actor, "baseline.delete", baseline_id, {"kind": row["kind"]})
    return True


# ── R14 §33/§34: primary known-good pointer + baseline deletion ───────────

_KV_TABLE = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def get_kv(key: str) -> str | None:
    c = conn()
    with _lock:
        _KV_TABLE and c.executescript(_KV_TABLE)
        try:
            r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return r["v"] if r else None


def set_kv(key: str, value: str) -> None:
    c = conn()
    with _lock:
        c.executescript(_KV_TABLE)
        c.execute("INSERT INTO kv (k, v) VALUES(?,?)"
                  " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
        c.commit()


def primary_baseline_id() -> str | None:
    return get_kv("primary_baseline_id")


def set_primary_baseline(baseline_id: str) -> None:
    set_kv("primary_baseline_id", baseline_id)


# delete_baseline(actor=...) is defined above (known-good aware).


# ── market snapshots (persisted /api/markets) ──────────────────────────────

def save_market_snapshot(provider: str, data: dict) -> None:
    c = conn()
    with _lock:
        _ensure_discovery_schema(c)
        c.execute("INSERT INTO market_snapshots (provider, fetched_at, data_json)"
                  " VALUES (?,?,?)",
                  (provider, time.time(), json.dumps(data)[:2_000_000]))
        c.execute("DELETE FROM market_snapshots WHERE provider=? AND fetched_at <"
                  " (SELECT MAX(fetched_at) FROM market_snapshots WHERE provider=?)",
                  (provider, provider))
        c.commit()


def load_market_snapshot(provider: str) -> dict | None:
    c = conn()
    try:
        r = c.execute("SELECT data_json FROM market_snapshots WHERE provider=?"
                      " ORDER BY fetched_at DESC LIMIT 1", (provider,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(r["data_json"]) if r else None
