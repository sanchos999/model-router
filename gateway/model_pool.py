"""R4 model pool builder — canonical pool, lifecycle and routing verdicts.

Bridges three evidence sources into the /models/pool view (spec R4 §7, §12):
  * RouteRegistry       — live routes (discount, health, price, context)
  * CanonicalRegistry   — quality evidence + confidence
  * LifecycleRegistry   — statuses, dominance, deprecation/sunset

Produced artifacts (generated, rebuildable, no secrets — spec R4 §13):
  state/model-registry.json   — canonicals + routes + metrics
  state/model-aliases.json    — alias normalization table + provenance
  state/model-lifecycle.json  — lifecycle records (via LifecycleRegistry)

Cache safety (spec R4 §10): the pool builder NEVER mutates selector state or
active session state. Existing warm session routes may continue when healthy,
eligible (>=80%), context-fitting, quality-floor-satisfying and not
hard-excluded (DOMINATED/DEPRECATED/SUNSET/DISABLED) — see
``session_route_permitted``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .canonical import CanonicalRegistry
from .lifecycle import (
    CACHE_SAFE_STATUSES,
    CORE,
    EXCLUDED_FROM_ROUTING,
    LifecycleRegistry,
    UNAVAILABLE,
    WATCH,
)
from .normalize import ALIAS_TABLE, family_of, generation_of, normalize, variant_of
from .registry import RouteRegistry
from .selector import safe_context

from gateway.state_paths import state_file

POOL_REGISTRY_PATH = os.environ.get(
    "GW_MODEL_REGISTRY_OUT",
    state_file("model-registry.json"),
)
POOL_ALIASES_PATH = os.environ.get(
    "GW_MODEL_ALIASES_OUT",
    state_file("model-aliases.json"),
)

# Quality confidence -> quality status label (spec R4 §6).
_CONF_TO_STATUS = {
    "VERIFIED": "VERIFIED",
    "PROVISIONAL": "PROVISIONAL",
    "INCOMPLETE": "INCOMPLETE",
    "UNKNOWN": "UNKNOWN_FRONTIER",
}


def session_route_permitted(
    lifecycle: LifecycleRegistry,
    canonical: str,
    *,
    health_ok: bool,
    discount_ok: bool,
    context_ok: bool,
    quality_floor_ok: bool,
) -> tuple[bool, str]:
    """R4 §10 — may an existing (warm) session keep its current route after a
    refresh? Never used for NEW decisions."""
    status = lifecycle.status(canonical, default="WATCH")
    if status in ("DISABLED", "SUNSET"):
        return False, f"lifecycle:{status}"
    if status == "DOMINATED" or status == "DEPRECATED":
        return False, f"lifecycle:{status}"
    if not health_ok:
        return False, "health"
    if not discount_ok:
        return False, "discount"
    if not context_ok:
        return False, "context"
    if not quality_floor_ok:
        return False, "quality_floor"
    return True, "ok"


class ModelPool:
    """Builds the canonical model pool view and persists generated artifacts."""

    def __init__(
        self,
        registry: RouteRegistry,
        canon: CanonicalRegistry,
        lifecycle: LifecycleRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._canon = canon
        self._lifecycle = lifecycle or LifecycleRegistry()
        self._lock = threading.RLock()

    @property
    def lifecycle(self) -> LifecycleRegistry:
        return self._lifecycle

    # ── main build ───────────────────────────────────────────────────────
    def build(self) -> dict:
        routes = self._registry.all()
        profiles = self._canon.all()
        cfg = self._registry.config()

        by_canonical: dict[str, list] = {}
        for r in routes:
            by_canonical.setdefault(r.canonical, []).append(r)

        pool: dict[str, dict] = {}
        for canonical, rlist in sorted(by_canonical.items()):
            prof = profiles.get(canonical)
            cprof = self._canon.get(canonical)
            routes_out = []
            eligible = False
            for r in sorted(rlist, key=lambda x: (x.provider, x.provider_model_id)):
                floor = self._registry.min_discount_for(r.provider)
                disc_ok = r.discount is not None and r.discount >= floor
                if disc_ok:
                    eligible = True
                routes_out.append({
                    "provider": r.provider,
                    "provider_model_id": r.provider_model_id,
                    "eligible": disc_ok,
                    "health": r.health,
                    "circuit_state": r.circuit_state,
                    "price_state": r.price_state,
                    "input_price": r.input_price,
                    "output_price": r.output_price,
                    "discount": r.discount,
                    "advertised_context": r.context_length,
                    "certified_context": r.context_length if r.certification_status == "CERTIFIED" else None,
                    "safe_context": safe_context(r.context_length),
                    "p50_ms": r.p50_ms,
                    "p95_ms": r.p95_ms,
                    "success_count": r.success_count,
                    "failure_count": r.failure_count,
                    "success_rate": round(1 - r.error_rate, 4),
                    "status": "ELIGIBLE" if disc_ok else "BELOW_FLOOR",
                })
            status = self._lifecycle.status(canonical, default="WATCH")
            if not eligible and status in ("CORE", "SPECIALIST", "WATCH", "FALLBACK_ONLY"):
                status = "UNAVAILABLE"
            quality_status = _CONF_TO_STATUS.get(cprof.confidence if cprof else "UNKNOWN", "UNKNOWN_FRONTIER")
            pool[canonical] = {
                "canonical": canonical,
                "display_name": getattr(cprof, "display_name", canonical) if cprof else canonical,
                "family": family_of(canonical),
                "generation": generation_of(canonical),
                "variant": variant_of(canonical),
                "lifecycle": status,
                "quality_status": quality_status,
                "quality_score": cprof.quality_score if cprof else 0.0,
                "tier": cprof.tier_eligibility if cprof else None,
                "routes": routes_out,
                "eligible_providers": sorted({r["provider"] for r in routes_out if r["eligible"]}),
                "reason": self._reason(canonical, status, prof),
            }

        # canonicals known to quality evidence/canonical registry but without routes
        for canonical, prof in profiles.items():
            if canonical not in pool:
                cprof = self._canon.get(canonical)
                status = self._lifecycle.status(canonical, default="WATCH")
                pool[canonical] = {
                    "canonical": canonical,
                    "display_name": canonical,
                    "family": family_of(canonical),
                    "generation": generation_of(canonical),
                    "variant": variant_of(canonical),
                    "lifecycle": status,
                    "quality_status": _CONF_TO_STATUS.get(prof.confidence, "UNKNOWN_FRONTIER"),
                    "quality_score": prof.quality_score,
                    "tier": cprof.tier_eligibility if cprof else None,
                    "routes": [],
                    "eligible_providers": [],
                    "reason": "no live route on any provider",
                }

        # lifecycle refresh transitions (spec R4 §9) — data-driven, conservative
        known = set(pool.keys())
        eligible_set = {c for c, v in pool.items() if v["eligible_providers"]}
        transitions = self._lifecycle.apply_refresh(
            eligible_canonicals=eligible_set,
            known_canonicals=known,
        )
        # Initial seeding (spec R4 §4): a canonical with quality evidence
        # (VERIFIED/PROVISIONAL + tier) and a live eligible route is CORE on
        # first sight. No evidence -> WATCH. Manual records never overridden.
        for canonical, v in pool.items():
            if not v["eligible_providers"]:
                continue
            rec = self._lifecycle.get(canonical)
            if rec is not None and (rec.manual or rec.status not in ("", WATCH, UNAVAILABLE)):
                continue
            if v["quality_status"] in ("VERIFIED", "PROVISIONAL") and v["tier"]:
                if v["lifecycle"] != CORE:
                    self._lifecycle.set_status(canonical, CORE, reason="seed: quality evidence + eligible route")
                    transitions.append({"canonical": canonical, "from": v["lifecycle"], "to": CORE})
                    v["lifecycle"] = CORE
        # re-read statuses post-refresh
        for canonical, v in pool.items():
            v["lifecycle"] = self._lifecycle.status(canonical, default=v["lifecycle"])
            if not v["eligible_providers"] and v["lifecycle"] in ("CORE", "SPECIALIST", "WATCH", "FALLBACK_ONLY"):
                v["lifecycle"] = "UNAVAILABLE"
            v["reason"] = self._reason(canonical, v["lifecycle"], profiles.get(canonical))
            elig = [r for r in v["routes"] if r["eligible"]]
            best = None
            if elig:
                healthy = [r for r in elig if r["health"] == "HEALTHY"]
                cand = healthy or elig
                best = min(cand, key=lambda r: r["input_price"] + r["output_price"])
            v["best_current_route"] = ({
                "route": f"{best['provider']}:{best['provider_model_id']}",
                "health": best["health"],
                "price_state": best["price_state"],
                "input_price": best["input_price"],
                "output_price": best["output_price"],
                "safe_context": best["safe_context"],
            } if best else None)

        counts: dict[str, int] = {}
        for v in pool.values():
            counts[v["lifecycle"]] = counts.get(v["lifecycle"], 0) + 1

        summary = {
            "built_at": time.time(),
            "live_provider_models_total": len(routes),
            "canonical_total": len(pool),
            "eligible_canonicals": len(eligible_set),
            "counts": counts,
            "transitions": transitions,
            "min_discount": cfg.min_discount,
        }
        out = {"summary": summary, "models": pool}
        with self._lock:
            self._last = out
        self._persist_registry(out)
        self._persist_aliases()
        return out

    def last(self) -> dict:
        with self._lock:
            return getattr(self, "_last", None) or {}

    # ── reasoning strings ────────────────────────────────────────────────
    def _reason(self, canonical: str, status: str, prof) -> str:
        if status == "DISABLED":
            return "manual policy ban"
        if status == "SUNSET":
            rec = self._lifecycle.get(canonical)
            return f"sunset {rec.sunset_date}" if rec and rec.sunset_date else "sunset scheduled"
        if status == "DEPRECATED":
            rec = self._lifecycle.get(canonical)
            src = rec.deprecation_source if rec else ""
            succ = rec.successor if rec else ""
            base = f"deprecated ({src})" if src else "deprecated"
            return f"{base}; successor={succ}" if succ else base
        if status == "DOMINATED":
            rec = self._lifecycle.get(canonical)
            return rec.reason if rec and rec.reason else "dominated by a proven-better canonical"
        if status == "UNAVAILABLE":
            return "no eligible (>=80%) route right now"
        if status == "WATCH":
            return "frontier/insufficient quality evidence; not auto-selected for high tiers"
        if status == "SPECIALIST":
            return "strong for specific task classes"
        if status == "FALLBACK_ONLY":
            return "failover only, not primary"
        if status == "CORE":
            return "normal routing pool, evidence-backed"
        return status

    # ── artifact persistence (generated, rebuildable, no secrets) ───────
    def _persist_registry(self, out: dict) -> None:
        try:
            p = Path(POOL_REGISTRY_PATH)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _persist_aliases(self) -> None:
        try:
            payload = {
                "updated_at": time.time(),
                "provider_heads": sorted([
                    "cb", "cx", "cc", "cmc", "ocg", "ali", "ag", "cbcn", "cp", "zai",
                ]),
                "vendor_heads": sorted([
                    "moonshotai", "zai-org", "z-ai", "qwen", "deepseek",
                    "minimaxai", "xai", "meta", "nvidia", "mistralai",
                ]),
                "alias_table": ALIAS_TABLE,
                "normalization": "strip provider/vendor heads recursively -> lowercase -> exact alias lookup; NO fuzzy merging",
            }
            p = Path(POOL_ALIASES_PATH)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass
