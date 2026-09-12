"""R7 §7D: streaming wire-format regression — raw SSE passthrough, no bytes-in-JSON."""
from __future__ import annotations

import asyncio

import pytest

from gateway.providers.base import UpstreamChunk, UpstreamRequest
from gateway.registry import RouteRegistry
from gateway.selector import PlanStep
from gateway.transport import stream_plan


class _FakeAdapter:
    name = "fake"

    def __init__(self, chunks):
        self._chunks = chunks
        self._models = []

    async def stream(self, model, req: UpstreamRequest):
        for c in self._chunks:
            yield c

    async def health(self, model):
        return {"ok": True}

    def add_model(self, slug, canonical, ctx, inp, out, discount=0.90):
        from gateway.providers.base import ProviderModel
        self._models.append(ProviderModel(
            provider=self.name, provider_model_id=slug, canonical_model=canonical,
            context_length=ctx, input_price=inp, output_price=out,
            discount=discount,
            capabilities=frozenset({"text", "streaming", "json", "tool_call",
                                    "streaming_tool_call"}),
            certification_status="CERTIFIED"))

    def models(self):
        return list(self._models)

    async def discover(self):
        return list(self._models)

    async def price(self, model):
        return model

    def capabilities(self, model):
        return model.capabilities

    def record_success(self, *a): ...
    def record_failure(self, *a): ...


def test_stream_chunk_payloads_are_wire_bytes():
    reg = RouteRegistry()
    ad = _FakeAdapter([
        UpstreamChunk(raw=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', meta={}),
        UpstreamChunk(raw=b"data: [DONE]\n\n", meta={}, done=True),
    ])
    # gpt-5.6-luna is in the static CANONICAL_MODELS registry — a synthetic
    # canonical like "m" would be dropped by the known-canonical filter.
    ad.add_model("fake-model", "gpt-5.6-luna", 1050000, 0.02, 0.12)
    reg.register_adapter(ad)
    asyncio.run(reg.build([ad]))
    assert reg.get("fake", "fake-model") is not None

    step = PlanStep(canonical="gpt-5.6-luna", provider="fake", provider_model_id="fake-model",
                    context_length=1050000, discount=0.9, quality_score=0.0,
                    reason="test", safe_context_limit=1050000)
    from gateway.timeouts import DEFAULT
    gen = stream_plan(plan=[step], registry=reg,
                      body={"messages": [{"role": "user", "content": "x"}]},
                      policy=DEFAULT, tier="T2")

    async def collect():
        return [f async for f in gen]

    frames = asyncio.run(collect())
    assert frames, "no frames"
    chunk_frames = [f for f in frames if f.get("event") == "chunk"]
    assert chunk_frames, "no chunk frames"
    for f in chunk_frames:
        # Chunk payload must be raw wire bytes (SSE passthrough), never
        # json.dumps()d into a wrapper dict (TypeError: bytes not serializable).
        assert isinstance(f["bytes"], bytes)
