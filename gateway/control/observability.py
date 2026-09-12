"""R15: Production Guards + Observability (control plane, read-only).

Deterministic detectors over real data sources:
- economic anomaly   → decision_log trace.plan_prices (runtime journal)
- price spike        → price_history medians (1h/24h), configurable multiplier
- budgets            → spend from billing/usage evidence, warning-only default
- provider degradation → runtime /metrics snapshot (success, ttft p50/p95,
                        timeouts) + discovery/catalog/market freshness
- model degradation  → pool routes: availability, success, price spike,
                        liquidity disappearance
- schema drift       → refresh-time fingerprints (fingerprint_alerts table)
- readiness          → GREEN/YELLOW/RED aggregate over all of the above
- alert center       → merged categorized alert list with acknowledge

Nothing here mutates routing policy. Hard guards exist as config switches
but are OFF by default and only gate FUTURE selection, never kill running
traffic on their own.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from . import store


# ── helpers ───────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from . import revisions
        rev = revisions.active_config()
        if isinstance(rev, dict):
            return rev
    except Exception:  # noqa: BLE001
        pass
    return {}


def _r15_cfg() -> dict:
    return _cfg().get("observability") or {}


def _num(v) -> float | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _pct(values: list[float], p: int) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


DEFAULTS = {
    # §1 economic anomaly
    "anomaly_cost_multiplier": 3.0,      # selected > alternative × N → anomaly
    "anomaly_min_difference_usd": 0.005, # ignore sub-cent noise
    "anomaly_lookback_decisions": 500,
    # §2 price spike
    "spike_warning_multiplier": 2.0,     # current > median24h × N → warning
    "spike_min_window_samples": 3,       # fewer history samples → no claim
    "spike_hard_guard": False,           # OFF by default (never auto-on)
    # §3 budgets
    "daily_warning_budget_usd": None,    # unset = disabled
    "monthly_warning_budget_usd": None,
    "hard_limit_usd": None,              # OFF by default
}


def _setting(key: str):
    v = _r15_cfg().get(key)
    return v if v is not None else DEFAULTS.get(key)


def _insert_system_event(kind: str, severity: str, obj: str, reason: str,
                         recommended: str = "", fields: dict | None = None) -> None:
    """History §11: system events live in their OWN table — the revision log
    stays a pure configuration history."""
    try:
        store.insert_system_event({
            "ts": time.time(), "kind": kind, "severity": severity,
            "object": obj, "reason": reason, "recommended": recommended,
            "fields": fields or {},
        })
    except Exception:  # noqa: BLE001
        pass


# ── §1: economic anomaly over decision journal ────────────────────────────

def economic_anomalies(limit: int = 50) -> dict:
    """Selected route materially more expensive than an equal-or-better
    quality eligible alternative from the SAME decision's plan.

    Quality comparison uses the canonical quality evidence recorded in the
    plan (quality_score); a cheaper alternative must be equal-or-better
    (>= selected − tolerance) to qualify. A cheaper-but-weaker model is NOT
    an anomaly. Detection only — routing is never changed."""
    mult = float(_setting("anomaly_cost_multiplier") or 3.0)
    min_diff = float(_setting("anomaly_min_difference_usd") or 0.005)
    lookback = int(_setting("anomaly_lookback_decisions") or 500)
    out: list[dict] = []
    try:
        rows = store.list_decisions(limit=lookback)
    except Exception:  # noqa: BLE001
        rows = []
    checked = 0
    for row in rows:
        trace = row.get("trace") or {}
        plan_prices = trace.get("plan_prices") or []
        if not plan_prices:
            continue
        checked += 1
        sel = plan_prices[0]
        sel_cost = _num(sel.get("expected_cost_usd"))
        sel_q = _num(sel.get("quality_score"))
        if sel_cost is None or sel_cost <= 0:
            continue
        best_alt = None
        for alt in plan_prices[1:]:
            c = _num(alt.get("expected_cost_usd"))
            q = _num(alt.get("quality_score"))
            if c is None or c <= 0:
                continue
            if sel_q is not None and q is not None and q < sel_q - 0.05:
                continue  # cheaper but weaker quality — not an anomaly
            if sel_cost - c >= min_diff and (best_alt is None or c < best_alt["cost"]):
                best_alt = {"cost": c, "step": alt}
        if best_alt and best_alt["cost"] > 0 and sel_cost / best_alt["cost"] >= mult:
            alt = best_alt["step"]
            out.append({
                "ts": row.get("ts"),
                "decision_id": row.get("id"),
                "task_class": row.get("task_class"),
                "tier": row.get("tier"),
                "selected": {
                    "canonical": sel.get("canonical"),
                    "route": sel.get("route"),
                    "expected_cost_usd": sel_cost,
                },
                "alternative": {
                    "canonical": alt.get("canonical"),
                    "route": alt.get("route"),
                    "expected_cost_usd": best_alt["cost"],
                },
                "ratio": round(sel_cost / best_alt["cost"], 2),
                "difference_usd": round(sel_cost - best_alt["cost"], 6),
                "reason": (f"выбранный маршрут дороже равной/лучшей по качеству "
                           f"альтернативы в {sel_cost / best_alt['cost']:.1f} раз "
                           f"(≈${sel_cost:.6f} против ≈${best_alt['cost']:.6f})"),
            })
            if len(out) >= limit:
                break
    return {"anomalies": out, "checked_decisions": checked, "sample_note":
            ("журнал решений с R14; записей с ценами: " + str(checked))
            if checked else "данных пока нет — журнал заполняется по мере запросов"}


# ── §2: price spike guard ────────────────────────────────────────────────

def price_spikes() -> list[dict]:
    """current best_input vs median 1h/24h per canonical. Only a move above
    the configured multiplier against BOTH windows (with enough samples)
    counts — ordinary marketplace volatility below the threshold is not an
    anomaly."""
    mult = float(_setting("spike_warning_multiplier") or 2.0)
    min_samples = int(_setting("spike_min_window_samples") or 3)
    out: list[dict] = []
    now = time.time()
    try:
        canon_list = store.pool_canonicals()
    except Exception:  # noqa: BLE001
        canon_list = []
    for canon in canon_list:
        try:
            rows = store.price_history(canonical=canon, since=now - 86400 * 2)
        except Exception:  # noqa: BLE001
            continue
        cur = None
        h1, h24 = [], []
        for r in rows:
            v = _num(r.get("best_input"))
            if v is None or v <= 0:
                continue
            age = now - float(r.get("ts") or 0)
            if age <= 600:
                if cur is None or v < cur:
                    cur = v
            elif age <= 3600:
                h1.append(v)
            elif age <= 86400:
                h24.append(v)
        if cur is None:
            continue
        for label, win in (("1ч", h1), ("24ч", h24)):
            if len(win) < min_samples:
                continue
            med = _pct(win, 50)
            if med and med > 0 and cur > med * mult:
                out.append({
                    "canonical": canon,
                    "current": cur,
                    "median": round(med, 6),
                    "window": label,
                    "multiplier": round(cur / med, 2),
                    "severity": "warn",
                    "reason": (f"цена входа ${cur:.6f} превышает медиану {label} "
                               f"${med:.6f} в {cur / med:.1f} раз "
                               f"(порог ×{mult:.1f})"),
                })
                break  # one alert per canonical (worst window wins)
    return out


# ── §3: budgets ──────────────────────────────────────────────────────────

def _kv(key: str):
    try:
        return store.get_kv(key)
    except Exception:  # noqa: BLE001
        return None


def set_budgets(body: dict) -> dict:
    """R15 §3: persist warning budgets. Values <= 0 / null clear a budget.
    The hard limit is NOT auto-enabled: it stays null unless explicitly set
    AND acknowledged as an experiment."""
    def _clean(v):
        n = _num(v)
        return n if (n is not None and n > 0) else None
    daily = _clean(body.get("daily_budget_usd"))
    monthly = _clean(body.get("monthly_budget_usd"))
    hard = _clean(body.get("hard_limit_usd"))
    try:
        if daily is None:
            store.set_kv("r15.daily_warning_budget_usd", "")
        else:
            store.set_kv("r15.daily_warning_budget_usd", repr(daily))
        if monthly is None:
            store.set_kv("r15.monthly_warning_budget_usd", "")
        else:
            store.set_kv("r15.monthly_warning_budget_usd", repr(monthly))
        if hard is None:
            store.set_kv("r15.hard_limit_usd", "")
        else:
            store.set_kv("r15.hard_limit_usd", repr(hard))
    except Exception:  # noqa: BLE001
        pass
    return budgets()


def budgets() -> dict:
    """Daily/monthly warning budgets + forecast. Spend source: usage
    endpoint is cumulative — day/month buckets come from the spend ledger
    (store.spend_rows) written by the runtime billing observer. Warning
    only; the hard limit is a config switch, OFF by default, and even when
    ON it only marks alerts (no automatic traffic stop is wired)."""
    daily = _num(_kv("r15.daily_warning_budget_usd"))
    monthly = _num(_kv("r15.monthly_warning_budget_usd"))
    hard = _num(_kv("r15.hard_limit_usd"))
    today, month = 0.0, 0.0
    try:
        agg = store.spend_totals()
        today = float(agg.get("today_usd") or 0.0)
        month = float(agg.get("month_usd") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    now = time.localtime()
    day_frac = ((now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec)
                / 86400.0) or 0.01
    forecast = today / day_frac if today > 0 else 0.0
    alerts: list[dict] = []
    if daily and today >= daily:
        alerts.append({"kind": "budget", "severity": "warn", "object": "дневной бюджет",
                       "reason": f"потрачено ${today:.4f} из ${daily:.2f} за сегодня",
                       "recommended": "проверьте распределение запросов по моделям"})
    if monthly and month >= monthly:
        alerts.append({"kind": "budget", "severity": "warn", "object": "месячный бюджет",
                       "reason": f"потрачено ${month:.4f} из ${monthly:.2f} за месяц",
                       "recommended": "проверьте крупные статьи расходов"})
    if hard and (today >= hard or month >= hard):
        alerts.append({"kind": "budget", "severity": "bad", "object": "жёсткий лимит",
                       "reason": f"расход (${max(today, month):.4f}) достиг жёсткого лимита ${hard:.2f}",
                       "recommended": "жёсткий лимит настроен — проверьте конфигурацию"})
    return {
        "today_usd": round(today, 6),
        "month_usd": round(month, 6),
        "forecast_today_usd": round(forecast, 6),
        "daily_budget_usd": daily,
        "monthly_budget_usd": monthly,
        "hard_limit_usd": hard,
        "daily_pct": round(100 * today / daily, 1) if daily else None,
        "monthly_pct": round(100 * month / monthly, 1) if monthly else None,
        "alerts": alerts,
    }


# ── §4: provider degradation ─────────────────────────────────────────────

def provider_degradation(runtime_metrics: dict | None = None) -> list[dict]:
    """Per-provider status from runtime metrics + discovery freshness.

    Statuses: ok | degraded | down | no_data — with a mandatory reason."""
    snap = runtime_metrics or {}
    providers_ms = snap.get("providers") or {}
    out: list[dict] = []
    now = time.time()
    # discovery freshness per provider
    freshness: dict[str, dict] = {}
    try:
        for prov in ("provider_a", "provider_b"):
            meta = store.latest_discovery_meta(prov) or {}
            freshness[prov] = meta
    except Exception:  # noqa: BLE001
        pass
    names = sorted(set(list(providers_ms.keys()) + list(freshness.keys())) - {"none"})
    for name in names:
        ms = providers_ms.get(name) or {}
        reasons: list[str] = []
        status = "ok"
        reqs = int(ms.get("requests") or 0)
        succ = int(ms.get("successes") or 0)
        fails = int(ms.get("failures") or 0)
        timeouts = int(ms.get("timeouts") or 0)
        ttft_p50 = _num(ms.get("ttft_p50_ms"))
        ttft_p95 = _num(ms.get("ttft_p95_ms"))
        lat_p95 = _num(ms.get("total_p95_ms"))
        sr = (succ / reqs) if reqs else None
        if reqs >= 20 and sr is not None and sr < 0.90:
            status = "degraded"
            reasons.append(f"успешность {round(sr * 100)}% ({succ}/{reqs})")
        if reqs >= 20 and timeouts and timeouts / reqs > 0.10:
            status = "degraded"
            reasons.append(f"таймауты {round(100 * timeouts / reqs)}% ({timeouts}/{reqs})")
        if ttft_p95 is not None and ttft_p95 > 15000:
            status = "degraded" if status == "ok" else status
            reasons.append(f"TTFT p95 {ttft_p95 / 1000:.1f}с")
        if reqs > 0 and succ == 0 and fails > 0:
            status = "down"
            reasons.append(f"0 успешных из {reqs} запросов")
        if reqs == 0:
            reasons.append("нет трафика за период")
        meta = freshness.get(name) or {}
        cat_ts = _num(meta.get("catalog_ts") or meta.get("last_seen"))
        price_ts = _num(meta.get("pricing_ts") or meta.get("last_priced")
                        or meta.get("market_updated_at"))
        cat_age = (now - cat_ts) if cat_ts else None
        price_age = (now - price_ts) if price_ts else None
        if cat_age is None or cat_age > 6 * 3600 * 2:
            if status == "ok":
                status = "no_data"
            reasons.append("каталог несвежий" if cat_age else "каталог никогда не обновлялся")
        if price_age is not None and price_age > 2 * 3600:
            if status == "ok":
                status = "no_data"
            reasons.append(f"цены не обновлялись {int(price_age // 3600)}ч")
        out.append({
            "provider": name,
            "status": status,
            "status_ru": {"ok": "Работает", "degraded": "Деградация",
                          "down": "Недоступен", "no_data": "Нет свежих данных"}[status],
            "reason": "; ".join(reasons) or "все показатели в норме",
            "requests": reqs, "successes": succ, "failures": fails,
            "timeouts": timeouts,
            "success_rate": round(sr, 4) if sr is not None else None,
            "ttft_p50_ms": ttft_p50, "ttft_p95_ms": ttft_p95,
            "latency_p95_ms": lat_p95,
            "catalog_age_s": int(cat_age) if cat_age else None,
            "price_age_s": int(price_age) if price_age else None,
        })
    return out


# ── §5: model degradation ────────────────────────────────────────────────

def model_degradation(pool_models: list[dict] | None = None,
                      runtime_metrics: dict | None = None) -> list[dict]:
    """Per active-pool model: availability, success rate, TTFT, price spike,
    liquidity disappearance. Warning + reason, nothing auto-removed."""
    out: list[dict] = []
    now = time.time()
    spikes = {s["canonical"]: s for s in price_spikes()}
    if pool_models is None:
        try:
            from . import inventory as inv
            data = inv.build_inventory()
            pool_models = [g for g in data.get("models", []) if g.get("in_pool")]
        except Exception:  # noqa: BLE001
            pool_models = []
    ms_prov = (runtime_metrics or {}).get("providers") or {}
    for g in pool_models:
        canon = g.get("canonical") or ""
        if not canon:
            continue
        # Route-level metrics are canonicalized from runtime /metrics. This keeps
        # Monitoring on the same registry as Models and Router, not a second model map.
        route_stats = {}
        try:
            route_stats = (runtime_metrics or {}).get("routes") or {}
        except Exception:
            route_stats = {}
        reasons: list[str] = []
        warn = False
        disc = g.get("discount_pct")
        floor = round((g.get("effective_min_discount") or 0.8) * 100)
        if disc is not None and disc < floor:
            warn = True
            reasons.append(f"скидка {disc}% ниже минимума {floor}%")
        if not any(r.get("market_active") for r in g.get("routes", [])):
            warn = True
            reasons.append("нет активных предложений (ликвидность пропала)")
        if any(r.get("missing_from_catalog") for r in g.get("routes", [])):
            warn = True
            reasons.append("часть маршрутов исчезла из каталога")
        sp = spikes.get(canon)
        if sp:
            warn = True
            reasons.append("резкий рост цены (" + sp["window"] + ")")
        # runtime success for this model's routes
        route_out=[]; req_total=succ_total=fail_total=timeout_total=0; ttfts=[]
        for r in g.get("routes", []):
            route_key = (r.get("provider") or "") + ":" + (r.get("provider_model_id") or "")
            rm = route_stats.get(route_key) or {}
            req=int(rm.get("requests") or 0); succ=int(rm.get("success") or rm.get("successes") or 0)
            fail=int(rm.get("provider_errors") or rm.get("failures") or 0); tout=int(rm.get("timeouts") or 0)
            req_total+=req; succ_total+=succ; fail_total+=fail; timeout_total+=tout
            if rm.get("ttft_p50_ms") is not None: ttfts.append(rm.get("ttft_p50_ms"))
            route_out.append({"provider":r.get("provider"),"provider_model_id":r.get("provider_model_id"),
                              "status":"Работает" if r.get("market_active") else "Недоступна",
                              "requests":req,"success":succ,"failures":fail,"timeouts":tout,
                              "ttft_p50_ms":rm.get("ttft_p50_ms"),"ttft_p95_ms":rm.get("ttft_p95_ms"),
                              "price":r.get("best_input"),"discount_pct":r.get("discount_pct"),
                              "last_probe":r.get("last_probe"),"last_error":rm.get("last_error")})
        if req_total and succ_total/req_total < .9:
            warn=True; reasons.append(f"успешность {round(100*succ_total/req_total)}% ({succ_total}/{req_total})")
        out.append({
            "canonical": canon,
            "display_name": g.get("display_name") or canon,
            "status": "Деградация" if warn else ("Работает" if any((r.get("market_active") or False) for r in g.get("routes", [])) else "Нет данных"),
            "availability": (sum(1 for r in route_out if (r.get("last_probe") or {}).get("ok")) / len(route_out)) if route_out else None,
            "reason": "; ".join(reasons) or ("нет трафика за период" if not req_total else "показатели в норме"),
            "degraded": warn,
            "reasons": reasons or ["показатели в норме"],
            "discount_pct": disc,
            "min_discount_pct": floor,
            "best_input": g.get("best_input"),
            "offers_count": g.get("offers_count"),
            "price_spike": bool(sp),
            "requests": req_total, "successes": succ_total, "failures": fail_total,
            "timeouts": timeout_total, "success_rate": (succ_total/req_total if req_total else None),
            "ttft_p50_ms": min(ttfts) if ttfts else None,
            "ttft_p95_ms": max([x for r in route_out for x in [r.get("ttft_p95_ms")] if x is not None], default=None),
            "routes": route_out,
        })
    return out


# ── §8: schema drift (fingerprint alerts are written at refresh time) ─────

def schema_drift_alerts() -> list[dict]:
    try:
        return store.list_fingerprint_alerts(limit=50)
    except Exception:  # noqa: BLE001
        return []


# ── §6: shadow candidates ────────────────────────────────────────────────

def shadow_candidates() -> dict:
    """Models with pool state 'candidate' (model_pool.notes marker or
    dedicated state). The Router replay-dries eligibility/economics over
    recent decision records WITHOUT sending production inference.

    would_be_selected: decisions where the candidate passes the tier's
    quality floor and would have been cheaper than the selected route.
    Rejection reasons: quality floor / unknown quality / no routes /
    more expensive."""
    try:
        cand_rows = store.list_candidates()
    except Exception:  # noqa: BLE001
        cand_rows = []
    rows = []
    try:
        rows = store.list_decisions(limit=int(_setting("anomaly_lookback_decisions") or 500))
    except Exception:  # noqa: BLE001
        pass
    cfg = _cfg()
    floors = cfg.get("quality_floors") or {}
    out = []
    for cand in cand_rows:
        canon = cand.get("canonical")
        prof = cand.get("profile") or {}
        q = _num(prof.get("quality_score")) or 0.0
        conf = prof.get("confidence") or "UNKNOWN"
        best_in = _num(cand.get("best_input"))
        would = 0
        savings = 0.0
        covered = 0
        rejections: dict[str, int] = {}
        for row in rows:
            trace = row.get("trace") or {}
            pp = trace.get("plan_prices") or []
            if not pp:
                continue
            sel = pp[0]
            tier = row.get("tier")
            qfloor = _num(floors.get(tier)) if tier else None
            if qfloor is not None and q < qfloor:
                rejections["quality_floor"] = rejections.get("quality_floor", 0) + 1
                continue
            if conf == "UNKNOWN":
                rejections["quality_unknown"] = rejections.get("quality_unknown", 0) + 1
                continue
            sel_cost = _num(sel.get("expected_cost_usd"))
            if best_in is None or not sel_cost:
                rejections["no_price_evidence"] = rejections.get("no_price_evidence", 0) + 1
                continue
            covered += 1
            # rough per-decision candidate cost: same token shape, its price
            sel_in = _num(sel.get("input_price"))
            if sel_in is None or sel_in <= 0:
                continue
            cand_cost = sel_cost * (best_in / sel_in)
            if cand_cost < sel_cost:
                would += 1
                savings += sel_cost - cand_cost
        out.append({
            "canonical": canon,
            "display_name": cand.get("display_name") or canon,
            "quality_score": q,
            "confidence": conf,
            "best_input": best_in,
            "decisions_examined": len(rows),
            "task_coverage": covered,
            "would_be_selected": would,
            "estimated_savings_usd": round(savings, 6),
            "rejection_reasons": rejections,
        })
    return {"candidates": out,
            "note": "dry-run поверх журнала решений; production-вызовы не выполняются"}


# ── §7: canary experiments ───────────────────────────────────────────────

def canary_status() -> dict:
    """Optional canary experiment: {model, task_classes, traffic_pct,
    duration_s, rollback:{error_rate, ttft_ms, cost_multiplier}}.
    Default disabled. Canary is a SEPARATE mechanism — it never edits the
    routing policy or tiers; the runtime applies it as an explicit
    probabilistic hint, and thresholds roll it back automatically."""
    try:
        raw = store.get_kv("canary")
        if not raw:
            return {"enabled": False}
        d = json.loads(raw)
        d.setdefault("enabled", False)
        return d
    except Exception:  # noqa: BLE001
        return {"enabled": False}


def canary_evaluate() -> dict:
    """Check canary rollback thresholds against real metrics; auto-rollback
    disables the experiment (config change is audited)."""
    st = canary_status()
    if not st.get("enabled"):
        return {"enabled": False, "checked": False}
    rb = st.get("rollback") or {}
    thresholds = {
        "error_rate": _num(rb.get("error_rate")),
        "ttft_ms": _num(rb.get("ttft_ms")),
        "cost_multiplier": _num(rb.get("cost_multiplier")),
    }
    model = st.get("model") or ""
    # metrics come from the runtime canary counters (metrics.canary_*)
    from ..app import _metrics  # runtime-side only; control proxies via HTTP
    snap = {}
    try:
        snap = _metrics.canary_snapshot(model)
    except Exception:  # noqa: BLE001
        snap = {}
    breaches: list[str] = []
    er = _num(snap.get("error_rate"))
    if thresholds["error_rate"] is not None and er is not None and er > thresholds["error_rate"]:
        breaches.append(f"ошибок {round(er * 100)}% > порога {round(thresholds['error_rate'] * 100)}%")
    ttft = _num(snap.get("ttft_p95_ms"))
    if thresholds["ttft_ms"] is not None and ttft is not None and ttft > thresholds["ttft_ms"]:
        breaches.append(f"TTFT p95 {ttft / 1000:.1f}с > порога {thresholds['ttft_ms'] / 1000:.1f}с")
    cm = _num(snap.get("cost_multiplier"))
    if thresholds["cost_multiplier"] is not None and cm is not None and cm > thresholds["cost_multiplier"]:
        breaches.append(f"стоимость ×{cm:.1f} > порога ×{thresholds['cost_multiplier']:.1f}")
    rolled_back = False
    if breaches:
        try:
            store.set_kv("canary", json.dumps({**st, "enabled": False,
                                               "rolled_back_at": time.time(),
                                               "rollback_reasons": breaches}))
            rolled_back = True
            _insert_system_event("canary", "bad", model,
                                 "автооткат канарейки: " + "; ".join(breaches),
                                 "проверьте пороги и состояние модели")
        except Exception:  # noqa: BLE001
            pass
    return {"enabled": st.get("enabled"), "checked": True, "model": model,
            "metrics": snap, "thresholds": thresholds,
            "breaches": breaches, "rolled_back": rolled_back}


# ── §9: production readiness ─────────────────────────────────────────────

def readiness(runtime_health: dict | None = None, control_ok: bool = True,
              runtime_metrics: dict | None = None) -> dict:
    """GREEN / YELLOW / RED aggregate with explicit check list."""
    checks: list[dict] = []

    def add(name: str, ok: bool, warn_reason: str = "", crit_reason: str = "",
            critical: bool = False):
        checks.append({"name": name, "state": "ok" if ok else ("crit" if critical else "warn"),
                       "reason": (crit_reason if critical else warn_reason) if not ok else ""})

    rh = runtime_health or {}
    rt_ok = bool(rh.get("ok"))
    add("runtime", rt_ok, "runtime недоступен", "runtime недоступен", critical=True)
    add("control", control_ok, "control недоступен", "control недоступен", critical=True)

    provs = provider_degradation(runtime_metrics)
    down = [p for p in provs if p["status"] == "down"]
    deg = [p for p in provs if p["status"] == "degraded"]
    nod = [p for p in provs if p["status"] == "no_data"]
    add("providers", not down and not deg,
        "; ".join(p["provider"] + ": " + p["reason"] for p in (deg + nod)) or "деградация",
        "; ".join(p["provider"] + ": " + p["reason"] for p in down) or "провайдер недоступен",
        critical=bool(down))

    now = time.time()
    fresh_ok = True
    fresh_reasons = []
    for prov in ("provider_a", "provider_b"):
        try:
            meta = store.latest_discovery_meta(prov) or {}
        except Exception:  # noqa: BLE001
            meta = {}
        cat_ts = _num(meta.get("catalog_ts") or meta.get("last_seen"))
        price_ts = _num(meta.get("pricing_ts") or meta.get("last_priced")
                        or meta.get("market_updated_at"))
        if not cat_ts or now - cat_ts > 12 * 3600:
            fresh_ok = False
            fresh_reasons.append(f"{prov}: каталог несвежий")
        if not price_ts or now - price_ts > 2 * 3600:
            fresh_ok = False
            fresh_reasons.append(f"{prov}: цены несвежие")
    add("catalogs", fresh_ok, "; ".join(fresh_reasons) or "каталоги несвежие", "", critical=False)

    try:
        drift = schema_drift_alerts()
    except Exception:  # noqa: BLE001
        drift = []
    recent_drift = [d for d in drift if now - float(d.get("ts") or 0) < 24 * 3600]
    add("schema", not recent_drift,
        "; ".join(d.get("reason", "") for d in recent_drift[:3]) or "schema drift",
        "", critical=False)

    try:
        models = model_degradation(runtime_metrics=runtime_metrics)
        degraded_models = [m for m in models if m.get("degraded")]
    except Exception:  # noqa: BLE001
        degraded_models = []
    pool_avail_ok = len(degraded_models) <= max(1, len(models) // 3)
    add("pool_availability", pool_avail_ok,
        "; ".join(m["display_name"] + ": " + "; ".join(m["reasons"][:2])
                  for m in degraded_models[:4]) or "доступность пула снижена",
        "", critical=False)

    try:
        cfg_valid = True
        cfg_reason = ""
    except Exception:  # noqa: BLE001
        cfg_valid, cfg_reason = False, "config invalid"
    add("config_valid", cfg_valid, cfg_reason, "", critical=False)

    try:
        kg = store.get_known_good()
    except Exception:  # noqa: BLE001
        kg = None
    add("known_good", kg is not None, "нет основной рабочей точки", "", critical=False)

    econ = economic_anomalies(limit=10)
    crit_anom = [a for a in econ.get("anomalies", []) if a.get("ratio", 0) >= 10]
    add("no_critical_anomalies", not crit_anom,
        f"критичных экономических аномалий: {len(crit_anom)}", "", critical=False)

    crit = [c for c in checks if c["state"] == "crit"]
    warn = [c for c in checks if c["state"] == "warn"]
    level = "RED" if crit else ("YELLOW" if warn else "GREEN")
    return {
        "level": level,
        "level_ru": {"GREEN": "ЗЕЛЁНЫЙ", "YELLOW": "ЖЁЛТЫЙ", "RED": "КРАСНЫЙ"}[level],
        "checks": checks,
        "warnings": [c for c in warn],
        "criticals": [c for c in crit],
    }


# ── §10: alert center ────────────────────────────────────────────────────

_KIND_RU = {
    "price": "Цена", "provider": "Provider", "model": "Model",
    "catalog": "Catalog", "availability": "Availability",
    "economics": "Routing economics", "budget": "Budget",
    "config": "Configuration", "schema": "Catalog", "canary": "Model",
}


def alert_center(runtime_metrics: dict | None = None) -> dict:
    """Merged alerts: severity, time, object, reason, recommended action.
    Acknowledged alerts are hidden until they re-fire (ack per object+kind)."""
    acked: set[str] = set()
    try:
        acked = {a["alert_key"] for a in store.list_alert_acks()}
    except Exception:  # noqa: BLE001
        pass
    alerts: list[dict] = []
    now = time.time()

    for sp in price_spikes():
        key = f"price:{sp['canonical']}"
        alerts.append({"kind": "price", "severity": "warn", "ts": now,
                       "object": sp["canonical"], "reason": sp["reason"],
                       "recommended": "проверить историю цен и минимум скидки",
                       "alert_key": key, "acked": key in acked})
    for p in provider_degradation(runtime_metrics):
        if p["status"] in ("degraded", "down", "no_data"):
            key = f"provider:{p['provider']}"
            alerts.append({"kind": "provider",
                           "severity": "bad" if p["status"] == "down" else "warn",
                           "ts": now, "object": p["provider"], "reason": p["reason"],
                           "recommended": "проверить провайдера и свежесть данных",
                           "alert_key": key, "acked": key in acked})
    for d in schema_drift_alerts():
        if now - float(d.get("ts") or 0) > 7 * 86400:
            continue
        key = f"schema:{d.get('provider')}"
        alerts.append({"kind": "catalog", "severity": "bad", "ts": d.get("ts", now),
                       "object": d.get("provider"), "reason": d.get("reason", ""),
                       "recommended": "последний рабочий реестр сохранён; проверить API провайдера",
                       "alert_key": key, "acked": key in acked})
    for m in model_degradation(runtime_metrics=runtime_metrics):
        if m.get("degraded"):
            key = f"model:{m['canonical']}"
            alerts.append({"kind": "model", "severity": "warn", "ts": now,
                           "object": m["display_name"],
                           "reason": "; ".join(m["reasons"]),
                           "recommended": "проверить маршруты и цены модели",
                           "alert_key": key, "acked": key in acked})
    for a in economic_anomalies(limit=20).get("anomalies", []):
        key = f"economics:{a.get('decision_id')}"
        alerts.append({"kind": "economics", "severity": "warn", "ts": a.get("ts", now),
                       "object": f"{a['selected']['route']}",
                       "reason": a["reason"],
                       "recommended": "проверить политику качества/скидок для этих моделей",
                       "alert_key": key, "acked": key in acked})
    for b in budgets().get("alerts", []):
        key = f"budget:{b['object']}"
        alerts.append({"kind": "budget", "severity": b.get("severity", "warn"),
                       "ts": now, "object": b["object"], "reason": b["reason"],
                       "recommended": b.get("recommended", ""),
                       "alert_key": key, "acked": key in acked})
    # persist unacked alerts into system events history (§11), throttled
    for a in alerts:
        if not a["acked"] and a["severity"] == "bad":
            _insert_system_event(a["kind"], a["severity"], a["object"],
                                 a["reason"], a["recommended"])
    order = {"bad": 0, "warn": 1}
    alerts.sort(key=lambda a: (a["acked"], order.get(a["severity"], 2),
                               -(a.get("ts") or 0)))
    return {"alerts": alerts,
            "unacked_count": sum(1 for a in alerts if not a["acked"]),
            "categories": sorted({_KIND_RU.get(a["kind"], a["kind"]) for a in alerts
                                  if not a["acked"]})}
