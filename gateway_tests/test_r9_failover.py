"""R9 failover regression tests — same-canonical failover, health penalty,
terminal error, privacy-safe trace.

Run: cd <model-router checkout> && PYTHONPATH=. python3 -m pytest gateway_tests/test_r9_failover.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROUTER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROUTER_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from gateway.providers.base import (  # noqa: E402
    ProviderAdapter, ProviderModel, ProviderError, UpstreamChunk,
    UpstreamRequest,
)
from gateway.registry import RouteRegistry  # noqa: E402
from gateway.selector import Selector, SelectionContext, PlanStep  # noqa: E402
from gateway.canonical import CanonicalRegistry  # noqa: E402
from gateway.lifecycle import LifecycleRegistry  # noqa: E402
from gateway.transport import execute_plan, stream_plan  # noqa: E402
from gateway.timeouts import TimeoutPolicy  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

class ScriptedAdapter(ProviderAdapter):
    """Adapter whose per-route behaviour is scripted by the test."""

    name = "stub"

    def __init__(self, scripts: dict[str, str]):
        # route key "provider:slug" -> "ok" | "500" | "429" | "timeout" | "net"
        self.scripts = scripts
        self.calls: list[str] = []

    def discover_models(self):
        return []

    async def discover(self):
        return []

    def pricing(self, pm: ProviderModel):
        return pm

    async def request(self, pm: ProviderModel, req: UpstreamRequest):
        key = f"{self.name}:{pm.provider_model_id}"
        self.calls.append(key)
        mode = self.scripts.get(key, "ok")
        if mode == "500":
            return 500, {"error": "boom"}, ProviderError(code="server_error", status=500, message="HTTP 500")
        if mode == "429":
            return 429, {"error": "rate"}, ProviderError(code="rate_limit", status=429, message="HTTP 429")
        if mode == "timeout":
            await asyncio.sleep(10)
            return 200, {}, None
        if mode == "net":
            raise ConnectionError("refused")
        return 200, {"id": "x", "choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, None

    async def stream(self, pm: ProviderModel, req: UpstreamRequest):
        key = f"{self.name}:{pm.provider_model_id}"
        mode = self.scripts.get(key, "ok")
        if mode == "timeout":
            await asyncio.sleep(10)
        if mode in ("500", "429", "net"):
            raise ConnectionError(f"stream {mode}")
        yield UpstreamChunk(raw=b'data: {"delta":"hi"}\n\n', meta={}, done=False)
        yield UpstreamChunk(raw=b"", meta={}, done=True)


def _make_registry(routes: list[tuple[str, str]], adapter: ProviderAdapter) -> RouteRegistry:
    """registry with stub routes (canonical, slug) all on provider 'stub'.
    Inserts RouteRecords directly (build() would hit live catalogs)."""
    from gateway.config import GatewayConfig
    from gateway.registry import RouteRecord
    cfg = GatewayConfig()
    reg = RouteRegistry(config=cfg)
    reg.register_adapter(adapter)
    with reg._lock:
        for canonical, slug in routes:
            key = f"stub:{slug}"
            rec = RouteRecord(
                provider="stub", provider_model_id=slug, canonical=canonical,
                context_length=100000, input_price=0.1, output_price=0.1,
                discount=0.9, capabilities=frozenset({"text"}),
            )
            reg._routes[key] = rec
            reg._by_canonical.setdefault(canonical, []).append(key)
    return reg


POLICY = TimeoutPolicy(connect_s=1.0, first_response_s=0.5, stream_idle_s=1.0,
                       normal_total_s=5.0, t4_total_s=5.0, pre_first_failover_s=1.2)


def _plan(registry: RouteRegistry, canonical: str, n: int) -> list[PlanStep]:
    return [PlanStep(canonical=canonical, provider="stub", provider_model_id=slug,
                     context_length=100000, discount=0.9, quality_score=0.8,
                     reason="test", safe_context_limit=90000)
            for _, slug in [(canonical, f"slug{i}") for i in range(n)]]


async def _collect_stream(plan, registry, policy):
    return [frame async for frame in stream_plan(plan=plan, registry=registry,
        body={}, policy=policy, tier="T2")]


# ── execute_plan failover matrix ────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["500", "429", "net"])
def test_primary_error_secondary_succeeds(mode):
    """Primary route 500/429/network-error -> secondary same-canonical succeeds,
    response is the secondary's, failure recorded for the primary."""
    adapter = ScriptedAdapter({f"stub:slug0": mode})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    plan = _plan(reg, "m", 2)
    step, failures, meta = asyncio.run(execute_plan(
        plan=plan, registry=reg, body={}, stream=False, policy=POLICY, tier="T2"))
    assert step.provider_model_id == "slug1"
    assert len(failures) == 1
    assert failures[0].error_code == ("server_error" if mode == "500"
                                      else "rate_limit" if mode == "429" else "protocol")
    # immediate health penalty on the failed route
    rec = reg.get("stub", "slug0")
    assert rec.failure_count >= 1 and rec.last_failure_ts > 0


def test_primary_timeout_secondary_succeeds():
    """Primary times out on first response (0.5s policy) -> secondary wins.
    Same-canonical failover completes inside ONE router request."""
    adapter = ScriptedAdapter({"stub:slug0": "timeout"})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    plan = _plan(reg, "m", 2)
    t0 = time.time()
    step, failures, meta = asyncio.run(execute_plan(
        plan=plan, registry=reg, body={}, stream=False, policy=POLICY, tier="T2"))
    elapsed = time.time() - t0
    assert step.provider_model_id == "slug1"
    assert failures[0].error_code == "timeout"
    assert failures[0].latency_ms >= 400  # waited the first-response budget
    assert elapsed < 4.0  # did not wait the full total budget


def test_all_fail_deterministic_structured_error():
    """All routes fail -> TransportError with per-route failure records."""
    adapter = ScriptedAdapter({"stub:slug0": "500", "stub:slug1": "429"})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    plan = _plan(reg, "m", 2)
    from gateway.transport import TransportError
    with pytest.raises(TransportError) as ei:
        asyncio.run(execute_plan(plan=plan, registry=reg, body={},
                                 stream=False, policy=POLICY, tier="T2"))
    codes = [f.error_code for f in ei.value.failures]
    assert codes == ["server_error", "rate_limit"]


def test_failed_route_not_blindly_reselected():
    """After a failure the recent-fail penalty pushes the failed route behind
    clean same-canonical routes for the NEXT request's ranking."""
    adapter = ScriptedAdapter({"stub:slug0": "500"})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    # first request: slug0 fails, slug1 wins
    plan = _plan(reg, "m", 2)
    step, _, _ = asyncio.run(execute_plan(plan=plan, registry=reg, body={},
                                           stream=False, policy=POLICY, tier="T2"))
    assert step.provider_model_id == "slug1"
    # selector ranking must now prefer slug1 over the recently failed slug0
    from gateway.selector import _key_score
    r0 = reg.get("stub", "slug0")
    r1 = reg.get("stub", "slug1")
    assert _key_score(r1) < _key_score(r0), "recently failed route must rank worse"
    # after the failure-TTL window the penalty decays
    r0.last_failure_ts = time.time() - 100
    assert _key_score(r0, now=time.time())[1] == 0


# ── selector plan: alternate canonical ordering ────────────────────────────

def test_same_canonical_first_alternate_second():
    """Plan lists every same-canonical route before any alternate canonical."""
    adapter = ScriptedAdapter({})
    reg = _make_registry([("m", "s0"), ("m", "s1"), ("alt", "a0")], adapter)
    canon = CanonicalRegistry()
    canon.rebuild()
    sel = Selector(reg, canon, LifecycleRegistry())
    ctx = SelectionContext(canonical_hint="m", tier="T2", task_class="NORMAL_CODING",
                           capabilities_required=frozenset({"text"}))
    _, plan, trace = sel.choose(ctx)
    same = [p for p in plan if p.canonical == "m"]
    others = [p for p in plan if p.canonical != "m"]
    if others:
        # every same-canonical step precedes the first alternate
        first_alt_idx = next(i for i, p in enumerate(plan) if p.canonical != "m")
        assert first_alt_idx >= len(same)



def test_stream_pre_first_token_chain_is_bounded():
    adapter = ScriptedAdapter({"stub:slug0": "timeout", "stub:slug1": "ok"})
    reg = _make_registry([("gpt-test", "slug0"), ("gpt-test", "slug1")], adapter)
    plan = _plan(reg, "gpt-test", 2)
    t0 = time.monotonic()
    frames = asyncio.run(_collect_stream(plan, reg, POLICY))
    elapsed = time.monotonic() - t0
    assert elapsed < 1.2
    assert any(f.get("event") == "done" for f in frames)


# ── stream_plan: failover before first token + terminal error ──────────────

def _stream_frames(reg, plan, tmp_path, body=None):
    os.environ["GATEWAY_STATE_DIR"] = str(tmp_path)
    import importlib
    import gateway.state_paths
    importlib.reload(gateway.state_paths)

    async def run():
        return [f async for f in stream_plan(plan=plan, registry=reg,
                                              body=body or {}, policy=POLICY,
                                              tier="T2", request_id="req-test")]

    return asyncio.run(run())


def test_stream_failover_before_first_token(tmp_path):
    """SSE failure BEFORE any chunk was emitted -> failover to the next route,
    no duplicate output, winner completes the stream."""
    adapter = ScriptedAdapter({"stub:slug0": "net"})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    plan = _plan(reg, "m", 2)
    frames = _stream_frames(reg, plan, tmp_path)
    kinds = [f["event"] for f in frames]
    assert "failover" in kinds
    assert kinds[-1] == "done"
    chunk_bytes = b"".join(f.get("bytes") or b"" for f in frames
                           if f["event"] == "chunk")
    assert chunk_bytes.count(b'"delta":"hi"') == 1  # exactly one provider's output


def test_stream_all_fail_terminal_error_not_silent_done(tmp_path):
    """All routes fail while streaming -> explicit terminal event with
    attempts — the client must see an error, never a silent empty [DONE]."""
    adapter = ScriptedAdapter({"stub:slug0": "net", "stub:slug1": "net"})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    plan = _plan(reg, "m", 2)
    frames = _stream_frames(reg, plan, tmp_path)
    kinds = [f["event"] for f in frames]
    assert "done" not in kinds
    assert kinds[-1] == "terminal"
    term = frames[-1]
    assert term["reason"] == "plan_exhausted"
    assert len(term["attempts"]) == 2
    assert term["attempts"][0]["failure_class"]


def test_failover_trace_written_privacy_safe(tmp_path):
    """Decision trace JSONL: request_id, canonical, attempts, winner —
    and no prompt content even when the body carries one."""
    adapter = ScriptedAdapter({"stub:slug0": "500"})
    reg = _make_registry([("m", "slug0"), ("m", "slug1")], adapter)
    plan = _plan(reg, "m", 2)
    body = {"messages": [{"role": "user", "content": "SECRET-PROMPT-XYZ"}]}
    frames = _stream_frames(reg, plan, tmp_path, body=body)
    assert frames[-1]["event"] == "done"
    trace_file = tmp_path / "failover-trace.jsonl"
    assert trace_file.exists()
    line = trace_file.read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["request_id"] == "req-test"
    assert entry["canonical"] == "m"
    assert entry["winner"]["route"] == "stub:slug1"
    assert entry["attempts"][0]["failure_class"] == "protocol"
    assert "SECRET-PROMPT-XYZ" not in line  # privacy-safe: no prompts


# ── end-to-end via app: SSE terminal error reaches the client ──────────────

def test_sse_terminal_error_reaches_client(monkeypatch, tmp_path):
    """Full app path: exhausted plan -> SSE stream carries a structured
    upstream_exhausted error event (not a bare empty [DONE])."""
    monkeypatch.setenv("GATEWAY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("GW_CONTROL_DB", str(tmp_path / "control.db"))
    import importlib
    import gateway.state_paths as sp
    importlib.reload(sp)
    import gateway.control.store as store
    importlib.reload(store)
    import gateway.app as appmod
    importlib.reload(appmod)

    adapter = ScriptedAdapter({"provider_a:cb/bad": "net"})
    appmod._registry.register_adapter(adapter)
    # force a plan whose only route is the scripted-failing one
    step = PlanStep(canonical="m", provider="provider_a",
                    provider_model_id="cb/bad", context_length=100000,
                    discount=0.9, quality_score=0.8, reason="test",
                    safe_context_limit=90000)

    client = TestClient(appmod.app)
    captured = {}

    async def fake_stream(**kwargs):
        async for f in stream_plan(plan=[step], registry=appmod._registry,
                                   body={}, policy=POLICY, tier="T2",
                                   request_id="req-e2e"):
            yield f

    monkeypatch.setattr(appmod, "stream_plan", fake_stream)

    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "main-auto", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}) as r:
        assert r.status_code == 200
        text = "".join(chunk.decode() for chunk in r.iter_raw())
    assert "upstream_exhausted" in text
    assert "plan_exhausted" in text
    assert text.count("[DONE]") == 1
