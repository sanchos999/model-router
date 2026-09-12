"""Availability probing (UI spec §F/§G) — control-plane side.

Safe model/provider availability checks performed BY the backend (the UI
never calls provider APIs directly):

  1. provider reachable          — GET {base}/models (or adapter health)
  2. model exists at provider    — slug present in the discovery result
  3. auth works                  — covered by 1 (401/403 -> auth error)
  4. endpoint accepts requests   — minimal chat completion
  5. capability compatible       — response is a valid chat object
  6. minimal inference probe     — user: "Reply exactly: OK", tiny max_tokens
  7. latency
  8. error, if any

Cheap-first policy: discovery/models endpoint first (free). A paid
completion probe is ONLY issued when explicitly requested
(probe="inference") or when the free check passes and deep=true — the UI
uses free checks by default and inference probes on explicit user click.

Results are stored as runtime health evidence in control.db
(availability_checks) with timestamps. Probing NEVER mutates policy.
"""
from __future__ import annotations

import asyncio
import os
import time

from . import store


class _ProbeTimeout(Exception):
    pass


def _provider_runtime_cfg(name: str) -> dict | None:
    """Runtime provider settings (base_url/secret_ref/adapter) merged from
    the control-DB row and the active config."""
    row = store.get_provider(name)
    cfg = {}
    try:
        import json as _json
        from . import revisions as _rev
        active, _ = store.get_active_config()
        cfg = (active or _rev.active_config() or {}).get("providers", {}).get(name) or {}
    except Exception:
        cfg = {}
    if row:
        return {
            "name": name,
            "adapter_type": row.get("adapter_type") or "",
            "base_url": row.get("base_url") or cfg.get("base_url"),
            "secret_ref": row.get("secret_ref") or cfg.get("api_key_env"),
            "min_discount": cfg.get("min_discount"),
        }
    if not cfg:
        cfg = {}
    # FALLBACK: active config has no base_url (adapters take theirs from ENV).
    # Use the same adapter defaults the runtime process uses so probes hit
    # the real endpoints even without a control-DB provider row.
    base = cfg.get("base_url") or _adapter_default_base(name)
    if not base:
        return None
    atype = "provider_b" if name == "provider_b" else "provider_a" if name == "provider_a" else "openai-compatible"
    return {"name": name, "adapter_type": atype,
            "base_url": base, "secret_ref": cfg.get("api_key_env"),
            "min_discount": cfg.get("min_discount")}


def _adapter_default_base(name: str) -> str | None:
    """Same defaults as the runtime adapters (ENV-overridable)."""
    if name == "provider_a":
        return os.environ.get("HERMES_ROUTER_PROVIDER_A_BASE", "https://provider-a.example/v1")
    if name == "provider_b":
        return os.environ.get("HERMES_ROUTER_PROVIDER_B_BASE", "https://provider-b.example/min80/v1")
    return None


async def _runtime_routes() -> list[dict]:
    """All routes from the runtime router (:4100) /models/pool?include_routes=1.

    The control process keeps its own registry instance that is never built
    (no FastAPI startup hook here), so ALL route listings must come from the
    runtime via HTTP — the same source of truth the Admin UI pool page uses.
    Returns [] when the runtime is unreachable (callers surface the error)."""
    import httpx
    base = os.environ.get("GW_RUNTIME_URL", "http://127.0.0.1:4100")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30.0) as c:
            r = await c.get("/models/pool", params={"include_routes": 1})
        if r.status_code != 200:
            return []
        data = r.json().get("models") or {}
        out = []
        for m in data.values():
            for rt in m.get("routes") or []:
                item = dict(rt)
                item["canonical"] = m["canonical"]
                out.append(item)
        return out
    except Exception:
        return []


async def provider_routes(provider: str) -> list[dict]:
    """Eligible+known routes for a provider (from the runtime router)."""
    out = []
    for r in await _runtime_routes():
        if r["provider"] == provider:
            out.append({"provider": r["provider"],
                        "provider_model_id": r["provider_model_id"],
                        "canonical": r["canonical"],
                        "price_state": r["price_state"],
                        "input_price": r["input_price"],
                        "output_price": r["output_price"],
                        "discount": r["discount"],
                        "eligible": r.get("eligible", False)})
    return out


async def _http(method: str, url: str, *, headers: dict | None = None,
                json_body: dict | None = None, timeout_s: float = 10.0):
    import httpx
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        return await c.request(method, url, headers=headers or {}, json=json_body)


def _api_key(secret_ref: str | None) -> tuple[str | None, str]:
    if not secret_ref:
        return None, "no secret_ref configured"
    val = os.environ.get(secret_ref)
    if not val:
        return None, f"env {secret_ref} is not set"
    return val, ""


# Default secret ENV names per provider (same as the runtime adapters).
_DEFAULT_SECRET_ENV = {
    "provider_a": "PROVIDER_A_API_KEY",
    "provider_b": "PROVIDER_B_API_KEY",
}


def _err_code(status: int | None, exc: Exception | None) -> str:
    if exc is not None:
        name = type(exc).__name__
        if "Timeout" in name:
            return "timeout"
        if "Connect" in name:
            return "connection_failed"
        return "network_error"
    if status in (401, 403):
        return "auth_error"
    if status == 404:
        return "model_not_found"
    if status == 402:
        return "payment_required"
    if status == 429:
        return "rate_limit"
    if status and 500 <= status < 600:
        return "server_error"
    return f"http_{status}"


async def probe_route(provider: str, provider_model_id: str,
                      *, deep: bool = False) -> dict:
    """Probe one (provider, slug) route. Returns a check-result dict —
    stored as evidence, never applied as policy."""
    t0 = time.monotonic()
    cfg = _provider_runtime_cfg(provider)
    if cfg is None:
        return {"ok": False, "provider": provider,
                "provider_model_id": provider_model_id,
                "error_code": "provider_unknown", "error": "provider not configured",
                "latency_ms": 0, "checked_at": time.time(), "probe": "none"}
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "provider": provider,
                "provider_model_id": provider_model_id,
                "error_code": "base_url_missing", "error": "base_url not configured",
                "latency_ms": 0, "checked_at": time.time(), "probe": "none"}
    secret_ref = cfg.get("secret_ref") or _DEFAULT_SECRET_ENV.get(provider)
    key, kerr = _api_key(secret_ref)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    result = {"provider": provider, "provider_model_id": provider_model_id,
              "probe": "models", "checked_at": time.time(), "ok": False}

    # Stage 1 (free): discovery — reachability, auth, model existence.
    exc: Exception | None = None
    status: int | None = None
    models_found: int | None = None
    model_present: bool | None = None
    try:
        r = await _http("GET", base + "/models", headers=headers, timeout_s=10.0)
        status = r.status_code
        if status == 200:
            try:
                data = r.json().get("data") or []
                models_found = len(data)
                ids = {m.get("id") for m in data if isinstance(m, dict)}
                model_present = provider_model_id in ids
            except Exception:
                models_found = None
    except Exception as e:  # noqa: BLE001
        exc = e
    result["http_status"] = status
    result["latency_ms"] = int((time.monotonic() - t0) * 1000)
    if exc is not None or status != 200:
        result["error_code"] = _err_code(status, exc)
        result["error"] = (f"{type(exc).__name__}: {exc}" if exc
                           else f"HTTP {status}")
        store.record_availability(result)
        return result
    result["models_found"] = models_found
    if model_present is False:
        # some marketplaces hide non-purchased models from /models but still
        # serve them. Catalog absence alone is not proof of unavailability,
        # BUT an unknown-to-registry model that is also absent from the
        # provider catalog is genuinely unavailable (404 on inference would
        # be certain) — report it as such instead of ok=true.
        result["model_in_catalog"] = False
        known = any(r["provider"] == provider and
                    r["provider_model_id"] == provider_model_id
                    for r in await _runtime_routes())
        if not known:
            result["ok"] = False
            result["error_code"] = "model_not_found"
            result["error"] = "model absent from provider catalog and router registry"
            store.record_availability(result)
            return result
    else:
        result["model_in_catalog"] = True

    # Stage 2 (cheap paid probe, only when deep): minimal completion.
    if deep:
        t1 = time.monotonic()
        result["probe"] = "inference"
        try:
            r2 = await _http(
                "POST", base + "/chat/completions", headers=headers,
                json_body={"model": provider_model_id,
                           "messages": [{"role": "user",
                                         "content": "Reply exactly: OK"}],
                           "max_tokens": 16, "stream": False},
                timeout_s=20.0)
            result["http_status"] = r2.status_code
            if r2.status_code == 200:
                body = r2.json()
                content = ((body.get("choices") or [{}])[0]
                           .get("message", {}).get("content") or "")
                result["ok"] = isinstance(content, str)
                result["reply"] = content[:40]
                if not result["ok"]:
                    result["error_code"] = "capability_incompatible"
                    result["error"] = "no message.content in response"
            else:
                result["error_code"] = _err_code(r2.status_code, None)
                result["error"] = f"HTTP {r2.status_code}"
        except Exception as e:  # noqa: BLE001
            result["error_code"] = _err_code(None, e)
            result["error"] = f"{type(e).__name__}: {e}"
        result["latency_ms"] = int((time.monotonic() - t1) * 1000)
    else:
        # free check passed: reachable + auth OK (+ catalog presence)
        result["ok"] = True
        result["error_code"] = None
        result["error"] = None
    store.record_availability(result)
    return result


async def probe_provider(provider: str, *, deep: bool = False,
                         limit: int | None = None) -> dict:
    """Probe every known route of a provider (catalog check by default).
    Bounded concurrency; per-route results + summary."""
    routes = await provider_routes(provider)
    if limit:
        routes = routes[:limit]
    sem = asyncio.Semaphore(4)

    async def one(rt):
        async with sem:
            return await probe_route(provider, rt["provider_model_id"], deep=deep)

    results = await asyncio.gather(*[one(rt) for rt in routes])
    ok = sum(1 for r in results if r.get("ok"))
    return {"provider": provider, "total": len(results), "ok": ok,
            "failed": len(results) - ok, "checked_at": time.time(),
            "results": results}


async def stale_models(older_than_s: float = 86400.0) -> list[dict]:
    """Models with no availability evidence newer than older_than_s."""
    out = []
    for r in await _runtime_routes():
        last = store.last_availability(r["provider"], r["provider_model_id"])
        if last is None or (time.time() - last["checked_at"]) > older_than_s:
            out.append({"canonical": r["canonical"], "provider": r["provider"],
                        "provider_model_id": r["provider_model_id"],
                        "last_checked_at": last["checked_at"] if last else None})
    return out
