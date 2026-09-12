"""Config revisioning (spec R6 §D/§J): DRAFT → VALIDATE → SIMULATE → APPLY →
ROLLBACK with atomic runtime apply and impact preview.

Config layers (R6 §C):
  config/defaults.yaml   — immutable defaults (validation contract + defaults)
  control.db config_active — persistent mutable policy (revisioned)
  EnvironmentFile (.env.gateway) — secrets only
  state/gateway-v2.json  — generated runtime registry snapshot (written on apply)

Apply is atomic: sqlite commit + tmp-file rename of the runtime snapshot +
in-place hot mutation of the running GatewayConfig object. An invalid config
never replaces the active one.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from pathlib import Path

from . import store

ROUTER_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULTS_PATH = ROUTER_ROOT / "config" / "defaults.yaml"
RUNTIME_CONFIG_PATH = os.environ.get(
    "GW_CONFIG", str(ROUTER_ROOT / "state" / "gateway-v2.json"))

# Policy key contract: key -> (type, min, max). Keys outside this set are
# rejected at VALIDATE — no free-form config reaches the runtime.
POLICY_CONTRACT: dict[str, tuple[type, float | None, float | None]] = {
    "min_discount": (float, 0.0, 1.0),
    "quality_first": (bool, None, None),
    "health_ttl_success_s": (float, 1.0, 86400.0),
    "health_ttl_failure_s": (float, 1.0, 86400.0),
    "cache_switch_horizon": (float, 0.0, 100.0),
    "cache_switch_margin": (float, 1.0, 10.0),
    "reliability_min_samples": (int, 1, 10000),
    "price_evidence_ttl_s": (float, 1.0, 30 * 86400.0),
    "cache_affinity_ttl_s": (float, 1.0, 86400.0),
    "canonical_switch_margin_factor": (float, 1.0, 10.0),
    "free_route_min_success_rate": (float, 0.0, 1.0),
    "quality_floors": (dict, None, None),
    # R14 §17: taCHANGE_ME → tier policy {CLASS: {enabled, tier}}
    "task_classes": (dict, None, None),
    # R15: production guards + observability (all warning-only defaults)
    "observability": (dict, None, None),
}
_PROVIDER_KEYS = {"enabled": bool, "min_discount": float}

# Runtime applier hook — registered by gateway/app.py at startup. Control
# plane never imports the inference app (dependency direction: app -> control).
_runtime_appliers: list = []


def register_runtime_applier(fn) -> None:
    """fn(config_dict) — hot-apply policy to the running registry/selector."""
    _runtime_appliers.append(fn)


def _load_defaults_yaml() -> dict:
    try:
        import yaml
        return yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def defaults() -> dict:
    return _load_defaults_yaml().get("policy", {})


def active_config() -> dict:
    cfg, _ = store.get_active_config()
    if cfg:
        return cfg
    d = defaults()
    return copy.deepcopy(d)


def diff_configs(old: dict, new: dict) -> dict:
    d: dict = {}
    for k in sorted(set(old) | set(new)):
        ov, nv = old.get(k, "<absent>"), new.get(k, "<absent>")
        if ov != nv:
            d[k] = {"old": ov, "new": nv}
    return d


def validate_config(config: dict) -> list[str]:
    """Returns a list of validation errors. Empty = valid."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["config must be an object"]
    for key, value in config.items():
        if key == "providers":
            if not isinstance(value, dict):
                errors.append("providers must be an object")
                continue
            for pname, pc in value.items():
                if not isinstance(pc, dict):
                    errors.append(f"providers.{pname} must be an object")
                    continue
                for pk, pv in pc.items():
                    if pk not in _PROVIDER_KEYS:
                        errors.append(f"providers.{pname}.{pk}: unknown key")
                    elif not isinstance(pv, _PROVIDER_KEYS[pk]):
                        errors.append(f"providers.{pname}.{pk}: wrong type")
                md = pc.get("min_discount")
                if md is not None and not (0.0 <= float(md) <= 1.0):
                    errors.append(f"providers.{pname}.min_discount out of range")
            continue
        if key not in POLICY_CONTRACT:
            errors.append(f"{key}: unknown policy key")
            continue
        typ, lo, hi = POLICY_CONTRACT[key]
        if typ is bool:
            if not isinstance(value, bool):
                errors.append(f"{key}: must be boolean")
            continue
        if typ is dict:
            if not isinstance(value, dict):
                errors.append(f"{key}: must be object")
                continue
            if key == "quality_floors":
                for t, v in value.items():
                    if t not in {"T1", "T2", "T3", "T4"}:
                        errors.append(f"quality_floors.{t}: unknown tier")
                    elif not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                        errors.append(f"quality_floors.{t}: must be 0..1")
            if key == "task_classes":
                from ..classifier import CLASSES
                for cls, tc in value.items():
                    if cls not in CLASSES:
                        errors.append(f"task_classes.{cls}: unknown class")
                    elif not isinstance(tc, dict):
                        errors.append(f"task_classes.{cls}: must be object")
                    else:
                        t = tc.get("tier")
                        if t is not None and t not in {"T1", "T2", "T3", "T4"}:
                            errors.append(f"task_classes.{cls}.tier: must be T1..T4")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{key}: must be number")
            continue
        value = float(value)
        if lo is not None and value < lo or hi is not None and value > hi:
            errors.append(f"{key}: out of range [{lo}, {hi}]")
    # Hard safety: min_discount can never go below the certified floor 0.80
    # without being an explicit, non-accidental choice (warned at simulate).
    return errors


# ── impact preview (R6 §J) ─────────────────────────────────────────────────

def _floor_for(cfg: dict, provider: str) -> float:
    pc = (cfg.get("providers") or {}).get(provider) or {}
    if not pc.get("enabled", True):
        return 2.0  # disabled provider: no route can pass a 2.0 floor
    md = pc.get("min_discount")
    if md is None:
        md = cfg.get("min_discount", 0.80)
    return float(md)


def simulate_impact(candidate: dict) -> dict:
    """Impact preview against the LIVE registry snapshot (R6 §J).

    Shows eligible routes before/after, canonical availability, affected CORE
    models and warnings. No prompts, no upstream calls.
    """
    from ..registry import RouteRegistry  # runtime import, read-only use
    from ..app import _registry
    from ..app import _pool

    before_cfg = active_config()
    after_cfg = copy.deepcopy(before_cfg)
    after_cfg.update(copy.deepcopy(candidate))

    routes = _registry.all()
    before_routes: list[dict] = []
    after_routes: list[dict] = []
    for r in routes:
        b_dis = bool((_before_provider_enabled(before_cfg, r.provider)))
        a_dis = bool((_before_provider_enabled(after_cfg, r.provider)))
        b_floor = _floor_for(before_cfg, r.provider)
        a_floor = _floor_for(after_cfg, r.provider)
        b_ok = b_dis and r.discount is not None and r.discount >= b_floor
        a_ok = a_dis and r.discount is not None and r.discount >= a_floor
        if b_ok:
            before_routes.append({"route": f"{r.provider}:{r.provider_model_id}",
                                  "canonical": r.canonical})
        if a_ok:
            after_routes.append({"route": f"{r.provider}:{r.provider_model_id}",
                                 "canonical": r.canonical})

    before_canon = sorted({x["canonical"] for x in before_routes})
    after_canon = sorted({x["canonical"] for x in after_routes})
    lost = sorted(set(before_canon) - set(after_canon))

    warnings: list[str] = []
    for k, dv in diff_configs(before_cfg, after_cfg).items():
        if k == "min_discount" and float(dv["new"]) < 0.80:
            warnings.append(f"min_discount lowered below certified floor 0.80 ({dv['old']} -> {dv['new']})")
    for canonical in lost:
        warnings.append(f"canonical {canonical} loses ALL eligible routes")
    pool = _pool.last() or {}
    core = [c for c, v in (pool.get("models") or {}).items()
            if v.get("lifecycle") == "CORE"]
    affected_core = sorted(set(core) & (set(lost) | {c for c in after_canon} | set(before_canon))
                           & set(lost))
    for c in affected_core:
        warnings.append(f"CORE model {c} would have no eligible route")

    return {
        "before": {"eligible_routes": len(before_routes), "routes": before_routes,
                   "available_canonicals": before_canon},
        "after": {"eligible_routes": len(after_routes), "routes": after_routes,
                  "available_canonicals": after_canon},
        "affected_core_models": affected_core,
        "warnings": warnings,
        "diff": diff_configs(before_cfg, after_cfg),
        "blockers": [w for w in warnings if "CORE model" in w or "loses ALL eligible" in w],
    }


def _before_provider_enabled(cfg: dict, provider: str) -> bool:
    return bool(((cfg.get("providers") or {}).get(provider) or {}).get("enabled", True))


# ── revision lifecycle ─────────────────────────────────────────────────────

def create_draft(config: dict, actor: str, reason: str) -> dict:
    base = active_config()
    merged = copy.deepcopy(base)
    merged.update(copy.deepcopy(config))
    rev = {
        "revision_id": store.new_id("rev"),
        "created_at": time.time(),
        "actor": actor,
        "reason": reason,
        "diff": diff_configs(base, merged),
        "config": merged,
        "previous_revision": store.get_active_config()[1],
        "status": "DRAFT",
        "warnings": [],
    }
    store.insert_revision(rev)
    store.audit(actor, "revision.create", rev["revision_id"], {"reason": reason})
    return rev


def validate_revision(revision_id: str) -> dict:
    rev = store.get_revision(revision_id)
    if rev is None:
        raise KeyError(revision_id)
    errors = validate_config(rev["config"])
    status = "VALIDATED" if not errors else "INVALID"
    store.update_revision_status(revision_id, status, rev.get("warnings"))
    store.audit("admin-api", "revision.validate", revision_id,
                {"status": status, "errors": errors})
    rev["status"] = status
    rev["errors"] = errors
    return rev


def simulate_revision(revision_id: str) -> dict:
    rev = store.get_revision(revision_id)
    if rev is None:
        raise KeyError(revision_id)
    impact = simulate_impact(_delta_of(rev))
    store.update_revision_status(revision_id, rev["status"] if rev["status"] != "DRAFT" else "SIMULATED",
                                 impact["warnings"])
    rev["impact"] = impact
    return rev


def _delta_of(rev: dict) -> dict:
    """Candidate delta = rev.config over the *current* active base."""
    base = active_config()
    delta = {}
    for k, v in rev["config"].items():
        if k not in base or base[k] != v:
            delta[k] = v
    return delta


def apply_revision(revision_id: str, actor: str = "admin-api",
                   force: bool = False) -> dict:
    """Atomic apply: validate -> (simulate if dirty) -> gate -> commit."""
    rev = store.get_revision(revision_id)
    if rev is None:
        raise KeyError(revision_id)
    if rev["status"] in ("APPLIED",):
        return {**rev, "already_applied": True}
    errors = validate_config(rev["config"])
    if errors:
        store.update_revision_status(revision_id, "INVALID", rev.get("warnings"))
        return {**rev, "status": "INVALID", "errors": errors, "applied": False}

    delta = _delta_of(rev)
    impact = simulate_impact(delta)
    if impact["blockers"] and not force:
        return {**rev, "applied": False, "blocked": True, "impact": impact,
                "message": "blocked by impact preview; re-apply with force=true to override"}

    previous_rev = store.get_active_config()[1]
    prev_cfg = active_config()
    # 1) persist active config in control.db (transactional source of truth)
    store.set_active_config(rev["config"], revision_id)
    # 2) atomic file write of the generated runtime snapshot; on failure the
    # DB pointer rolls back (first apply rolls back to EMPTY = defaults only).
    try:
        _atomic_write_runtime(rev["config"])
    except Exception:
        if previous_rev:
            prev = store.get_revision(previous_rev)
            if prev:
                store.set_active_config(prev["config"], previous_rev)
        else:
            # no previous revision: revert to empty (defaults-only) state
            from . import store as _s
            c = _s.conn()
            with _s._lock:
                c.execute("DELETE FROM config_active WHERE id=1")
                c.commit()
        raise
    # 3) hot-apply to the running registry/selector (in-place, no restart)
    applied: list[str] = []
    for fn in _runtime_appliers:
        try:
            fn(rev["config"])
            applied.append(getattr(fn, "__name__", "applier"))
        except Exception as e:  # hot-apply failure must not corrupt state
            store.audit(actor, "revision.apply_partial", revision_id,
                        {"applier_error": repr(e), "applied": applied})
    store.update_revision_status(revision_id, "APPLIED", impact["warnings"],
                                 applied_at=time.time())
    store.audit(actor, "revision.apply", revision_id,
                {"diff": rev["diff"], "warnings": impact["warnings"]})
    # refresh so callers see the post-apply status (APPLIED), not the stale
    # pre-apply row (UI showed VALIDATED after a successful apply)
    rev = store.get_revision(revision_id) or rev
    return {**rev, "applied": True, "impact": impact, "hot_applied_by": applied}


def rollback(target_revision_id: str, actor: str = "admin-api",
             reason: str = "rollback") -> dict:
    rev = store.get_revision(target_revision_id)
    if rev is None or rev["status"] != "APPLIED":
        raise ValueError("rollback target must be an APPLIED revision")
    # R6 contract: rollback to revision X re-applies X's config (restores the
    # state as it was right after X was applied).
    base = active_config()
    new_rev = {
        "revision_id": store.new_id("rev"),
        "created_at": time.time(),
        "actor": actor,
        "reason": f"rollback:{target_revision_id}:{reason}",
        "diff": diff_configs(base, rev["config"]),
        "config": rev["config"],
        "previous_revision": store.get_active_config()[1],
        "status": "DRAFT",
        "warnings": [],
    }
    store.insert_revision(new_rev)
    return apply_revision(new_rev["revision_id"], actor=actor, force=True)


def _atomic_write_runtime(config: dict) -> None:
    path = Path(RUNTIME_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gw-config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
