"""Admin API (spec R6 §B/§E/§G/§H/§I/§J/§K/§N) — separate FastAPI app.

Mounted as a SECOND ASGI application served on a different port (4111 by
default) by gateway/serve_control.py, or mounted under /admin via an
ASGI-sub-mount when the single-port layout is desired. It is NOT in the
inference critical path: control-plane import failure or crash must never
affect the inference app (dependency direction: serve_control -> admin_api
-> control.* -> runtime appliers registered by app.py).

Auth boundary (R6 §R): localhost-only by default; ADMIN_TOKEN env may be
required for non-loopback callers (checked here, enforced at bind).
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import APIRouter, HTTPException, Request

from . import store, overrides as ovr, revisions as rev
from .adapters import ADAPTER_TYPES
from ..metrics import Metrics

_dash_metrics: Metrics | None = None

router = APIRouter(prefix="/admin")

# Runtime router (production :4100) — source of runtime telemetry for the
# control-plane UI. Control DB (:4111) holds control-plane data only.
RUNTIME_BASE_URL = os.environ.get("GW_RUNTIME_URL", "http://127.0.0.1:4100")


async def _runtime_get(path: str, timeout_s: float = 5.0) -> dict:
    """Fetch runtime telemetry from the production router.

    Returns {"ok": bool, "status": int|None, "data": dict,
             "endpoint": str, "error": str|None} — never raises, so the UI
    can surface a readable error instead of an empty table.
    A fresh AsyncClient per call: a cached client binds to the creating
    event loop and breaks under TestClient (and any loop restart).
    """
    import httpx
    endpoint = RUNTIME_BASE_URL + path
    try:
        async with httpx.AsyncClient(base_url=RUNTIME_BASE_URL,
                                     timeout=timeout_s) as client:
            r = await client.get(path)
    except Exception as e:  # noqa: BLE001 — surfaced to the UI, not swallowed
        return {"ok": False, "status": None, "data": {},
                "endpoint": endpoint, "error": f"{type(e).__name__}: {e}"}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {"raw": r.text[:200]}
    return {"ok": r.status_code == 200, "status": r.status_code, "data": data,
            "endpoint": endpoint, "error": None if r.status_code == 200
            else f"HTTP {r.status_code}"}


def _prov_summary(v: dict) -> dict:
    n = v.get("requests", 0)
    ok = v.get("successes", 0)
    return {
        "requests": n,
        "successes": ok,
        "failures": v.get("failures", 0),
        "success_rate": (ok / n) if n else None,
        "p50_ms": v.get("total_p50_ms"),
        "p95_ms": v.get("total_p95_ms"),
        "cost_est": v.get("cost_usd"),
        "cost_actual": v.get("cost_usd"),
        "cost_per_success": v.get("cost_per_success"),
        "eligible_routes": v.get("eligible_routes"),
    }


def bind_dashboard_metrics(m) -> None:
    global _dash_metrics
    _dash_metrics = m


@router.get("/router/metrics-summary")
async def router_metrics_summary(request: Request):
    """Dashboard summary (R6 §M). Runtime telemetry comes from the
    production router (:4100); falls back to same-process metrics when the
    admin router is mounted inside the inference app. No prompts, no keys."""
    _check_auth(request)
    errors = []
    provs: dict = {}
    raw_runtime = None
    rt = await _runtime_get("/provider/share")
    if rt["ok"]:
        raw_runtime = rt["data"]
        for p, v in (raw_runtime.get("providers") or {}).items():
            if p == "none":
                # telemetry placeholder for failed attempts without a route
                # (R11 §18: never shown as a provider)
                continue
            provs[p] = _prov_summary(v)
    else:
        errors.append({"endpoint": rt["endpoint"], "status": rt["status"],
                       "error": rt["error"]})
        if _dash_metrics is not None:
            snap = _dash_metrics.snapshot()
            for p, v in (snap.get("providers") or {}).items():
                if p == "none":
                    continue
                provs[p] = _prov_summary(v)
        else:
            errors.append({"endpoint": "local-metrics", "status": None,
                           "error": "runtime router unreachable and metrics "
                                    "not bound in this process"})
    return {"providers": provs,
            "runtime_endpoint": rt["endpoint"],
            "runtime_status": rt["status"],
            "errors": errors,
            "raw": raw_runtime}


@router.get("/runtime/health")
async def runtime_health(request: Request):
    """Runtime router (:4100) health — proxied for the Dashboard."""
    _check_auth(request)
    rt = await _runtime_get("/health")
    if not rt["ok"]:
        return {"ok": False, "endpoint": rt["endpoint"],
                "status": rt["status"], "error": rt["error"]}
    return {"ok": True, "endpoint": rt["endpoint"], "status": 200,
            "data": rt["data"]}


@router.get("/models/pool")
async def models_pool(request: Request, include_routes: int = 0):
    """Model pool snapshot proxied from the runtime router (:4100) —
    canonicals, lifecycle, routes. Control plane does not own this state."""
    _check_auth(request)
    rt = await _runtime_get(f"/models/pool?include_routes={int(include_routes)}")
    if not rt["ok"]:
        return {"models": {}, "summary": {},
                "errors": [{"endpoint": rt["endpoint"],
                            "status": rt["status"], "error": rt["error"]}]}
    return {"models": rt["data"].get("models") or {},
            "summary": rt["data"].get("summary") or {},
            "runtime_endpoint": rt["endpoint"], "errors": []}


def _check_auth(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost", "testclient"):
        return
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token or request.headers.get("x-admin-token") != token:
        raise HTTPException(status_code=401, detail="admin token required")


def remote_check(request: Request) -> None:
    """_check_auth variant for tests: always treats the peer as remote."""
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token or request.headers.get("x-admin-token") != token:
        raise HTTPException(status_code=401, detail="admin token required")


def _actor(request: Request) -> str:
    return request.headers.get("x-admin-actor") or "admin-api"


# ── config revisions (R6 §D/§J) ───────────────────────────────────────────

@router.get("/config/active")
async def config_active(request: Request):
    _check_auth(request)
    cfg, rid = store.get_active_config()
    return {"config": cfg or rev.active_config(), "revision_id": rid,
            "source": "control.db" if rid else "defaults.yaml"}


@router.get("/config/revisions")
async def config_revisions(request: Request, limit: int = 100):
    _check_auth(request)
    return {"revisions": store.list_revisions(limit)}


@router.post("/config/revisions")
async def create_revision(request: Request):
    _check_auth(request)
    body = await request.json()
    cfg = body.get("config") or body.get("patch") or {}
    reason = body.get("reason") or ""
    if not isinstance(cfg, dict) or not cfg or not reason:
        raise HTTPException(status_code=400, detail="config (object, non-empty) and reason required")
    r = rev.create_draft(cfg, actor=_actor(request), reason=reason)
    return {"revision": r}


@router.post("/config/revisions/{rid}/validate")
async def validate_revision(rid: str, request: Request):
    _check_auth(request)
    try:
        return {"revision": rev.validate_revision(rid)}
    except KeyError:
        raise HTTPException(status_code=404, detail="revision not found")


@router.post("/config/revisions/{rid}/simulate")
async def simulate_revision(rid: str, request: Request):
    try:
        return {"revision": rev.simulate_revision(rid)}
    except KeyError:
        raise HTTPException(status_code=404, detail="revision not found")


@router.post("/config/revisions/{rid}/apply")
async def apply_revision(rid: str, request: Request):
    _check_auth(request)
    body = await request.json() if request.headers.get("content-length", "0") not in ("", "0") else {}
    try:
        out = rev.apply_revision(rid, actor=_actor(request), force=bool(body.get("force")))
    except KeyError:
        raise HTTPException(status_code=404, detail="revision not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=repr(e))
    if not out.get("applied"):
        raise HTTPException(status_code=out.get("status") in ("INVALID",) and 422 or 409, detail=out)
    return {"revision": out}


@router.post("/config/revisions/{rid}/rollback")
async def rollback_revision(rid: str, request: Request):
    _check_auth(request)
    try:
        out = rev.rollback(rid, actor=_actor(request))
    except KeyError:
        raise HTTPException(status_code=404, detail="revision not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"rolled_back_to": rid, "revision": out}


@router.post("/config/impact-preview")
async def impact_preview(request: Request):
    _check_auth(request)
    body = await request.json()
    return rev.simulate_impact(body.get("config") or {})


# ── UI-contract aliases (R11 §0): the Admin UI calls /config, /revisions/...
# Keep these stable — they are the browser contract, verified by E2E.

@router.get("/config")
async def config_ui_alias(request: Request):
    return await config_active(request)


@router.get("/revisions")
async def revisions_ui_alias(request: Request, limit: int = 100):
    return await config_revisions(request, limit)


@router.post("/revisions")
async def create_revision_ui_alias(request: Request):
    return await create_revision(request)


@router.post("/revisions/{rid}/validate")
async def validate_revision_ui(rid: str, request: Request):
    return await validate_revision(rid, request)


@router.post("/revisions/{rid}/simulate")
async def simulate_revision_ui(rid: str, request: Request):
    return await simulate_revision(rid, request)


@router.post("/revisions/{rid}/apply")
async def apply_revision_ui(rid: str, request: Request):
    return await apply_revision(rid, request)


@router.post("/revisions/{rid}/rollback")
async def rollback_revision_ui(rid: str, request: Request):
    return await rollback_revision(rid, request)


@router.post("/revisions/{rid}/impact")
async def revision_impact_ui(rid: str, request: Request):
    """Impact preview for an existing draft revision."""
    _check_auth(request)
    r = store.get_revision(rid)
    if r is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return rev.simulate_impact(r.get("config") or {})


@router.get("/revisions/{rid}/diff")
async def revision_diff_ui(rid: str, request: Request):
    """Structured diff of a revision vs its previous revision (or active)."""
    _check_auth(request)
    r = store.get_revision(rid)
    if r is None:
        raise HTTPException(status_code=404, detail="revision not found")
    prev_id = r.get("previous_revision")
    prev = (store.get_revision(prev_id) or {}).get("config") if prev_id else None
    if prev is None:
        _, active_id = store.get_active_config()
        prev = rev.active_config()
    else:
        active_id = prev_id
    return {"revision_id": rid, "previous_revision": active_id,
            "diff": rev.diff_configs(prev, r.get("config") or {}),
            "reason": r.get("reason"), "status": r.get("status"),
            "created_at": r.get("created_at"), "actor": r.get("actor")}


# ── providers (R6 §E) ─────────────────────────────────────────────────────

def _config_providers() -> list[dict]:
    """Providers declared via the active config revision (config-based
    management). Control-DB provider rows take precedence per name."""
    cfg = rev.active_config()  # DB active config, or defaults.yaml fallback
    out = []
    for name, v in (cfg or {}).get("providers", {}).items():
        out.append({
            "name": name,
            "adapter_type": ("provider_b" if name == "provider_b" else
                             "provider_a" if name == "provider_a" else
                             "openai-compatible"),
            "base_url": None,
            "enabled": bool(v.get("enabled", True)),
            "secret_ref": None,
            "secret_configured": None,  # unknown here — secret lives in env
            "catalog_refresh_interval_s": None,
            "min_discount_override": v.get("min_discount"),
            "health_policy": {},
            "source": "active-config",
        })
    return out


@router.get("/providers")
async def providers(request: Request, include_archived: bool = False):
    _check_auth(request)
    rows = store.list_providers(include_archived)
    by_name = {p["name"]: p for p in rows}
    for cp in _config_providers():
        if cp["name"] not in by_name:
            cp["source"] = "active-config"
            rows.append(cp)
        else:
            by_name[cp["name"]]["source"] = "control-db+active-config"
    return {"providers": rows,
            "adapter_types": sorted(ADAPTER_TYPES)}


@router.post("/providers")
async def add_provider(request: Request):
    _check_auth(request)
    b = await request.json()
    name = str(b.get("name") or "").strip().lower()
    atype = str(b.get("adapter_type") or "").strip()
    if not name or atype not in ADAPTER_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"adapter_type must be one of {sorted(ADAPTER_TYPES)}")
    secret_ref = b.get("secret_ref")
    if secret_ref:
        import os
        if not os.environ.get(str(secret_ref)):
            raise HTTPException(status_code=422,
                                detail=f"secret_ref env var {secret_ref!r} is not set in the gateway environment")
    p = store.upsert_provider(
        name, atype, b.get("base_url"), secret_ref,
        enabled=bool(b.get("enabled", True)),
        catalog_refresh_interval_s=b.get("catalog_refresh_interval_s"),
        min_discount_override=b.get("min_discount_override"),
        health_policy=b.get("health_policy") or {})
    return {"provider": p}


@router.patch("/providers/{name}")
async def edit_provider(name: str, request: Request):
    _check_auth(request)
    cur = store.get_provider(name)
    if cur is None:
        raise HTTPException(status_code=404, detail="provider not found")
    b = await request.json()
    p = store.upsert_provider(
        name,
        b.get("adapter_type") or cur["adapter_type"],
        b.get("base_url", cur["base_url"]),
        b.get("secret_ref", cur["secret_ref"]),
        enabled=bool(b.get("enabled", cur["enabled"])),
        catalog_refresh_interval_s=b.get("catalog_refresh_interval_s", cur["catalog_refresh_interval_s"]),
        min_discount_override=b.get("min_discount_override", cur["min_discount_override"]),
        health_policy=b.get("health_policy") or cur["health_policy"])
    return {"provider": p}


async def _set_provider_enabled(name: str, enabled: bool, request: Request) -> dict:
    """Enable/disable a provider. Prefers a control-DB row; for config-based
    providers (no DB row) it applies an atomic config revision instead."""
    if store.set_provider_enabled(name, enabled):
        return {"ok": True, "name": name, "enabled": enabled, "via": "control-db"}
    # config-based provider: check it exists in the active config
    cfg = rev.active_config()
    if name not in (cfg or {}).get("providers", {}):
        raise HTTPException(status_code=404, detail="provider not found")
    new_cfg = json.loads(json.dumps(cfg))  # deep copy without mutating active
    new_cfg["providers"][name]["enabled"] = enabled
    draft = rev.create_draft(new_cfg, actor=_actor(request),
                             reason=f"provider {'enable' if enabled else 'disable'}: {name}")
    out = rev.apply_revision(draft["revision_id"], actor=_actor(request))
    if not out.get("applied"):
        raise HTTPException(status_code=409, detail=out)
    return {"ok": True, "name": name, "enabled": enabled, "via": "config-revision",
            "revision_id": draft["revision_id"]}


@router.post("/providers/{name}/enable")
async def enable_provider(name: str, request: Request):
    _check_auth(request)
    return await _set_provider_enabled(name, True, request)


@router.post("/providers/{name}/disable")
async def disable_provider(name: str, request: Request):
    _check_auth(request)
    return await _set_provider_enabled(name, False, request)


@router.delete("/providers/{name}")
async def archive_provider(name: str, request: Request):
    _check_auth(request)
    if not store.archive_provider(name):
        raise HTTPException(status_code=404, detail="provider not found")
    return {"ok": True, "name": name, "archived": True}


# ── models (R6 §G) ────────────────────────────────────────────────────────

@router.get("/models/policy")
async def model_policy_all(request: Request):
    _check_auth(request)
    return {"models": store.list_model_policy()}


@router.post("/models/{canonical}/policy")
async def set_model_policy(canonical: str, request: Request):
    _check_auth(request)
    b = await request.json()
    lo = b.get("lifecycle_override")
    if lo is not None and lo not in ("WATCH", "CORE", "DEPRECATED", "SUNSET", "DISABLED", "DOMINATED"):
        raise HTTPException(status_code=400, detail="invalid lifecycle_override")
    rp = b.get("restricted_providers")
    if rp is not None and not isinstance(rp, list):
        raise HTTPException(status_code=400, detail="restricted_providers must be a list")
    # Safety: never silently enable an unknown canonical (no fuzzy alias merge).
    from ..policy import get_canonical
    if get_canonical(canonical) is None:
        raise HTTPException(status_code=404, detail="unknown canonical (no alias merge allowed)")
    p = store.set_model_policy(canonical, lifecycle_override=lo,
                               restricted_providers=rp,
                               tier_restriction=b.get("tier_restriction"))
    return {"model_policy": p}


# ── overrides (R6 §I) ─────────────────────────────────────────────────────

@router.get("/overrides")
async def overrides_list(request: Request):
    _check_auth(request)
    return {"overrides": store.list_overrides(active_only=True)}


@router.post("/overrides")
async def override_create(request: Request):
    _check_auth(request)
    b = await request.json()
    try:
        ov = ovr.create(
            kind=str(b.get("kind") or ""), target=str(b.get("target") or ""),
            reason=str(b.get("reason") or ""), actor=_actor(request),
            ttl_s=b.get("ttl_s"), persistent=bool(b.get("persistent")),
            params=b.get("params"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"override": ov}


@router.delete("/overrides/{oid}")
async def override_delete(oid: str, request: Request):
    _check_auth(request)
    if not ovr.disable(oid, actor=_actor(request)):
        raise HTTPException(status_code=404, detail="override not found")
    return {"ok": True}


# ── simulator (R6 §K) ─────────────────────────────────────────────────────

import asyncio as _asyncio
import time as _time

_registry_build_lock: "_asyncio.Lock | None" = None
_registry_built_at: float = 0.0
_REGISTRY_TTL_S = 120.0


async def _ensure_registry_built(force: bool = False) -> int:
    """Build the inference registry on demand (control process has no
    FastAPI startup hook for gateway.app). TTL-cached."""
    global _registry_build_lock, _registry_built_at
    from ..app import _registry, _canon
    now = _time.time()
    if not force and _registry.all() and now - _registry_built_at < _REGISTRY_TTL_S:
        return len(_registry.all())
    if _registry_build_lock is None:
        _registry_build_lock = _asyncio.Lock()
    async with _registry_build_lock:
        if not _registry.all() or _time.time() - _registry_built_at >= _REGISTRY_TTL_S:
            await _registry.build(_registry.adapters())
            _canon.rebuild()
            _registry_built_at = _time.time()
    return len(_registry.all())


@router.post("/simulate")
async def admin_simulate(request: Request):
    _check_auth(request)
    b = await request.json()
    from ..app import _selector, _registry, _canon
    from ..classifier import capabilities_for_class, tier_for_class
    from ..selector import SelectionContext
    # R6: in the separate control-plane process the inference app module is
    # imported fresh and its startup event never ran — ensure the registry
    # is built before simulating (TTL-cached, single-flight guarded).
    if not _registry.all():
        await _ensure_registry_built()
    mode = str(b.get("mode") or "AUTO").upper()
    task_class = str(b.get("task_class") or "NORMAL_CODING").upper()
    tier = b.get("tier") or tier_for_class(task_class)
    ovr_active = ovr.resolve()
    canonical_hint = b.get("canonical")
    provider_hint = b.get("provider_hint")
    forced_route = None
    if mode == "FORCE_CANONICAL":
        canonical_hint = b.get("canonical") or ovr_active["force_canonical"]
    elif mode == "FORCE_ROUTE":
        fr = b.get("route") or ovr_active["force_route"]
        if fr and ":" in fr:
            forced_route = fr
    ctx = SelectionContext(
        canonical_hint=canonical_hint,
        provider_hint=provider_hint,
        prompt_cache_key=b.get("session_key"),
        required_context=int(b.get("context_tokens") or 0),
        reserved_output=int(b.get("reserved_output") or 1024),
        capabilities_required=frozenset(b.get("capabilities") or capabilities_for_class(task_class)),
        tier=tier,
        task_class=task_class,
        exclude_route_keys=frozenset(ovr_active["excluded_route_keys"]) if ovr_active["excluded_route_keys"] else frozenset(),
        allow_unknown_quality=bool(b.get("allow_unknown_quality", False)),
        current_route_key=b.get("current_route_key"),
        cache_state=str(b.get("cache_state") or "UNKNOWN").upper(),
        warm_prefix_tokens=int(b.get("warm_prefix_tokens") or 0),
        forced_route=forced_route,
    )
    primary, plan, trace = _selector.choose(ctx)
    # R14 §23: candidate table with prices/context/health per plan step
    candidates = []
    for p in plan:
        try:
            rec = _registry.get_any(p.provider, p.provider_model_id)
        except Exception:
            rec = None
        candidates.append({
            "canonical": p.canonical, "provider": p.provider,
            "provider_model_id": p.provider_model_id,
            "role": p.reason,
            "input_price": rec.input_price if rec else None,
            "output_price": rec.output_price if rec else None,
            "price_state": rec.price_state if rec else None,
            "discount": rec.discount if rec else None,
            "context_length": rec.context_length if rec else None,
            "health": rec.health if rec else None,
            "quality_score": rec.quality_score if rec else None,
        })
    gates_ru = []
    for rr_ in trace.get("rejected_routes", []):
        rsn = rr_.get("reason") or ""
        ru = ("здоровье/цепь" if rsn.startswith(("health", "circuit")) else
              "скидка ниже минимума" if rsn.startswith("discount") else
              "не хватает контекста" if rsn.startswith("context") else
              "нет требуемых возможностей" if rsn.startswith("capability") else
              "не сертифицирован" if rsn.startswith("certification") else
              "качество ниже порога" if rsn.startswith("quality") else
              "статус жизненного цикла" if rsn.startswith("lifecycle") else rsn)
        gates_ru.append({**rr_, "reason_ru": ru})
    reason_ru = None
    if primary:
        r = primary.reason or ""
        reason_ru = ("модель задана явно в запросе" if r.startswith("hint") else
                     "лучшее качество среди подходящих" if r.startswith("quality") else
                     "сохранён тёплый маршрут сессии" if "cache" in r else
                     "самый выгодный допущенный маршрут" if r else r)
    return {
        "mode": mode,
        "task_class": task_class, "tier": tier,
        "canonical_candidates": trace.get("candidate_canonicals", []),
        "rejected_canonicals": trace.get("rejected_canonicals", []),
        "hard_gates": trace.get("rejected_routes", []),
        "hard_gates_ru": gates_ru,
        "candidates": candidates,
        "cache_decision": trace.get("cache_economics", {}),
        "reason_ru": reason_ru,
        "winner": ({"canonical": primary.canonical, "provider": primary.provider,
                    "provider_model_id": primary.provider_model_id,
                    "reason": primary.reason} if primary else None),
        "plan": [{"canonical": p.canonical, "provider": p.provider,
                  "provider_model_id": p.provider_model_id, "reason": p.reason}
                 for p in plan],
        "reason_codes": {"cache": (trace.get("cache_economics") or {}).get("reason_code"),
                         "hint_fallback": trace.get("hint_fallback", False)},
    }


# ── audit (R6 §N) — superseded by the R12 human-format /audit below ──────


# ── config export / import (R8 §11) ────────────────────────────────────────

@router.get("/config/export")
async def config_export(request: Request):
    """Secret-free config snapshot. Provider secret refs are NAMES only."""
    _check_auth(request)
    from . import config_io
    return config_io.export_config()


@router.post("/config/import")
async def config_import(request: Request):
    """Validate (dry_run=true, default) or apply an exported config."""
    _check_auth(request)
    from . import config_io
    body = await request.json()
    dry_run = bool(body.pop("_dry_run", True))
    payload = body.get("payload") or body
    return config_io.import_config(payload, actor=_actor(request), dry_run=dry_run)


# ── control-plane health ──────────────────────────────────────────────────

@router.get("/adapters")
async def adapters_list(request: Request):
    """R14 §5: adapter types for the provider form (UI contract, was 404)."""
    _check_auth(request)
    return {"types": sorted(ADAPTER_TYPES),
            "ru": {
                "provider_a": "Provider A — [OI]-совместимый маркетплейс (дополнительно Anthropic-совместимый)",
                "provider_b": "Provider B — [OI]-совместимый маркетплейс",
                "openai-compatible": "Универсальный [OI]-совместимый адаптер (кастомный провайдер)",
            }}


@router.get("/healthz")
async def control_health():
    return {"ok": True, "control_db": store.db_path(),
            "active_revision": store.get_active_config()[1],
            "overrides_active": len(store.list_overrides(active_only=True)),
            "ts": time.time()}


# ── availability probing (UI spec §F/§G) ──────────────────────────────────

@router.post("/probe/route")
async def probe_route(request: Request):
    """Safe availability check for one route (provider + provider_model_id).
    Free catalog check by default; deep=true adds a minimal paid inference
    probe ("Reply exactly: OK", max_tokens=16). Result is stored as runtime
    health evidence — never applied as policy."""
    _check_auth(request)
    b = await request.json()
    provider = str(b.get("provider") or "").strip()
    slug = str(b.get("provider_model_id") or "").strip()
    if not provider or not slug:
        raise HTTPException(status_code=400, detail="provider and provider_model_id required")
    from . import probe as _probe
    return await _probe.probe_route(provider, slug, deep=bool(b.get("deep")))


@router.post("/probe/model/{canonical}")
async def probe_model(canonical: str, request: Request):
    """Probe every route of a canonical model."""
    _check_auth(request)
    from . import probe as _probe
    routes = [r for r in await _probe._runtime_routes() if r["canonical"] == canonical]
    if not routes:
        raise HTTPException(status_code=404, detail="canonical has no routes")
    deep = await _request_flag(request)
    sem = _asyncio.Semaphore(4)

    async def one(r):
        async with sem:
            return await _probe.probe_route(r["provider"], r["provider_model_id"], deep=deep)

    results = await _asyncio.gather(*[one(r) for r in routes])
    ok = sum(1 for r in results if r.get("ok"))
    return {"canonical": canonical, "total": len(results), "ok": ok,
            "failed": len(results) - ok, "checked_at": time.time(), "results": results}


@router.post("/probe/provider/{name}")
async def probe_provider(name: str, request: Request):
    """Probe all routes of one provider."""
    _check_auth(request)
    from . import probe as _probe
    deep = await _request_flag(request)
    return await _probe.probe_provider(name, deep=deep)


@router.post("/probe/all")
async def probe_all(request: Request):
    """Probe every route of every known provider (catalog checks)."""
    _check_auth(request)
    from . import probe as _probe
    providers = sorted({r["provider"] for r in await _probe._runtime_routes()})
    out = []
    for p in providers:
        out.append(await _probe.probe_provider(p, deep=False))
    total = sum(o["total"] for o in out)
    ok = sum(o["ok"] for o in out)
    store.audit(_actor(request), "availability.check", "all",
                {"total": total, "ok": ok, "failed": total - ok})
    return {"providers": out, "total": total, "ok": ok,
            "failed": total - ok, "checked_at": time.time()}


async def _request_flag(request: Request) -> bool:
    """Read optional JSON body {deep: true} tolerating an empty body."""
    try:
        body = await request.json()
        return bool((body or {}).get("deep"))
    except Exception:
        return False


@router.get("/availability")
async def availability(request: Request, provider: str | None = None):
    """Stored availability evidence (latest per route)."""
    _check_auth(request)
    if provider:
        return {"provider": provider,
                "checks": store.availability_for_provider(provider)}
    from . import probe as _probe
    out = []
    for r in await _probe._runtime_routes():
        last = store.last_availability(r["provider"], r["provider_model_id"])
        out.append({"canonical": r["canonical"], "provider": r["provider"],
                    "provider_model_id": r["provider_model_id"],
                    "last_probe": last})
    return {"checks": out}


# ── provider connection test (UI spec §I) ─────────────────────────────────

@router.post("/providers/test-connection")
async def provider_test_connection(request: Request):
    """Pre-save connectivity check: reachable + auth + models endpoint +
    models found + pricing coverage + latency. No secrets in the response."""
    _check_auth(request)
    b = await request.json()
    base = str(b.get("base_url") or "").rstrip("/")
    secret_ref = str(b.get("secret_ref") or "").strip()
    if not base:
        raise HTTPException(status_code=400, detail="base_url required")
    import os as _os
    import httpx
    key = _os.environ.get(secret_ref) if secret_ref else None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    t0 = time.time()
    result: dict = {"base_url": base,
                    "secret_ref": secret_ref or None,
                    "secret_env_set": bool(key)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(base + "/models", headers=headers)
        result["http_status"] = r.status_code
        result["api_reachable"] = True
        result["auth_ok"] = r.status_code not in (401, 403)
        if r.status_code == 200:
            try:
                data = r.json().get("data") or []
                result["models_endpoint_ok"] = True
                result["models_found"] = len(data)
                priced = sum(
                    1 for m in data if isinstance(m, dict) and m.get("pricing"))
                result["pricing_found"] = (priced if priced else "UNKNOWN")
            except Exception:
                result["models_endpoint_ok"] = False
                result["models_found"] = 0
                result["pricing_found"] = "UNKNOWN"
        else:
            result["models_endpoint_ok"] = False
            result["models_found"] = 0
            result["pricing_found"] = "UNKNOWN"
    except Exception as e:  # noqa: BLE001
        result["api_reachable"] = False
        result["auth_ok"] = False
        result["models_endpoint_ok"] = False
        result["models_found"] = 0
        result["pricing_found"] = "UNKNOWN"
        result["error"] = f"{type(e).__name__}: {e}"
    result["latency_ms"] = int((time.time() - t0) * 1000)
    result["ok"] = bool(result.get("api_reachable") and result.get("auth_ok")
                        and result.get("models_endpoint_ok"))
    return result


# ── provider detail (UI spec §J) ──────────────────────────────────────────

@router.get("/providers/{name}/detail")
async def provider_detail(name: str, request: Request):
    """Provider page data: settings, last checks, model table with prices."""
    _check_auth(request)
    rows = store.list_providers(include_archived=True)
    p = next((x for x in rows if x["name"] == name), None)
    if p is None:
        p = next((x for x in _config_providers() if x["name"] == name), None)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    from . import probe as _probe
    await _ensure_registry_built()
    from ..app import _registry
    routes = [r for r in _registry.all() if r.provider == name]
    models = [{
        "canonical": r.canonical,
        "provider_model_id": r.provider_model_id,
        "price_state": r.price_state,
        "input_price": r.input_price,
        "output_price": r.output_price,
        "discount": r.discount,
        "eligible": (r.discount or 0.0) >= _registry.min_discount_for(name),
        "context": r.context_length,
        "health": r.health,
        "last_probe": store.last_availability(name, r.provider_model_id),
    } for r in sorted(routes, key=lambda x: (x.canonical, x.provider_model_id))]
    checks = store.availability_for_provider(name)
    last_check = max((c.get("checked_at") for c in checks), default=None)
    return {"provider": p, "models": models, "models_found": len(models),
            "eligible_routes": sum(1 for m in models if m["eligible"]),
            "last_check_ts": last_check,
            "recent_checks": checks[:50]}


# ── catalog / pricing refresh (UI spec §K) ─────────────────────────────────

def _registry_snapshot() -> dict:
    from ..app import _registry
    snap = {}
    for r in _registry.all():
        snap[(r.provider, r.provider_model_id)] = {
            "canonical": r.canonical, "input_price": r.input_price,
            "output_price": r.output_price, "price_state": r.price_state}
    return snap


async def _runtime_refresh() -> dict:
    """Trigger runtime registry re-discovery (catalogs + prices)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(RUNTIME_BASE_URL + "/registry/refresh")
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:200]}
        return {"ok": r.status_code == 200, "status": r.status_code, "data": data}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "data": {},
                "error": f"{type(e).__name__}: {e}"}


def _refresh_diff(before: dict, after: dict) -> dict:
    new = [f"{p}:{m}" for (p, m) in after if (p, m) not in before]
    gone = [f"{p}:{m}" for (p, m) in before if (p, m) not in after]
    price_changed, unknown_price = [], []
    for key, v in after.items():
        b = before.get(key)
        if b is None:
            continue
        if (b["input_price"], b["output_price"]) != (v["input_price"], v["output_price"]):
            price_changed.append(f"{key[0]}:{key[1]}")
        if v["price_state"] == "UNKNOWN":
            unknown_price.append(f"{key[0]}:{key[1]}")
    return {"models_found": len(after), "new": len(new), "new_routes": new[:50],
            "disappeared": len(gone), "disappeared_routes": gone[:50],
            "price_changed": len(price_changed), "price_changed_routes": price_changed[:50],
            "unknown_prices": len(unknown_price),
            "errors": 0, "updated_at": time.time()}


@router.post("/catalog/refresh")
async def catalog_refresh(request: Request):
    """Обновить все каталоги и цены (runtime re-discovery) + diff report."""
    _check_auth(request)
    before = _registry_snapshot()
    rr = await _runtime_refresh()
    if not rr["ok"]:
        raise HTTPException(status_code=502, detail={
            "error": "runtime registry refresh failed",
            "runtime_status": rr["status"], "runtime_error": rr.get("error")})
    # refresh the control-process registry view too
    from . import probe as _probe  # noqa: F401  (ensures imports stay valid)
    from ..app import _registry
    await _ensure_registry_built(force=True)
    after = _registry_snapshot()
    diff = _refresh_diff(before, after)
    diff["runtime_summary"] = rr["data"]
    store.audit(_actor(request), "catalog.refresh", "all",
                {"models": diff.get("models_found"),
                 "price_changed": diff.get("price_changed")})
    return {"ok": True, "diff": diff}


@router.post("/catalog/refresh/{name}")
async def catalog_refresh_provider(name: str, request: Request):
    """Обновить каталог/цены одного провайдера (runtime re-discovery,
    diff ограничен маршрутами этого провайдера)."""
    _check_auth(request)
    before = _registry_snapshot()
    rr = await _runtime_refresh()
    if not rr["ok"]:
        raise HTTPException(status_code=502, detail={
            "error": "runtime registry refresh failed",
            "runtime_status": rr["status"], "runtime_error": rr.get("error")})
    from ..app import _registry
    await _ensure_registry_built(force=True)
    after = _registry_snapshot()
    b = {k: v for k, v in before.items() if k[0] == name}
    a = {k: v for k, v in after.items() if k[0] == name}
    diff = _refresh_diff(b, a)
    diff["runtime_summary"] = rr["data"]
    return {"ok": True, "provider": name, "diff": diff}


# ── dashboard attention block (UI spec §L) ────────────────────────────────

@router.get("/attention")
async def attention(request: Request):
    """'Требуют внимания': stale probes, UNKNOWN prices, outdated catalogs."""
    _check_auth(request)
    from . import probe as _probe
    await _ensure_registry_built()
    from ..app import _registry
    stale = await _probe.stale_models(86400.0)
    unknown_price = [r.provider + ":" + r.provider_model_id
                     for r in _registry.all() if r.price_state == "UNKNOWN"]
    return {"stale_probes": stale,
            "stale_probes_count": len(stale),
            "unknown_price_routes": unknown_price,
            "unknown_price_count": len(unknown_price)}


# ── R11: inventory / market / mapping / pool ───────────────────────────────

@router.post("/discovery/refresh")
async def discovery_refresh(request: Request):
    """Live re-discovery of ALL provider catalogs + market data. No filtering.

    R14 §6: returns a human summary — models/offers counts and HOW MANY
    prices actually changed (before/after compare over discovered rows)."""
    _check_auth(request)
    from . import discovery
    actor = _actor(request)
    before = {}
    for prov in ("provider_a", "provider_b"):
        for m in store.list_discovered(provider=prov):
            before[(prov, m["provider_model_id"])] = (
                m.get("best_input"), m.get("best_output"))
    out = await discovery.refresh_all()
    changed = 0
    for prov in ("provider_a", "provider_b"):
        for m in store.list_discovered(provider=prov):
            b = before.get((prov, m["provider_model_id"]))
            if b is not None and b != (m.get("best_input"), m.get("best_output")):
                changed += 1
    summary = {}
    for prov, v in out.items():
        if not isinstance(v, dict):
            continue
        summary[prov] = {
            "ok": bool(v.get("ok")),
            "models": v.get("total"),
            "new": v.get("new"),
            "gone": v.get("gone"),
            "offers": (v.get("offers_count") if isinstance(v.get("offers_count"), int)
                       else v.get("sellers_count")),
            "error": v.get("error") or v.get("blocked_by"),
        }
    ok = all(v.get("ok") for v in out.values() if isinstance(v, dict))
    store.audit(actor, "market.refresh", "all", {"ok": ok, "changed": changed})
    return {"ok": ok, "providers": out, "summary": summary,
            "prices_changed": changed, "updated_at": time.time(),
            "updated_at_iso": time.strftime("%H:%M:%S")}


@router.get("/inventory")
async def inventory_view(request: Request, provider: str | None = None):
    """R11 §6: 3-level model inventory — all discovered / my pool / hidden,
    with market prices, discounts, eligibility REASONS. Zero silent drops."""
    _check_auth(request)
    from . import inventory
    return inventory.build_inventory(provider=provider)


@router.get("/inventory/unmatched")
async def unmatched_inbox(request: Request):
    """R11 §26: provider models without a canonical mapping."""
    _check_auth(request)
    from . import inventory
    inv = inventory.build_inventory()
    return {"unmatched": inv["unmatched"], "counts": inv["counts"]}


@router.get("/inventory/reconciliation/{provider}")
async def reconciliation_view(provider: str, request: Request):
    """R11 §33: live catalog vs imported vs matched vs eligible + reasons."""
    _check_auth(request)
    from . import inventory
    return inventory.reconciliation(provider)


@router.post("/canonical-map")
async def set_canonical_mapping(request: Request):
    """Map/unmap a provider model to a canonical (R11 §7/§26)."""
    _check_auth(request)
    body = await request.json()
    from . import store
    provider = body.get("provider")
    pid = body.get("provider_model_id")
    canonical = body.get("canonical")  # None/"" -> unmap
    if not provider or not pid or not str(pid).strip():
        raise HTTPException(status_code=422,
                            detail="provider and provider_model_id required (non-empty)")
    ok = store.set_canonical(provider, pid, canonical or None,
                             source=body.get("source") or "manual")
    return {"ok": ok}


@router.post("/model-pool/{canonical}")
async def set_model_pool_policy(request: Request, canonical: str):
    """R11 §9/§10/§12: pool membership + per-model policy (min discount,
    max prices, preferred/banned providers, auto routing)."""
    _check_auth(request)
    body = await request.json()
    from . import store
    pol = store.set_pool_policy(canonical, body)
    return {"ok": True, "policy": pol}


@router.get("/model-pool/{canonical}")
async def get_model_pool_policy(request: Request, canonical: str):
    _check_auth(request)
    from . import store
    return store.get_pool_policy(canonical)


@router.get("/market/live")
async def market_live(request: Request):
    """R11 §19: 'Рынок сейчас' — live Provider B markets with short TTL cache."""
    _check_auth(request)
    from . import discovery
    data = await discovery.market_snapshot_cached()
    rows = []
    for model, m in data.items():
        rows.append({"model": model,
                     "best_input_per_1m": m.get("best_input_per_1m"),
                     "best_output_per_1m": m.get("best_output_per_1m"),
                     "best_discount_pct": m.get("best_discount_pct"),
                     "num_sellers": m.get("num_sellers"),
                     "credits_sold_24h": m.get("credits_sold_24h")})
    return {"markets": sorted(rows, key=lambda r: -(r.get("best_discount_pct") or 0)),
            "count": len(rows)}


def price_format_usd(v) -> str:
    """R12 §13: never round cheap prices to $0.00.
    >=1 -> 2dp; >=0.01 -> 4dp; >=0.0001 -> 6dp."""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1:
        return f"${v:.2f}"
    if v >= 0.01:
        return f"${v:.4f}"
    if v >= 0.0001:
        return f"${v:.6f}"
    return f"${v:.8f}"


def cost_preview_raw(canonical: str, input_tokens: float, output_tokens: float,
                     cache_read: float = 0.0, cache_write: float = 0.0) -> dict:
    """Shared cost math for /market/cost-preview and the calculator UI."""
    from . import inventory, store as _store
    inv_data = inventory.build_inventory()
    g = next((m for m in inv_data["models"] if m["canonical"] == canonical), None)
    if g is None:
        raise KeyError(canonical)
    M = 1_000_000.0
    floor = g.get("effective_min_discount")
    best_in, best_out = g.get("best_input"), g.get("best_output")
    off_in, off_out = g.get("official_input"), g.get("official_output")
    pol_in = off_in * (1 - floor) if off_in else None
    pol_out = off_out * (1 - floor) if off_out else None

    def cost(i, o, cache_i=None):
        if i is None or o is None:
            return None
        c = (input_tokens * i + output_tokens * o) / M
        if cache_i is not None:
            c += cache_read * cache_i / M
        return c

    best_cost = cost(best_in, best_out)
    pol_cost = cost(pol_in, pol_out)
    off_cost = cost(off_in, off_out)
    savings = (1 - best_cost / off_cost) * 100 if (best_cost and off_cost) else None
    # per-provider comparison (R12 §15)
    per_provider = []
    for r in g.get("routes", []):
        ri, ro = r.get("best_input"), r.get("best_output")
        rc = cost(ri, ro)
        if rc is None:
            continue
        per_provider.append({
            "provider": r["provider"], "provider_model_id": r["provider_model_id"],
            "input_per_1m": ri, "output_per_1m": ro,
            "cost": rc, "discount_pct": r.get("discount_pct"),
            "market_active": r.get("market_active"),
        })
    per_provider.sort(key=lambda x: x["cost"])
    # actual last-request cost (R12 §17): billing evidence from metrics
    actual = None
    try:
        actual = _last_actual_cost(canonical)
    except Exception:  # noqa: BLE001
        pass
    savings_usd = (off_cost - best_cost) if (best_cost is not None and off_cost is not None) else None
    breakdown = None
    if best_in is not None and best_out is not None:
        breakdown = {
            "input_cost_usd": round(input_tokens * best_in / M, 6),
            "output_cost_usd": round(output_tokens * best_out / M, 6),
            "input_per_1m": best_in, "output_per_1m": best_out,
        }
    return {
        "canonical": canonical,
        "display_name": g.get("display_name"),
        "best_ask_cost": best_cost, "policy_worst_cost": pol_cost,
        "official_cost": off_cost,
        "savings_usd": round(savings_usd, 6) if savings_usd is not None else None,
        "breakdown": breakdown,
        "savings_pct": round(savings, 2) if savings is not None else None,
        "best_input_per_1m": best_in, "best_output_per_1m": best_out,
        "official_input_per_1m": off_in, "official_output_per_1m": off_out,
        "policy_max_input_per_1m": pol_in, "policy_max_output_per_1m": pol_out,
        "effective_min_discount": floor,
        "per_provider": per_provider,
        "actual_last_cost": actual,
        "price_state": "ESTIMATED" if best_cost is not None else "UNKNOWN",
        "market_as_of": time.strftime("%H:%M:%S"),
        "market_as_of_ts": g.get("last_priced"),
    }


def _last_actual_cost(canonical: str) -> dict | None:
    """Last actual USD cost for a canonical from runtime metrics (billing
    evidence). Returns None when the runtime has no data (honest display)."""
    import httpx
    try:
        with httpx.Client(base_url=RUNTIME_BASE_URL, timeout=4.0) as client:
            r = client.get("/metrics")
        if r.status_code != 200:
            return None
        snap = r.json()
    except Exception:  # noqa: BLE001
        return None
    models = snap.get("models") or {}
    m = models.get(canonical) or {}
    cost = m.get("cost_usd")
    reqs = m.get("requests")
    if not reqs:
        return None
    return {"cost_total_usd": cost, "requests": reqs,
            "avg_cost_usd": (cost / reqs) if cost is not None else None,
            "cost_state": "ACTUAL" if cost is not None else "UNKNOWN",
            "period": "с момента запуска Router (кумулятивно)"}


@router.post("/market/cost-preview")
async def cost_preview(request: Request):
    """R11 §16 + R12 §15: cost calculator (best ask / policy max / official,
    per-provider comparison, ACTUAL vs ESTIMATED, live timestamps)."""
    _check_auth(request)
    body = await request.json()
    canonical = body.get("canonical") or ""
    input_tokens = float(body.get("input_tokens") or 0)
    output_tokens = float(body.get("output_tokens") or 0)
    cache_read = float(body.get("cache_read_tokens") or 0)
    cache_write = float(body.get("cache_write_tokens") or 0)
    try:
        return cost_preview_raw(canonical, input_tokens, output_tokens,
                                cache_read, cache_write)
    except KeyError:
        raise HTTPException(status_code=404,
                            detail=f"canonical {canonical} not in inventory")


# ── R12: insights (why / value / economics / opportunities / alerts) ───────

@router.get("/insights/why/{canonical}")
async def insights_why(canonical: str, request: Request):
    """R12 §6: 'Почему Router использует/не использует эту модель'."""
    _check_auth(request)
    from . import inventory, insights
    inv_data = inventory.build_inventory()
    g = next((m for m in inv_data["models"] if m["canonical"] == canonical), None)
    if g is None:
        raise HTTPException(status_code=404, detail="canonical not found")
    out = insights.why_router(g)
    out["value"] = insights.value_rating(g)
    return out


@router.get("/insights/economics")
async def insights_economics(request: Request):
    """R12 §29: почему выбрана эта модель вместо альтернатив + детектор
    неэффективности (diagnostic only, never auto-changes routing)."""
    _check_auth(request)
    from . import insights
    return insights.economics()


@router.get("/insights/opportunities")
async def insights_opportunities(request: Request, limit: int = 10):
    """R12 §30: выгодные модели вне моего пула."""
    _check_auth(request)
    from . import insights
    return {"opportunities": insights.opportunities(limit)}


@router.get("/insights/alerts")
async def insights_alerts(request: Request):
    """R12 §31: 'Требуют внимания' — ценовые/рыночные аномалии (UI only)."""
    _check_auth(request)
    from . import insights
    return {"alerts": insights.alerts(), "ts": time.time()}


@router.get("/insights/provider/{name}")
async def insights_provider(name: str, request: Request):
    """R12 §19: почему провайдер используется/не используется."""
    _check_auth(request)
    from . import insights
    return insights.provider_explanation(name)


@router.get("/insights/price-units")
async def price_units_audit(request: Request, limit: int = 10):
    """R12-B7: for N pool models compare UI price vs LIVE provider data.

    For every checked model returns: raw provider value, provider unit,
    normalized USD/1M and the UI value — with MATCH yes/no. Catches any
    repeat of the Provider B micro-USD bug at the source."""
    _check_auth(request)
    from . import discovery, inventory
    inv_data = inventory.build_inventory()
    pool = [g for g in inv_data["models"] if g.get("in_pool")][:limit]
    ih, sp = await asyncio.gather(discovery.fetch_provider_a_catalog(),
                                  discovery.fetch_provider_b_data())
    ih_by = {m["provider_model_id"]: m for m in (ih.get("models") or [])}
    sp_by = {m["provider_model_id"]: m for m in (sp.get("models") or [])}
    rows = []
    for g in pool:
        for r in g.get("routes") or []:
            pid = r["provider_model_id"]
            if r["provider"] == "provider_a" and pid in ih_by:
                src = ih_by[pid]
                raw_in = src.get("min_ask_in")
                raw_unit = "USD per 1M tokens (min_ask_in)"
                norm = raw_in
            elif r["provider"] == "provider_b" and pid in sp_by:
                src = sp_by[pid]
                mk = src.get("market") or {}
                raw_in = mk.get("best_input_per_1m")
                raw_unit = "µUSD per 1M -> converted to USD (best_input_per_1m)"
                norm = raw_in
            else:
                raw_in, raw_unit, norm = None, "provider fetch failed", None
            ui_in = r.get("best_input")
            match = None
            if norm is not None and ui_in is not None:
                match = abs(norm - ui_in) <= max(1e-6, norm * 0.01)
            rows.append({
                "MODEL": g.get("display_name") or g.get("canonical"),
                "canonical": g.get("canonical"),
                "provider": r["provider"], "provider_model_id": pid,
                "PROVIDER_RAW_VALUE": raw_in, "PROVIDER_UNIT": raw_unit,
                "NORMALIZED_USD_PER_1M": norm,
                "UI_VALUE": ui_in,
                "MATCH": match,
            })
    return {"checked": len(rows),
            "mismatches": sum(1 for x in rows if x["MATCH"] is False),
            "rows": rows}


@router.get("/insights/reconciliation-snapshot")
async def reconciliation_snapshot(request: Request):
    """R12-B6: ONE snapshot across every source — catalogs, control DB,
    runtime registry, dashboard and Models UI all answer to these numbers."""
    _check_auth(request)
    from . import inventory
    inv_data = inventory.build_inventory()
    counts = inv_data["counts"]
    pool = await _runtime_get("/models/pool?include_routes=1")
    runtime_routes = 0
    if pool["ok"]:
        for m in ((pool["data"].get("models") or {}).values()):
            runtime_routes += len(m.get("routes") or [])
    providers_cfg = []
    active, _ = store.get_active_config()
    for name in ((active or {}).get("providers") or {}):
        providers_cfg.append(name)
    offers_provider_b = sum(g.get("sellers_count") or 0 for g in inv_data["models"])
    offers_provider_a = sum(g.get("offers_count") or 0 for g in inv_data["models"])
    now = time.time()
    fresh = stale = 0
    for g in inv_data["models"]:
        lp = g.get("last_priced")
        if lp is None:
            continue
        if now - lp < 7200:
            fresh += 1
        else:
            stale += 1
    disc = {}
    for p in ("provider_a", "provider_b"):
        disc[p] = (store.latest_discovery_meta(p) or {}).get("models") or 0
    return {
        "CONFIGURED_PROVIDERS": providers_cfg,
        "ONLINE_PROVIDERS": [p for p in providers_cfg
                             if p != "none"],
        "PROVIDER_A_DISCOVERED": disc["provider_a"],
        "PROVIDER_B_DISCOVERED": disc["provider_b"],
        "POOL_MODELS": counts.get("in_pool", 0),
        "AVAILABLE_POOL_MODELS": counts.get("eligible", 0),
        "UNMATCHED": counts.get("unmatched", 0),
        "RUNTIME_ROUTES": runtime_routes,
        "MARKET_OFFERS": {
            "provider_b_sellers_sum": offers_provider_b,
            "provider_a_price_offers": offers_provider_a,
        },
        "STALE_PRICES": stale,
        "FRESH_PRICES": fresh,
    }


@router.get("/insights/usage")
async def insights_usage(request: Request):
    """R12 §16: расходы с периодами. Telemetry is cumulative — say so."""
    _check_auth(request)
    rt = await _runtime_get("/health")
    snap = {}
    try:
        rt2 = await _runtime_get("/metrics")
        if rt2["ok"]:
            snap = rt2["data"]
    except Exception:  # noqa: BLE001
        pass
    providers = {}
    cost_total = 0.0
    req_total = 0
    for p, v in (snap.get("providers") or {}).items():
        if p == "none":
            continue
        c = v.get("cost_usd") or 0.0
        cost_total += c
        req_total += v.get("requests") or 0
        providers[p] = {"requests": v.get("requests"),
                        "successes": v.get("successes"),
                        "failures": v.get("failures"),
                        "cost_usd": v.get("cost_usd"),
                        "cost_state": "ACTUAL" if v.get("cost_usd") is not None else "UNKNOWN"}
    uptime_s = ((rt.get("data") or {}).get("data") or {}).get("uptime_s") \
        or ((rt.get("data") or {}).get("uptime_s"))
    return {"providers": providers, "cost_total_usd": cost_total,
            "requests_total": req_total, "uptime_s": uptime_s,
            "period_label": "с момента запуска Router (кумулятивно)",
            "daily_available": bool(uptime_s and uptime_s < 86400),
            "cost_state": "ACTUAL"}


# ── R12: price history ─────────────────────────────────────────────────────

def _ph_windows(rows: list[dict]) -> dict:
    """now/h1/d1/d7/d30 min-avg-max windows over price_history rows."""
    from .store import _agg as _s_agg
    now = time.time()
    windows = {"now": (0, 900), "h1": (900, 3600), "d1": (3600, 86400),
               "d7": (86400, 7 * 86400), "d30": (7 * 86400, 31 * 86400)}
    out = {}
    for name, (lo, hi) in windows.items():
        wr = [r for r in rows if lo <= now - r["ts"] < hi]
        out[name] = {
            "best_input": _s_agg(wr, "best_input"),
            "best_output": _s_agg(wr, "best_output"),
            "discount_pct": _s_agg(wr, "discount_pct"),
            "offers": _s_agg(wr, "offers_count"),
            "ts_range": [min((r["ts"] for r in wr), default=None),
                         max((r["ts"] for r in wr), default=None)],
        }
    return out


@router.get("/price-history/route")
async def price_history_route(request: Request, provider: str,
                              pid: str):
    """R12-B1: history for a provider route (works for UNMATCHED models —
    e.g. 'the model 5' has no canonical but has a Provider B route)."""
    _check_auth(request)
    rows = store.price_history(provider=provider, pid=pid)
    if not rows:
        return {"provider": provider, "provider_model_id": pid, "points": 0,
                "empty_reason": "история ещё не накапливалась",
                **{k: {"best_input": None, "best_output": None,
                       "discount_pct": None, "offers": None,
                       "ts_range": [None, None]}
                   for k in ("now", "h1", "d1", "d7", "d30")}, "latest": None}
    latest = rows[0]
    return {"provider": provider, "provider_model_id": pid,
            "points": len(rows), **_ph_windows(rows),
            "latest": {k: latest.get(k) for k in
                       ("ts", "best_input", "best_output", "discount_pct",
                        "offers_count", "sellers_count")}}


@router.get("/price-history/")
async def price_history_empty(request: Request):
    """R12-B1: explicit empty-canonical route — FastAPI does NOT match an
    empty path parameter, so /admin/price-history/ needs its own handler
    (the UI previously 404'd here for unmatched models)."""
    _check_auth(request)
    return await price_history_view("", request)


@router.get("/price-history/{canonical}")
async def price_history_view(canonical: str, request: Request):
    """R12 §28: aggregated price history (now/1h/24h/7d/30d)."""
    _check_auth(request)
    c = (canonical or "").strip()
    if not c:
        # unmatched/provider-discovered model: no canonical -> no history YET.
        # Empty-state, not an error (fixes the /admin/price-history/ 404).
        return {"canonical": None, "points": 0, "empty_reason":
                "модель не сопоставлена — история появится после сопоставления",
                **{k: {"best_input": None, "best_output": None,
                       "discount_pct": None, "offers": None,
                       "ts_range": [None, None]}
                   for k in ("now", "h1", "d1", "d7", "d30")}, "latest": None}
    return store.price_history_aggregates(c)


@router.post('/refresh/run/{job}')
async def refresh_run_now(request: Request, job: str):
    _check_auth(request)
    from . import refresh_scheduler as rs
    if job not in rs.JOBS: raise HTTPException(422, 'unknown refresh job')
    started=time.time()
    if job == 'availability': ok, err = await rs._run_availability()
    else: ok, err = await rs._run_discovery()
    c=store.conn(); rs._table(c); st=rs._get_state(c,job) or {}
    rs._set_state(c,job,ok=ok,err=err,next_run=time.time()+rs.JOBS[job]['ttl'],runs=(st.get('runs') or 0)+1); c.commit()
    store.audit(_actor(request),'scheduler.run_now',job,{'ok':ok,'error':err,'duration_s':round(time.time()-started,3)})
    return {'ok':ok,'job':job,'error':err,'duration_s':round(time.time()-started,3),'schedule':rs.schedule_state()}

@router.get("/refresh/schedule")
async def refresh_schedule(request: Request):
    """R13 §5: auto-refresh jobs — last run, next run, last error per job."""
    _check_auth(request)
    from . import refresh_scheduler
    return refresh_scheduler.schedule_state()


@router.get("/market/freshness")
async def market_freshness(request: Request):
    """R12-B3: per-provider catalog/market snapshot age + freshness verdict.

    Distinguishes the three refresh operations explicitly:
    catalog (models list), pricing (official/asks), market (live sellers)."""
    _check_auth(request)
    out = {}
    now = time.time()
    for p in ("provider_a", "provider_b"):
        row = store.latest_discovery_meta(p) or {}
        last = row.get("last_seen")
        priced = row.get("last_priced") or last
        market = row.get("market_updated_at")
        age = now - last if last else None
        state = ("нет данных" if age is None else
                 "свежие" if age < 3600 else
                 "устарели" if age < 86400 else "очень старые")
        out[p] = {"last_updated": last, "priced_at": priced,
                  "market_updated_at": market, "age_s": age,
                  "state": state, "models": row.get("models")}
    all_ts = [v["last_updated"] for v in out.values() if v["last_updated"]]
    return {"providers": out,
            "overall_updated_at": max(all_ts) if all_ts else None,
            "now": now}


# ── R12: golden baselines / restore points ─────────────────────────────────

def _baseline_payload() -> dict:
    """Everything persistent except secrets, metrics, billing, transient state."""
    cfg, rid = store.get_active_config()
    return {
        "config": cfg or {},
        "providers": [dict(p) for p in store.list_providers(include_archived=False)],
        "canonical_mappings": store.list_canonical_map(),
        "model_pool": store.list_pool_policies(),
        "model_policies": store.list_model_policy(),
        "persistent_overrides": [dict(o) for o in store.list_overrides(active_only=True)
                                 if o.get("persistent")],
    }


@router.get("/baselines")
async def baselines_list(request: Request, kind: str | None = None):
    """R12 §25/§26 + R14 §30: recovery points; primary known-good marked."""
    _check_auth(request)
    bl = store.list_baselines(kind=kind)
    _, active_rid = store.get_active_config()
    kg = store.get_known_good()
    for b in bl:
        b["is_primary_known_good"] = bool(kg and kg["baseline_id"] == b["id"])
    return {"baselines": bl, "active_revision": active_rid,
            "primary_known_good": kg}


@router.post("/baselines/known-good/set")
async def baselines_set_known_good(request: Request):
    """R14 §33: 'Сделать основной рабочей точкой'. NOT factory default —
    the factory baseline is immutable and always available."""
    _check_auth(request)
    body = await request.json()
    bid = str(body.get("baseline_id") or "")
    row = store.get_baseline(bid)
    if row is None:
        raise HTTPException(status_code=404, detail="baseline not found")
    store.set_known_good(bid, actor=_actor(request))
    return {"ok": True, "primary_known_good": store.get_known_good()}


@router.post("/baselines/{bid}/delete")
async def baselines_delete(bid: str, request: Request):
    """R14 §34: delete a user recovery point. Factory default and the active
    primary known-good are protected server-side."""
    _check_auth(request)
    try:
        ok = store.delete_baseline(bid, actor=_actor(request))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="baseline not found")
    return {"ok": True, "deleted": bid}


@router.post("/baselines")
async def baselines_create(request: Request):
    """Создать точку восстановления (KNOWN_GOOD) из текущего состояния."""
    _check_auth(request)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    name = str(body.get("name") or "").strip()
    desc = str(body.get("description") or "").strip()
    if not desc:
        desc = name or "known-good snapshot (admin UI)"
    if name and not desc.startswith(name):
        desc = f"{name} — {desc}"
    payload = _baseline_payload()
    source_commit = str(body.get("source_commit") or "") or None
    row = store.create_baseline("KNOWN_GOOD", desc, payload,
                                source_commit=source_commit)
    return {"baseline": {k: row[k] for k in
                         ("id", "kind", "content_hash", "schema_version",
                          "created_at", "description")}}


def _list_diff(a, b):
    ka = {json.dumps(x, sort_keys=True, default=str) for x in a or []}
    kb = {json.dumps(x, sort_keys=True, default=str) for x in b or []}
    return {"removed": len(ka - kb), "added": len(kb - ka),
            "changed": None if ka == kb else True}


def _apply_baseline_payload(payload: dict, actor: str, diff_preview: bool):
    """Diff + (optionally) apply a baseline payload through revisions.
    Config goes through the draft→validate→apply pipeline; canonical mappings
    and pool policies go through their audited setters. Never raw DB writes."""
    from . import revisions as rev
    cur = _baseline_payload()
    report = {"config_diff": rev.diff_configs(cur.get("config") or {},
                                              payload.get("config") or {}),
              "providers_diff": _list_diff(cur.get("providers"), payload.get("providers")),
              "mappings_added": [], "mappings_removed": [], "mappings_changed": [],
              "pool_diff": _list_diff(cur.get("model_pool"), payload.get("model_pool")),
              "applied": []}

    def _key(m):
        return (m.get("provider"), m.get("provider_model_id"))

    cur_map = {_key(m): (m.get("canonical") or "")
               for m in cur.get("canonical_mappings") or []}
    want_map = {_key(m): (m.get("canonical") or "")
                for m in payload.get("canonical_mappings") or []}
    for k, c in want_map.items():
        if k not in cur_map:
            report["mappings_added"].append(f"{k[0]}:{k[1]}")
        elif cur_map[k] != c:
            report["mappings_changed"].append(
                f"{k[0]}:{k[1]}: {cur_map[k] or '—'} → {c or '—'}")
    for k, c in cur_map.items():
        if k not in want_map:
            report["mappings_removed"].append(f"{k[0]}:{k[1]}")
    if diff_preview:
        return report
    # apply: config via revision pipeline
    cfg = payload.get("config")
    if cfg and cfg != cur.get("config"):
        draft = rev.create_draft(cfg, actor=actor, reason="baseline restore")
        out = rev.apply_revision(draft["revision_id"], actor=actor, force=True)
        report["applied"].append({"config_revision": draft["revision_id"],
                                  "applied": bool(out.get("applied"))})
    # providers (audited setters)
    for p in payload.get("providers") or []:
        cur_p = next((x for x in cur.get("providers") or [] if x.get("name") == p.get("name")), None)
        if cur_p is None:
            store.upsert_provider(p["name"], p.get("adapter_type") or "openai",
                                  p.get("base_url"), p.get("secret_ref"),
                                  enabled=p.get("enabled", True),
                                  catalog_refresh_interval_s=p.get("catalog_refresh_interval_s"),
                                  min_discount_override=p.get("min_discount_override"),
                                  health_policy=p.get("health_policy"))
            report["applied"].append({"provider_created": p.get("name")})
        elif cur_p.get("enabled") != p.get("enabled") or \
                cur_p.get("min_discount_override") != p.get("min_discount_override"):
            store.upsert_provider(p.get("name"), p.get("adapter_type") or cur_p.get("adapter_type"),
                                  p.get("base_url") or cur_p.get("base_url"),
                                  p.get("secret_ref") or cur_p.get("secret_ref"),
                                  enabled=p.get("enabled", True),
                                  catalog_refresh_interval_s=p.get("catalog_refresh_interval_s"),
                                  min_discount_override=p.get("min_discount_override"),
                                  health_policy=p.get("health_policy") or {})
            report["applied"].append({"provider_updated": p.get("name")})
    # canonical mappings (audited setters): added + changed
    for m in payload.get("canonical_mappings") or []:
        key = f"{m.get('provider')}:{m.get('provider_model_id')}"
        if key in report["mappings_added"] or any(key in ch for ch in report["mappings_changed"]):
            store.set_canonical(m.get("provider"), m.get("provider_model_id"),
                                m.get("canonical"), source=m.get("source") or "manual")
            if key not in report["mappings_added"]:
                report["applied"].append({"mapping_changed": key})
    for key in report["mappings_removed"]:
        p, pid = key.split(":", 1)
        store.set_canonical(p, pid, None)
    # model pool
    for pol in payload.get("model_pool") or []:
        store.set_pool_policy(pol.get("canonical"), pol)
    return report


@router.get("/baselines/{bid}/diff")
async def baseline_diff(bid: str, request: Request):
    """Сравнить текущее состояние с baseline (без применения)."""
    _check_auth(request)
    bl = store.get_baseline(bid)
    if bl is None:
        raise HTTPException(status_code=404, detail="baseline not found")
    return {"baseline": {k: bl[k] for k in ("id", "kind", "created_at", "description")},
            "diff": _apply_baseline_payload(bl["payload"], _actor(request), True)}


@router.post("/baselines/{bid}/restore")
async def baseline_restore(bid: str, request: Request):
    """Восстановить baseline. Клиент показывает diff ДО вызова; применение
    идёт через ревизии и audited setters."""
    _check_auth(request)
    bl = store.get_baseline(bid)
    if bl is None:
        raise HTTPException(status_code=404, detail="baseline not found")
    report = _apply_baseline_payload(bl["payload"], _actor(request), False)
    report["restored_from"] = bid
    return report


# ── R12: unmatched inbox v2 (search/group/filters) ─────────────────────────

_FAMILY_PATTERNS = [
    ("Claude", ("claude",)), ("GPT", ("gpt", "openai", "o1", "o3", "o4")),
    ("Gemini", ("gemini",)), ("DeepSeek", ("deepseek",)),
    ("Kimi", ("kimi",)), ("Qwen", ("qwen",)), ("GLM", ("glm",)),
    ("Grok", ("grok",)), ("Llama", ("llama",)), ("Mistral", ("mistral",)),
]


def _family(name: str) -> str:
    n = (name or "").lower()
    for fam, keys in _FAMILY_PATTERNS:
        if any(k in n for k in keys):
            return fam
    return "Прочие"


@router.get("/unmatched")
async def unmatched_v2(request: Request, q: str = "", family: str = "",
                       provider: str = ""):
    """R12 §20: unmatched inbox with search + family/provider grouping."""
    _check_auth(request)
    from . import inventory
    inv_data = inventory.build_inventory()
    items = inv_data["unmatched"]
    if q:
        ql = q.lower()
        items = [u for u in items
                 if ql in (u.get("provider_model_id") or "").lower()
                 or ql in (u.get("display_name") or "").lower()]
    if provider:
        items = [u for u in items if u.get("provider") == provider]
    for u in items:
        u["family"] = _family(u.get("display_name") or u.get("provider_model_id") or "")
    if family:
        items = [u for u in items if u["family"] == family]
    fam_counts: dict[str, int] = {}
    for u in inv_data["unmatched"]:
        f = _family(u.get("display_name") or u.get("provider_model_id") or "")
        fam_counts[f] = fam_counts.get(f, 0) + 1
    prov_counts: dict[str, int] = {}
    for u in inv_data["unmatched"]:
        prov_counts[u.get("provider") or "?"] = prov_counts.get(u.get("provider") or "?", 0) + 1
    return {"unmatched": items, "total": len(inv_data["unmatched"]),
            "shown": len(items),
            "family_counts": dict(sorted(fam_counts.items(), key=lambda kv: -kv[1])),
            "provider_counts": prov_counts}


@router.get("/search/catalog")
async def search_catalog(request: Request, q: str = ""):
    """R12 §7 wizard step 1: search across ALL provider catalogs (matched +
    unmatched); step-2 variants included per result."""
    _check_auth(request)
    from . import inventory
    ql = (q or "").lower().strip()
    if len(ql) < 2:
        return {"results": [], "query": q}
    inv_data = inventory.build_inventory()
    results = []
    for g in inv_data["models"]:
        hay = f"{g['display_name']} {g['canonical']}".lower()
        if ql not in hay:
            continue
        variants = []
        for r in g.get("routes", []):
            variants.append({
                "provider": r["provider"], "provider_model_id": r["provider_model_id"],
                "official_input": r.get("official_input"),
                "official_output": r.get("official_output"),
                "best_input": r.get("best_input"), "best_output": r.get("best_output"),
                "discount_pct": r.get("discount_pct"),
                "context": r.get("context"), "market_active": r.get("market_active"),
            })
        results.append({"canonical": g["canonical"], "display_name": g["display_name"],
                        "in_pool": g.get("in_pool"), "providers": sorted(set(g["providers"])),
                        "context_max": g.get("context_max"),
                        "official_input": g.get("official_input"),
                        "official_output": g.get("official_output"),
                        "best_input": g.get("best_input"),
                        "best_output": g.get("best_output"),
                        "discount_pct": g.get("discount_pct"),
                        "variants": variants})
    for u in inv_data["unmatched"]:
        hay = f"{u.get('display_name','')} {u.get('provider_model_id','')}".lower()
        if ql in hay:
            results.append({"canonical": None,
                            "display_name": u.get("display_name") or u.get("provider_model_id"),
                            "in_pool": False,
                            "providers": [u["provider"]],
                            "context_max": u.get("context"),
                            "official_input": u.get("official_input"),
                            "official_output": u.get("official_output"),
                            "best_input": u.get("best_input"),
                            "best_output": u.get("best_output"),
                            "discount_pct": u.get("discount_pct"),
                            "variants": [{
                                "provider": u["provider"],
                                "provider_model_id": u["provider_model_id"],
                                "official_input": u.get("official_input"),
                                "official_output": u.get("official_output"),
                                "best_input": u.get("best_input"),
                                "best_output": u.get("best_output"),
                                "discount_pct": u.get("discount_pct"),
                                "context": u.get("context"),
                                "market_active": u.get("market_active"),
                                "unmatched": True}]})
    results.sort(key=lambda r: (r["canonical"] is None, -(r.get("discount_pct") or 0)))
    return {"results": results[:40], "query": q}



# ── R15.3: matching assistant — canonical is selected, not typed ─────────
def _match_norm(value: str) -> str:
    """Safe matching key: keeps semantic version digits; removes only provider
    prefixes, separators and harmless punctuation."""
    import re
    x = (value or '').lower().strip()
    x = re.sub(r'^[a-z0-9_-]+/', '', x)
    return re.sub(r'[^a-z0-9]+', '', x)


def _matching_catalog(q: str = '') -> dict:
    from . import inventory
    inv = inventory.build_inventory()
    existing = []
    for g in inv.get('models', []):
        hay = ' '.join([g.get('display_name') or '', g.get('canonical') or ''])
        if q and _match_norm(q) not in _match_norm(hay):
            continue
        existing.append({
            'canonical': g.get('canonical'), 'display_name': g.get('display_name'),
            'providers': sorted(set(g.get('providers') or [])),
            'provider_model_ids': sorted(set(r.get('provider_model_id') or '' for r in g.get('routes') or [])),
            'context_min': g.get('context_min'), 'context_max': g.get('context_max'),
            'best_input': g.get('best_input'), 'best_output': g.get('best_output'),
            'in_pool': bool(g.get('in_pool')), 'routes': g.get('routes') or [],
        })
    return {'existing': existing[:100], 'query': q}


@router.get('/matching/suggest')
async def matching_suggest(request: Request, provider: str = '', provider_model_id: str = '', q: str = ''):
    _check_auth(request)
    from . import inventory
    inv = inventory.build_inventory()
    raw = q or provider_model_id
    candidates = _matching_catalog(raw).get('existing', [])
    exact_key = _match_norm(raw)
    exact = [x for x in candidates if _match_norm(x.get('display_name','')) == exact_key
             or _match_norm(x.get('canonical','')) == exact_key
             or exact_key in {_match_norm(a) for r in x.get('routes',[]) for a in [r.get('provider_model_id','')]}]
    suggestions = [x for x in candidates if x not in exact][:10]
    return {'provider': provider, 'provider_model_id': provider_model_id,
            'display_name': next((u.get('display_name') for u in inv.get('unmatched', [])
                                  if u.get('provider') == provider and u.get('provider_model_id') == provider_model_id), raw),
            'exact_safe': exact[:10], 'suggestions': suggestions,
            'confidence': 'Высокая' if exact else ('Средняя' if suggestions else 'Низкая')}


@router.get('/matching/duplicates')
async def matching_duplicates(request: Request):
    _check_auth(request)
    from . import inventory
    groups = {}
    for g in inventory.build_inventory().get('models', []):
        key = _match_norm(g.get('display_name') or g.get('canonical') or '')
        if not key or not g.get('canonical'):
            continue
        groups.setdefault(key, []).append(g)
    out=[]
    for key, items in groups.items():
        if len(items) < 2 or not key: continue
        for a,b in zip(items, items[1:]):
            out.append({'canonical_a':a.get('canonical'),'canonical_b':b.get('canonical'),
                        'routes_a':a.get('routes',[]),'routes_b':b.get('routes',[]),
                        'providers_a':a.get('providers',[]),'providers_b':b.get('providers',[]),
                        'reason':'одинаковое нормализованное название; требуется ручная проверка'})
    # junk mappings: empty provider_model_id or empty canonical break grouping
    for m in store.list_canonical_map():
        pid = (m.get('provider_model_id') or '').strip()
        canon = (m.get('canonical') or '').strip()
        if not pid or not canon:
            out.append({'canonical_a': canon or '(пусто)', 'canonical_b': m.get('provider')+':'+(pid or '(пусто)'),
                        'routes_a': [], 'routes_b': [m],
                        'providers_a': [m.get('provider')], 'providers_b': [m.get('provider')],
                        'reason': ('пустой provider model id' if not pid else 'пустой canonical')
                                  + ' — битая запись сопоставления, требует ручной чистки'})
    return {'possible_duplicates':out}


@router.post('/matching/attach')
async def matching_attach(request: Request):
    _check_auth(request); body=await request.json()
    provider, pid, canonical = body.get('provider'), body.get('provider_model_id'), body.get('canonical')
    if not provider or not pid or not canonical: raise HTTPException(422, 'provider, provider_model_id and existing canonical required')
    if not any(x.get('canonical') == canonical for x in _matching_catalog().get('existing', [])):
        raise HTTPException(422, 'canonical must be selected from existing models')
    ok=store.set_canonical(provider,pid,canonical,source='exact_match_confirmed')
    return {'ok':ok,'canonical':canonical,'route_attached':ok}

# ── R12: dashboard semantic counters (routes vs offers) ────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary(request: Request):
    """R12 §18: semantic counters. Router route = конкретный маршрут
    provider+model из registry. Market offer = рыночное предложение продавца.
    Считаются раздельно, никогда не суммируются."""
    _check_auth(request)
    from . import inventory
    inv_data = inventory.build_inventory()
    counts = inv_data["counts"]
    pool = await _runtime_get("/models/pool?include_routes=1")
    runtime_routes = 0
    canonicals = 0
    if pool["ok"]:
        models = (pool["data"].get("models") or {})
        canonicals = len(models)
        for m in models.values():
            runtime_routes += len(m.get("routes") or [])
    offers_provider_b = sum(g.get("sellers_count") or 0 for g in inv_data["models"])
    offers_provider_a = sum(g.get("offers_count") or 0 for g in inv_data["models"])
    # §3/§18: seller sums per-model double-count the same seller across
    # models when identity is unknown — label honestly, never as "продавцов".
    offers_provider_b = {"sum_sellers_across_models": offers_provider_b,
                      "identity_known": False}
    catalog: dict[str, int] = {}
    for g in inv_data["models"]:
        for p in g["providers"]:
            catalog[p] = catalog.get(p, 0) + 1
    for u in inv_data["unmatched"]:
        catalog[u["provider"]] = catalog.get(u["provider"], 0) + 1
    return {
        "models_in_pool": counts.get("in_pool", 0),
        "models_available_now": counts.get("market_active", 0),
        "models_eligible": counts.get("eligible", 0),
        "canonical_matched": counts.get("canonical_matched", 0),
        "unmatched": counts.get("unmatched", 0),
        "hidden": counts.get("hidden", 0),
        "router_routes": runtime_routes,
        "runtime_canonicals": canonicals,
        "market_offers_provider_b_sellers": offers_provider_b,
        "market_offers_provider_a_asks": offers_provider_a,
        "catalog_counts": catalog,
        "catalog_total": sum(catalog.values()),
    }


# ── R12: журнал изменений (людской формат) ────────────────────────────────

_AUDIT_RENDER = {
    "set_canonical": lambda d: ("Сопоставление обновлено: {p}:{pid} → {c}"
                                .format(p=d.get("provider"), pid=d.get("provider_model_id"),
                                        c=d.get("canonical") or "(без сопоставления)")),
    "pool_policy": lambda d: "Изменена политика модели {c}".format(c=d.get("canonical")),
    "apply": lambda d: "Применена конфигурация (ревизия {r})".format(r=d.get("revision_id", "")),
    "baseline.create": lambda d: "Создана точка восстановления",
    "canonical_map.set": lambda d: "Сопоставление модели: → {c}".format(c=d.get("canonical") or "?"),
    "canonical_map.delete": lambda d: "Удалено сопоставление модели",
    "model_pool.set": lambda d: "Изменена политика модели {c}".format(c=d.get("canonical")),
    "model_policy.set": lambda d: "Изменён статус модели {c}".format(c=d.get("canonical")),
    "provider.upsert": lambda d: "Сохранён провайдер",
    "provider.enable": lambda d: "Провайдер включён",
    "provider.disable": lambda d: "Провайдер отключён",
    "provider.archive": lambda d: "Провайдер архивирован",
    "discovery.refresh": lambda d: "Обновлены каталоги провайдеров",
    "market.refresh": lambda d: ("Обновлены цены рынка"
        + (f" · изменились у {d.get('changed')}" if d.get("changed") else "")),
    "catalog.refresh": lambda d: ("Обновлён каталог/цены runtime"
        + (f" · {d.get('models')} моделей" if d.get("models") is not None else "")),
    "availability.check": lambda d: ("Проверка доступности"
        + (f" · {d.get('ok')}/{d.get('total')} доступны" if d.get("total") else "")),
    "override.create": lambda d: "Создано временное правило"
        + ((" · без TTL (постоянное)" if d.get("persistent")
            else f" · TTL {round((d.get('ttl_s') or 0)/60)} мин")
           if d.get("ttl_s") or d.get("persistent") else ""),
    "override.disable": lambda d: "Удалено временное правило",
    "revision.create": lambda d: "Черновик конфигурации",
    "revision.apply": lambda d: "Применена конфигурация",
    "revision.rollback": lambda d: "Откат конфигурации",
    "baseline.known_good": lambda d: "Назначена основная рабочая точка",
    "baseline.delete": lambda d: "Удалена точка восстановления",
    "baseline.restore": lambda d: "Восстановление из точки",
}


@router.get("/audit")
async def audit_list(request: Request, limit: int = 200):
    """R12 §27: журнал изменений в человеческом формате; raw JSON — только
    'Для разработчика' (diagnostics mode в UI)."""
    _check_auth(request)
    rows = store.list_audit(limit)
    out = []
    for r in rows:
        d = r.get("fields") or {}
        render = _AUDIT_RENDER.get(r.get("action"))
        out.append({
            "ts": r.get("ts"),
            "iso": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(r.get("ts") or 0)),
            "actor": r.get("actor"),
            "action": r.get("action"),
            "entity": r.get("entity") or "",
            "text": render(d) if render else
                    ((r.get("action") or "") + ((" · " + r.get("entity")) if r.get("entity") else "")),
            "data": d,
        })
    return {"audit": out}


# ── R12: per-model policy with inheritance display ─────────────────────────

@router.get("/policy/inheritance/{canonical}")
async def policy_inheritance(canonical: str, request: Request):
    """R12 §22/§24: effective policy + inherited vs overridden."""
    _check_auth(request)
    from . import inventory
    pol = store.get_pool_policy(canonical)
    g_min = inventory._global_min_discount()
    inv_data = inventory.build_inventory()
    g = next((m for m in inv_data["models"] if m["canonical"] == canonical), None)
    market_disc = g.get("discount_pct") if g else None
    eff_min = pol.get("min_discount_override")
    value = eff_min if eff_min is not None else g_min
    return {
        "canonical": canonical,
        "policy": pol,
        "global_min_discount": g_min,
        "min_discount": {
            "value": value,
            "source": "override" if eff_min is not None else "inherit",
            "overridden": eff_min is not None,
        },
        "market_discount_now": market_disc,
        "fits": (market_disc is not None and market_disc >= round(value * 100)),
    }


# ── R12: model lifecycle actions (no single destructive delete) ───────────

@router.post("/model-lifecycle/{canonical}")
async def model_lifecycle(request: Request, canonical: str):
    """R12 §8: action = remove_auto | hide | unhide | disable | enable |
    restore | unmap. Все действия обратимы и попадают в журнал."""
    _check_auth(request)
    body = await request.json()
    action = str(body.get("action") or "")
    allowed = {"remove_auto", "hide", "unhide", "disable", "enable", "restore", "unmap"}
    if action not in allowed:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(allowed)}")
    if action == "remove_auto":
        pol = store.set_pool_policy(canonical, {"in_pool": False})
    elif action == "restore":
        pol = store.set_pool_policy(canonical, {"in_pool": True, "hidden": False,
                                                "auto_routing": True})
    elif action == "hide":
        pol = store.set_pool_policy(canonical, {"hidden": True})
    elif action == "unhide":
        pol = store.set_pool_policy(canonical, {"hidden": False})
    elif action == "disable":
        pol = store.set_pool_policy(canonical, {"in_pool": False, "hidden": False,
                                                "auto_routing": False})
    elif action == "enable":
        pol = store.set_pool_policy(canonical, {"in_pool": True, "auto_routing": True})
    elif action == "unmap":
        removed = []
        for m in store.list_canonical_map():
            if m["canonical"] == canonical and m.get("source") in ("manual", "automatch"):
                store.set_canonical(m["provider"], m["provider_model_id"], None)
                removed.append(f"{m['provider']}:{m['provider_model_id']}")
        return {"ok": True, "action": action, "removed_mappings": removed}
    return {"ok": True, "action": action, "policy": pol}
