"""V2 selector — two-level separation (spec G2 §1, §7, §8, §9, §16).

LEVEL A — canonical model selection:
  required capabilities → minimum quality floor → context requirement →
  confidence/evidence → candidate canonical models. QUALITY FIRST.

LEVEL B — provider/route selection (within the chosen canonical):
  discount >= min_discount → health/circuit → context fit (route-level
  safe_context) → semantic certification → reliability → sticky/cache
  affinity → cost_per_success → latency.

Provider name is NEVER a ranking factor: Provider A and Provider B compete on the
same metrics. No static provider prior exists anywhere in this module.

Failover (spec G2 §9): same canonical / another route FIRST, alternate
canonical model SECOND.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Iterable

from .canonical import CanonicalProfile, CanonicalRegistry
from .policy import CanonicalModel, get_canonical
from .registry import DISCOUNT_FLOOR, RouteRecord, RouteRegistry


# route-level safe context (spec G2 §16, mirrors route_engine semantics)
RESERVED_OUTPUT = 8192
SAFE_CONTEXT_RATIO = 0.90

# R5 §4: UNKNOWN is never free — sentinel ranks behind every priced route.
UNKNOWN_COST_SENTINEL = 1e9

# R5 §16 machine-readable reason codes for route-level rejections.
REASON_CODE_MAP = {
    "health": "HEALTH_INELIGIBLE",
    "circuit": "HEALTH_INELIGIBLE",
    "discount": "DISCOUNT_INELIGIBLE",
    "context": "CONTEXT_INELIGIBLE",
    "capability_gap": "CAPABILITY_INELIGIBLE",
    "certification": "CERTIFICATION_INELIGIBLE",
    "quality_floor": "QUALITY_INELIGIBLE",
    "quality_unknown": "QUALITY_INELIGIBLE",
    "lifecycle": "LIFECYCLE_INELIGIBLE",
    "canonical_unknown": "QUALITY_INELIGIBLE",
}


def reason_code(reason: str) -> str:
    """Map a rejection reason string to its machine-readable R5 code."""
    for prefix, code in REASON_CODE_MAP.items():
        if reason == prefix or reason.startswith(prefix + ":"):
            return code
    return "OTHER"


def safe_context(context_length: int, reserved_output: int = RESERVED_OUTPUT) -> int:
    return min(int(context_length * SAFE_CONTEXT_RATIO), context_length - reserved_output)


@dataclass(frozen=True)
class SelectionContext:
    """Inputs to the selector — pure data, no I/O. No raw prompt text."""
    canonical_hint: str | None = None                 # explicit canonical (model=canonical) or mapped slug
    provider_hint: str | None = None                  # sticky preference ONLY (never a prior)
    prompt_cache_key: str | None = None
    required_context: int = 0
    reserved_output: int = 1024
    capabilities_required: frozenset[str] = frozenset({"text", "streaming"})
    tier: str | None = None                           # taCHANGE_ME tier; drives quality floor
    task_class: str | None = None
    exclude_route_keys: frozenset[str] = frozenset()  # routes removed by failover
    allow_unknown_quality: bool = False               # explicit-policy escape for frontier models
    # R5 §7 cache state (privacy-safe: route key + counts only, no prompt).
    current_route_key: str | None = None              # session's established route
    cache_state: str = "UNKNOWN"                      # WARM | LIKELY_WARM | COLD | UNKNOWN
    warm_prefix_tokens: int = 0                       # estimated warm prefix size
    # R6 §I: hard admin override — route locked to exactly this provider:slug.
    forced_route: str | None = None


@dataclass(frozen=True)
class PlanStep:
    """One step in the failover plan. transport consumes this in order."""
    canonical: str
    provider: str
    provider_model_id: str
    context_length: int
    discount: float
    quality_score: float
    reason: str                                   # chosen by which rule
    safe_context_limit: int = 0
    context_eligible: bool = True


def _quality_floor(tier: str | None, floors: dict | None = None) -> float:
    """Quality gates per tier (R6 §H: runtime-editable via routing policy)."""
    table = floors or {"T4": 0.75, "T3": 0.55, "T2": 0.40, "T1": 0.0}
    return float(table.get((tier or "").upper(), 0.0))


def _tier_rank(tier: str | None) -> int:
    return {"T1": 1, "T2": 2, "T3": 3, "T4": 4}.get((tier or "").upper(), 0)


def _route_passes(record: RouteRecord, ctx: SelectionContext, min_discount: float) -> str | None:
    """LEVEL B filter. Returns rejection reason or None."""
    if record.health in ("DOWN", "UNHEALTHY"):
        return f"health:{record.health}"
    if record.circuit_state == "OPEN":
        return "circuit:OPEN"
    if record.discount is None or record.discount < min_discount:
        return f"discount:{record.discount}"
    sc = safe_context(record.context_length, RESERVED_OUTPUT + ctx.reserved_output)
    if ctx.required_context > 0 and sc < ctx.required_context:
        return f"context:{sc}<{ctx.required_context}"
    if not ctx.capabilities_required.issubset(record.capabilities):
        return f"capability_gap:{sorted(ctx.capabilities_required - record.capabilities)}"
    if str(record.certification_status).upper() not in {"CERTIFIED", "CERTIFIED_ROUTE", "PASS", "VERIFIED"}:
        return f"certification:{record.certification_status}"
    return None


def _reliability(record: RouteRecord) -> float:
    return 1.0 - record.error_rate


# ── R5 economics: everything normalized to USD/request ──────────────────────

def estimate_request_cost(
    record: RouteRecord,
    tokens_in: int,
    tokens_out: int,
) -> tuple[float | None, str]:
    """R5 §5 — expected cost of ONE request in USD.

    Returns (cost, state): cost is None iff price state is UNKNOWN
    (UNKNOWN != 0, UNKNOWN != FREE). ESTIMATED_UPPER_BOUND is preserved as a
    state — never reported as exact."""
    if record.price_state not in ("EXACT", "ESTIMATED_UPPER_BOUND"):
        return None, "UNKNOWN"
    cost = (tokens_in / 1e6) * record.input_price + (tokens_out / 1e6) * record.output_price
    return cost, record.price_state


def is_free(record: RouteRecord) -> bool:
    """R5 §4 — TRUE free requires proven effective price = 0 (EXACT state).
    UNKNOWN is never free."""
    return (
        record.price_state == "EXACT"
        and record.input_price == 0.0
        and record.output_price == 0.0
    )


def effective_cost_per_success(
    record: RouteRecord,
    est_request_cost: float | None,
    reliability_min_samples: int = 10,
) -> tuple[float | None, str, dict]:
    """R5 §6 — cost weighted by reliability, with honest evidence tracking.

    * observed cost_per_success when sample size is sufficient -> EXACT;
    * otherwise conservative estimate: request cost / reliability penalty
      (reliability floor 0.3; unknown reliability defaults to 0.5) -> ESTIMATED;
    * UNKNOWN price stays UNKNOWN (never free, never 0).
    Sample count / confidence returned alongside — no fake precision.
    """
    evidence = {
        "cost_sample_count": record.cost_sample_count,
        "reliability": round(_reliability(record), 4)
        if (record.success_count + record.failure_count) > 0 else None,
    }
    if record.cost_sample_count >= reliability_min_samples and record.cost_per_success > 0:
        return record.cost_per_success, "EXACT", evidence
    if est_request_cost is None:
        return None, "UNKNOWN", evidence
    if (record.success_count + record.failure_count) > 0:
        rel = max(0.3, _reliability(record))
    else:
        rel = 0.5
    return est_request_cost / rel, "ESTIMATED", {**evidence, "reliability_penalty": rel}


def cache_switch_economics(
    *,
    stay_cost: float | None,
    candidate_cost: float | None,
    cache_state: str,
    warm_prefix_tokens: int,
    candidate_input_price: float,
    candidate_free: bool,
    candidate_success_rate: float,
    candidate_healthy: bool,
    horizon: float = 3.0,
    margin: float = 1.25,
    free_min_success_rate: float = 0.90,
) -> dict:
    """R5 §8-§10 — stay-vs-switch break-even for a warm session route.

    switch iff  projected_saving > cache_rebuild_penalty * margin
      projected_saving    = (stay_cost - candidate_cost) * horizon
      rebuild_penalty     = warm_prefix/1e6 * candidate_input_price (ESTIMATED;
                            LIKELY_WARM halves the penalty via confidence factor;
                            COLD/UNKNOWN -> 0)
    FREE candidate: monetary penalty is 0 but the non-monetary gate (reliability)
    still applies. UNKNOWN price can never win (UNKNOWN != FREE).
    """
    out = {
        "switch": False,
        "reason_code": "CACHE_STAY_CHEAPER",
        "per_request_saving": None,
        "projected_saving": None,
        "cache_rebuild_penalty": None,
        "penalty_state": "ESTIMATED",
    }
    if not candidate_healthy:
        # unhealthy candidate never wins; caller's hard gates already exclude it
        out["reason_code"] = "HEALTH_INELIGIBLE"
        return out
    if candidate_cost is None:
        out["reason_code"] = "PRICE_UNKNOWN"
        return out
    if stay_cost is None and not candidate_free:
        # Can't prove the switch saves money: stay (conservative, R5 §4).
        out["reason_code"] = "PRICE_UNKNOWN"
        return out
    per_req = (stay_cost - candidate_cost) if stay_cost is not None else -candidate_cost
    out["per_request_saving"] = per_req
    out["projected_saving"] = per_req * horizon

    if candidate_free:
        # R5 §10: monetary rebuild penalty 0; non-monetary cost = reliability.
        out["cache_rebuild_penalty"] = 0.0
        out["penalty_state"] = "EXACT_ZERO_FREE"
        if candidate_success_rate >= free_min_success_rate:
            out["switch"] = True
            out["reason_code"] = "FREE_ROUTE"
        else:
            out["reason_code"] = "FREE_ROUTE_REJECTED_RELIABILITY"
        return out

    if cache_state in ("WARM", "LIKELY_WARM"):
        confidence = 1.0 if cache_state == "WARM" else 0.5
        penalty = (warm_prefix_tokens / 1e6) * candidate_input_price * confidence
        out["cache_rebuild_penalty"] = penalty
        if out["projected_saving"] > penalty * margin:
            out["switch"] = True
            out["reason_code"] = "CACHE_SWITCH_BREAK_EVEN"
        else:
            out["reason_code"] = "CACHE_STAY_CHEAPER"
        return out

    # COLD / UNKNOWN: no warm prefix at stake on this pairing.
    out["cache_rebuild_penalty"] = 0.0
    out["penalty_state"] = "NONE_COLD"
    if per_req > 0:
        out["switch"] = True
        out["reason_code"] = "COLD_CHEAPER"
    return out


def _route_cost(record: RouteRecord) -> tuple[float, str]:
    """Ranking cost + its state. UNKNOWN is never free.

    Returns (cost_value, state) where cost_value is:
      * actual observed cost_per_success (EXACT) when present,
      * route input+output price sum when priced (EXACT or ESTIMATED_UPPER_BOUND),
      * sentinel 1e9 for UNKNOWN (ranks behind every priced route).
    """
    if record.cost_per_success:
        return record.cost_per_success, "EXACT"
    price_sum = record.input_price + record.output_price
    if price_sum > 0:
        state = record.price_state if record.price_state in ("EXACT", "ESTIMATED_UPPER_BOUND") else "EXACT"
        return price_sum, state
    return 1e9, "UNKNOWN"


def _key_score(record: RouteRecord, now: float | None = None) -> tuple:
    """LEVEL B ranking. Provider name is not part of the key.

    R9 failover fix: a route that failed within the failure-TTL window is
    deprioritized for subsequent plans, so the next request does not
    blindly re-select the route that just timed out. A success clears the
    penalty (last_success_ts newer than last_failure_ts)."""
    health_score = {"HEALTHY": 0, "DEGRADED": 1, "UNKNOWN": 2, "UNHEALTHY": 3, "DOWN": 4}.get(record.health, 2)
    t = now if now is not None else time.time()
    recent_fail = (record.last_failure_ts > record.last_success_ts
                   and t - record.last_failure_ts < 45.0)
    cost, _ = _route_cost(record)
    return (
        health_score,
        1 if recent_fail else 0,               # recent failure: rank after clean routes
        -record.success_count,             # prefer routes with observed successes
        -_reliability(record),
        cost,
        record.p95_ms or 9999.0,
        record.provider_model_id,          # deterministic tiebreak only
    )


def _cost_reason(record: RouteRecord) -> str:
    """Observability string that matches EXACTLY what the ranking used."""
    cost, state = _route_cost(record)
    if state == "UNKNOWN":
        return "cost=UNKNOWN"
    if state == "ESTIMATED_UPPER_BOUND":
        return f"cost<={cost:.4f} ESTIMATED_UPPER_BOUND"
    return f"cost={cost:.4f} EXACT"


class Selector:
    def __init__(
        self,
        registry: RouteRegistry,
        canonical_registry: CanonicalRegistry | None = None,
        lifecycle=None,
    ) -> None:
        self._registry = registry
        self._canon = canonical_registry
        # R4 lifecycle gate (optional dep to keep old call sites working).
        # Excluded statuses (DOMINATED/DEPRECATED/SUNSET/DISABLED) never enter
        # NEW decisions. Existing warm sessions keep their route via
        # model_pool.session_route_permitted (cache safety, spec R4 §10).
        self._lifecycle = lifecycle

    # ── LEVEL A ────────────────────────────────────────────────────────
    def candidate_canonicals(
        self,
        ctx: SelectionContext,
    ) -> tuple[list[str], list[tuple[str, str]], CanonicalProfile | None]:
        """Returns (accepted canonicals, rejected with reason, hinted profile).

        The canonical_hint is authoritative when present (explicit model or
        mapped slug). Without a hint the registry selects candidates by
        capability + quality floor + tier evidence (quality first).
        """
        rejected: list[tuple[str, str]] = []
        if ctx.canonical_hint:
            cm = get_canonical(ctx.canonical_hint)
            prof = self._canon.get(ctx.canonical_hint) if self._canon else None
            # Distinguish "canonical truly unknown" (no static policy entry AND no
            # canonical-registry profile) from "QUALITY_UNKNOWN_FRONTIER" (known
            # canonical without quality evidence). The latter is rejected for
            # high tiers under explicit policy, but is not a weak model.
            if cm is None and prof is None:
                return [], [(ctx.canonical_hint, "canonical_unknown")], None
            if cm is None and prof is not None:
                # Known to canonical-registry but not the static policy pool —
                # treat as quality_unknown for explainability.
                if prof.confidence == "UNKNOWN" and not ctx.allow_unknown_quality:
                    return [], [(ctx.canonical_hint, "quality_unknown:no_evidence")], prof
                return [ctx.canonical_hint], rejected, prof
            if prof is not None and prof.confidence == "UNKNOWN" and not ctx.allow_unknown_quality:
                if ctx.tier in {"T3", "T4"}:
                    return [], [(ctx.canonical_hint, "quality_unknown:tier_requires_evidence")], prof
            return [ctx.canonical_hint], rejected, prof

        # No hint: derive candidates from the evidence-driven canonical registry.
        if self._canon is None:
            return [], [("selector", "no_canonical_registry")], None
        floor = _quality_floor(ctx.tier, self._registry.config().quality_floors)
        accepted, rej = self._canon.candidates_for_task(
            task_class=ctx.task_class or "",
            capabilities=ctx.capabilities_required,
            quality_floor=floor,
            allow_unknown=ctx.allow_unknown_quality,
        )
        # Order: quality desc, then confidence rank, then name (deterministic).
        conf_rank = {"VERIFIED": 3, "PROVISIONAL": 2, "INCOMPLETE": 1, "UNKNOWN": 0}
        accepted.sort(key=lambda m: (-m.quality_score, -conf_rank.get(m.confidence, 0), m.canonical_id))
        return [m.canonical_id for m in accepted], rejected, None

    # ── LEVEL B ────────────────────────────────────────────────────────
    def plan(self, ctx: SelectionContext) -> tuple[list[PlanStep], dict]:
        """Return (ordered failover plan, explainability trace).

        If a canonical hint yields no eligible routes and no taCHANGE_ME
        selection is possible from a hint alone, fall back to LEVEL A over
        the canonical registry so failover can reach alternate canonicals.
        """
        trace: dict = {
            "task_class": ctx.task_class,
            "tier": ctx.tier,
            "required_capabilities": sorted(ctx.capabilities_required),
            "required_context": ctx.required_context,
            "candidate_canonicals": [],
            "rejected_canonicals": [],
            "eligible_routes": [],
            "rejected_routes": [],
        }
        canonicals, rejected, hinted = self.candidate_canonicals(ctx)
        trace["candidate_canonicals"] = canonicals
        trace["rejected_canonicals"] = [
            {"canonical": c, "reason": r, "code": reason_code(r)} for c, r in rejected
        ]

        primary: list[PlanStep] = []
        for c in canonicals:
            steps, rej_routes = self._plan_for_canonical(c, ctx)
            primary.extend(steps)
            trace["rejected_routes"].extend(rej_routes)

        primary = [p for p in primary if f"{p.provider}:{p.provider_model_id}" not in ctx.exclude_route_keys]

        # Hint exhausted (empty plan): LEVEL A candidate search instead of
        # leaving the user with a hard failure while healthy alternatives exist.
        if not primary and ctx.canonical_hint and self._canon is not None:
            base_ctx = SelectionContext(
                canonical_hint=None,
                provider_hint=ctx.provider_hint,
                prompt_cache_key=ctx.prompt_cache_key,
                required_context=ctx.required_context,
                reserved_output=ctx.reserved_output,
                capabilities_required=ctx.capabilities_required,
                tier=ctx.tier,
                task_class=ctx.task_class,
                exclude_route_keys=ctx.exclude_route_keys,
                allow_unknown_quality=ctx.allow_unknown_quality,
            )
            fallback_steps, fallback_trace = self.plan(base_ctx)
            if fallback_steps:
                merged = dict(trace)
                merged["hint_fallback"] = True
                merged["rejected_canonicals"].extend(fallback_trace.get("rejected_canonicals", []))
                merged["rejected_routes"].extend(fallback_trace.get("rejected_routes", []))
                merged["candidate_canonicals"].extend(fallback_trace.get("candidate_canonicals", []))
                merged["eligible_routes"].extend(fallback_trace.get("eligible_routes", []))
                return fallback_steps, merged
        trace["eligible_routes"] = [
            {"route": f"{p.provider}:{p.provider_model_id}", "canonical": p.canonical, "reason": p.reason}
            for p in primary
        ]

        # R5 §8-§12: cache-aware economics on the assembled plan. Hard gates
        # already filtered; this only reorders stay-vs-switch, never admits
        # a route the gates rejected.
        econ = self._apply_cache_affinity(primary, ctx)
        trace["cache_economics"] = econ

        # Alternate canonical models ONLY after same-canonical routes (§9).
        alternate: list[PlanStep] = []
        if ctx.tier:
            seen = {p.canonical for p in primary} | set(canonicals)
            if self._canon is not None:
                conf_rank = {"VERIFIED": 3, "PROVISIONAL": 2, "INCOMPLETE": 1, "UNKNOWN": 0}
                all_models = sorted(
                    self._canon.all().values(),
                    key=lambda m: (-m.quality_score, -conf_rank.get(m.confidence, 0), m.canonical_id),
                )
                tier_floor = _quality_floor(ctx.tier, self._registry.config().quality_floors)
                for m in all_models:
                    if m.canonical_id in seen:
                        continue
                    if m.confidence == "UNKNOWN" and not ctx.allow_unknown_quality:
                        continue
                    if m.quality_score < max(0.0, tier_floor - 0.20):
                        continue
                    steps, rej_routes = self._plan_for_canonical(m.canonical_id, ctx, reason_prefix="alternate")
                    trace["rejected_routes"].extend(rej_routes)
                    if steps:
                        alternate.extend(steps)
                        break  # one alternate canonical tier step is enough
            else:
                # fallback: downgrade one tier over static policy pool
                target_tier = _downgrade_tier(ctx.tier)
                for name, cm in _canonical_pool():
                    if name in seen or cm.tier != target_tier:
                        continue
                    steps, rej_routes = self._plan_for_canonical(name, ctx, reason_prefix="alternate")
                    trace["rejected_routes"].extend(rej_routes)
                    if steps:
                        alternate.extend(steps)
                        break

        return primary + alternate, trace

    def choose(self, ctx: SelectionContext) -> tuple[PlanStep | None, list[PlanStep], dict]:
        plan, trace = self.plan(ctx)
        return (plan[0] if plan else None), plan, trace

    # ── R5 §8-§12: cache-aware stay/switch economics ────────────────────
    def _apply_cache_affinity(self, steps: list[PlanStep], ctx: SelectionContext) -> dict:
        cfg = self._registry.config()
        if not ctx.current_route_key:
            return {"applied": False, "reason_code": "NO_SESSION_ROUTE"}
        if ctx.cache_state not in ("WARM", "LIKELY_WARM"):
            # COLD/UNKNOWN: cheapest eligible route wins (plain ranking);
            # no warm prefix at stake — cache penalty is 0 (R5 §9).
            return {"applied": False, "reason_code": "NO_WARM_CACHE", "cache_state": ctx.cache_state}
        provider, _, model_id = str(ctx.current_route_key).partition(":")
        warm = self._registry.get_any(provider, model_id)
        if warm is None:
            return {"applied": False, "reason_code": "NO_SESSION_ROUTE", "current_route": ctx.current_route_key}
        if warm.health in ("DOWN", "UNHEALTHY") or warm.circuit_state == "OPEN":
            # R5 scenario D: unhealthy current route bypasses cache protection.
            return {
                "applied": False,
                "reason_code": "HEALTH_INELIGIBLE",
                "current_route": ctx.current_route_key,
                "current_health": warm.health,
            }
        warm_idx = next(
            (i for i, s in enumerate(steps)
             if s.provider == provider and s.provider_model_id == model_id),
            None,
        )
        if warm_idx is None:
            # Hard gate excluded the warm route — gates beat cache affinity.
            floor = self._registry.min_discount_for(warm.provider)
            gate = _route_passes(warm, ctx, floor)
            return {
                "applied": False,
                "reason_code": reason_code(gate) if gate else "GATE_EXCLUDED",
                "current_route": ctx.current_route_key,
                "gate_reason": gate,
            }
        if warm_idx == 0:
            return {
                "applied": True,
                "reason_code": "CACHE_RETAINED",
                "cache_state": ctx.cache_state,
                "warm_prefix_tokens": ctx.warm_prefix_tokens,
                "current_route": ctx.current_route_key,
            }
        cand = steps[0]
        cand_rec = self._registry.get_any(cand.provider, cand.provider_model_id)
        if cand_rec is None:
            return {"applied": False, "reason_code": "GATE_EXCLUDED", "current_route": ctx.current_route_key}
        tokens_in = max(int(ctx.required_context or 0), 0)
        tokens_out = max(int(ctx.reserved_output or 0), 0)
        est_stay, _stay_pstate = estimate_request_cost(warm, tokens_in, tokens_out)
        stay_cost, stay_state, stay_ev = effective_cost_per_success(
            warm, est_stay, cfg.reliability_min_samples)
        est_cand, _cand_pstate = estimate_request_cost(cand_rec, tokens_in, tokens_out)
        cand_cost, cand_state, cand_ev = effective_cost_per_success(
            cand_rec, est_cand, cfg.reliability_min_samples)
        cross_canonical = warm.canonical != cand.canonical
        margin = cfg.cache_switch_margin * (
            cfg.canonical_switch_margin_factor if cross_canonical else 1.0)
        decision = cache_switch_economics(
            stay_cost=stay_cost,
            candidate_cost=cand_cost,
            cache_state=ctx.cache_state,
            warm_prefix_tokens=int(ctx.warm_prefix_tokens or 0),
            candidate_input_price=cand_rec.input_price,
            candidate_free=is_free(cand_rec),
            candidate_success_rate=(
                1.0 - cand_rec.error_rate
                if (cand_rec.success_count + cand_rec.failure_count) > 0 else 1.0
            ),
            candidate_healthy=(
                cand_rec.health not in ("DOWN", "UNHEALTHY")
                and cand_rec.circuit_state != "OPEN"
            ),
            horizon=cfg.cache_switch_horizon,
            margin=margin,
            free_min_success_rate=cfg.free_route_min_success_rate,
        )
        result = {
            "applied": True,
            "cache_state": ctx.cache_state,
            "warm_prefix_tokens": int(ctx.warm_prefix_tokens or 0),
            "current_route": ctx.current_route_key,
            "current_canonical": warm.canonical,
            "current_price_state": warm.price_state,
            "candidate_route": f"{cand.provider}:{cand.provider_model_id}",
            "candidate_canonical": cand.canonical,
            "candidate_price_state": cand_rec.price_state,
            "canonical_switch": cross_canonical,
            "stay_cost_usd": stay_cost,
            "stay_cost_state": stay_state,
            "stay_evidence": stay_ev,
            "candidate_cost_usd": cand_cost,
            "candidate_cost_state": cand_state,
            "candidate_evidence": cand_ev,
            "horizon": cfg.cache_switch_horizon,
            "margin_effective": margin,
            **decision,
        }
        if not decision["switch"]:
            warm_step = steps.pop(warm_idx)
            steps.insert(0, dataclasses.replace(
                warm_step, reason=f"cache:{decision['reason_code']}"))
        else:
            steps[0] = dataclasses.replace(
                steps[0], reason=f"cache:{decision['reason_code']}")
        return result

    def _plan_for_canonical(
        self,
        canonical: str,
        ctx: SelectionContext,
        reason_prefix: str = "primary",
    ) -> tuple[list[PlanStep], list[dict]]:
        cm = get_canonical(canonical)
        if cm is None:
            return [], [{"route": canonical, "reason": "canonical_unknown"}]
        # R4 lifecycle gate for NEW decisions (never for warm-session keep).
        if self._lifecycle is not None:
            _st = self._lifecycle.status(canonical, default="WATCH")
            if _st in ("DOMINATED", "DEPRECATED", "SUNSET", "DISABLED"):
                return [], [{"route": canonical, "reason": f"lifecycle:{_st}"}]
        min_discount = self._registry.min_discount_for("*")  # global default
        # Enumerate ALL routes (including below-floor) so the trace can
        # report discount-rejections explicitly instead of silently skipping.
        routes = self._registry.for_canonical_all(canonical)
        routes = self._registry.admin_model_filter(routes)
        # R6 §I: FORCE_ROUTE hard lock — keep only the forced provider:slug.
        if ctx.forced_route:
            forced = [r for r in routes
                      if f"{r.provider}:{r.provider_model_id}" == ctx.forced_route]
            if forced:
                routes = forced
            elif canonical == (get_canonical(ctx.forced_route.split(":", 1)[-1] if ":" in ctx.forced_route else "") or ""):
                pass  # slug may map to this canonical — keep list, filter below
            else:
                return [], [{"route": canonical, "reason": "forced_route:other_canonical"}]
        eligible: list[RouteRecord] = []
        rejected: list[dict] = []
        for r in routes:
            # provider-specific discount floor from config
            floor = self._registry.min_discount_for(r.provider)
            reason = _route_passes(r, ctx, floor)
            if reason is None:
                eligible.append(r)
            else:
                rejected.append({
                    "route": f"{r.provider}:{r.provider_model_id}",
                    "canonical": canonical,
                    "reason": reason,
                    "code": reason_code(reason),
                })
        if not eligible:
            return [], rejected

        # quality floor is canonical-level (LEVEL A result already filtered,
        # but re-assert here for hint-driven paths)
        qfloor = (_quality_floor(ctx.tier, self._registry.config().quality_floors)
                  if ctx.tier else 0.0)
        prof = self._canon.get(canonical) if self._canon else None
        if ctx.tier and prof is not None:
            if prof.confidence == "UNKNOWN" and not ctx.allow_unknown_quality:
                return [], rejected + [{"route": canonical, "reason": "quality_unknown:no_evidence", "code": "QUALITY_INELIGIBLE"}]
            if prof.quality_score < qfloor:
                return [], rejected + [{"route": canonical, "reason": f"quality_floor:{prof.quality_score:.3f}<{qfloor:.2f}", "code": "QUALITY_INELIGIBLE"}]

        eligible = sorted(eligible, key=_key_score)

        # Sticky provider hint: ordering preference only (never a prior —
        # unhealthy/failed routes are excluded before this sort).
        if ctx.provider_hint in ("provider_a", "provider_b"):
            eligible = sorted(eligible, key=lambda r: (0 if r.provider == ctx.provider_hint else 1, _key_score(r)))

        steps = []
        for r in eligible:
            sc = safe_context(r.context_length, RESERVED_OUTPUT + ctx.reserved_output)
            steps.append(PlanStep(
                canonical=canonical,
                provider=r.provider,
                provider_model_id=r.provider_model_id,
                context_length=r.context_length,
                discount=r.discount or 0.0,
                quality_score=prof.quality_score if prof is not None else r.quality_score,
                reason=f"{reason_prefix}:{r.health}:{_cost_reason(r)}",
                safe_context_limit=sc,
                context_eligible=(ctx.required_context <= sc if ctx.required_context else True),
            ))
        return steps, rejected


def _downgrade_tier(tier: str) -> str:
    order = ["T1", "T2", "T3", "T4"]
    if tier in order:
        idx = order.index(tier)
        return order[max(0, idx - 1)]
    return "T1"


def _canonical_pool() -> Iterable[tuple[str, CanonicalModel]]:
    from .policy import CANONICAL_MODELS
    return CANONICAL_MODELS.items()
