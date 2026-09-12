"""Gateway V2 integration — bridges control plane to the inference runtime.

Registered from gateway/app.py at startup:
  - runtime applier: hot-apply mutable policy onto the running GatewayConfig
    (in-place — selector/registry hold the same instance), without touching
    active sessions' cache state (R6 §Q: policy refresh never invalidates
    a warm route; only hard DISABLE/hard gates do, which already bypass
    cache affinity in selector._apply_cache_affinity).
  - selector hooks: apply lifecycle overrides (model_policy) and routing
    overrides (FORCE_*/DISABLE_*/PREFER_PROVIDER) inside the selection path.
"""
from __future__ import annotations

import time

from ..config import GatewayConfig
from . import store, overrides as ovr, revisions as rev


def cleanup_expired_overrides() -> int:
    return ovr.cleanup_expired_overrides()


def runtime_applier(config: dict) -> None:
    """Hot-apply mutable policy IN PLACE to the running GatewayConfig."""
    from ..app import _registry
    cfg = _registry.config()
    assert isinstance(cfg, GatewayConfig)
    cfg.apply_fields(config)
    # Do NOT invalidate session cache on policy refresh (R6 §Q). The Context
    # Manager keeps selected_route_key per session; only hard gates change it.


# ── selector hooks ─────────────────────────────────────────────────────────

def apply_overrides_to_context(ctx) -> dict:
    """Apply routing overrides to a SelectionContext before choose().
    Returns the applied override summary for the decision trace."""
    o = ovr.resolve()
    summary = {"overrides": []}
    if o["force_canonical"]:
        ctx = type(ctx)(**{**ctx.__dict__, "canonical_hint": o["force_canonical"]})
        summary["overrides"].append({"kind": "FORCE_CANONICAL", "target": o["force_canonical"]})
    if o["force_route"]:
        provider, _, slug = str(o["force_route"]).partition(":")
        ctx = type(ctx)(**{**ctx.__dict__, "forced_route": o["force_route"]})
        summary["overrides"].append({"kind": "FORCE_ROUTE", "target": o["force_route"]})
    if o["prefer_provider"]:
        if not getattr(ctx, "provider_hint", None):
            ctx = type(ctx)(**{**ctx.__dict__, "provider_hint": o["prefer_provider"]})
        summary["overrides"].append({"kind": "PREFER_PROVIDER", "target": o["prefer_provider"]})
    excl = frozenset(o["excluded_route_keys"]) if o["excluded_route_keys"] else frozenset()
    if excl or o["excluded_providers"] or o["excluded_canonicals"]:
        cur_excl = frozenset(ctx.exclude_route_keys) if ctx.exclude_route_keys else frozenset()
        ctx = type(ctx)(**{**ctx.__dict__, "exclude_route_keys": cur_excl | excl})
        if o["excluded_providers"]:
            summary["overrides"].append({"kind": "DISABLE_PROVIDER", "target": ",".join(sorted(o["excluded_providers"]))})
        if o["excluded_canonicals"]:
            summary["overrides"].append({"kind": "DISABLE_MODEL", "target": ",".join(sorted(o["excluded_canonicals"]))})
        if excl:
            summary["overrides"].append({"kind": "DISABLE_ROUTE", "target": ",".join(sorted(excl))})
    return ctx, summary


def append_spend(cost_usd: float, route: str) -> None:
    """R15 §3: real-cost ledger for budget tracking. Best-effort."""
    try:
        from . import store
        store.append_spend(float(cost_usd), route)
    except Exception:
        pass


def apply_canary(ctx, task_class: str | None) -> dict:
    """R15 §7: optional canary experiment — probabilistic canonical hint.

    A SEPARATE mechanism: it never edits the routing policy, tiers, or the
    pool. Disabled unless kv "canary" has enabled=true. Matches by task
    class (empty list = all classes), traffic percentage, and duration.
    Returns {"canonical_hint": <model>} when the canary fires, {} otherwise."""
    import json as _json
    import random
    try:
        raw = store.get_kv("canary")
        if not raw:
            return {}
        exp = _json.loads(raw)
        if not exp.get("enabled"):
            return {}
        started = float(exp.get("started_at") or 0)
        dur = float(exp.get("duration_s") or 3600)
        if time.time() - started > dur:
            return {"expired": True}
        classes = exp.get("task_classes") or []
        if classes and task_class and task_class not in classes:
            return {}
        pct = float(exp.get("traffic_pct") or 0)
        if pct <= 0 or random.random() * 100.0 >= pct:
            return {}
        return {"canonical_hint": exp.get("model"), "model": exp.get("model")}
    except Exception:
        return {}


def filter_routes_by_model_policy(routes: list, model_policy_fn) -> list:
    """Drop routes whose canonical is admin-restricted to specific providers
    (R6 §G provider restriction). model_policy_fn(canonical) -> dict.

    R15 §6: canonicals with lifecycle_override='CANDIDATE' are shadow
    candidates — they NEVER receive production inference. Evaluation happens
    in the control plane (observability.shadow_candidates) over decision
    records only."""
    out = []
    for r in routes:
        pol = model_policy_fn(r.canonical)
        if pol.get("lifecycle_override") == "CANDIDATE":
            continue
        restricted = pol.get("restricted_providers") or []
        if restricted and r.provider not in restricted:
            continue
        out.append(r)
    return out


def override_lifecycle_status(canonical: str, base_status: str) -> str:
    """Admin lifecycle_override beats automatic lifecycle (R6 §G manual
    override). Unknown canonicals keep their automatic status."""
    try:
        pol = store.get_model_policy(canonical)
    except Exception:
        return base_status
    lo = pol.get("lifecycle_override")
    return lo if lo else base_status


def install() -> None:
    """Idempotent install called from gateway/app.py _build_app()."""
    rev.register_runtime_applier(runtime_applier)


# R6 §G: enable/disable at the canonical level is expressed through the
# existing LifecycleRegistry — model_policy lifecycle_override is applied on
# every _plan_for_canonical read via override_lifecycle_status.
def get_admin_lifecycle_status(lifecycle_registry, canonical: str) -> str:
    auto = lifecycle_registry.status(canonical, default="WATCH")
    return override_lifecycle_status(canonical, auto)


def snapshot_for_dashboard() -> dict:
    """Privacy-safe dashboard summary (R6 §M). No prompts, no keys."""
    from ..app import _metrics
    try:
        snap = _metrics.snapshot()
    except Exception:
        snap = {}
    o = ovr.resolve()
    return {
        "metrics": snap,
        "overrides_active": [k for k in ("force_canonical", "force_route",
                                         "prefer_provider") if o.get(k)] +
                           [f"excluded_{len(o['excluded_route_keys'])}routes"
                            if o["excluded_route_keys"] else None],
        "active_revision": store.get_active_config()[1],
        "ts": time.time(),
    }
