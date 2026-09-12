"""Dynamic route registry.

Replaces the historical ``active-pool.csv`` as source of truth. ``active-pool.csv``
remains valid as a *generated* debug snapshot but is never read at request time.

Per route, the registry stores:
  - canonical_model, provider, provider_model_id
  - discount, context_length, capabilities
  - health, last_success, last_failure, error_rate
  - p50, p95, cost_per_success, quality_score (canonical-level)
  - tier eligibility

Quality is canonical-level (provider-agnostic).
Transport metrics are route-level.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from .providers.base import ProviderAdapter, ProviderModel, ProviderError
from .policy import CanonicalModel, CANONICAL_MODELS, get_canonical
from . import quality as qreg


# Discount floor. Anything below this is excluded by the registry.
DISCOUNT_FLOOR = 0.80   # 0.80 == 80% off; pay 20%


@dataclass
class RouteRecord:
    """Runtime state for one (canonical, provider, slug)."""
    provider: str
    provider_model_id: str
    canonical: str
    context_length: int
    input_price: float
    output_price: float
    discount: float | None
    capabilities: frozenset[str]
    certification_status: str = "CERTIFIED"

    # Runtime transport metrics — provider/route level
    health: str = "UNKNOWN"                 # HEALTHY | UNHEALTHY | DOWN | UNKNOWN
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    cost_per_success: float = 0.0
    cost_sample_count: int = 0
    last_actual_cost_usd: float = 0.0
    last_actual_cost_ts: float = 0.0
    price_state: str = "EXACT"               # EXACT | ESTIMATED_UPPER_BOUND | UNKNOWN
    circuit_state: str = "CLOSED"           # CLOSED | OPEN | HALF_OPEN

    # Quality (canonical-level, frozen into the record at registration time)
    quality_score: float = 0.5
    tier: str = "T2"

    @property
    def error_rate(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.failure_count / total

    def to_dict(self, include_metrics: bool = False) -> dict:
        d = {
            "provider": self.provider,
            "provider_model_id": self.provider_model_id,
            "canonical": self.canonical,
            "context_length": self.context_length,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "price_state": self.price_state,
            "discount": self.discount,
            "capabilities": sorted(self.capabilities),
            "certification_status": self.certification_status,
            "health": self.health,
            "circuit_state": self.circuit_state,
            "quality_score": self.quality_score,
            "tier": self.tier,
        }
        if include_metrics:
            d.update({
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "error_rate": self.error_rate,
                "p50_ms": self.p50_ms,
                "p95_ms": self.p95_ms,
                "last_success_ts": self.last_success_ts,
                "last_failure_ts": self.last_failure_ts,
                "cost_per_success": self.cost_per_success,
                "cost_sample_count": self.cost_sample_count,
                "last_actual_cost_usd": self.last_actual_cost_usd,
            })
        return d


class RouteRegistry:
    """In-memory registry. Thread-safe. Built once from provider catalogs +
    canonical mapping, then refreshable at runtime WITHOUT restart (dynamic
    discovery, spec G2 §11).

    The registry is the ONLY place where ``discount >= min_discount`` and
    ``capabilities`` eligibility are enforced. The selector consumes
    RouteRecord views only. No provider-name priors: ranking uses health,
    reliability, cost_per_success and latency identical for every provider.
    """

    def __init__(self, config: "object | None" = None, canon_registry=None) -> None:
        from .config import GatewayConfig, load_config  # local import: avoids cycle
        self._lock = threading.RLock()
        self._routes: dict[str, RouteRecord] = {}     # key: "{provider}:{model_id}"
        self._by_canonical: dict[str, list[str]] = {}  # canonical -> list of keys
        self._providers: dict[str, ProviderAdapter] = {}
        self._built_at: float = 0.0
        self._cfg: GatewayConfig = config if isinstance(config, GatewayConfig) else load_config()
        self._quality = qreg.QualityRegistry()
        self._canon = canon_registry if canon_registry is not None else self._make_canon()
        # health TTL state (spec G2 §10)
        self._health_ok_until: dict[str, float] = {}    # route key -> ts until which last success counts
        self._health_bad_until: dict[str, float] = {}   # route key -> ts until which last failure taints

    @staticmethod
    def _make_canon():
        from .canonical import CanonicalRegistry
        cr = CanonicalRegistry()
        cr.rebuild()
        return cr

    # ── config access ──
    def min_discount_for(self, provider: str) -> float:
        pc = self._cfg.providers.get(provider)
        return pc.min_discount if pc and pc.min_discount else self._cfg.min_discount

    def provider_enabled(self, provider: str) -> bool:
        pc = self._cfg.providers.get(provider)
        return pc.enabled if pc else True

    def config(self):
        return self._cfg

    @property
    def quality(self):
        return self._quality

    # ── Construction ──
    def register_adapter(self, adapter: ProviderAdapter) -> None:
        with self._lock:
            self._providers[adapter.name] = adapter

    def adapters(self) -> list[ProviderAdapter]:
        return list(self._providers.values())

    def get_adapter(self, name: str) -> ProviderAdapter | None:
        return self._providers.get(name)

    async def build(self, providers: Iterable[ProviderAdapter] | None = None) -> None:
        """Re-discover all provider catalogs and refresh price/discount.

        Dynamic discovery: new model slugs appear automatically when the
        provider catalog adds them (>= provider min_discount); disappeared
        slugs are dropped. Runtime metrics/health of surviving routes carry
        over. No restart needed.
        """
        targets = list(providers) if providers is not None else list(self._providers.values())
        # Honor provider-enabled policy even when the caller passes an explicit
        # list — disabled adapters must never contribute routes regardless of
        # how ``build()`` is invoked.
        targets = [a for a in targets if self.provider_enabled(a.name)]
        with self._lock:
            prev = self._routes
            new_routes: dict[str, RouteRecord] = {}
            new_by_canonical: dict[str, list[str]] = {}

            for adapter in targets:
                floor = self.min_discount_for(adapter.name)
                try:
                    models = await adapter.discover()
                except Exception as e:
                    # An adapter failure does not break the registry; we keep what's still there.
                    print(f"[registry] {adapter.name} discover failed: {e!r}")
                    continue
                for pm in models:
                    pm = await _try_price(adapter, pm)
                    # Registry keeps every discovered route — eligibility is
                    # applied lazily by ``for_canonical``/``get``/``_meets_provider_floor``
                    # so the explainability trace can still surface a discount
                    # rejection for routes that fall below the floor.
                    canonical = pm.canonical_model
                    # Accept canonicals known to the static policy registry OR
                    # present in the evidence-driven canonical registry (G2 §2).
                    known = canonical in CANONICAL_MODELS or self._canon.get(canonical) is not None
                    if not known:
                        continue
                    cm: CanonicalModel | None = get_canonical(canonical)
                    cprof = self._canon.get(canonical)
                    q_score = cprof.quality_score if cprof is not None else cm.quality
                    tier = cprof.tier_eligibility or (cm.tier if cm else None)
                    key = f"{pm.provider}:{pm.provider_model_id}"
                    rec = new_routes.get(key)
                    if rec is None:
                        old = prev.get(key)
                        rec = RouteRecord(
                            provider=pm.provider,
                            provider_model_id=pm.provider_model_id,
                            canonical=canonical,
                            context_length=pm.context_length,
                            input_price=pm.input_price,
                            output_price=pm.output_price,
                            price_state=pm.price_state,
                            discount=pm.discount,
                            capabilities=pm.capabilities,
                            certification_status=pm.certification_status,
                            quality_score=q_score,
                            tier=tier or "",
                        )
                        if old is not None:
                            # carry over live transport state across discovery refreshes
                            rec.health = old.health
                            rec.success_count = old.success_count
                            rec.failure_count = old.failure_count
                            rec.latencies_ms = list(old.latencies_ms)
                            rec.p50_ms = old.p50_ms
                            rec.p95_ms = old.p95_ms
                            rec.circuit_state = old.circuit_state
                            rec.last_success_ts = old.last_success_ts
                            rec.last_failure_ts = old.last_failure_ts
                            rec.cost_per_success = old.cost_per_success
                            rec.cost_sample_count = old.cost_sample_count
                            rec.last_actual_cost_usd = old.last_actual_cost_usd
                            rec.last_actual_cost_ts = old.last_actual_cost_ts
                        new_routes[key] = rec
                    else:
                        rec.input_price = pm.input_price
                        rec.output_price = pm.output_price
                        rec.price_state = pm.price_state
                        rec.discount = pm.discount
                        rec.context_length = pm.context_length
                    new_by_canonical.setdefault(canonical, []).append(key)

            self._routes = new_routes
            self._by_canonical = new_by_canonical
            self._built_at = time.time()

    # ── Health TTL (spec G2 §10) ──
    def apply_health_ttl(self, ttl_success_s: float | None = None, ttl_failure_s: float | None = None) -> None:
        """Expire stale health: successes older than TTL decay to UNKNOWN,
        failures stop tainting after the failure TTL. Not a probe storm —
        pure time-based decay of already-collected signals."""
        ok_ttl = ttl_success_s if ttl_success_s is not None else self._cfg.health_ttl_success_s
        bad_ttl = ttl_failure_s if ttl_failure_s is not None else self._cfg.health_ttl_failure_s
        now = time.time()
        with self._lock:
            for rec in self._routes.values():
                key = f"{rec.provider}:{rec.provider_model_id}"
                if rec.circuit_state == "OPEN":
                    # half-open after failure TTL
                    if rec.last_failure_ts and now - rec.last_failure_ts >= bad_ttl:
                        rec.circuit_state = "HALF_OPEN"
                    continue
                if rec.last_success_ts and now - rec.last_success_ts > ok_ttl:
                    # no fresh positive signal
                    if rec.last_failure_ts > rec.last_success_ts:
                        rec.health = "DEGRADED"
                    else:
                        rec.health = "UNKNOWN"

    # ── Read access ──
    def all(self) -> list[RouteRecord]:
        with self._lock:
            return list(self._routes.values())

    def for_canonical(self, canonical: str) -> list[RouteRecord]:
        """Routes that pass the provider-level eligibility gates (discount, certification)."""
        with self._lock:
            keys = self._by_canonical.get(canonical, [])
            return [self._routes[k] for k in keys
                    if k in self._routes and self._meets_provider_floor(self._routes[k])]

    def for_canonical_all(self, canonical: str) -> list[RouteRecord]:
        """Every route discovered for a canonical — including those that
        fail the discount/health gate. Used by explainability/trace to
        surface WHY a route was rejected, even if the registry hides it
        from the ranking path."""
        with self._lock:
            keys = self._by_canonical.get(canonical, [])
            return [self._routes[k] for k in keys if k in self._routes]

    def _meets_provider_floor(self, rec: RouteRecord) -> bool:
        floor = self.min_discount_for(rec.provider)
        return rec.discount is not None and rec.discount >= floor

    def admin_model_filter(self, routes: list) -> list:
        """R6 §G: drop routes whose canonical is admin-restricted to specific
        providers. Control-plane optional — any failure = no filtering."""
        try:
            from .control.integration import filter_routes_by_model_policy
            from .control import store
            return filter_routes_by_model_policy(routes, store.get_model_policy)
        except Exception:
            return routes

    def get(self, provider: str, model_id: str) -> RouteRecord | None:
        """Return a route by (provider, model_id) — eligibility-filtered.

        Below-floor routes are not returned by ``get``/``for_canonical`` so
        ranking and external lookups ignore them. The explainability/trace
        path uses ``for_canonical_all`` to surface the discount reason.
        """
        with self._lock:
            rec = self._routes.get(f"{provider}:{model_id}")
            if rec is None:
                return None
            if not self._meets_provider_floor(rec):
                return None
            return rec

    def get_any(self, provider: str, model_id: str) -> RouteRecord | None:
        """Return a route regardless of eligibility floor. Used ONLY for
        cache-affinity bookkeeping on an existing warm session route (the
        stay/switch economics still enforce health/gates explicitly)."""
        with self._lock:
            return self._routes.get(f"{provider}:{model_id}")

    def record_cost_feedback(
        self,
        provider: str,
        model_id: str,
        cost_usd: float,
        success: bool,
    ) -> None:
        """R5 §14 — actual billed-cost feedback (e.g. Provider B
        x-si-buyer-cost-micro). Updates observed cost_per_success with sample
        count. No keys, no prompt content — a single number."""
        if cost_usd < 0:
            return
        with self._lock:
            rec = self._routes.get(f"{provider}:{model_id}")
            if rec is None:
                return
            rec.last_actual_cost_usd = float(cost_usd)
            rec.last_actual_cost_ts = time.time()
            if success:
                total = rec.cost_sample_count
                rec.cost_per_success = (
                    (rec.cost_per_success * total + float(cost_usd)) / (total + 1)
                )
                rec.cost_sample_count = total + 1

    def snapshot(self) -> dict[str, dict]:
        """Generated debug snapshot — replaces active-pool.csv at rest."""
        with self._lock:
            return {k: v.to_dict(include_metrics=True) for k, v in self._routes.items()}

    # ── Write access (mutations come from transport layer) ──
    def record_success(self, provider: str, model_id: str, latency_ms: float) -> None:
        with self._lock:
            rec = self._routes.get(f"{provider}:{model_id}")
            if rec is None:
                return
            rec.last_success_ts = time.time()
            rec.success_count += 1
            # sliding-window latency (last 32 samples)
            rec.latencies_ms = (rec.latencies_ms + [latency_ms])[-32:]
            rec.p50_ms = _pct(rec.latencies_ms, 50)
            rec.p95_ms = _pct(rec.latencies_ms, 95)
            if rec.error_rate < 0.05:
                rec.health = "HEALTHY"
            elif rec.error_rate < 0.20:
                rec.health = "DEGRADED"
            else:
                rec.health = "UNHEALTHY"
            rec.circuit_state = "CLOSED"

    def record_failure(self, provider: str, model_id: str, error: ProviderError, latency_ms: float = 0.0) -> None:
        with self._lock:
            rec = self._routes.get(f"{provider}:{model_id}")
            if rec is None:
                return
            rec.last_failure_ts = time.time()
            rec.failure_count += 1
            if latency_ms > 0:
                rec.latencies_ms = (rec.latencies_ms + [latency_ms])[-32:]
                rec.p50_ms = _pct(rec.latencies_ms, 50)
                rec.p95_ms = _pct(rec.latencies_ms, 95)
            # Healthy gate: >= 3 failures with > 50% error rate triggers OPEN.
            total = rec.success_count + rec.failure_count
            if rec.failure_count >= 3 and rec.failure_count / max(1, total) > 0.5:
                rec.health = "UNHEALTHY"
                rec.circuit_state = "OPEN"
            elif rec.failure_count >= 1 and rec.error_rate > 0.30:
                rec.health = "DEGRADED"

    # ── Export helpers ──
    def canonicals(self) -> list[str]:
        with self._lock:
            return sorted(self._by_canonical.keys())

    def providers_active(self) -> set[str]:
        with self._lock:
            return {r.provider for r in self._routes.values()}

    def stats(self) -> dict:
        with self._lock:
            return {
                "routes": len(self._routes),
                "canonicals": len(self._by_canonical),
                "providers": sorted(self.providers_active()),
                "built_at": self._built_at,
            }


async def _try_price(adapter: ProviderAdapter, pm: ProviderModel) -> ProviderModel:
    try:
        return await adapter.price(pm)
    except Exception:
        return pm


def _pct(samples: list[float], p: int) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = max(0, min(len(s) - 1, int((p / 100.0) * (len(s) - 1))))
    return s[idx]
