"""Provider B adapter. Discount is encoded in the base_url path (``/minN/``).

Lowering the discount floor means changing the base_url, not the API key.
For V2 the default floor is ``/min80/`` (>= 80% off) which matches the Phase 3B
route_engine floor and the registry ``DISCOUNT_FLOOR``.
"""
from __future__ import annotations

import os
import re
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


# Pattern: /minN/ — the discount floor encoded in the URL itself.
_DISCOUNT_RE = re.compile(r"/min(\d+)/")


def default_provider_b_base() -> str:
    return os.environ.get("HERMES_ROUTER_PROVIDER_B_BASE", "https://provider-b.example/min80/v1")


PROVIDER_B_KEY_ENV = "PROVIDER_B_API_KEY"


class ProviderBAdapter(ProviderAdapter):
    name = "provider_b"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = (base_url or default_provider_b_base()).rstrip("/")
        self._discount_floor = self._parse_discount_floor(self.base_url)
        self._api_key = api_key or os.environ.get(PROVIDER_B_KEY_ENV, "")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0),
            follow_redirects=False,
        )
        self._lock = threading.Lock()
        self._models_cache: list[ProviderModel] = []
        self._models_loaded_at: float = 0.0

    @property
    def discount_floor(self) -> float:
        """Effective discount floor encoded in the base_url."""
        return self._discount_floor

    # ── Discovery ──
    async def discover(self) -> list[ProviderModel]:
        with self._lock:
            if self._models_cache and (time.time() - self._models_loaded_at) < 60:
                return list(self._models_cache)
        # 1) Try live /v1/models
        live: list[ProviderModel] = []
        if self._api_key:
            try:
                r = await self._client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self._api_key}"})
                if r.status_code == 200:
                    j = r.json()
                    data = j.get("data", j) if isinstance(j, dict) else j
                    for entry in data:
                        pm = _entry_to_model("provider_b", entry, self.base_url, self._discount_floor)
                        if pm is not None:
                            live.append(pm)
            except Exception:
                pass
        # 2) Always include the proven Phase 3B Provider B mappings as a safety net.
        static = self._static_catalog()
        seen = {(p.provider, p.provider_model_id) for p in live}
        merged = live + [p for p in static if (p.provider, p.provider_model_id) not in seen]
        with self._lock:
            self._models_cache = merged
            self._models_loaded_at = time.time()
        return merged

    async def price(self, model: ProviderModel) -> ProviderModel:
        return model

    def capabilities(self, model: ProviderModel) -> frozenset[str]:
        return model.capabilities

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
                    payload = r.json()
                except Exception:
                    payload = {"raw": r.text}
            else:
                payload = {"raw": r.text}
            # Actual billed cost feedback (proven live, G2.4): Provider B reports the
            # real marketplace offer cost in USD-micro on every response. Proxy it
            # through the payload so transport/app can record actual cost.
            cost_micro = r.headers.get("x-si-buyer-cost-micro")
            if cost_micro is not None:
                try:
                    payload.setdefault("gateway_cost", {})["actual_cost_usd"] = int(cost_micro) / 1e6
                except (ValueError, AttributeError):
                    pass
            return r.status_code, payload, err
        except httpx.TimeoutException as e:
            return 0, {}, ProviderError(code="timeout", status=None, message=str(e))
        except Exception as e:
            return 0, {}, ProviderError(code="protocol", status=None, message=repr(e))

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
                raw = await r.aread()
                yield UpstreamChunk(raw=raw, meta={"provider": "provider_b", "model": model.provider_model_id, "http_status": r.status_code}, done=True)
                return
            async for chunk in r.aiter_bytes():
                if not chunk:
                    continue
                yield UpstreamChunk(raw=chunk, meta={"provider": "provider_b", "model": model.provider_model_id})
                if chunk.rstrip().endswith(b"[DONE]"):
                    yield UpstreamChunk(raw=b"", meta={"provider": "provider_b", "model": model.provider_model_id}, done=True)
                    return

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
        return {"provider": "provider_b", "model": model.provider_model_id}

    # ── helpers ──
    @staticmethod
    def _parse_discount_floor(base_url: str) -> float:
        m = _DISCOUNT_RE.search(base_url)
        return (int(m.group(1)) / 100.0) if m else 0.80

    # ── Static safety net ──
    # Prices here are a stale Phase 3B min85-era snapshot — NOT exact. They are
    # an upper-bound estimate only; live catalog discovery overrides them.
    def _static_catalog(self) -> list[ProviderModel]:
        return [
            ProviderModel(
                provider="provider_b",
                provider_model_id="gpt-5.6-luna",
                canonical_model="gpt-5.6-luna",
                context_length=1050000,
                input_price=0.030,
                output_price=0.180,
                price_state="ESTIMATED_UPPER_BOUND",
                discount=self._discount_floor,
                capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
                certification_status="CERTIFIED",
            ),
            ProviderModel(
                provider="provider_b",
                provider_model_id="grok-4.3",
                canonical_model="grok-4.3",
                context_length=132000,
                input_price=0.225,
                output_price=0.900,
                price_state="ESTIMATED_UPPER_BOUND",
                discount=self._discount_floor,
                capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
                certification_status="CERTIFIED",
            ),
            ProviderModel(
                provider="provider_b",
                provider_model_id="kimi-k3",
                canonical_model="kimi-k3",
                context_length=1000000,
                input_price=0.450,
                output_price=2.250,
                price_state="ESTIMATED_UPPER_BOUND",
                discount=self._discount_floor,
                capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
                certification_status="CERTIFIED",
            ),
        ]


def _entry_to_model(provider: str, entry: dict, base_url: str, discount_floor: float) -> ProviderModel | None:
    if not isinstance(entry, dict):
        return None
    pid = entry.get("id") or entry.get("model") or entry.get("slug")
    if not pid:
        return None
    slug = str(pid)
    from ..policy import lookup_canonical
    canonical = lookup_canonical(provider, slug)
    if canonical is None:
        return None
    # R11 §13: real catalog context; no fake fallback
    ctx = int(entry.get("context_length") or 0)
    pricing = entry.get("pricing") or {}
    # Provider B catalog schema (proven live, G2.4): pricing.prompt / pricing.completion
    # are OFFICIAL base prices in USD per TOKEN (e.g. "0.0000002000" = 0.2 USD/Mtok).
    # There is no pricing.input/output field. The actual paid price is the
    # marketplace offer price bounded above by official * (1 - discount_floor):
    # min80 is a FLOOR, not the exact discount (measured actual 88-90%).
    inp_official = float(pricing.get("prompt") or 0.0) * 1e6     # USD/token -> USD/Mtok
    out_official = float(pricing.get("completion") or 0.0) * 1e6  # USD/token -> USD/Mtok
    if inp_official > 0 or out_official > 0:
        inp = inp_official * (1.0 - discount_floor)
        out = out_official * (1.0 - discount_floor)
        price_state = "ESTIMATED_UPPER_BOUND"
    else:
        inp = 0.0
        out = 0.0
        price_state = "UNKNOWN"
    return ProviderModel(
        provider=provider,
        provider_model_id=slug,
        canonical_model=canonical,
        context_length=ctx,
        input_price=round(inp, 6),
        output_price=round(out, 6),
        price_state=price_state,
        discount=discount_floor,
        capabilities=frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"}),
        certification_status="CERTIFIED",
        metadata={
            "base_url": base_url,
            "official_in_per_mtok": inp_official,
            "official_out_per_mtok": out_official,
        },
    )


def _err_from_status(r) -> ProviderError | None:
    if 200 <= r.status_code < 400:
        return None
    code_map = {408: "timeout", 429: "rate_limit", 500: "server", 502: "server", 503: "no_healthy_sellers", 504: "timeout", 524: "timeout", 402: "payment_required", 413: "context_too_large"}
    code = code_map.get(r.status_code, "protocol" if r.status_code >= 400 else None)
    if code is None:
        return None
    return ProviderError(code=code, status=r.status_code, message=f"Provider B HTTP {r.status_code}", retries_safe=(code in {"rate_limit", "server", "no_healthy_sellers", "timeout"}))
