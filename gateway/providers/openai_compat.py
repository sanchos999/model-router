"""Generic OpenAI-compatible provider adapter (spec R6 §F/§E).

Provider N connects as an adapter plugin: base_url + api key env ref + optional
catalog mapping. No routing-core changes. Uses the SAME contract as the
Provider A/Provider B adapters — no provider-name logic anywhere else.
"""
from __future__ import annotations

import httpx
from typing import Any, AsyncIterator

from .base import (
    ProviderAdapter,
    ProviderError,
    ProviderModel,
    UpstreamChunk,
    UpstreamRequest,
)

_CANON_CAPS = frozenset({"text", "streaming", "json", "tool_call", "streaming_tool_call"})


def _err(status: int, message: str) -> ProviderError:
    code = {
        401: "payment_required", 402: "payment_required", 403: "payment_required",
        404: "no_healthy_sellers", 429: "rate_limit",
    }.get(status, "server" if status >= 500 else "unknown")
    return ProviderError(code=code, status=status, message=message,
                         retries_safe=status >= 500 or status == 429)


class OpenAICompatibleAdapter(ProviderAdapter):
    """[OI]-compatible marketplace/provider. Configured via control plane:

      name         — provider key (e.g. "custom1")
      base_url     — e.g. https://api.example.com/v1
      secret_ref   — env var NAME holding the API key (never the key itself)
      model_map    — {provider_slug: canonical} mapping for discovery
      context_length / prices / discount per mapped model (from catalog or cfg)
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        secret_ref: str | None = None,
        api_key: str | None = None,
        model_map: dict[str, str] | None = None,
        defaults: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._secret_ref = secret_ref
        self._api_key = api_key or (os_secret(secret_ref) if secret_ref else "")
        self._model_map = dict(model_map or {})
        self._defaults = defaults or {}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def reload_key(self) -> None:
        self._api_key = os_secret(self._secret_ref) if self._secret_ref else ""

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def discover(self) -> list[ProviderModel]:
        out: list[ProviderModel] = []
        if not self._model_map:
            return out
        try:
            r = await self._client.get(f"{self.base_url}/models", headers=self._headers())
            if r.status_code != 200:
                return out
            live = {m.get("id") for m in (r.json() or {}).get("data") or []}
        except Exception:
            live = set(self._model_map)
        for slug, canonical in self._model_map.items():
            if live and slug not in live:
                continue
            d = self._defaults.get(slug) or self._defaults.get("*") or {}
            out.append(ProviderModel(
                provider=self.name,
                provider_model_id=slug,
                canonical_model=canonical,
                context_length=int(d.get("context_length", 128000)),
                input_price=float(d.get("input_price", 0.0)),
                output_price=float(d.get("output_price", 0.0)),
                discount=(None if d.get("discount") is None else float(d.get("discount"))),
                capabilities=_CANON_CAPS,
                price_state=str(d.get("price_state", "EXACT")),
                certification_status=str(d.get("certification_status", "UNVERIFIED")),
                metadata={"base_url": self.base_url},
            ))
        return out

    async def price(self, model: ProviderModel) -> ProviderModel:
        return model

    def capabilities(self, model: ProviderModel) -> frozenset[str]:
        return model.capabilities

    async def request(self, model: ProviderModel, req: UpstreamRequest) -> tuple[int, dict[str, Any], ProviderError | None]:
        try:
            r = await self._client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(), json=req.body)
        except httpx.TimeoutException:
            return 0, {}, ProviderError(code="timeout", status=None,
                                        message="upstream timeout", retries_safe=True)
        except httpx.HTTPError as e:
            return 0, {}, ProviderError(code="protocol", status=None, message=repr(e), retries_safe=True)
        if r.status_code != 200:
            return r.status_code, {}, _err(r.status_code, r.text[:300])
        return 200, r.json(), None

    async def stream(self, model: ProviderModel, req: UpstreamRequest) -> AsyncIterator[UpstreamChunk]:
        body = {**req.body, "stream": True}
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions",
                headers=self._headers(), json=body) as r:
                if r.status_code != 200:
                    text = (await r.aread()).decode(errors="replace")[:300]
                    raise ProviderErrorCode(_err(r.status_code, text))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        yield UpstreamChunk(raw=b"[DONE]", meta={"done": True}, done=True)
                        return
                    yield UpstreamChunk(raw=payload.encode(), meta={"json": payload})
        except httpx.TimeoutException:
            raise ProviderErrorCode(ProviderError(code="timeout", status=None,
                                                  message="stream timeout", retries_safe=True))
        except httpx.HTTPError as e:
            raise ProviderErrorCode(ProviderError(code="protocol", status=None, message=repr(e)))

    async def health(self, model: ProviderModel) -> dict[str, Any]:
        try:
            r = await self._client.get(f"{self.base_url}/models", headers=self._headers())
            return {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    def record_success(self, model: ProviderModel, latency_ms: float) -> None: ...
    def record_failure(self, model: ProviderModel, error: ProviderError) -> None: ...

    def usage(self, model: ProviderModel) -> dict[str, Any]:
        return {}

    def normalize_error(self, status: int, message: str) -> ProviderError:
        return _err(status, message)


class ProviderErrorCode(RuntimeError):
    """Adapter-level abort carrying a normalized ProviderError."""

    def __init__(self, err: ProviderError) -> None:
        super().__init__(err.message)
        self.error = err


def os_secret(env_name: str | None) -> str:
    import os
    return os.environ.get(env_name or "", "")
