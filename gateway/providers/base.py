"""ProviderAdapter — universal marketplace contract.

Any marketplace (Provider A, Provider B, future) implements this. Higher layers
(classifier, selector, transport) consume ONLY this interface. Adding a new
marketplace never requires changes to classifier/router logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable


# ── Normalized datatypes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderModel:
    """Single model entry in a marketplace catalog."""
    provider: str                 # "provider_a" | "provider_b" | "<custom>"
    provider_model_id: str        # exact slug sent in the request
    canonical_model: str          # logical name (e.g., "gpt-5.6-luna")
    context_length: int           # raw provider window
    input_price: float            # USD per million tokens (effective price AFTER discount)
    output_price: float           # USD per million tokens (effective price AFTER discount)
    discount: float | None        # 0.0 - 1.0; 0.80 means 80% off
    capabilities: frozenset[str]  # {"text","streaming","json","tool_call","streaming_tool_call"}
    price_state: str = "EXACT"    # EXACT | ESTIMATED_UPPER_BOUND | UNKNOWN
    certification_status: str = "CERTIFIED"   # CERTIFIED | UNVERIFIED | UNKNOWN
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderError:
    code: str                     # stable, normalized ("timeout","rate_limit","no_healthy_sellers","payment_required","server","context_too_large","protocol","unknown")
    status: int | None            # raw HTTP status, if known
    message: str
    retries_safe: bool = False    # hint to the selector


@dataclass(frozen=True)
class UpstreamRequest:
    body: dict[str, Any]
    stream: bool
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class UpstreamChunk:
    """Single SSE/JSON chunk as it arrives — kept transport-agnostic."""
    raw: bytes
    meta: dict[str, Any] = field(default_factory=dict)
    done: bool = False


# ── Adapter interface ──────────────────────────────────────────────────────


@runtime_checkable
class ProviderAdapter(Protocol):
    """Every marketplace implements the methods below. New adapters are wired
    through the provider registry; classifier/router code knows nothing about
    individual providers."""

    name: str

    # ── Discovery & pricing ──
    async def discover(self) -> list[ProviderModel]:
        """Return all catalog entries this provider knows about right now."""

    async def price(self, model: ProviderModel) -> ProviderModel:
        """Refresh price/discount. Default returns the model unchanged."""

    # ── Capability advertisement (cheap; no HTTP) ──
    def capabilities(self, model: ProviderModel) -> frozenset[str]:
        return model.capabilities

    # ── Transport: non-streaming ──
    async def request(self, model: ProviderModel, req: UpstreamRequest) -> tuple[int, dict[str, Any], ProviderError | None]:
        """Send a non-streaming request. Returns (status, json, error)."""

    # ── Transport: streaming ──
    def stream(self, model: ProviderModel, req: UpstreamRequest) -> AsyncIterator[UpstreamChunk]:
        """Yield chunks. MUST raise ProviderTimeout to indicate TTFT/stream-idle."""

    # ── Health & usage ──
    async def health(self, model: ProviderModel) -> dict[str, Any]:
        """Return live probe result, e.g., {'ok': bool, 'latency_ms': int}."""

    def record_success(self, model: ProviderModel, latency_ms: float) -> None: ...
    def record_failure(self, model: ProviderModel, error: ProviderError) -> None: ...
    def usage(self, model: ProviderModel) -> dict[str, Any]:
        """Transport-level metrics: success rate, p50, p95, last_success/failure."""
