"""Routing overrides (spec R6 §I).

Kinds:
  FORCE_CANONICAL   target=canonical        — selection locked to one canonical
  FORCE_ROUTE       target=provider:slug    — selection locked to one route
  DISABLE_PROVIDER  target=provider         — provider routes excluded
  DISABLE_MODEL     target=canonical        — canonical routes excluded
  DISABLE_ROUTE     target=provider:slug    — single route excluded
  PREFER_PROVIDER   target=provider         — ordering preference only

Every override carries reason/actor/created_at. A temporary override MUST
carry expires_at (TTL); permanent ones require explicit persistent=true.
Resolution never raises: control-plane failure degrades to "no overrides"
(spec R6 §B — control plane failure must not break inference).
"""
from __future__ import annotations

import time

from . import store

ALLOWED_KINDS = {
    "FORCE_CANONICAL", "FORCE_ROUTE", "DISABLE_PROVIDER",
    "DISABLE_MODEL", "DISABLE_ROUTE", "PREFER_PROVIDER",
}


def create(kind: str, target: str, reason: str, actor: str,
           ttl_s: float | None = None, persistent: bool = False,
           params: dict | None = None) -> dict:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unknown override kind {kind!r}; allowed: {sorted(ALLOWED_KINDS)}")
    if not target:
        raise ValueError("target is required")
    if persistent and ttl_s is not None:
        raise ValueError("persistent override must not carry a TTL")
    if not persistent and (ttl_s is None or ttl_s <= 0):
        raise ValueError("temporary override requires ttl_s > 0 (use persistent=true for permanent)")
    ov = {
        "override_id": store.new_id("ovr"),
        "kind": kind,
        "target": target,
        "params": params or {},
        "reason": reason,
        "actor": actor,
        "created_at": time.time(),
        "expires_at": (time.time() + float(ttl_s)) if not persistent else None,
        "persistent": bool(persistent),
    }
    store.insert_override(ov)
    store.audit(actor, "override.create", f"{kind}:{target}",
                {"reason": reason, "ttl_s": ttl_s, "persistent": persistent})
    return ov


def resolve() -> dict:
    """Active overrides → a decision-ready structure. Never raises."""
    out = {
        "force_canonical": None,
        "force_route": None,
        "prefer_provider": None,
        "excluded_route_keys": set(),
        "excluded_canonicals": set(),
        "excluded_providers": set(),
    }
    try:
        ovs = store.list_overrides(active_only=True)
    except Exception:
        return out
    for ov in ovs:
        kind, target = ov["kind"], ov["target"]
        if kind == "FORCE_CANONICAL":
            out["force_canonical"] = target
        elif kind == "FORCE_ROUTE":
            out["force_route"] = target
        elif kind == "PREFER_PROVIDER":
            out["prefer_provider"] = target
        elif kind == "DISABLE_PROVIDER":
            out["excluded_providers"].add(target)
        elif kind == "DISABLE_MODEL":
            out["excluded_canonicals"].add(target)
        elif kind == "DISABLE_ROUTE":
            out["excluded_route_keys"].add(target)
    return out


def disable(override_id: str, actor: str = "admin-api") -> bool:
    ok = store.disable_override(override_id)
    if ok:
        store.audit(actor, "override.disable", override_id, {})
    return ok


def cleanup_expired_overrides() -> int:
    """Disable expired temporary overrides. Never raises."""
    n = 0
    try:
        now = time.time()
        for ov in store.list_overrides(active_only=False):
            if ov["enabled"] and ov["expires_at"] is not None and ov["expires_at"] <= now:
                if disable(ov["override_id"], actor="ttl-sweeper"):
                    n += 1
    except Exception:
        pass
    return n
