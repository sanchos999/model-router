"""Metrics: per-canonical-model and per-provider-route counters + provider share.

Privacy-safe: counts/latencies/cost only; no prompts, no content.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class _Counters:
    requests: int = 0
    success: int = 0
    provider_errors: int = 0
    timeouts: int = 0
    context_ineligible: int = 0
    ttft_ms: list[float] = field(default_factory=list)
    total_ms: list[float] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    selected_count: int = 0
    failover_count: int = 0
    # R5 §17 cache decisions (global; stored on provider-level "provider_a" key only)
    cache_retained_count: int = 0
    cache_switch_count: int = 0
    cache_protected_count: int = 0
    estimated_savings_usd: float = 0.0
    actual_savings_usd: float = 0.0

    def snapshot(self) -> dict:
        def _pct(v: list[float], p: int) -> float | None:
            if not v:
                return None
            s = sorted(v)
            return round(s[min(len(s) - 1, int(p / 100 * (len(s) - 1)))], 1)
        return {
            "requests": self.requests,
            "success": self.success,
            "provider_errors": self.provider_errors,
            "timeouts": self.timeouts,
            "context_ineligible": self.context_ineligible,
            "ttft_p50_ms": _pct(self.ttft_ms, 50),
            "ttft_p95_ms": _pct(self.ttft_ms, 95),
            "total_p50_ms": _pct(self.total_ms, 50),
            "total_p95_ms": _pct(self.total_ms, 95),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "cost_per_success": round(self.cost_usd / self.success, 6) if self.success else None,
            "selected_count": self.selected_count,
            "failover_count": self.failover_count,
            "cache_retained_count": self.cache_retained_count,
            "cache_switch_count": self.cache_switch_count,
            "cache_protected_count": self.cache_protected_count,
            "estimated_savings_usd": round(self.estimated_savings_usd, 6),
            "actual_savings_usd": round(self.actual_savings_usd, 6),
        }.copy()


class Metrics:
    """Thread-safe two-level metrics: canonical models + provider routes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._models: dict[str, _Counters] = defaultdict(_Counters)
        self._routes: dict[str, _Counters] = defaultdict(_Counters)   # key provider:slug
        self._providers: dict[str, _Counters] = defaultdict(_Counters)
        self._canary: dict[str, _Counters] = defaultdict(_Counters)
        self._started = time.time()

    def record_attempt(self, *, canonical: str, provider: str, slug: str, error_code: str | None,
                       latency_ms: float, ttft_ms: float | None = None,
                       tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
                       failover: bool = False, context_ineligible: bool = False) -> None:
        with self._lock:
            mc = self._models[canonical]
            rc = self._routes[f"{provider}:{slug}"]
            pc = self._providers[provider]
            for c in (mc, rc, pc):
                c.requests += 1
                if failover:
                    c.failover_count += 1
                if context_ineligible:
                    c.context_ineligible += 1
                if tokens_in:
                    c.tokens_in += tokens_in
                if tokens_out:
                    c.tokens_out += tokens_out
                c.cost_usd += cost_usd
                c.total_ms.append(latency_ms)
                c.total_ms[:] = c.total_ms[-256:]
                if ttft_ms is not None:
                    c.ttft_ms.append(ttft_ms)
                    c.ttft_ms[:] = c.ttft_ms[-256:]
                if error_code is None:
                    c.success += 1
                elif error_code == "timeout":
                    c.timeouts += 1
                else:
                    c.provider_errors += 1

    def record_selected(self, *, canonical: str, provider: str, slug: str) -> None:
        with self._lock:
            self._models[canonical].selected_count += 1
            self._routes[f"{provider}:{slug}"].selected_count += 1
            self._providers[provider].selected_count += 1

    # ── R15 §7: canary experiment counters ─────────────────────────────
    def record_canary(self, *, model: str, route: str, error: bool,
                      ttft_ms: float | None, latency_ms: float,
                      cost_usd: float) -> None:
        with self._lock:
            c = self._canary.setdefault(model, _Counters())
            c.requests += 1
            c.cost_usd += cost_usd
            c.total_ms.append(latency_ms)
            c.total_ms[:] = c.total_ms[-256:]
            if error:
                c.provider_errors += 1
            else:
                c.success += 1

    def canary_snapshot(self, model: str) -> dict:
        with self._lock:
            c = self._canary.get(model)
            if c is None or c.requests == 0:
                return {}
            s = c.snapshot()
            return {"requests": s["requests"], "successes": s["success"],
                    "error_rate": round(1 - (s["success"] / max(1, s["requests"])), 4),
                    "ttft_p95_ms": s.get("ttft_p95_ms"),
                    "cost_per_success": s.get("cost_per_success")}

    def record_cache_decision(self, *, reason_code: str, estimated_saving_usd: float = 0.0) -> None:
        """R5 §17 — cache decision counters. Stored once on the global view
        (piggybacks on the 'provider_a' provider counters that always exist);
        no provider-share impact: providers never become a routing target."""
        with self._lock:
            c = self._providers["provider_a"]
            if reason_code == "CACHE_RETAINED":
                c.cache_retained_count += 1
            elif reason_code in ("CACHE_SWITCH_BREAK_EVEN", "COLD_CHEAPER", "FREE_ROUTE",
                                 "HEALTH_INELIGIBLE"):
                c.cache_switch_count += 1
            elif reason_code in ("CACHE_STAY_CHEAPER", "PRICE_UNKNOWN"):
                c.cache_protected_count += 1
                if estimated_saving_usd > 0:
                    c.estimated_savings_usd += estimated_saving_usd

    def provider_share(self) -> dict:
        with self._lock:
            # materialize all three known providers even without traffic
            for name in ("provider_a", "provider_b"):
                _ = self._providers[name]
            out = {}
            for name, c in self._providers.items():
                s = c.snapshot()
                out[name] = {
                    "eligible_routes": s.get("requests", 0) + s["selected_count"],  # alias for share endpoint
                    "selected_requests": s["selected_count"],
                    "requests": s["requests"],
                    "successes": s["success"],
                    "failures": s["provider_errors"] + s["timeouts"],
                    "timeouts": s["timeouts"],
                    "provider_errors": s["provider_errors"],
                    "failover_count": s["failover_count"],
                    "cost_usd": s["cost_usd"],
                    "cost_per_success": s["cost_per_success"],
                    "ttft_p50_ms": s["ttft_p50_ms"],
                    "ttft_p95_ms": s["ttft_p95_ms"],
                    "total_p50_ms": s["total_p50_ms"],
                    "total_p95_ms": s["total_p95_ms"],
                    "won_routing": s["success"],
                    "cache_retained_count": s["cache_retained_count"],
                    "cache_switch_count": s["cache_switch_count"],
                    "cache_protected_count": s["cache_protected_count"],
                    "estimated_savings_usd": s["estimated_savings_usd"],
                    "actual_savings_usd": s["actual_savings_usd"],
                }
            return out

    def model_metrics(self) -> dict:
        with self._lock:
            return {k: v.snapshot() for k, v in self._models.items()}

    def route_metrics(self) -> dict:
        with self._lock:
            return {k: v.snapshot() for k, v in self._routes.items()}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self._started,
                "providers": self.provider_share(),
                "models": self.model_metrics(),
                "routes": self.route_metrics(),
            }
