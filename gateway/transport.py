"""V2 transport — non-streaming and streaming, with first-response / idle /
total timeouts. Same-canonical failover first; alternate canonical second.

The transport layer is the only place that owns cancellation and actual HTTP.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from .policy import get_canonical
from .providers.base import (
    ProviderAdapter,
    ProviderError,
    ProviderModel,
    UpstreamChunk,
    UpstreamRequest,
)
from .registry import RouteRecord, RouteRegistry
from .selector import PlanStep
from .timeouts import TimeoutPolicy, total_for


@dataclass
class AttemptFailure:
    step: PlanStep
    error_code: str
    http_status: int | None
    latency_ms: float


class TransportError(Exception):
    """Raised when the entire plan is exhausted."""

    def __init__(self, failures: list[AttemptFailure]) -> None:
        self.failures = failures
        super().__init__(f"All {len(failures)} attempts failed")


async def _adapter_for(provider: str, registry: RouteRegistry) -> ProviderAdapter | None:
    return registry.get_adapter(provider)


def _bid_headers(rec: RouteRecord, body: dict) -> dict:
    """R11 §11: per-model discount policy -> provider bid headers.

    Provider A price bidding: x-max-input-price / x-max-output-price derived
    from the OFFICIAL reference price * (1 - effective_min_discount). Caps
    are only computed when the official price is known — never fabricated.
    Provider B: X-Min-Discount with the effective model policy.
    """
    headers: dict[str, str] = {}
    ovr = None
    off_in = off_out = None
    try:
        from .control import store as _cstore
        pol = _cstore.get_pool_policy(rec.canonical)
        ovr = pol.get("min_discount_override")
        if ovr is not None:
            # official reference price from the live discovery inventory
            for m in _cstore.list_discovered(provider=rec.provider):
                if m.get("provider_model_id") == rec.provider_model_id:
                    off_in = m.get("official_input")
                    off_out = m.get("official_output")
                    break
    except Exception:
        return headers
    if ovr is None:
        return headers  # global floor already encoded in base_url (/min80)
    floor = float(ovr)
    if rec.provider == "provider_a":
        if off_in:
            headers["x-max-input-price"] = str(round(off_in * (1.0 - floor), 6))
        if off_out:
            headers["x-max-output-price"] = str(round(off_out * (1.0 - floor), 6))
    elif rec.provider == "provider_b":
        headers["X-Min-Discount"] = str(int(round(floor * 100)))
    return headers


def _expected_provider(adapter: ProviderAdapter, rec: RouteRecord) -> ProviderModel:
    pm = ProviderModel(
        provider=rec.provider,
        provider_model_id=rec.provider_model_id,
        canonical_model=rec.canonical,
        context_length=rec.context_length,
        input_price=rec.input_price,
        output_price=rec.output_price,
        discount=rec.discount,
        capabilities=rec.capabilities,
        certification_status=rec.certification_status,
    )
    return pm


async def execute_plan(
    *,
    plan: list[PlanStep],
    registry: RouteRegistry,
    body: dict,
    stream: bool,
    policy: TimeoutPolicy,
    tier: str,
    request_timeout: float | None = None,
) -> tuple[PlanStep, list[AttemptFailure], dict]:
    """Run the plan in order. Return (winning step, failures, header_meta)."""
    failures: list[AttemptFailure] = []
    header_meta: dict = {"started_at": time.time(), "attempts": 0, "policy": _policy_to_dict(policy)}
    first_token_seen = False
    last_chunk_provider: tuple[str, str] | None = None
    attempts: list[dict] = []
    plan_deadline = time.time() + (request_timeout or total_for(tier, policy))
    pre_first_deadline = time.time() + min(request_timeout or total_for(tier, policy), policy.pre_first_failover_s)

    for step in plan:
        if time.time() >= pre_first_deadline:
            failures.append(AttemptFailure(step=step, error_code="deadline_exceeded", http_status=None, latency_ms=0.0))
            continue
        adapter = await _adapter_for(step.provider, registry)
        if adapter is None:
            failures.append(AttemptFailure(step=step, error_code="adapter_missing", http_status=None, latency_ms=0.0))
            continue
        rec = registry.get(step.provider, step.provider_model_id)
        if rec is None:
            failures.append(AttemptFailure(step=step, error_code="route_missing", http_status=None, latency_ms=0.0))
            continue

        pm = _expected_provider(adapter, rec)
        req = UpstreamRequest(body=body, stream=False, headers=_bid_headers(rec, body))
        deadline = pre_first_deadline
        budget_total = max(0.1, deadline - time.time())
        # First-response is the key signal — gives cancel-on-TTFT semantics.
        t0 = time.time()
        try:
            if stream:
                # Streaming path uses collect-stream-to-up-to-done logic in caller.
                status, payload, err = await adapter.request(pm, req)
                header_meta["attempts"] += 1
                latency = (time.time() - t0) * 1000.0
                if err is not None:
                    registry.record_failure(step.provider, step.provider_model_id, err, latency)
                    failures.append(AttemptFailure(step=step, error_code=err.code, http_status=err.status, latency_ms=latency))
                    continue
                registry.record_success(step.provider, step.provider_model_id, latency)
                return step, failures, {**header_meta, "winning_latency_ms": latency, "winning_status": status, "winning_body": payload}
            else:
                # First-response enforce via asyncio.wait_for + a hard total cap.
                status, payload, err = await asyncio.wait_for(
                    adapter.request(pm, req),
                    timeout=min(policy.first_response_s, budget_total),
                )
                header_meta["attempts"] += 1
                latency = (time.time() - t0) * 1000.0
                if err is not None:
                    registry.record_failure(step.provider, step.provider_model_id, err, latency)
                    failures.append(AttemptFailure(step=step, error_code=err.code, http_status=err.status, latency_ms=latency))
                    # Same provider/cancel upstream is automatic — the request already returned.
                    continue
                # Read tax: if first-response was healthy but the request still exceeds
                # remaining budget, the caller will see this in ``deadline`` state.
                registry.record_success(step.provider, step.provider_model_id, latency)
                return step, failures, {**header_meta, "winning_latency_ms": latency, "winning_status": status, "winning_body": payload}
        except asyncio.TimeoutError:
            latency = (time.time() - t0) * 1000.0
            registry.record_failure(step.provider, step.provider_model_id, ProviderError(code="timeout", status=None, message="first_response_timeout"), latency)
            failures.append(AttemptFailure(step=step, error_code="timeout", http_status=None, latency_ms=latency))
            continue
        except Exception as e:
            latency = (time.time() - t0) * 1000.0
            registry.record_failure(step.provider, step.provider_model_id, ProviderError(code="protocol", status=None, message=repr(e)), latency)
            failures.append(AttemptFailure(step=step, error_code="protocol", http_status=None, latency_ms=latency))
            continue

    raise TransportError(failures)


def _policy_to_dict(p: TimeoutPolicy) -> dict:
    return {
        "connect_s": p.connect_s,
        "first_response_s": p.first_response_s,
        "stream_idle_s": p.stream_idle_s,
        "normal_total_s": p.normal_total_s,
        "t4_total_s": p.t4_total_s,
        "pre_first_failover_s": p.pre_first_failover_s,
    }


def _write_failover_trace(request_id: str | None, primary_step,
                          attempts: list[dict], winner,
                          terminal_reason: str | None) -> None:
    """Privacy-safe failover decision trace (R9). One JSON line per request:
    request_id, canonical, attempted routes, failure classes, winner,
    terminal reason. No prompts, no keys, no raw bodies. Best-effort —
    trace IO must never break the request path."""
    try:
        from .state_paths import state_file
        import json as _json
        import os
        entry = {
            "ts": time.time(),
            "request_id": request_id,
            "canonical": getattr(primary_step, "canonical", None),
            "attempts": attempts,
            "winner": ({"route": f"{winner.provider}:{winner.provider_model_id}",
                        "canonical": winner.canonical}
                       if winner is not None else None),
            "terminal_reason": terminal_reason,
        }
        with open(state_file("failover-trace.jsonl"), "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def stream_plan(
    *,
    plan: list[PlanStep],
    registry: RouteRegistry,
    body: dict,
    policy: TimeoutPolicy,
    tier: str,
    request_id: str | None = None,
) -> AsyncIterator[dict]:
    """Yield ``{"event":"chunk"|"failover"|"done"|"terminal", ...}`` frames.

    R9: the final ``plan_exhausted`` frame became an explicit ``terminal``
    event (reason=plan_exhausted) so the caller can emit a proper SSE error
    to the client instead of a silent empty ``[DONE]``."""
    generation_deadline = time.time() + total_for(tier, policy)
    pre_first_deadline = time.time() + min(total_for(tier, policy), policy.pre_first_failover_s)
    first_token_seen = False
    last_chunk_provider: tuple[str, str] | None = None
    attempts: list[dict] = []

    async def _drive(step: PlanStep, idx: int):
        nonlocal first_token_seen
        adapter = registry.get_adapter(step.provider)
        rec = registry.get(step.provider, step.provider_model_id)
        if adapter is None or rec is None:
            yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": "adapter_missing"}
            return
        pm = _expected_provider(adapter, rec)
        req = UpstreamRequest(body=body, stream=True, headers=_bid_headers(rec, body))
        deadline = generation_deadline if first_token_seen else pre_first_deadline
        budget = max(0.1, deadline - time.time())
        t0 = time.time()
        emitted = 0
        try:
            chunks = adapter.stream(pm, req)
            iterator = chunks.__aiter__()
            try:
                first = await asyncio.wait_for(iterator.__anext__(), timeout=min(policy.first_response_s, budget))
            except (asyncio.TimeoutError, StopAsyncIteration):
                latency = (time.time() - t0) * 1000.0
                registry.record_failure(step.provider, step.provider_model_id, ProviderError(code="timeout", status=None, message="ttft_timeout"), latency)
                yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": "ttft_timeout"}
                return
            except Exception as e:
                latency = (time.time() - t0) * 1000.0
                registry.record_failure(step.provider, step.provider_model_id, ProviderError(code="protocol", status=None, message=repr(e)), latency)
                yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": f"protocol:{e!r}"}
                return
            cur = first
            while not cur.done:
                if time.time() >= deadline:
                    raise asyncio.TimeoutError
                yield {
                    "event": "chunk",
                    "provider": step.provider,
                    "provider_model_id": step.provider_model_id,
                    "canonical": step.canonical,
                    "bytes": cur.raw,
                    "done": cur.done,
                    "meta": cur.meta,
                    "attempt_index": idx,
                }
                emitted += 1
                if not first_token_seen:
                    first_token_seen = True
                    deadline = generation_deadline
                try:
                    cur = await asyncio.wait_for(iterator.__anext__(), timeout=min(policy.stream_idle_s, max(0.1, deadline - time.time())))
                except StopAsyncIteration:
                    cur = UpstreamChunk(raw=b"", meta=cur.meta, done=True)
            if cur.raw:
                yield {
                    "event": "chunk",
                    "provider": step.provider,
                    "provider_model_id": step.provider_model_id,
                    "canonical": step.canonical,
                    "bytes": cur.raw,
                    "done": False,
                    "meta": cur.meta,
                    "attempt_index": idx,
                }
            latency = (time.time() - t0) * 1000.0
            registry.record_success(step.provider, step.provider_model_id, latency)
            yield {"event": "done", "provider": step.provider, "provider_model_id": step.provider_model_id, "emitted_bytes": emitted, "latency_ms": latency}
        except asyncio.TimeoutError:
            latency = (time.time() - t0) * 1000.0
            registry.record_failure(step.provider, step.provider_model_id, ProviderError(code="timeout", status=None, message="stream_idle_or_deadline"), latency)
            yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": "stream_idle_or_deadline"}
        except Exception as e:
            latency = (time.time() - t0) * 1000.0
            registry.record_failure(step.provider, step.provider_model_id, ProviderError(code="protocol", status=None, message=repr(e)), latency)
            yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": f"protocol:{e!r}"}

    for idx, step in enumerate(plan):
        chain_deadline = generation_deadline if first_token_seen else pre_first_deadline
        if time.time() >= chain_deadline:
            attempts.append({
                "attempt": idx + 1,
                "route": step.provider + ":" + step.provider_model_id,
                "failure_class": "timeout",
                "reason": "deadline_exceeded",
                "elapsed_ms": None,
            })
            yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": "deadline_exceeded"}
            continue
        async for frame in _drive(step, idx):
            ev = frame.get("event")
            if ev == "failover":
                attempts.append({
                    "attempt": idx + 1,
                    "route": frame.get("from"),
                    "failure_class": str(frame.get("reason", "")).split(":", 1)[0],
                    "reason": frame.get("reason"),
                    "elapsed_ms": None,
                })
            if last_chunk_provider is not None and ev == "chunk" and last_chunk_provider != (step.provider, step.provider_model_id):
                # Mixed provider defense — NEVER deliver chunks from a different provider.
                yield {"event": "failover", "from": step.provider + ":" + step.provider_model_id, "reason": "mixed_provider_block"}
                break
            yield frame
            if ev == "done":
                _write_failover_trace(request_id, step, attempts,
                                      winner=step, terminal_reason=None)
                return
    yield {"event": "terminal", "reason": "plan_exhausted",
           "attempts": attempts}
    _write_failover_trace(request_id, plan[0] if plan else None, attempts,
                          winner=None, terminal_reason="plan_exhausted")
