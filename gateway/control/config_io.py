"""Config export/import (R8 §11).

Export NEVER contains secrets: provider records keep only secret_ref names
(env var NAMES, not values). Import validates first (dry_run), then applies
through the normal revision pipeline (create draft -> validate -> apply),
so imported config lands in control.db revisioned, not by direct write.
"""
from __future__ import annotations

import time
from typing import Any

from . import revisions as rev
from . import store

EXPORT_KIND = "model-router-config-export"
EXPORT_VERSION = 1

# Secret-ish fields that must never leave the control DB in export.
_FORBIDDEN_KEYS = {"api_key", "token", "secret", "password", "key_env_value"}


def _scrub(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: ("***REDACTED***" if any(s in k.lower() for s in _FORBIDDEN_KEYS) else _scrub(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_scrub(x) for x in node]
    return node


def export_config() -> dict:
    """Snapshot of persistent, non-secret control-plane config."""
    cfg, rid = store.get_active_config()
    providers = [
        dict(p) for p in store.list_providers(include_archived=False)
    ]
    policies = [
        dict(p) for p in store.list_model_policy()
    ]
    persistent_overrides = [
        dict(o) for o in store.list_overrides(active_only=True)
        if o.get("persistent")
    ]
    payload = {
        "kind": EXPORT_KIND,
        "export_version": EXPORT_VERSION,
        "exported_at": time.time(),
        "source_revision_id": rid,
        "config": _scrub(cfg or rev.active_config()),
        "providers": _scrub(providers),
        "model_policies": _scrub(policies),
        "persistent_overrides": _scrub(persistent_overrides),
    }
    return payload


def import_config(payload: dict, actor: str, *, dry_run: bool) -> dict:
    """Validate + (optionally) apply an exported config.

    dry_run=True only validates; dry_run=False creates a revision and
    applies it. Returns a report dict.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {"ok": False, "errors": ["payload must be a JSON object"], "applied": False}
    if payload.get("kind") != EXPORT_KIND:
        errors.append(f"unexpected kind {payload.get('kind')!r}")
    if int(payload.get("export_version") or 0) > EXPORT_VERSION:
        errors.append(f"export_version {payload.get('export_version')} newer than supported {EXPORT_VERSION}")
    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        errors.append("missing config object")
    else:
        errors.extend(rev.validate_config(cfg))
    if "***REDACTED***" in str(payload):
        errors.append("payload contains redacted secrets — export a fresh copy")

    if errors or dry_run:
        return {"ok": not errors, "errors": errors, "warnings": [],
                "applied": False, "dry_run": dry_run}

    created = rev.create_draft(cfg, actor=actor,
                               reason="config import")
    rid = created["revision_id"]
    val = rev.validate_revision(rid)
    if val.get("status") != "VALIDATED":
        return {"ok": False, "errors": val.get("errors", ["validation failed"]),
                "revision_id": rid, "applied": False}
    applied = rev.apply_revision(rid, actor=actor)
    ok = bool(applied.get("applied"))
    return {"ok": ok,
            "errors": applied.get("errors") or (["apply blocked"] if not ok else []),
            "warnings": applied.get("warnings", []),
            "revision_id": rid, "applied": ok, "dry_run": False}
