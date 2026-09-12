"""V2 FastAPI app — Gateway V2 on 127.0.0.1:4101.

Strictly isolated from main.py (:4100). The two apps use independent processes;
:4100`` is unchanged and serves the production router. ``4101`` is the
G2 dynamic router canary.

[OI]-compatible surface only:
  - GET  /health
  - GET  /v1/models                       — registry snapshot (debug)
  - GET  /v1/registry                     — same as /v1/models
  - POST /v1/chat/completions             — the only inference endpoint

G2 dynamic-routing surface (privacy-safe, no prompts/keys/content):
  - GET  /router/decision                  — last routing decision trace
  - POST /router/simulate                  — offline routing simulation
  - GET  /provider/share                   — provider-level share metrics
  - GET  /metrics                          — full model/route metrics
  - POST /registry/refresh                 — dynamic catalog re-discovery

Headers preserved: x-hermes-taCHANGE_ME, x-hermes-taCHANGE_ME, prompt_cache_key.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import time
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .canonical import CanonicalRegistry
from .classifier import capabilities_for_class, classify, tier_for_class
from .config import load_config, write_default_config
from .metrics import Metrics
from .policy import get_canonical, lookup_canonical
from .providers.provider_a import ProviderAAdapter
from .providers.provider_b import ProviderBAdapter
from .registry import DISCOUNT_FLOOR, RouteRegistry
from .selector import PlanStep, SelectionContext, Selector, safe_context
from .timeouts import TimeoutPolicy, load_policy, total_for
from .transport import execute_plan, stream_plan
from .context_manager import get_context_manager, route_safe_context
from .lifecycle import LifecycleRegistry
from .model_pool import ModelPool, POOL_REGISTRY_PATH, POOL_ALIASES_PATH
from .version import version_payload, instance_id

# R6 §B: control plane is OPTIONAL — its failure must never break inference.
try:
    from .control import integration as _control
    _CONTROL_OK = True
except Exception as _e:  # pragma: no cover
    print(f"[gateway-v2] control plane disabled: {_e!r}")
    _CONTROL_OK = False



LISTEN_HOST = os.environ.get("GATEWAY_V2_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GATEWAY_V2_PORT", "4101"))

REFRESH_INTERVAL_S = float(os.environ.get("GW_REFRESH_INTERVAL_S", "120"))


def _build_app() -> tuple[FastAPI, RouteRegistry, Selector, TimeoutPolicy, CanonicalRegistry, Metrics]:
    write_default_config()
    app = FastAPI(title="hermes-gateway-v2", docs_url=None, redoc_url=None)
    cfg = load_config()
    registry = RouteRegistry(config=cfg)
    canon_registry = CanonicalRegistry()
    canon_registry.rebuild()
    policy = load_policy()

    registry.register_adapter(ProviderAAdapter())
    registry.register_adapter(ProviderBAdapter())
    metrics = Metrics()
    lifecycle = LifecycleRegistry()
    selector = Selector(registry, canon_registry, lifecycle)
    model_pool = ModelPool(registry, canon_registry, lifecycle)
    if _CONTROL_OK:
        try:
            _control.install()
            from .control import admin_api as _admin_api
            _admin_api.bind_dashboard_metrics(metrics)
        except Exception as e:
            print(f"[gateway-v2] control plane install failed: {e!r}")
    return app, registry, selector, policy, canon_registry, metrics, model_pool


app, _registry, _selector, _policy, _canon, _metrics, _pool = _build_app()
_ctx = get_context_manager()
START_TIME = time.time()
_last_decision: dict = {}


async def _periodic_refresh() -> None:
    """Background dynamic discovery + health TTL decay. No probe storm:
    one catalog refresh per interval, TTL is pure time-based."""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_S)
        try:
            _registry.apply_health_ttl()
            await _registry.build(_registry.adapters())
            _canon.rebuild()
            _pool.build()
            _ctx.cleanup_expired()
            if _CONTROL_OK:
                try:
                    _control.cleanup_expired_overrides()
                except Exception:
                    pass
        except Exception as e:
            print(f"[gateway-v2] refresh failed: {e!r}")


@app.on_event("startup")
async def _on_startup() -> None:
    await _registry.build(_registry.adapters())
    _canon.rebuild()
    _pool.build()
    asyncio.get_event_loop().create_task(_periodic_refresh())


@app.get("/health")
async def health() -> dict:
    stats = _registry.stats()
    return {
        "ok": True,
        "service": "model-router",
        "instance_id": instance_id(),
        "port": LISTEN_PORT,
        "started_at": START_TIME,
        "uptime_s": round(time.time() - START_TIME, 2),
        "registry": stats,
        "policy": {
            "connect_s": _policy.connect_s,
            "first_response_s": _policy.first_response_s,
            "stream_idle_s": _policy.stream_idle_s,
            "normal_total_s": _policy.normal_total_s,
            "t4_total_s": _policy.t4_total_s,
        },
        "discount_floor": DISCOUNT_FLOOR,
    }


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": list(_registry.snapshot().values())}


@app.get("/v1/registry")
async def registry_view() -> dict:
    return _registry.snapshot()


def _session_key_from(body: dict, headers) -> str:
    if isinstance(body, dict):
        if body.get("prompt_cache_key"):
            return str(body["prompt_cache_key"])
        eb = body.get("extra_body")
        if isinstance(eb, dict) and eb.get("prompt_cache_key"):
            return str(eb["prompt_cache_key"])
    for h in ("x-hermes-session-id", "x-cache-scope-id", "x-session-id"):
        v = headers.get(h)
        if v:
            return str(v)
    model = body.get("model", "unknown") if isinstance(body, dict) else "unknown"
    return "default:" + hashlib.sha256(model.encode()).hexdigest()[:16]


# R8.1C: client-facing provider aliases for <alias>:<slug> / <alias>/<slug>
# forms. Resolution-only (no ranking bias): normalised to the canonical
# provider names used in CANONICAL_MAPPING.
_PROVIDER_ALIASES = {
    "sp": "provider_b",
    "provider_b": "provider_b",
    "ih": "provider_a",
    "provider_a": "provider_a",
}


def _canonical_from_alias_or_mapping(body: dict) -> tuple[str | None, str | None]:
    """Resolve canonical + provider hint from a model alias or raw slug."""
    if not isinstance(body, dict):
        return None, None
    model = body.get("model")
    if not model:
        return None, None

    # Direct canonical name (e.g. "gpt-5.6-luna") = both providers eligible.
    cm = get_canonical(model)
    if cm is not None:
        return cm.canonical, None

    # Logical alias (e.g. "compression-auto" -> gpt-5.6-luna, G3.1 contract).
    from .policy import MODEL_ALIASES
    aliased = MODEL_ALIASES.get(str(model))
    if aliased:
        cm = get_canonical(aliased)
        if cm is not None:
            return cm.canonical, None

    # R5: main-auto = selector-level routing (no fixed canonical, no provider
    # preference). LEVEL A picks canonical from the evidence registry.
    if str(model) == "main-auto":
        return None, None

    # R8.1C: provider-aliased forms "<alias>:<slug>" and "<alias>/<slug>".
    # A canonical alias behind a provider prefix must NOT become unknown_model.
    raw = body.get("model") if isinstance(body, dict) else None
    if isinstance(raw, str):
        sep = ":" if ":" in raw else ("/" if "/" in raw else "")
        if sep:
            prefix, slug = raw.split(sep, 1)
            provider_norm = _PROVIDER_ALIASES.get(prefix.strip().lower())
            if provider_norm and slug:
                # Exact provider slug first...
                canonical_hint = lookup_canonical(provider_norm, slug)
                if canonical_hint is not None:
                    return canonical_hint, provider_norm
                # ...then canonical name behind the prefix.
                cm = get_canonical(slug)
                if cm is not None:
                    return cm.canonical, provider_norm

    # Raw provider slug -> map to canonical if known.
    for provider in ("provider_a", "provider_b"):
        c = lookup_canonical(provider, model)
        if c is not None:
            return c, provider
    return None, None


def _task_class_from_header(headers, body: dict | None = None) -> str:
    for h in ("x-hermes-taCHANGE_ME", "x-taCHANGE_ME"):
        v = headers.get(h)
        if v:
            return str(v).upper()
    # No header: classify ephemeral text in-gateway (features discarded).
    if isinstance(body, dict):
        last = None
        for m in reversed(body.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "user":
                last = m.get("content")
                break
        if last:
            return classify(last)
    return "NORMAL_CODING"


def _tier_from_class(task_class: str) -> str:
    # R14 §17: taCHANGE_ME policy from the active config revision overrides
    # the code-level class→tier mapping (tier change / class disable).
    try:
        return tier_for_class(task_class, config=_policy)
    except Exception:
        return tier_for_class(task_class)


def _estimate_required_context(body: dict, header_value) -> int:
    safety = 8192
    estimated = 0
    for m in (body.get("messages") or []):
        if isinstance(m, dict):
            content = m.get("content") or ""
            estimated += max(1, len(str(content)) // 3)
    reserved = int(body.get("max_tokens") or 0)
    header = int(header_value or 0)
    return max(header, estimated + reserved + safety)


def _inference_auth_enabled() -> bool:
    """R8 §21: auth.mode=disabled (default, localhost-compatible) | bearer."""
    mode = (os.environ.get("MODEL_ROUTER_AUTH_MODE", "disabled")).strip().lower()
    return mode == "bearer"


def _check_inference_auth(request: Request) -> bool:
    if not _inference_auth_enabled():
        return True
    expected = os.environ.get("MODEL_ROUTER_PROVIDER_A_TOKEN", "")
    if not expected:
        # bearer mode without a token configured = misconfig; fail closed.
        return False
    authz = request.headers.get("authorization", "")
    return authz == f"Bearer {expected}"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _check_inference_auth(request):
        return JSONResponse(
            status_code=401,
            content={"error": {"type": "invalid_api_key",
                               "message": "Missing or invalid inference token"}},
        )
    body = await request.json()
    headers = dict(request.headers)

    canonical_hint, provider_hint = _canonical_from_alias_or_mapping(body)
    if canonical_hint is None:
        # Allow raw discovery for parity-test mode: if model == "<provider>:<slug>",
        # resolve; also allow provider-prefixed main-auto (R5 forced-provider).
        raw = body.get("model") if isinstance(body, dict) else None
        if isinstance(raw, str) and ":" in raw:
            provider_hint, slug = raw.split(":", 1)
            canonical_hint = lookup_canonical(provider_hint, slug)
            if canonical_hint is None and slug == "main-auto":
                # Forced provider on selector-level alias: LEVEL A picks canonical.
                canonical_hint = "__main_auto__"
                provider_hint = provider_hint if provider_hint in ("provider_a", "provider_b") else None
            elif canonical_hint is None:
                provider_hint = None

    # R5 §11: main-auto = selector-level routing — no hint, no 400. LEVEL A
    # picks the canonical; forced-provider requests keep provider_hint only.
    main_auto_unresolved = canonical_hint is None and str(body.get("model")) != "main-auto"
    if main_auto_unresolved:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "unknown_model", "message": f"Cannot resolve canonical for model={body.get('model')!r}"}},
        )
    if canonical_hint == "__main_auto__":
        canonical_hint = None

    task_class = _task_class_from_header(headers, body)
    tier = _tier_from_class(task_class)
    required_context = _estimate_required_context(body, headers.get("x-hermes-context-tokens"))
    reserved_output = int(body.get("max_tokens") or 1024)
    session_key = _session_key_from(body, headers)
    ctx = SelectionContext(
        canonical_hint=canonical_hint,
        provider_hint=provider_hint,
        prompt_cache_key=session_key,
        required_context=required_context,
        reserved_output=reserved_output,
        capabilities_required=capabilities_for_class(task_class),
        tier=tier,
        task_class=task_class,
    )
    # R5 §7: per-session cache state from the Context Manager (privacy-safe:
    # route key + token counts only). No session state => COLD/UNKNOWN, no
    # cache affinity, plain gate-then-cost ranking.
    _st = _ctx.get_or_create(session_key)
    ctx = dataclasses.replace(
        ctx,
        current_route_key=_st.selected_route_key or None,
        cache_state=_ctx.cache_state_for(session_key, _st.selected_route_key or "")
        if _st.selected_route_key else "UNKNOWN",
        warm_prefix_tokens=int(_st.context_tokens or 0)
        if _st.selected_route_key else 0,
    )

    # R15 §7: optional canary experiment — a SEPARATE probabilistic hint,
    # never a routing-policy change. Disabled unless kv "canary" says so.
    _canary_applied: dict = {}
    if _CONTROL_OK and not canonical_hint:
        try:
            _canary_applied = _control.apply_canary(ctx, task_class)
            if _canary_applied.get("canonical_hint"):
                ctx = dataclasses.replace(
                    ctx, canonical_hint=_canary_applied["canonical_hint"])
        except Exception as _e:
            _canary_applied = {}

    # R6 §I: apply admin overrides (FORCE_*/DISABLE_*/PREFER_PROVIDER) before
    # selection. Control-plane failure degrades to plain routing (§B).
    _ovr_summary: dict = {}
    if _CONTROL_OK:
        try:
            ctx, _ovr_summary = _control.apply_overrides_to_context(ctx)
        except Exception as _e:
            print(f"[gateway-v2] override apply failed: {_e!r}")

    primary, plan, trace = _selector.choose(ctx)
    if not plan:
        # Force a registry rebuild and retry once in case catalog is stale.
        await _registry.build(_registry.adapters())
        _registry.apply_health_ttl()
        primary, plan, trace = _selector.choose(ctx)
    _record_decision(ctx, trace, primary, plan)
    _econ = trace.get("cache_economics") or {}
    if _econ:
        try:
            _metrics.record_cache_decision(
                reason_code=str(_econ.get("reason_code") or ""),
                estimated_saving_usd=float(_econ.get("per_request_saving") or 0.0),
            )
        except Exception:
            pass
    _metrics.record_selected(canonical=canonical_hint, provider=primary.provider if primary else "none", slug=primary.provider_model_id if primary else "-")
    if not plan:
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "model_selection_unavailable", "message": f"No eligible route for canonical={canonical_hint}, tier={tier}, required_context={required_context}"}},
        )

    # Branch on stream vs non-stream.
    stream = bool(body.get("stream"))

    routing_headers = {
        "x-gateway-v2": "true",
        "x-gateway-selected-canonical": plan[0].canonical,
        "x-gateway-selected-provider": primary.provider if primary else "",
        "x-gateway-selected-slug": primary.provider_model_id if primary else "",
        "x-gateway-tier": tier,
        "x-gateway-plan-len": str(len(plan)),
        "x-gateway-safe-context": str(plan[0].safe_context_limit),
    }
    if stream:
        request_id = f"req-{time.time_ns()}"
        async def gen() -> AsyncIterator[bytes]:
            usage_seen: dict = {}
            # R7 §7D fix: UpstreamChunk.raw is raw provider SSE bytes — relay
            # them verbatim (standard [OI] SSE wire format), never json-encode.
            async for frame in stream_plan(plan=plan, registry=_registry, body=body, policy=_policy, tier=tier, request_id=request_id):
                if not isinstance(frame, dict):
                    yield bytes(frame)
                    continue
                ev = frame.get("event")
                if ev == "chunk":
                    payload = frame.get("bytes") or b""
                    if payload:
                        yield payload
                elif ev == "failover":
                    yield (b'data: {"error": {"type": "upstream_failover", "message": '
                           b'"upstream route failed, trying alternate"}}\n\n')
                elif ev == "terminal":
                    # R9: plan exhausted — emit a terminal SSE error so the
                    # client sees a structured failure instead of a silent
                    # empty [DONE]. (Decision trace is written by stream_plan.)
                    yield (b'data: {"error": {"type": "upstream_exhausted", "message": '
                           b'"all routes failed", "terminal_reason": "plan_exhausted"}}\n\n')
                elif ev == "done":
                    try:
                        _observe_and_decide(headers, body, plan[0], usage_seen, routing_headers)
                    except Exception:
                        pass
                    yield b"data: [DONE]\n\n"
                    return
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers=routing_headers)

    t0 = time.time()
    request_id = f"req-{time.time_ns()}"
    try:
        step, failures, meta = await execute_plan(plan=plan, registry=_registry, body=body, stream=False, policy=_policy, tier=tier)
    except Exception as e:
        _metrics.record_attempt(
            canonical=canonical_hint, provider="none", slug="-",
            error_code="plan_exhausted", latency_ms=(time.time() - t0) * 1000.0,
        )
        from .transport import _write_failover_trace
        _write_failover_trace(request_id, plan[0] if plan else None,
                              [{"attempt": i + 1,
                                "route": f"{f.step.provider}:{f.step.provider_model_id}",
                                "failure_class": f.error_code,
                                "reason": f.error_code,
                                "elapsed_ms": f.latency_ms}
                               for i, f in enumerate(getattr(e, "failures", []))],
                              winner=None, terminal_reason="plan_exhausted")
        body_out = {"error": {"type": "transport_exhausted", "failures": [_f_to_dict(f) for f in getattr(e, "failures", [])]}}
        return JSONResponse(status_code=502, content=body_out, headers=routing_headers)

    latency_ms = (time.time() - t0) * 1000.0
    err_code = failures[-1].error_code if failures else None
    usage = (meta.get("winning_body") or {}).get("usage") or {}
    actual_cost = (usage.get("gateway_cost") or {}).get("actual_cost_usd")
    # R5 §14: real billing evidence (Provider B x-si-buyer-cost-micro proxied as
    # gateway_cost.actual_cost_usd) updates observed cost_per_success.
    if isinstance(actual_cost, (int, float)) and step is not None:
        try:
            _registry.record_cost_feedback(
                step.provider, step.provider_model_id, float(actual_cost), success=err_code is None)
        except Exception:
            pass
    _metrics.record_attempt(
        canonical=step.canonical if step else canonical_hint,
        provider=step.provider if step else "none",
        slug=step.provider_model_id if step else "-",
        error_code=err_code,
        latency_ms=latency_ms,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cost_usd=_cost_of(step, usage) if step else 0.0,
        failover=bool(failures),
    )
    if step is not None:
        from .transport import _write_failover_trace
        _write_failover_trace(
            request_id, plan[0] if plan else None,
            [{"attempt": i + 1,
              "route": f"{f.step.provider}:{f.step.provider_model_id}",
              "failure_class": f.error_code,
              "reason": f.error_code,
              "elapsed_ms": f.latency_ms}
             for i, f in enumerate(failures)],
            winner=step, terminal_reason=None)
    # R15 §3: append real cost to the spend ledger (budgets source of truth).
    _spend = _cost_of(step, usage) if step else 0.0
    if _CONTROL_OK and _spend and _spend > 0:
        try:
            _control.append_spend(float(_spend),
                                  f"{step.provider}:{step.provider_model_id}" if step else "")
        except Exception:
            pass
    # R15 §7: canary observation counters
    if _canary_applied and step is not None:
        try:
            _metrics.record_canary(
                model=_canary_applied.get("model") or "",
                route=f"{step.provider}:{step.provider_model_id}",
                error=err_code is not None,
                ttft_ms=None, latency_ms=latency_ms, cost_usd=float(_spend or 0.0))
        except Exception:
            pass
    try:
        if step is not None:
            _observe_and_decide(headers, body, step, usage, routing_headers)
    except Exception:
        pass

    status = meta.get("winning_status", 200)
    payload = meta.get("winning_body") or {}
    return JSONResponse(status_code=status, content=payload, headers=routing_headers)


def _observe_and_decide(headers, body: dict, step: PlanStep, usage: dict, routing_headers: dict) -> dict:
    """G3 observe hook: update per-session context state, evaluate the
    compression policy, and surface the decision in response headers.
    Privacy-safe: token counts + hashed session only."""
    session_key = _session_key_from(body, headers)
    tokens_in = int((usage or {}).get("prompt_tokens") or 0)
    tokens_out = int((usage or {}).get("completion_tokens") or 0)
    tokens_used = tokens_in + tokens_out
    safe_ctx = int(step.safe_context_limit or 0)
    route_key = f"{step.provider}:{step.provider_model_id}"
    st = _ctx.observe(session_key, tokens_used, step.canonical, step.provider, route_key, safe_ctx)
    should, kind, reason = _ctx.should_compress(session_key, tokens_used, safe_ctx)
    routing_headers["x-gateway-ctx-compression"] = kind if should else "none"
    routing_headers["x-gateway-ctx-utilization"] = f"{st.utilization_pct:.1f}"
    routing_headers["x-gateway-ctx-reason"] = reason
    return {"should_compress": should, "trigger": kind, "reason": reason}


@app.post("/v1/compress")
async def compress_decision(request: Request):
    """G3 compression policy decision endpoint (for thin Hermes client, G5).
    Input: session_id|prompt_cache_key, context_tokens, safe_context (optional,
    else derived from canonical context_length). No upstream calls."""
    body = await request.json()
    session_key = str(body.get("session_id") or body.get("prompt_cache_key") or "")
    context_tokens = int(body.get("context_tokens") or 0)
    safe_ctx = int(body.get("safe_context") or 0)
    if safe_ctx <= 0 and body.get("canonical"):
        best_ctx = 0
        for rec in _registry.all():
            if rec.canonical == str(body["canonical"]):
                best_ctx = max(best_ctx, int(rec.context_length or 0))
        if best_ctx > 0:
            safe_ctx = route_safe_context(best_ctx, int(body.get("reserved_output") or 8192))
    if not session_key or context_tokens <= 0 or safe_ctx <= 0:
        return JSONResponse(status_code=400, content={
            "error": {"type": "invalid_input",
                      "message": "require session_id, context_tokens>0 and safe_context>0 (or canonical)"}})
    should, kind, reason = _ctx.should_compress(session_key, context_tokens, safe_ctx)
    lo, hi = _ctx.target_tokens(safe_ctx)
    return {
        "should_compress": should,
        "trigger": kind,
        "reason": reason,
        "safe_context": safe_ctx,
        "target_tokens": {"min": lo, "max": hi},
    }


@app.post("/v1/compress/record")
async def compress_record(request: Request):
    """G3 bookkeeping: caller performed a compression, record the event.
    Privacy-safe: counts only, no prompt content accepted."""
    body = await request.json()
    try:
        ev = _ctx.record_compression(
            raw_session_id=str(body.get("session_id") or ""),
            before_tokens=int(body.get("before_tokens") or 0),
            after_tokens=int(body.get("after_tokens") or 0),
            summary_tokens=int(body.get("summary_tokens") or 0),
            retained_tail_tokens=int(body.get("retained_tail_tokens") or 0),
            compressor_canonical=str(body.get("compressor_canonical") or ""),
            compressor_provider=str(body.get("compressor_provider") or ""),
            compressor_route_key=str(body.get("compressor_route_key") or ""),
            compressor_kind=str(body.get("compressor_kind") or "unknown"),
            trigger_kind=str(body.get("trigger_kind") or "soft"),
            trigger_threshold=int(body.get("trigger_threshold") or 0),
            compressor_api_ms=float(body.get("compressor_api_ms") or 0.0),
            total_compression_ms=float(body.get("total_compression_ms") or 0.0),
            cost_usd=float(body.get("cost_usd") or 0.0),
            cost_state=str(body.get("cost_state") or "UNKNOWN"),
            token_counters=body.get("token_counters") or {},
            failure_reason=str(body.get("failure_reason") or ""),
        )
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"type": "invalid_record", "message": repr(e)}})
    return {"ok": True, "event_id": ev.event_id}


@app.get("/context/sessions")
async def context_sessions() -> dict:
    return {"sessions": _ctx.snapshot(), "count": _ctx.session_count()}


@app.get("/context/events")
async def context_events(limit: int = 50) -> dict:
    return {"events": _ctx.recent_events(max(1, min(limit, 500)))}


def _cost_of(step: PlanStep, usage: dict) -> float:
    """Actual billed cost when the provider reports it (Provider B
    x-si-buyer-cost-micro proxied as gateway_cost.actual_cost_usd); otherwise
    usage * estimated route prices. Never invents a value."""
    actual = (usage.get("gateway_cost") or {}).get("actual_cost_usd")
    if isinstance(actual, (int, float)) and actual >= 0:
        return float(actual)
    ti = float(usage.get("prompt_tokens") or 0) / 1e6
    to = float(usage.get("completion_tokens") or 0) / 1e6
    rec = _registry.get(step.provider, step.provider_model_id)
    if rec is None:
        return 0.0
    return ti * rec.input_price + to * rec.output_price


def _record_decision(ctx: SelectionContext, trace: dict, primary, plan) -> int | None:
    """Privacy-safe decision trace: classes, routes, reasons only.

    R14 §13: additionally appended to the persistent decision journal
    (control.db decision_log) for the Admin UI «Последние решения» and
    distribution views. Journal write is best-effort — a control-DB failure
    must never affect inference.

    R15 §1: the journal trace now carries per-step price evidence
    (input/output USD per 1M, price_state) and expected cost for the request
    shape, so the control plane can detect economic anomalies (selected
    route materially more expensive than an equal-quality eligible
    alternative) without re-deriving prices. Read-only diagnostics; routing
    itself is NEVER changed automatically."""
    econ = trace.get("cache_economics") or {}

    def _step_cost(p, tokens_in: float, tokens_out: float) -> float | None:
        try:
            rec = _registry.get(p.provider, p.provider_model_id)
            if rec is None or rec.input_price is None or rec.output_price is None:
                return None
            return round(tokens_in / 1e6 * rec.input_price
                         + tokens_out / 1e6 * rec.output_price, 8)
        except Exception:  # noqa: BLE001
            return None

    _ti = float(getattr(ctx, "required_context", 0) or 0)
    _to = float(getattr(ctx, "reserved_output", 0) or 0)
    plan_prices: list[dict] = []
    for p in plan[:8]:
        rec = None
        try:
            rec = _registry.get(p.provider, p.provider_model_id)
        except Exception:  # noqa: BLE001
            rec = None
        plan_prices.append({
            "canonical": p.canonical,
            "route": f"{p.provider}:{p.provider_model_id}",
            "reason": p.reason,
            "input_price": rec.input_price if rec else None,
            "output_price": rec.output_price if rec else None,
            "price_state": rec.price_state if rec else None,
            "discount": getattr(p, "discount", None),
            "quality_score": getattr(p, "quality_score", None),
            "expected_cost_usd": _step_cost(p, _ti, _to),
        })
    trace = {**trace, "plan_prices": plan_prices}

    _last_decision.clear()
    _last_decision.update({
        "ts": time.time(),
        "task_class": ctx.task_class,
        "tier": ctx.tier,
        "required_context": ctx.required_context,
        "required_capabilities": sorted(ctx.capabilities_required),
        "cache_state": ctx.cache_state,
        "warm_prefix_tokens": ctx.warm_prefix_tokens,
        "cache_economics": econ,
        "selected": {
            "canonical": primary.canonical if primary else None,
            "provider": primary.provider if primary else None,
            "provider_model_id": primary.provider_model_id if primary else None,
            "safe_context_limit": primary.safe_context_limit if primary else None,
        } if primary else None,
        "plan": [
            {"canonical": p.canonical, "provider": p.provider, "provider_model_id": p.provider_model_id,
             "safe_context_limit": p.safe_context_limit, "reason": p.reason}
            for p in plan
        ],
        "trace": trace,
    })
    did = None
    if _CONTROL_OK:
        try:
            from .control import store as _cstore
            did = _cstore.insert_decision({
                "ts": time.time(),
                "task_class": ctx.task_class,
                "tier": ctx.tier,
                "canonical": primary.canonical if primary else None,
                "provider": primary.provider if primary else None,
                "provider_model_id": primary.provider_model_id if primary else None,
                "plan_len": len(plan),
                "reason": (primary.reason if primary else "no_eligible_route"),
                "trace": {
                    "required_context": ctx.required_context,
                    "cache_state": ctx.cache_state,
                    "cache_economics": econ,
                    "candidate_canonicals": trace.get("candidate_canonicals", [])[:10],
                    "hint_fallback": bool(trace.get("hint_fallback")),
                    # R15 §1: price evidence for economic-anomaly detection.
                    "plan_prices": trace.get("plan_prices", [])[:8],
                },
            })
        except Exception as _e:
            print(f"[gateway-v2] decision journal write failed: {_e!r}")
    return did


@app.get("/router/decision")
async def router_decision() -> dict:
    return _last_decision


@app.post("/router/simulate")
async def router_simulate(request: Request):
    """Offline routing simulation. Input: task_class/context_tokens/
    capabilities/canonical hint. No upstream calls, no prompt retention."""
    body = await request.json()
    task_class = str(body.get("task_class") or "NORMAL_CODING").upper()
    tier = _tier_from_class(task_class)
    ctx = SelectionContext(
        canonical_hint=body.get("canonical") or None,
        provider_hint=body.get("provider_hint") or None,
        required_context=int(body.get("context_tokens") or 0),
        reserved_output=int(body.get("reserved_output") or 1024),
        capabilities_required=frozenset(body.get("capabilities") or capabilities_for_class(task_class)),
        tier=tier,
        task_class=task_class,
        allow_unknown_quality=bool(body.get("allow_unknown_quality", False)),
        current_route_key=body.get("current_route_key") or None,
        cache_state=str(body.get("cache_state") or "UNKNOWN").upper(),
        warm_prefix_tokens=int(body.get("warm_prefix_tokens") or 0),
    )
    primary, plan, trace = _selector.choose(ctx)
    econ = trace.get("cache_economics") or {}
    result = {
        "input": {"task_class": task_class, "tier": tier, "context_tokens": ctx.required_context,
                  "capabilities": sorted(ctx.capabilities_required), "canonical_hint": ctx.canonical_hint,
                  "current_route_key": ctx.current_route_key, "cache_state": ctx.cache_state,
                  "warm_prefix_tokens": ctx.warm_prefix_tokens},
        "cache_economics": econ,
        "selected": {"canonical": primary.canonical if primary else None,
                     "provider": primary.provider if primary else None,
                     "provider_model_id": primary.provider_model_id if primary else None,
                     "reason": primary.reason if primary else None},
        "plan": [{"canonical": p.canonical, "provider": p.provider, "provider_model_id": p.provider_model_id,
                  "safe_context_limit": p.safe_context_limit, "reason": p.reason} for p in plan],
        "trace": trace,
    }
    return result


@app.get("/provider/share")
async def provider_share() -> dict:
    """Answers 'why is Provider B/Provider A used/not used' with numbers."""
    share = _metrics.provider_share()
    routes_by_provider: dict[str, list[str]] = {}
    for rec in _registry.all():
        routes_by_provider.setdefault(rec.provider, []).append(f"{rec.provider}:{rec.provider_model_id}")
    return {
        "providers": share,
        "eligible_routes": {k: len(v) for k, v in routes_by_provider.items()},
        "routes": routes_by_provider,
        "registry_built_at": _registry.stats().get("built_at"),
    }


@app.get("/metrics")
async def metrics_view() -> dict:
    return _metrics.snapshot()


@app.get("/version")
async def version() -> dict:
    """R8 §12 — product/build/schema/API version. No private paths/secrets."""
    return version_payload()


@app.post("/registry/refresh")
async def registry_refresh() -> dict:
    """Dynamic discovery: re-read provider catalogs, rebuild routes in place."""
    await _registry.build(_registry.adapters())
    _canon.rebuild()
    pool = _pool.build()
    return {"ok": True, "registry": _registry.stats(), "pool": pool["summary"]}


@app.get("/models/pool")
async def models_pool(include_routes: int = 0) -> dict:
    """R4 §16 — canonical pool with lifecycle, quality status, eligible
    providers and best current route. No secrets.

    include_routes=1 additionally returns the full per-route table
    (price states, cache prices, health, latency, last availability probe).
    Used by the Admin UI Models page."""
    from .control import store as _cstore
    out = _pool.last()
    if not out:
        out = _pool.build()
    summary = out.get("summary", {})
    models = {}
    for canonical, v in sorted(out.get("models", {}).items()):
        best = None
        eligible = [r for r in v["routes"] if r["eligible"]]
        if eligible:
            healthy = [r for r in eligible if r["health"] == "HEALTHY"]
            cand = healthy or eligible
            best = min(cand, key=lambda r: r["input_price"] + r["output_price"])
        # cheapest eligible route by combined input+output price
        cheapest = min(eligible, key=lambda r: r["input_price"] + r["output_price"]) if eligible else None
        models[canonical] = {
            "canonical": v["canonical"],
            "display_name": v["display_name"],
            "family": v["family"],
            "variant": v["variant"],
            "lifecycle": v["lifecycle"],
            "quality_status": v["quality_status"],
            "quality_score": v["quality_score"],
            "tier": v["tier"],
            "eligible_providers": v["eligible_providers"],
            "best_current_route": ({
                "route": f"{best['provider']}:{best['provider_model_id']}",
                "health": best["health"],
                "price_state": best["price_state"],
                "input_price": best["input_price"],
                "output_price": best["output_price"],
                "safe_context": best["safe_context"],
            } if best else None),
            "cheapest_route": ({
                "provider": cheapest["provider"],
                "route": f"{cheapest['provider']}:{cheapest['provider_model_id']}",
                "price_state": cheapest["price_state"],
                "input_price": cheapest["input_price"],
                "output_price": cheapest["output_price"],
            } if cheapest else None),
            "reason": v["reason"],
        }
        if include_routes:
            rr = []
            for r in v["routes"]:
                probe = None
                try:
                    probe = _cstore.last_availability(r["provider"], r["provider_model_id"])
                except Exception:
                    probe = None
                floor = _registry.min_discount_for(r["provider"])
                rr.append({
                    "provider": r["provider"],
                    "provider_model_id": r["provider_model_id"],
                    "eligible": r["eligible"],
                    "health": r["health"],
                    "circuit_state": r["circuit_state"],
                    "price_state": r["price_state"],
                    "input_price": r["input_price"],
                    "output_price": r["output_price"],
                    "cache_read_price": r.get("cache_read_price"),
                    "cache_write_price": r.get("cache_write_price"),
                    "discount": r["discount"],
                    "min_discount_floor": floor,
                    "context": r.get("safe_context") or r.get("advertised_context"),
                    "advertised_context": r.get("advertised_context"),
                    "p50_ms": r.get("p50_ms"),
                    "p95_ms": r.get("p95_ms"),
                    "success_rate": r.get("success_rate"),
                    "status": r.get("status"),
                    "reject_reason": (None if r["eligible"] else
                                      f"discount {r['discount'] if r['discount'] is not None else 'UNKNOWN'} < floor {floor}"),
                    "last_probe": ({"ok": probe.get("ok"),
                                    "latency_ms": probe.get("latency_ms"),
                                    "error_code": probe.get("error_code"),
                                    "checked_at": probe.get("checked_at")}
                                   if probe else None),
                })
            models[canonical]["routes"] = rr
    return {
        "summary": summary,
        "models": models,
        "artifacts": {
            "registry": POOL_REGISTRY_PATH,
            "aliases": POOL_ALIASES_PATH,
            "lifecycle": _pool.lifecycle._path if hasattr(_pool.lifecycle, "_path") else None,
        },
    }


@app.get("/models/lifecycle")
async def models_lifecycle() -> dict:
    return {"models": _pool.lifecycle.snapshot()}


def _f_to_dict(f) -> dict:
    return {"from": f"{f.step.provider}:{f.step.provider_model_id}", "error_code": f.error_code, "http_status": f.http_status, "latency_ms": f.latency_ms}
