"""Live smoke probes against Provider A and Provider B.

These probes are MINIMAL by design (G1 requirement 6: "использовать только
минимальные probes"). They:

  - exercise the dynamic registry against real catalogs,
  - prove direct provider POST round-trip on both marketplaces,
  - prove V2 selector can pick Provider A AND Provider B for the same canonical.

Run with:
    cd <model-router checkout> && PYTHONPATH=. python3 -m pytest \\
        gateway_tests/test_live_smoke.py -q -s

Requires PROVIDER_A_API_KEY and PROVIDER_B_API_KEY in env (or the private env file).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from gateway.providers.provider_a import ProviderAAdapter
from gateway.providers.provider_b import ProviderBAdapter
from gateway.registry import RouteRegistry
from gateway.selector import SelectionContext, Selector


pytestmark = pytest.mark.live

# Live smoke depends on real marketplace availability (a probe can 404/503
# when a model temporarily has no sellers) — opt-in only for determinism:
#   RUN_LIVE_TESTS=1 PYTHONPATH=. pytest gateway_tests/test_live_smoke.py -q -s
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS", "") != "1",
                       reason="live probes disabled by default; set RUN_LIVE_TESTS=1"),
]


def _env_or_skip(name: str) -> str:
    val = os.environ.get(name) or ""
    if not val:
        # Try sourcing from .env
        env_path = os.environ.get("MODEL_ROUTER_ENV_FILE", os.path.expanduser("~/.config/model-router/router.env"))
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{name}="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    return val


@pytest.mark.asyncio
async def test_provider_a_request_minimal_probe():
    if not _env_or_skip("PROVIDER_A_API_KEY"):
        pytest.skip("PROVIDER_A_API_KEY missing")
    a = ProviderAAdapter()
    models = await a.discover()
    assert models, "Provider A /v1/models returned 0 entries"
    target = next((m for m in models if m.provider_model_id == "cb/gpt-5.6-luna"), models[0])
    from gateway.providers.base import UpstreamRequest
    status, payload, err = await a.request(target, UpstreamRequest(
        body={"messages": [{"role": "user", "content": "ok"}], "max_tokens": 6, "temperature": 0.0},
        stream=False,
    ))
    assert err is None, f"Provider A returned error: {err}"
    assert status == 200
    assert payload.get("choices")


@pytest.mark.asyncio
async def test_provider_b_request_minimal_probe():
    if not _env_or_skip("PROVIDER_B_API_KEY"):
        pytest.skip("PROVIDER_B_API_KEY missing")
    a = ProviderBAdapter()
    models = await a.discover()
    assert models, "Provider B /v1/models returned 0 entries"
    target = next((m for m in models if m.provider_model_id == "gpt-5.6-luna"), models[0])
    from gateway.providers.base import UpstreamRequest
    status, payload, err = await a.request(target, UpstreamRequest(
        body={"messages": [{"role": "user", "content": "ok"}], "max_tokens": 6, "temperature": 0.0},
        stream=False,
    ))
    assert err is None, f"Provider B returned error: {err}"
    assert status == 200
    assert payload.get("choices")


@pytest.mark.asyncio
async def test_dynamic_registry_both_providers():
    if not _env_or_skip("PROVIDER_A_API_KEY") or not _env_or_skip("PROVIDER_B_API_KEY"):
        pytest.skip("API keys missing")
    reg = RouteRegistry()
    reg.register_adapter(ProviderAAdapter())
    reg.register_adapter(ProviderBAdapter())
    await reg.build(reg.adapters())
    stats = reg.stats()
    assert stats["routes"] > 0
    assert set(stats["providers"]) == {"provider_a", "provider_b"}
    # Both providers should be addressable for at least one canonical.
    sel = Selector(reg)
    ctx = SelectionContext(canonical_hint="gpt-5.6-luna", tier="T2", required_context=1000)
    _, plan, _trace = sel.choose(ctx)
    providers_in_plan = {p.provider for p in plan}
    assert "provider_a" in providers_in_plan
    assert "provider_b" in providers_in_plan
