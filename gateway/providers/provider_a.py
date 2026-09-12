"""Provider A adapter. Uses HTTPS via httpx, OpenAI-compatible /v1 routes.

Live catalog: ``GET /v1/models`` (already proven in PERF-1 / discover_catalog.py).
Price source: per-model "pricing.official_in / official_out" + discount field.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, AsyncIterator

import httpx

from .base import (
    ProviderAdapter,
    ProviderError,
    ProviderModel,
    UpstreamChunk,
    UpstreamRequest,
)


PROVIDER_A_BASE = os.environ.get("HERMES_ROUTER_PROVIDER_A_BASE", "https://provider-a.example/v1")
PROVIDER_A_KEY_ENV = "PROVIDER_A_API_KEY"


class ProviderAAdapter(ProviderAdapter):
    name = "provider_a"

    def __init__(self, base_url: str = PROVIDER_A_BASE, api_key: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get(PROVIDER_A_KEY_ENV, "")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0),
            follow_redirects=False,
        )
        self._lock = threading.Lock()
        self._models_cache: list[ProviderModel] = []
        self._models_loaded_at: float = 0.0

    # ── Discovery ──
    async def discover(self) -> list[ProviderModel]:
        # Live catalog. Cache for 60s.
        with self._lock:
            if self._models_cache and (time.time() - self._models_loaded_at) < 60:
                return list(self._models_cache)
        if not self._api_key:
            return self._fallback_static_catalog()
        try:
            r = await self._client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self._api_key}"})
            if r.status_code != 200:
                return self._fallback_static_catalog()
            j = r.json()
            data = j.get("data", j) if isinstance(j, dict) else j
            out: list[ProviderModel] = []
            for entry in data:
                pm = _entry_to_model("provider_a", entry, self.base_url)
                if pm is not None:
                    out.append(pm)
            with self._lock:
                self._models_cache = out
                self._models_loaded_at = time.time()
            return out
        except Exception:
            return self._fallback_static_catalog()

    async def price(self, model: ProviderModel) -> ProviderModel:
        return model

    def capabilities(self, model: ProviderModel) -> frozenset[str]:
        return model.capabilities

    # ── Transport: non-streaming ──
    async def request(self, model: ProviderModel, req: UpstreamRequest) -> tuple[int, dict[str, Any], ProviderError | None]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        headers.update(req.headers)
        body = dict(req.body)
        body["model"] = model.provider_model_id
        body["stream"] = False
        try:
            r = await self._client.post(url, headers=headers, json=body, timeout=180.0)
            err = _err_from_status(r)
            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    return r.status_code, r.json(), err
                except Exception:
                    return r.status_code, {"raw": r.text}, err
            return r.status_code, {"raw": r.text}, err
        except httpx.TimeoutException as e:
            return 0, {}, ProviderError(code="timeout", status=None, message=str(e))
        except Exception as e:
            return 0, {}, ProviderError(code="protocol", status=None, message=repr(e))

    # ── Transport: streaming ──
    async def stream(self, model: ProviderModel, req: UpstreamRequest) -> AsyncIterator[UpstreamChunk]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        headers.update(req.headers)
        body = dict(req.body)
        body["model"] = model.provider_model_id
        body["stream"] = True
        async with self._client.stream("POST", url, headers=headers, json=body, timeout=httpx.Timeout(connect=10.0, read=240.0, write=60.0, pool=10.0)) as r:
            err = _err_from_status(r)
            if err is not None:
                # Drain status for non-2xx (OpenAI-compatible SSE error frame variant).
                if r.status_code >= 400:
                    err.status = r.status_code
                raw = await r.aread()
                yield UpstreamChunk(raw=raw, meta={"provider": "provider_a", "model": model.provider_model_id, "http_status": r.status_code}, done=True)
                return
            buffer: bytes = b""
            async for chunk in r.aiter_bytes():
                if not chunk:
                    continue
                buffer += chunk
                yield UpstreamChunk(raw=chunk, meta={"provider": "provider_a", "model": model.provider_model_id})
                if buffer.rstrip().endswith(b"[DONE]"):
                    yield UpstreamChunk(raw=b"", meta={"provider": "provider_a", "model": model.provider_model_id}, done=True)
                    return

    # ── Health & usage ──
    async def health(self, model: ProviderModel) -> dict[str, Any]:
        t0 = time.time()
        try:
            r = await self._client.get(f"{self.base_url}/models/{model.provider_model_id}", headers={"Authorization": f"Bearer {self._api_key}"}, timeout=10.0)
            return {"ok": r.status_code == 200, "status": r.status_code, "latency_ms": (time.time() - t0) * 1000.0}
        except Exception as e:
            return {"ok": False, "error": repr(e), "latency_ms": (time.time() - t0) * 1000.0}

    def record_success(self, model: ProviderModel, latency_ms: float) -> None: ...
    def record_failure(self, model: ProviderModel, error: ProviderError) -> None: ...
    def usage(self, model: ProviderModel) -> dict[str, Any]:
        return {"provider": "provider_a", "model": model.provider_model_id}

    # ── Static fallback (used when /models is unreachable for tests) ──
    def _fallback_static_catalog(self) -> list[ProviderModel]:
        # Static catalog matching the proven Phase 3B mapping (PERF-1 / GATEWAY-0).
        return [
            _static("cb/gpt-5.6-luna",    "gpt-5.6-luna",    1050000, 0.020, 0.120, 0.90),
            _static("cx/gpt-5.6-luna",    "gpt-5.6-luna",     272000, 0.020, 0.120, 0.90),
            _static("cb/glm-5.3",         "glm-5.3",         1000000, 0.140, 0.440, 0.90),
            _static("cb/gemini-3.7-flash","gemini-3.7-flash",1000000, 0.050, 0.240, 0.90),
            _static("cb/deepseek-v4-pro", "deepseek-v4-pro", 1000000, 0.066, 0.198, 0.90),
            _static("cb/minimax-m3",      "minimax-m3",      1000000, 0.030, 0.120, 0.90),
            _static("cb/kimi-k3",         "kimi-k3",         1000000, 0.300, 1.500, 0.90),
        ]


def _static(slug: str, canonical: str, ctx: int, inp: float, out: float, discount: float) -> ProviderModel:
    return ProviderModel(
        provider="provider_a",
        provider_model_id=slug,
        canonical_model=canonical,
        context_length=ctx,
        input_price=inp,
        output_price=out,
        discount=discount,
        capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
        certification_status="CERTIFIED",
    )


def _entry_to_model(provider: str, entry: dict, base_url: str) -> ProviderModel | None:
    if not isinstance(entry, dict):
        return None
    pid = entry.get("id") or entry.get("model") or entry.get("slug")
    if not pid:
        return None
    slug = str(pid)
    # Provider A lives across prefixes (cx/, cb/, cmc/, ocg/, ali/, ag/, etc.).
    # We map through the canonical mapping, not raw name.
    from ..policy import lookup_canonical
    canonical = lookup_canonical(provider, slug)
    if canonical is None:
        return None
    pricing = entry.get("pricing") or {}
    # R11 §13: context from provider metadata only; UNKNOWN stays 0
    # (fake 1M fallback removed — context gate is per route)
    ctx = int(entry.get("input_token_limit") or entry.get("context_length")
               or entry.get("context_window") or 0)
    official_in = float(pricing.get("official_in") or pricing.get("input") or 0.0)
    official_out = float(pricing.get("official_out") or pricing.get("output") or 0.0)
    disc = pricing.get("discount_input") or pricing.get("discount")
    try:
        disc_f = float(disc) if disc is not None else 0.90
    except (TypeError, ValueError):
        disc_f = 0.90
    inp_paid = official_in * (1.0 - disc_f)
    out_paid = official_out * (1.0 - disc_f)
    return ProviderModel(
        provider=provider,
        provider_model_id=slug,
        canonical_model=canonical,
        context_length=ctx,
        input_price=round(inp_paid, 6),
        output_price=round(out_paid, 6),
        discount=disc_f,
        capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
        certification_status="CERTIFIED",
        metadata={"base_url": base_url, "official_in": official_in, "official_out": official_out},
    )


def _err_from_status(r) -> ProviderError | None:
    code = None
    if r.status_code in (408, 504, 524):
        code = "timeout"
    elif r.status_code == 429:
        code = "rate_limit"
    elif r.status_code in (502, 503, 500):
        code = "server"
    elif r.status_code == 402:
        code = "payment_required"
    elif r.status_code == 413:
        code = "context_too_large"
    elif r.status_code >= 400:
        code = "protocol"
    if code is None:
        return None
    return ProviderError(code=code, status=r.status_code, message=f"Provider A HTTP {r.status_code}", retries_safe=(code == "rate_limit"))
