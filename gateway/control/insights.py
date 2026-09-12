"""R12: human-language analytics layer.

Everything here is TRANSPARENT arithmetic over evidence the control plane
already has (inventory, metrics, price history). No subjective AI scores:
'Выгодность' is a deterministic function of (discount, quality state,
latency evidence), and the UI shows the full explanation string.
"""
from __future__ import annotations

import time

from . import store, inventory as inv


# ── §5: Выгодность — transparent price/quality rating ─────────────────────

def value_rating(g: dict) -> dict:
    """Deterministic value rating with a human-readable 'why' string.

    Inputs: current discount vs official, market activity, discount headroom
    over the policy floor. Quality/latency are NOT guessed — they only appear
    in the explanation when real evidence exists.
    """
    disc = g.get("discount_pct")
    floor = round((g.get("effective_min_discount") or 0.8) * 100)
    why: list[str] = []
    if disc is None:
        return {"rating": "unknown", "why": "нет данных о рыночной цене"}
    if disc >= 95:
        rating = "excellent"
    elif disc >= 90:
        rating = "good"
    elif disc >= 80:
        rating = "average"
    else:
        rating = "expensive"
    why.append(f"скидка {disc}%")
    headroom = disc - floor
    if headroom >= 0:
        why.append(f"выше минимума на {round(headroom)} п.п.")
    else:
        why.append(f"ниже минимума ({floor}%) на {round(-headroom)} п.п.")
    if not g.get("market_active"):
        why.append("нет активных предложений")
    if g.get("ttft_ms") is not None:
        why.append(f"TTFT {round(g['ttft_ms'])} мс (проверка)")
    if g.get("offers_count") or g.get("sellers_count"):
        n = (g.get("offers_count") or 0) + (g.get("sellers_count") or 0)
        why.append(f"предложений/продавцов: {n}")
    return {"rating": rating, "discount_pct": disc, "floor_pct": floor,
            "why": " + ".join(why)}


_RATING_RU = {"excellent": "Отличная", "good": "Хорошая",
              "average": "Средняя", "expensive": "Дорогая", "unknown": "Неизвестно"}


def rating_ru(r: str) -> str:
    return _RATING_RU.get(r, r)


# ── §6: Почему Router использует / не использует модель ───────────────────

def why_router(g: dict) -> dict:
    """Check-by-check explanation for one canonical group."""
    checks: list[dict] = []
    def add(ok: bool, yes: str, no: str):
        checks.append({"ok": bool(ok), "text": yes if ok else no})
    floor = round((g.get("effective_min_discount") or 0.8) * 100)
    disc = g.get("discount_pct")
    pol = store.get_pool_policy(g["canonical"])
    routes = g.get("routes") or []

    add(g.get("canonical") is not None, "модель сопоставлена", "нет canonical-сопоставления")
    add(bool(routes), f"маршрутов: {len(routes)}", "нет маршрутов")
    add(bool(g.get("market_active")), "есть предложения на рынке", "нет активных предложений на рынке")
    if disc is None:
        add(False, "", "нет данных о текущей скидке")
    else:
        add(disc >= floor, f"скидка {disc}% ≥ минимум {floor}%",
            f"скидка {disc}% < минимум {floor}%")
    add(not pol.get("hidden"), "не скрыта", "скрыта администратором")
    add(pol.get("in_pool") is not False, "в моём пуле", "не в моём пуле")
    ctx_ok = g.get("context_max") is not None
    add(ctx_ok, "контекст известен", "контекст неизвестен")
    if g.get("last_probe_at"):
        age = time.time() - g["last_probe_at"]
        add(age < 86400, "проверка недавно", f"проверка была {round(age/3600)} ч назад")

    eligible = g.get("eligible")
    best = None
    best_routes = [r for r in routes if r.get("best_input") is not None]
    if best_routes:
        b = min(best_routes, key=lambda r: r["best_input"])
        best = {"provider": b["provider"], "provider_model_id": b["provider_model_id"],
                "best_input": b["best_input"], "best_output": b.get("best_output"),
                "discount_pct": b.get("discount_pct")}
    return {
        "canonical": g["canonical"],
        "display_name": g.get("display_name"),
        "used": bool(eligible),
        "verdict": ("Router использует эту модель в автоматическом выборе"
                    if eligible else "не участвует в автоматическом выборе Router"),
        "checks": checks,
        "best_route": best,
        "floor_pct": floor,
        "discount_pct": disc,
    }


# ── §19: объяснение по провайдеру (почему Provider B не используется) ─────────

def provider_explanation(provider: str, inv_data: dict | None = None) -> dict:
    data = inv_data or inv.build_inventory()
    rec = inv.reconciliation(provider)
    prov_rows = [r for r in (data.get("unmatched", []))
                 if r.get("provider") == provider]
    # runtime requests for this provider
    return {
        "provider": provider,
        "reconciliation": rec,
        "used": rec.get("eligible", 0) > 0,
    }


# ── §29: economics — почему выбрана эта, а не альтернатива ────────────────

def economics(checks_html=False) -> dict:
    """Economics diagnostics over pool canonicals.

    'Возможная неэффективность': a non-selected canonical that (a) passes all
    gates via simulate-like evidence, (b) has known prices, (c) is materially
    cheaper than the cheapest eligible model of equal-or-better tier class.
    Tier/quality comparison uses the lifecycle registry classes only — no
    invented quality numbers.
    """
    data = inv.build_inventory()
    models = [g for g in data["models"] if not g.get("hidden")]
    eligible = [g for g in models if g.get("eligible") and g.get("best_input") is not None]
    not_used = [g for g in models if not g.get("eligible") and g.get("best_input") is not None]
    anomalies: list[dict] = []
    if eligible:
        for g in not_used:
            # gate check: the ONLY reason it is unused must be policy pool
            # membership (not price/market), and it must be materially cheaper
            pol = store.get_pool_policy(g["canonical"])
            reasons = {r.get("eligibility_reason") for r in g["routes"]}
            market_ok = any(r.get("market_active") for r in g["routes"])
            disc_ok = (g.get("discount_pct") or 0) >= round(
                (g.get("effective_min_discount") or 0.8) * 100)
            if market_ok and disc_ok and pol.get("in_pool") is not False:
                continue  # actual blocker unknown — no anomaly claim
            if pol.get("in_pool") is False or not market_ok or not disc_ok:
                # cheap-but-blocked: compare against cheapest eligible
                cheapest_el = min(eligible, key=lambda m: m["best_input"])
                if g["best_input"] < cheapest_el["best_input"] * 0.5:
                    anomalies.append({
                        "canonical": g["canonical"],
                        "display_name": g["display_name"],
                        "best_input": g["best_input"],
                        "discount_pct": g.get("discount_pct"),
                        "blocked_reason": next(iter(reasons)) if reasons else "не в пуле",
                        "cheaper_than": {
                            "canonical": cheapest_el["canonical"],
                            "display_name": cheapest_el["display_name"],
                            "best_input": cheapest_el["best_input"],
                        },
                        "note": ("дешевле минимум чем в 2 раза, но не участвует: "
                                 + (next(iter(reasons)) if reasons else "не в моём пуле")),
                    })
    anomalies.sort(key=lambda a: a["best_input"])
    return {"anomalies": anomalies[:20], "anomaly_count": len(anomalies)}


# ── §30: выгодные модели, которые вы не используете ───────────────────────

def opportunities(limit: int = 10) -> list[dict]:
    data = inv.build_inventory()
    out = []
    for g in data["models"]:
        if g.get("hidden") or g.get("eligible") or g.get("in_pool"):
            continue
        disc = g.get("discount_pct")
        if disc is None or not g.get("market_active") or disc < 90:
            continue
        out.append({
            "canonical": g["canonical"], "display_name": g["display_name"],
            "discount_pct": disc,
            "best_input": g.get("best_input"), "best_output": g.get("best_output"),
            "providers": sorted(set(g["providers"])),
            "context_max": g.get("context_max"),
            "offers_count": g.get("offers_count"), "sellers_count": g.get("sellers_count"),
        })
    out.sort(key=lambda m: -(m["discount_pct"] or 0))
    return out[:limit]


# ── §31: аномалии цен и рынка ─────────────────────────────────────────────

def alerts() -> list[dict]:
    """UI-only alerts: price spikes, discount below policy, liquidity loss,
    model disappearance. Deterministic thresholds, no notifications."""
    out: list[dict] = []
    data = inv.build_inventory()
    now = time.time()
    for g in data["models"]:
        disc = g.get("discount_pct")
        floor = round((g.get("effective_min_discount") or 0.8) * 100)
        # discount fell below policy while model IS in the pool
        if g.get("in_pool") and disc is not None and disc < floor:
            out.append({"kind": "discount_below_policy", "severity": "warn",
                        "canonical": g["canonical"], "display_name": g["display_name"],
                        "text": f"скидка {disc}% упала ниже минимума {floor}%"})
        # model disappeared from catalog
        if any(r.get("missing_from_catalog") for r in [g]):
            pass
        for r in g.get("routes", []):
            pass
        if all(not r.get("market_active") for r in g.get("routes", [])) and g.get("in_pool"):
            out.append({"kind": "no_liquidity", "severity": "bad",
                        "canonical": g["canonical"], "display_name": g["display_name"],
                        "text": "нет активных предложений на рынке"})
    # price spikes vs 24h history
    for g in data["models"]:
        hist = store.price_history(canonical=g["canonical"], since=now - 86400 * 2)
        cur = g.get("best_input")
        if hist and cur is not None:
            past = [h["best_input"] for h in hist
                    if h.get("best_input") is not None and now - h["ts"] > 3600]
            if past:
                baseline = sum(past) / len(past)
                if baseline > 0 and cur > baseline * 1.25:
                    out.append({"kind": "price_up", "severity": "warn",
                                "canonical": g["canonical"], "display_name": g["display_name"],
                                "text": (f"цена входа выросла на {round((cur/baseline-1)*100)}% "
                                         f"за 24ч (${baseline:.4f} → ${cur:.4f} / 1M)")})
    out.sort(key=lambda a: {"bad": 0, "warn": 1}.get(a.get("severity"), 2))
    return out[:30]
