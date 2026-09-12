"""R11 §1/§5/§26/§27: provider discovery layer.

DISCOVERY != ELIGIBILITY. This module fetches RAW provider catalogs and
market data WITHOUT policy filtering, persists them (first_seen/last_seen/
last_priced), and returns everything the Admin UI needs to show the real
marketplace. Policy (min_discount, my-pool membership, canonical mapping)
is applied at READ time, never at discovery time.

Data model (R11 §1):
  PROVIDER          provider_a | provider_b | custom N
  CANONICAL MODEL   gpt-5.6-luna  (mapping may be absent -> unmatched)
  ROUTE             provider + provider_model_id (router route)
  MARKET OFFER      dynamic asks inside one marketplace (Provider A asks_in/
                    asks_out lists, Provider B /api/markets rows) — NEVER a
                    router route.

Sources of truth (live, no static copies):
  Provider A  GET /v1/models            174 models; pricing.official_in/out,
                                     asks_in[], asks_out[], min_ask_in/out
                                     (USD per 1M tokens), input_token_limit,
                                     upstream_label, modality
  Provider B   GET /v1/models            404 catalog entries (pricing.prompt/
                                     completion = OFFICIAL USD/token)
            GET /api/markets          397 active markets: best_input_per_1m,
                                     best_output_per_1m, best_discount_pct,
                                     num_sellers, credits_sold_24h, ...
            GET /v1/prices            404-model reference matrix (per-provider
                                     official pricing)
Inference base URLs stay on /min80 (Provider B) and /v1 (Provider A); DISCOVERY
uses the unfiltered catalog endpoints regardless of the discount floor.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from . import store

IH_BASE = os.environ.get("HERMES_ROUTER_PROVIDER_A_BASE", "https://provider-a.example/v1")
SP_CATALOG_BASE = "https://provider-b.example"   # unfiltered discovery
IH_KEY_ENV = "PROVIDER_A_API_KEY"
SP_KEY_ENV = "PROVIDER_B_API_KEY"

MARKET_TTL_S = 120.0          # live market data cache in RAM
CATALOG_STALE_S = 6 * 3600.0  # UI hint: "catalog устарел"

_market_cache: dict[str, tuple[float, Any]] = {}


# ── raw fetchers (no policy, no filtering) ─────────────────────────────────

async def _fetch(url: str, key: str, timeout_s: float = 30.0) -> tuple[int, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {key}"})
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        return r.status_code, body
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


async def fetch_provider_a_catalog() -> dict:
    """Live Provider A catalog: ALL models, official + asks, no filtering."""
    key = os.environ.get(IH_KEY_ENV, "")
    if not key:
        return {"ok": False, "error": f"env {IH_KEY_ENV} not set", "models": []}
    status, body = await _fetch(IH_BASE + "/models", key)
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", "models": []}
    out = []
    for e in (body.get("data") or []):
        pr = e.get("pricing") or {}
        out.append({
            "provider": "provider_a",
            "provider_model_id": e.get("id") or "",
            "display_name": e.get("upstream_label") or e.get("id") or "",
            "upstream_label": e.get("upstream_label"),
            "context": e.get("input_token_limit"),
            "max_output_tokens": e.get("max_output_tokens"),
            "modality": e.get("modality"),
            "supports_cache": e.get("supports_cache"),
            "owned_by": e.get("owned_by"),
            "official_input": pr.get("official_in"),
            "official_output": pr.get("official_out"),
            "asks_in": pr.get("asks_in") or [],
            "asks_out": pr.get("asks_out") or [],
            "min_ask_in": pr.get("min_ask_in"),
            "min_ask_out": pr.get("min_ask_out"),
            "ask_count": len(pr.get("asks_in") or []),
        })
    return {"ok": True, "models": out, "fetched_at": time.time()}


async def fetch_provider_b_data() -> dict:
    """Live Provider B: catalog + active markets + reference prices, unfiltered."""
    key = os.environ.get(SP_KEY_ENV, "")
    if not key:
        return {"ok": False, "error": f"env {SP_KEY_ENV} not set",
                "models": [], "markets": {}, "prices": {}}
    st_cat, cat = await _fetch(SP_CATALOG_BASE + "/v1/models", key)
    st_mkt, mkt = await _fetch(SP_CATALOG_BASE + "/api/markets", key)
    if st_cat != 200:
        return {"ok": False, "error": f"catalog HTTP {st_cat}",
                "models": [], "markets": {}, "prices": {}}
    # reference price matrix (best-effort; UI marks ESTIMATED without it)
    st_pr, prices = await _fetch(SP_CATALOG_BASE + "/v1/prices", key)
    price_by_model: dict[str, dict] = {}
    if st_pr == 200:
        for row in (prices.get("models") or []):
            price_by_model[row.get("model") or ""] = {
                "display_name": row.get("displayName"),
                "official_input": min((p.get("pricing", {}).get("input") or 1e12)
                                      for p in row.get("providers") or []
                                      if p.get("pricing", {}).get("input") is not None)
                if row.get("providers") else None,
                "official_output": min((p.get("pricing", {}).get("output") or 1e12)
                                       for p in row.get("providers") or []
                                       if p.get("pricing", {}).get("output") is not None)
                if row.get("providers") else None,
            }
    market_by_model: dict[str, dict] = {}
    if st_mkt == 200:
        for m in (mkt.get("markets") or []):
            mid = m.get("model") or ""
            if not mid:
                continue  # empty market rows (placeholder entries) — not models
            # /api/markets prices are MICRO-USD per 1M tokens (verified live:
            # direct_input_per_1m == official USD price * 1e6). Convert to USD.
            def usd(v):
                return round(v / 1e6, 6) if isinstance(v, (int, float)) else None
            market_by_model[mid] = {
                "best_input_per_1m": usd(m.get("best_input_per_1m")),
                "best_output_per_1m": usd(m.get("best_output_per_1m")),
                "best_cache_read_per_1m": usd(m.get("best_cache_read_per_1m")),
                "best_cache_write_per_1m": usd(m.get("best_cache_write_per_1m")),
                "best_discount_pct": m.get("best_discount_pct"),
                "num_sellers": m.get("num_sellers"),
                "healthy_seller_count": m.get("healthy_seller_count"),
                "credits_sold_24h": m.get("credits_sold_24h"),
                "volume_24h": m.get("volume_24h"),
                "requests_24h": m.get("requests_24h"),
                "discounted_liquidity_usd": m.get("discounted_liquidity_usd"),
            }
    out = []
    for e in (cat.get("data") or []):
        pid = e.get("id") or ""
        if not pid:
            continue
        pr = e.get("pricing") or {}
        mk = market_by_model.get(pid) or {}
        ref = price_by_model.get(pid) or {}
        official_in = ref.get("official_input")
        official_out = ref.get("official_output")
        if official_in is None:
            oi = float(pr.get("prompt") or 0) * 1e6
            official_in = oi if oi > 0 else None
        if official_out is None:
            oo = float(pr.get("completion") or 0) * 1e6
            official_out = oo if oo > 0 else None
        out.append({
            "provider": "provider_b",
            "provider_model_id": pid,
            "display_name": ref.get("display_name") or e.get("name") or pid,
            "upstream_label": e.get("name"),
            "context": e.get("context_length"),
            "modality": (e.get("architecture") or {}).get("modality"),
            "official_input": official_in,      # USD per 1M
            "official_output": official_out,     # USD per 1M
            "cache_read": mk.get("best_cache_read_per_1m"),
            "cache_write": mk.get("best_cache_write_per_1m"),
            "market": mk or None,                # None -> no active sellers
        })
    return {"ok": True, "models": out, "markets": market_by_model,
            "prices_fetched": st_pr == 200, "fetched_at": time.time()}


# ── persistence: discovered_models, market_snapshots ───────────────────────

def schema_fingerprint(provider: str, models: list[dict]) -> str:
    """R15 §8: structural fingerprint of a fetched catalog — expected fields,
    their presence ratio, and unit scale. A later fetch whose fingerprint
    differs materially (pricing field gone, unit changed, offers vanished)
    is schema drift and must NOT destroy the last good registry."""
    if not models:
        return "empty"
    fields = ("official_input", "official_output", "min_ask_in", "min_ask_out",
              "context_length", "capabilities")
    parts = []
    for f in fields:
        present = sum(1 for m in models
                      if m.get(f) is not None or (m.get("market") or {}).get(f) is not None)
        parts.append(f"{f}:{round(present / len(models), 2)}")
    # unit scale: median of positive numeric prices (µUSD vs USD jump)
    vals = sorted(v for v in
                  (_positive(m.get("official_input")) for m in models) if v)
    med = vals[len(vals) // 2] if vals else 0
    scale = "k+" if med >= 1000 else ("1k" if med >= 100 else ("0.1k" if med >= 10 else "small"))
    priced = sum(1 for m in models
                 if m.get("official_input") is not None or m.get("min_ask_in") is not None
                 or (m.get("market") or {}).get("best_input_per_1m") is not None)
    offers = sum(1 for m in models if (m.get("market") or {}).get("num_sellers")
                 or m.get("ask_count"))
    parts.append(f"scale:{scale}")
    parts.append(f"priced:{round(priced / len(models), 2)}")
    parts.append(f"offers:{round(offers / max(1, len(models)), 2)}")
    return ";".join(parts)


def _positive(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def check_schema_drift(provider: str, models: list[dict]) -> dict | None:
    """Compare the fetched catalog fingerprint to the last accepted one
    (kv: fp:<provider>). Pricing field disappearance / unit change /
    offers collapse → drift alert; the caller must SKIP persist (the last
    good registry survives) and record a critical warning."""
    fp = schema_fingerprint(provider, models)
    try:
        prev = store.get_kv(f"fp:{provider}")
    except Exception:  # noqa: BLE001
        prev = None
    if prev is None:
        try:
            store.set_kv(f"fp:{provider}", fp)
        except Exception:  # noqa: BLE001
            pass
        return None
    if prev == fp:
        return None
    reasons: list[str] = []
    def _part(fp_: str, key: str) -> float:
        for p_ in fp_.split(";"):
            if p_.startswith(key + ":"):
                try:
                    return float(p_.split(":", 1)[1])
                except ValueError:
                    return 0.0
        return 0.0
    for f in ("official_input", "min_ask_in"):
        if _part(prev, f) >= 0.5 and _part(fp, f) < 0.2:
            reasons.append(f"поле цен {f} исчезло у большинства моделей")
    if _part(prev, "scale") != _part(fp, "scale") and _part(prev, "official_input") > 0:
        reasons.append("масштаб цен изменился — подозрение на смену единиц")
    if _part(prev, "offers") >= 0.3 and _part(fp, "offers") < 0.05:
        reasons.append("предложения резко исчезли")
    if reasons:
        try:
            store.insert_fingerprint_alert(provider, fp, prev,
                                            "; ".join(reasons), severity="bad")
            store.insert_system_event({"kind": "schema", "severity": "bad",
                                       "object": provider,
                                       "reason": "; ".join(reasons),
                                       "recommended": "последний рабочий реестр сохранён; проверить API провайдера"})
        except Exception:  # noqa: BLE001
            pass
        return {"provider": provider, "reasons": reasons,
                "fingerprint": fp, "prev_fingerprint": prev}
    try:
        store.set_kv(f"fp:{provider}", fp)
    except Exception:  # noqa: BLE001
        pass
    return None


def persist_discovery(provider: str, models: list[dict]) -> dict:
    """Upsert raw catalog rows. Returns {new, gone, total}."""
    now = time.time()
    new, seen = 0, set()
    for m in models:
        pid = m.get("provider_model_id") or ""
        if not pid:
            continue
        seen.add(pid)
        priced = (m.get("official_input") is not None
                  or m.get("min_ask_in") is not None
                  or (m.get("market") or {}).get("best_input_per_1m") is not None)
        if store.upsert_discovered(provider, pid, m, priced=priced, now=now):
            new += 1
    gone = store.mark_missing(provider, seen, now)
    return {"total": len(seen), "new": new, "gone": gone, "updated_at": now}



# ── R13 §7: price sanity guards ────────────────────────────────────────────
# Reject impossible values BEFORE they enter discovery/history/ranking.
# $0 is allowed ONLY when explicitly proven FREE (a positive official price
# with a 100% discount flag from the provider, or provider price == 0).

def _is_bad_number(v) -> bool:
    return v is None or isinstance(v, bool) or not isinstance(v, (int, float)) \
        or (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))))


def sanitize_prices(model: dict) -> tuple[dict, list[str]]:
    """Return (model, warnings). Drops invalid/negative prices, marks unit
    anomalies, flags absurd jumps and extreme discounts for review."""
    warns: list[str] = []
    for f in ("official_input", "official_output",
              "min_ask_in", "min_ask_out"):
        v = model.get(f)
        if v is None:
            continue
        if _is_bad_number(v) or v < 0:
            model[f] = None
            warns.append(f"{f}: invalid value {v!r} rejected")
    mk = model.get("market") or {}
    for f in ("best_input_per_1m", "best_output_per_1m",
              "best_cache_read_per_1m", "best_cache_write_per_1m"):
        v = mk.get(f)
        if v is None:
            continue
        if _is_bad_number(v) or v < 0:
            mk[f] = None
            warns.append(f"market.{f}: invalid value {v!r} rejected")
    # Provider B µUSD conversion guard: converted USD must be strictly below
    # the raw µ-value magnitude AND below a sane ceiling ($10k/1M).
    if mk.get("best_input_per_1m") is not None:
        ui = mk["best_input_per_1m"]
        if ui > 10000:
            mk["best_input_per_1m"] = None
            warns.append(f"market.best_input_per_1m={ui} exceeds $10k/1M — unit suspect, dropped")
    # $0 only if FREE is proven (official 0 or explicit free flag)
    for f in ("min_ask_in", "min_ask_out"):
        if model.get(f) == 0 and (model.get("official_input") or 0) > 0:
            model[f] = None
            warns.append(f"{f}=0 without proven FREE — dropped")
    # extreme discount -> warning (kept, but flagged)
    oi = model.get("official_input")
    mi = model.get("min_ask_in")
    if oi and mi:
        d = 100 * (1 - mi / oi)
        if d > 99.99:
            warns.append(f"discount {d:.4f}% > 99.99% — recheck source")
        if mi > oi:
            warns.append(f"market {mi} > official {oi} (recorded, not an error)")
    # official < market is legal (record only)
    oo, mo = model.get("official_output"), model.get("min_ask_out")
    if oo and mo and mo > oo:
        warns.append(f"market out {mo} > official out {oo} (recorded)")
    return model, warns


# ── R13 §6: discovery shrink guard ─────────────────────────────────────────
# A provider catalog collapse (Provider B 404 -> 1) must NEVER silently replace
# the working registry: keep the previous count, emit an anomaly, skip the
# destructive persist (old rows stay, missing_since is not set).

_SHRINK_WARN_RATIO = 0.5


def _previous_count(provider: str) -> int | None:
    try:
        rows = store.list_discovered(provider=provider, include_missing=True)
        return len(rows)
    except Exception:  # noqa: BLE001
        return None


def check_shrink(provider: str, new_count: int) -> dict | None:
    """Returns an anomaly dict when the catalog collapses >50%."""
    prev = _previous_count(provider)
    if prev is None or prev == 0:
        return None
    if new_count < prev * _SHRINK_WARN_RATIO:
        return {"provider": provider, "previous": prev, "new": new_count,
                "ratio": round(new_count / prev, 3),
                "text": (f"каталог {provider} сократился с {prev} до {new_count} "
                         f"({round(100*new_count/prev)}%) — обновление НЕ применено,"
                         f" проверьте провайдера")}
    return None


def _history_rows(provider: str, models: list[dict], now: float) -> list[dict]:
    """R12 §28: one price snapshot per route per refresh (no fabrication)."""
    rows = []
    for m in models:
        pid = m.get("provider_model_id") or ""
        if not pid:
            continue
        mk = m.get("market") or {}
        best_in = mk.get("best_input_per_1m") or m.get("min_ask_in")
        best_out = mk.get("best_output_per_1m") or m.get("min_ask_out")
        disc = mk.get("best_discount_pct")
        oi = m.get("official_input")
        if disc is None and oi and best_in:
            disc = round(100 * (1 - best_in / oi), 2)
        if best_in is None and best_out is None:
            continue  # nothing to snapshot
        rows.append({
            "provider": provider, "provider_model_id": pid,
            "canonical": store.get_canonical_for(provider, pid),
            "ts": now, "best_input": best_in, "best_output": best_out,
            "official_input": oi, "official_output": m.get("official_output"),
            "discount_pct": disc,
            # Provider A: ask lists are PRICE OFFERS, seller identity unknown.
            # Provider B: real seller count. Never double-count in/out asks.
            "offers_count": m.get("ask_count") if provider == "provider_a" else None,
            "sellers_count": mk.get("num_sellers") if provider == "provider_b" else None,
        })
    return rows


async def refresh_all(force: bool = False) -> dict:
    """Fetch + persist both providers. Market data cached MARKET_TTL_S.

    R13: price sanity guards run first; a catalog shrink >50% skips the
    destructive persist for that provider (anomaly recorded instead)."""
    out: dict[str, Any] = {}
    out["warnings"] = []
    out["anomalies"] = []
    ih, sp = await asyncio.gather(fetch_provider_a_catalog(), fetch_provider_b_data())
    # R13 §7: sanitize every model's prices first (drop invalid, warn anomalies)
    for _res in (ih, sp):
        if _res.get("ok"):
            _ws = []
            for _m in _res["models"]:
                _m, _w = sanitize_prices(_m)
                _ws.extend(_w)
            if _ws:
                out["warnings"].extend(_ws[:50])
    # R13 §6: shrink guard — a >50% catalog collapse must NOT be persisted
    if ih["ok"]:
        shrink = check_shrink("provider_a", len(ih["models"]))
        drift = check_schema_drift("provider_a", ih["models"])
        if shrink:
            out["anomalies"].append(shrink)
            out["provider_a"] = {"ok": False, "blocked_by": "catalog_shrink_guard",
                               **shrink}
        elif drift:
            out["anomalies"].append({"provider": "provider_a",
                                     "text": "schema drift: " + "; ".join(drift["reasons"])
                                             + " — обновление НЕ применено, реестр сохранён"})
            out["provider_a"] = {"ok": False, "blocked_by": "schema_drift_guard", **drift}
        else:
            out["provider_a"] = persist_discovery("provider_a", ih["models"])
            out["provider_a"]["ok"] = True
    else:
        out["provider_a"] = {"ok": False, "error": ih.get("error")}
    if sp["ok"]:
        shrink = check_shrink("provider_b", len(sp["models"]))
        drift = check_schema_drift("provider_b", sp["models"])
        if shrink:
            out["anomalies"].append(shrink)
            out["provider_b"] = {"ok": False, "blocked_by": "catalog_shrink_guard",
                              **shrink}
        elif drift:
            out["anomalies"].append({"provider": "provider_b",
                                     "text": "schema drift: " + "; ".join(drift["reasons"])
                                             + " — обновление НЕ применено, реестр сохранён"})
            out["provider_b"] = {"ok": False, "blocked_by": "schema_drift_guard", **drift}
        else:
            out["provider_b"] = persist_discovery("provider_b", sp["models"])
            out["provider_b"]["ok"] = True
            store.save_market_snapshot("provider_b", sp["markets"])
    else:
        out["provider_b"] = {"ok": False, "error": sp.get("error")}
    # R12: price snapshots for history (best effort, per-provider)
    hist = []
    if ih["ok"] and out["provider_a"].get("ok"):
        hist += _history_rows("provider_a", ih["models"], ih.get("fetched_at") or time.time())
    if sp["ok"] and out["provider_b"].get("ok"):
        hist += _history_rows("provider_b", sp["models"], sp.get("fetched_at") or time.time())
    try:
        out["price_history_points"] = store.record_price_history(hist)
    except Exception as e:  # noqa: BLE001 — history must never break discovery
        out["price_history_points"] = 0
        out["price_history_error"] = f"{type(e).__name__}: {e}"
    return out


async def market_snapshot_cached() -> dict:
    """Provider B /api/markets with a short RAM cache (UI latency).
    Prices converted micro-USD -> USD per 1M (same as fetch_provider_b_data)."""
    ts, data = _market_cache.get("provider_b", (0.0, None))
    if data is not None and time.time() - ts < MARKET_TTL_S:
        return data
    key = os.environ.get(SP_KEY_ENV, "")
    st, body = await _fetch(SP_CATALOG_BASE + "/api/markets", key)
    if st == 200:
        def usd(v):
            return round(v / 1e6, 6) if isinstance(v, (int, float)) else None
        data = {}
        for m in (body.get("markets") or []):
            mid = m.get("model")
            if not mid:
                continue
            n = dict(m)
            for k in ("best_input_per_1m", "best_output_per_1m",
                      "best_cache_read_per_1m", "best_cache_write_per_1m"):
                n[k] = usd(m.get(k))
            data[mid] = n
        _market_cache["provider_b"] = (time.time(), data)
        return data
    return store.load_market_snapshot("provider_b") or {}
