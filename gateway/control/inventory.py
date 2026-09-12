"""R11 §6/§14: model inventory — 3 levels, filters, per-model market view.

Read layer over discovery + canonical_map + model_pool + live market data.
Every discovered model appears here with:
  - in my pool?      (model_pool)
  - canonical match? (canonical_map; absent -> "Не сопоставлена")
  - market state?    (provider_b market rows / provider_a asks)
  - eligibility + REASON (never silently dropped)
"""
from __future__ import annotations

import time

from . import store, discovery


def _global_min_discount() -> float:
    """Global floor from the active config (default 0.8)."""
    try:
        from . import revisions as rev
        cfg = rev.active_config() or {}
        for name, v in (cfg.get("providers") or {}).items():
            md = v.get("min_discount")
            if md is not None:
                return float(md)
        return float((cfg.get("routing") or {}).get("min_discount") or 0.8)
    except Exception:
        return 0.8


def _market_best_ask(m: dict) -> dict:
    """Best independent minima for input/output (NEVER a fabricated pair)."""
    out: dict = {"best_input": None, "best_output": None,
                 "best_discount_pct": None, "liquidity": None,
                 "sellers": None, "active": False}
    mk = m.get("market") or {}
    if mk:
        out.update({
            "best_input": mk.get("best_input_per_1m"),
            "best_output": mk.get("best_output_per_1m"),
            "best_discount_pct": mk.get("best_discount_pct"),
            "liquidity": mk.get("credits_sold_24h"),
            "sellers": mk.get("num_sellers"),
            "active": True,
        })
    if m.get("provider") == "provider_a":
        if m.get("min_ask_in") is not None:
            out["best_input"] = m["min_ask_in"]
        if m.get("min_ask_out") is not None:
            out["best_output"] = m["min_ask_out"]
        oi, oo = m.get("official_input"), m.get("official_output")
        if oi and (m.get("min_ask_in") is not None):
            out["best_discount_pct"] = round(100 * (1 - m["min_ask_in"] / oi), 2)
        out["active"] = bool(m.get("ask_count"))
        out["sellers"] = m.get("ask_count")
    return out


def _discount_pct(m: dict) -> float | None:
    """Current market discount vs official price (independent minima)."""
    oi, oo = m.get("official_input"), m.get("official_output")
    bi, bo = m.get("min_ask_in"), m.get("min_ask_out")
    if oi and bi is not None:
        return round(100 * (1 - bi / oi), 2)
    if oo and bo is not None:
        return round(100 * (1 - bo / oo), 2)
    mk = m.get("market") or {}
    return mk.get("best_discount_pct")


def _effective_min_discount(canonical: str) -> float:
    pol = store.get_pool_policy(canonical)
    ovr = pol.get("min_discount_override")
    return float(ovr) if ovr is not None else _global_min_discount()


def _eligibility(m: dict, canonical: str | None) -> tuple[bool, str, float]:
    """(eligible, human reason, effective_min_discount). Discovery is NEVER
    the gate — only policy (discount/pool/hidden) decides eligibility."""
    floor = _effective_min_discount(canonical) if canonical else _global_min_discount()
    if canonical is None:
        return False, "не сопоставлена с canonical (нужно сопоставление)", floor
    pol = store.get_pool_policy(canonical)
    if pol.get("hidden"):
        return False, "скрыта администратором", floor
    if pol.get("in_pool") is False:
        return False, "не в моём пуле", floor
    disc = _discount_pct(m)
    if disc is None:
        # no market evidence: neither eligible nor rejected — unknown state
        return False, "нет данных о рыночной цене/скидке", floor
    if disc < floor * 100:
        return False, f"скидка {disc}% < требуемых {round(floor*100)}%", floor
    return True, f"скидка {disc}% ≥ {round(floor*100)}%", floor


def build_inventory(provider: str | None = None) -> dict:
    """Full inventory: every discovered model, grouped for the 3 UI levels."""
    models = store.list_discovered(provider=provider, include_missing=True)
    pool_policies = {p["canonical"]: p for p in store.list_pool_policies()}
    # R12 §4: route latency evidence from availability probes (deep probes
    # record real TTFT; catalog checks do not). Never fabricated.
    last_probes: dict[tuple[str, str], dict] = {}
    for p in ("provider_a", "provider_b"):
        try:
            for c in store.availability_for_provider(p):
                last_probes[(p, c.get("provider_model_id"))] = c
        except Exception:  # noqa: BLE001
            pass
    rows = []
    for m in models:
        canonical = store.get_canonical_for(m["provider"], m["provider_model_id"])
        pol = pool_policies.get(canonical) if canonical else None
        in_pool = bool(pol and pol.get("in_pool")) if pol else (canonical is not None)
        ask = _market_best_ask(m)
        disc = _discount_pct(m)
        eligible, reason, floor = _eligibility(m, canonical)
        ctx = m.get("context")
        probe = last_probes.get((m["provider"], m["provider_model_id"])) or {}
        rows.append({
            "provider": m["provider"],
            "provider_model_id": m["provider_model_id"],
            "display_name": m.get("display_name") or m["provider_model_id"],
            "canonical": canonical,
            "in_pool": in_pool if canonical else False,
            "hidden": bool(pol and pol.get("hidden")),
            "unmatched": canonical is None,
            "context": ctx,                      # None -> "неизвестен" (never fake)
            "official_input": m.get("official_input"),
            "official_output": m.get("official_output"),
            "best_input": ask["best_input"],
            "best_output": ask["best_output"],
            "discount_pct": disc,
            "effective_min_discount": floor,
            "market_active": ask["active"],
            "sellers": ask["sellers"],
            "offers_kind": ("sellers" if m.get("provider") == "provider_b" and ask["sellers"]
                            else "price_offers" if ask["sellers"] else None),
            "liquidity_24h": ask.get("liquidity"),
            "eligible": eligible,
            "eligibility_reason": reason,
            "missing_from_catalog": bool(m.get("missing")),
            "last_seen": m.get("last_seen"),
            "last_priced": m.get("last_priced"),
            "first_seen": m.get("first_seen"),
            "last_probe": {k: probe.get(k) for k in
                           ("ok", "checked_at", "latency_ms", "probe", "error_code")}
            if probe else None,
        })
    # group by canonical for the "Models" view (a canonical may span providers)
    by_canonical: dict[str, dict] = {}
    unmatched: list[dict] = []
    for r in rows:
        # R12-B1 fix: canonical "" (empty string in the map) == NO mapping
        if not (r["canonical"] or "").strip():
            r["canonical"] = None
            r["key"] = f"u:{r['provider']}:{r['provider_model_id']}"
            unmatched.append(r)
            continue
        c = r["canonical"]
        if c not in by_canonical:
            by_canonical[c] = {
                "canonical": c,
                "key": c,
                # Catalog rows can carry a provider-prefixed technical ID as
                # display_name. Use canonical as the stable user-facing fallback.
                "display_name": (r.get("display_name") or c)
                    if "/" not in (r.get("display_name") or "")
                    else c,
                "providers": [],
                "in_pool": r["in_pool"], "hidden": r["hidden"],
                "official_input": None, "official_output": None,
                "best_input": None, "best_output": None,
                "discount_pct": None, "market_active": False,
                "context_min": None, "context_max": None,
                "eligible": False, "eligibility_reason": "",
                "effective_min_discount": r["effective_min_discount"],
                "routes": [],
                "offers_count": 0, "sellers_count": 0,
                "offers_kind": None, "offers_providers": [],
                "last_probe_at": None, "ttft_ms": None,
                "last_priced": None,
            }
        g = by_canonical[c]
        if (not g.get("display_name") or g.get("display_name") in ("", "unknown")) and r.get("display_name"):
            g["display_name"] = r["display_name"]
        g["providers"].append(r["provider"])
        g["routes"].append({
            "provider": r["provider"],
            "provider_model_id": r["provider_model_id"],
            "context": r["context"],
            "official_input": r["official_input"],
            "official_output": r["official_output"],
            "best_input": r["best_input"], "best_output": r["best_output"],
            "discount_pct": r["discount_pct"], "eligible": r["eligible"],
            "eligibility_reason": r["eligibility_reason"],
            "market_active": r["market_active"], "sellers": r["sellers"],
            "offers_kind": r["offers_kind"],
            "last_probe": r["last_probe"],
        })
        # R12 §3: liquidity aggregation — offers and sellers are DIFFERENT
        # units (price offers vs sellers); sum only within a kind and record
        # which providers contributed. Never infer seller identity from asks.
        if r["sellers"] is not None:
            if r["offers_kind"] == "sellers":
                g["sellers_count"] = (g["sellers_count"] or 0) + r["sellers"]
            else:
                g["offers_count"] = (g["offers_count"] or 0) + r["sellers"]
            g["offers_kind"] = r["offers_kind"] if g["offers_kind"] is None else "mixed"
            g["offers_providers"].append(r["provider"])
        # R12 §4: probe latency evidence — catalog probes measure reachability
        # only; a real model TTFT is displayed ONLY from inference probes.
        lp = r.get("last_probe") or {}
        if lp.get("checked_at"):
            if g["last_probe_at"] is None or lp["checked_at"] > g["last_probe_at"]:
                g["last_probe_at"] = lp["checked_at"]
            if lp.get("probe") == "inference" and lp.get("latency_ms") is not None:
                if g["ttft_ms"] is None or lp["latency_ms"] < g["ttft_ms"]:
                    g["ttft_ms"] = lp["latency_ms"]
        if r.get("last_priced") and (g["last_priced"] is None
                                     or r["last_priced"] > g["last_priced"]):
            g["last_priced"] = r["last_priced"]
        # canonical-level aggregates: best (cheapest) values, honest context range
        for f in ("official_input", "official_output", "best_input", "best_output"):
            v = r[f]
            if v is not None and (g[f] is None or v < g[f]):
                g[f] = v
        if r["discount_pct"] is not None and (g["discount_pct"] is None
                                              or r["discount_pct"] > g["discount_pct"]):
            g["discount_pct"] = r["discount_pct"]
        if r["eligible"]:
            g["eligible"] = True
            g["eligibility_reason"] = r["eligibility_reason"]
        elif not g["eligible"] and not g["eligibility_reason"]:
            g["eligibility_reason"] = r["eligibility_reason"]
        g["market_active"] = g["market_active"] or r["market_active"]
        for f, cur in (("context_min", g["context_min"]), ("context_max", g["context_max"])):
            v = r["context"]
            if v is not None:
                if cur is None or (f == "context_min" and v < cur) or (f == "context_max" and v > cur):
                    g[f] = v
    for g in by_canonical.values():
        # Canonical grouping must expose the stable display name, not a raw
        # provider technical ID from the first route.
        if "/" in (g.get("display_name") or ""):
            g["display_name"] = g.get("canonical") or g.get("display_name")

    # R12-B1: every group gets a UNIQUE stable key — unmatched groups are
    # per-route (canonical '' is NOT unique); the UI opens models by key.
    return {
        "models": sorted(by_canonical.values(), key=lambda g: g["canonical"]),
        "unmatched": unmatched,
        "counts": {
            "discovered_total": len(rows),
            "canonical_matched": len(by_canonical),
            "unmatched": len(unmatched),
            "in_pool": sum(1 for g in by_canonical.values() if g["in_pool"] and not g["hidden"]),
            "hidden": sum(1 for g in by_canonical.values() if g["hidden"]),
            "eligible": sum(1 for g in by_canonical.values() if g["eligible"]),
            "market_active": sum(1 for r in rows if r["market_active"]),
        },
        "global_min_discount": _global_min_discount(),
        "built_at": time.time(),
    }


def reconciliation(provider: str) -> dict:
    """R11 §33: live catalog vs imported vs matched vs eligible — every lost
    model gets a reason, zero silently dropped."""
    models = store.list_discovered(provider=provider)
    total = len(models)
    matched = unmatched = eligible = rejected_discount = rejected_other = 0
    in_pool = on_market = 0
    reasons: dict[str, int] = {}
    for m in models:
        canonical = store.get_canonical_for(provider, m["provider_model_id"])
        if _market_best_ask(m).get("active"):
            on_market += 1
        if canonical is None:
            unmatched += 1
            reasons["нет сопоставления с моделью Router (canonical)"] = \
                reasons.get("нет сопоставления с моделью Router (canonical)", 0) + 1
            continue
        matched += 1
        pol = store.get_pool_policy(canonical)
        if pol.get("in_pool") is not False:
            in_pool += 1
        el, reason, _ = _eligibility(m, canonical)
        if el:
            eligible += 1
        elif "скидка" in reason:
            rejected_discount += 1
            reasons[reason.split(" <")[0]] = reasons.get(reason.split(" <")[0], 0) + 1
        else:
            rejected_other += 1
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"provider": provider, "catalog_models": total,
            "on_market": on_market,
            "canonical_matched": matched, "unmatched": unmatched,
            "in_pool": in_pool,
            "eligible": eligible, "rejected_by_discount": rejected_discount,
            "rejected_other": rejected_other, "top_reasons":
            sorted(reasons.items(), key=lambda kv: -kv[1])[:10]}
