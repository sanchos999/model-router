"""R14: Router Control Center — explainability endpoints.

Everything here is READ-ONLY over code truth + live state. No invented
pipelines: the gate order mirrors gateway/selector.py exactly, the parameter
table mirrors gateway/config.py + control revisions, the class/tier table
mirrors gateway/classifier.py. Where a value is hardcoded in code it is
reported with source="code" and configurable=false — the honest answer to
«а почему именно так» (R14 §39/§40: no hidden magic parameters).

Endpoints (mounted under /admin):
  GET  /routing/effective       — every routing parameter: value/source/default
  GET  /routing/pipeline        — the real decision pipeline (code truth)
  GET  /routing/distribution    — actual request distribution (24h default)
  GET  /routing/decisions       — last routing decisions (privacy-safe)
  POST /routing/replay-impact   — replay recent decisions under a candidate policy
  GET  /taCHANGE_ME            — real task classes + tiers + candidates
  GET  /quality/models          — per-model quality evidence (RU-mapped)
"""
from __future__ import annotations

import time
from collections import Counter

from fastapi import APIRouter, HTTPException, Request

from . import store
from .revisions import POLICY_CONTRACT, active_config, defaults
from .admin_api import _runtime_get

router = APIRouter(prefix="/admin")


def _check_auth(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost", "testclient"):
        return
    import os
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token or request.headers.get("x-admin-token") != token:
        raise HTTPException(status_code=401, detail="admin token required")


# ── §10/§39: effective configuration — value, source, default, override ────

_POLICY_RU = {
    "min_discount": ("Минимальная скидка (глобальный флор)",
                     "Маршрут допускается, только если скидка ≥ этого значения. "
                     "Меньше — плата выше допустимой."),
    "quality_first": ("Качество в приоритете",
                      "Сначала отсев по качеству, потом цена."),
    "health_ttl_success_s": ("TTL здоровья после успеха",
                             "Сколько секунд успех считается свежим доказательством здоровья."),
    "health_ttl_failure_s": ("TTL здоровья после сбоя",
                             "Сколько секунд сбой «портит» маршрут."),
    "cache_switch_horizon": ("Горизонт смены маршрута (кэш)",
                             "На сколько будущих запросов проектируется выгода при смене маршрута."),
    "cache_switch_margin": ("Запас для смены маршрута (кэш)",
                            "Выгода смены должна превышать потерю кэша в это число раз."),
    "reliability_min_samples": ("Минимум примеров надёжности",
                                "При меньшем числе примеров стоимость считается оценкой, не фактом."),
    "price_evidence_ttl_s": ("TTL ценовых доказательств",
                             "Сколько секунд цена считается актуальной."),
    "cache_affinity_ttl_s": ("TTL привязки кэша к сессии",
                             "Сколько секунд сессия «держит» тёплый маршрут."),
    "canonical_switch_margin_factor": ("Запас смены модели (кэш)",
                                       "Во сколько раз выгода должна превышать потерю, чтобы сменить модель."),
    "free_route_min_success_rate": ("Мин. успешность бесплатного маршрута",
                                    "Бесплатный маршрут выигрывает только при этой надёжности."),
    "quality_floors": ("Минимальное качество по уровням",
                       "Модель допускается к уровню, если её качество ≥ порога."),
}

_CODE_PARAMS = None


def _code_params() -> list[dict]:
    """Hardcoded routing parameters (R14 §40 config-coverage audit)."""
    global _CODE_PARAMS
    if _CODE_PARAMS is not None:
        return _CODE_PARAMS
    from ..registry import DISCOUNT_FLOOR
    from ..selector import RESERVED_OUTPUT, SAFE_CONTEXT_RATIO, UNKNOWN_COST_SENTINEL
    from ..timeouts import load_policy
    tp = load_policy()
    _CODE_PARAMS = [
        {"key": "certified_discount_floor", "value": DISCOUNT_FLOOR,
         "source": "код (registry.DISCOUNT_FLOOR)", "default": DISCOUNT_FLOOR,
         "configurable": False,
         "ru": "Сертифицированный минимальный флор скидки — конфигурация не может опуститься ниже без явного предупреждения при валидации."},
        {"key": "safe_context_ratio", "value": SAFE_CONTEXT_RATIO,
         "source": "код (selector.SAFE_CONTEXT_RATIO)", "default": SAFE_CONTEXT_RATIO,
         "configurable": False,
         "ru": "Доля контекста маршрута, считающаяся безопасной (запас на вывод)."},
        {"key": "reserved_output_tokens", "value": RESERVED_OUTPUT,
         "source": "код (selector.RESERVED_OUTPUT)", "default": RESERVED_OUTPUT,
         "configurable": False,
         "ru": "Резерв токенов вывода, вычитаемый из контекста маршрута."},
        {"key": "recent_failure_penalty_s", "value": 45.0,
         "source": "код (selector._key_score)", "default": 45.0,
         "configurable": False,
         "ru": "Сколько секунд маршрут после сбоя ранжируется позади чистых маршрутов."},
        {"key": "alternate_quality_slack", "value": 0.20,
         "source": "код (selector.plan)", "default": 0.20,
         "configurable": False,
         "ru": "Запас качества для запасной (failover) модели: порог уровня минус 0.20."},
        {"key": "unknown_cost_sentinel", "value": UNKNOWN_COST_SENTINEL,
         "source": "код (selector.UNKNOWN_COST_SENTINEL)", "default": UNKNOWN_COST_SENTINEL,
         "configurable": False,
         "ru": "Неизвестная цена никогда не выигрывает: ранжируется позади всех ценовых маршрутов."},
        {"key": "connect_timeout_s", "value": tp.connect_s,
         "source": "env GW_CONNECT_TIMEOUT_S / код (timeouts)", "default": tp.connect_s,
         "configurable": True, "config_hint": "переменная окружения GW_CONNECT_TIMEOUT_S",
         "ru": "Таймаут установки соединения с провайдером."},
        {"key": "first_response_timeout_s", "value": tp.first_response_s,
         "source": "env GW_FIRST_RESPONSE_TIMEOUT_S / код (timeouts)", "default": tp.first_response_s,
         "configurable": True, "config_hint": "переменная окружения GW_FIRST_RESPONSE_TIMEOUT_S",
         "ru": "Таймаут до первого токена (TTFT)."},
        {"key": "stream_idle_timeout_s", "value": tp.stream_idle_s,
         "source": "env GW_STREAM_IDLE_TIMEOUT_S / код (timeouts)", "default": tp.stream_idle_s,
         "configurable": True, "config_hint": "переменная окружения GW_STREAM_IDLE_TIMEOUT_S",
         "ru": "Максимальная пауза между чанками стрима."},
        {"key": "normal_total_timeout_s", "value": tp.normal_total_s,
         "source": "env GW_NORMAL_TOTAL_TIMEOUT_S / код (timeouts)", "default": tp.normal_total_s,
         "configurable": True, "config_hint": "переменная окружения GW_NORMAL_TOTAL_TIMEOUT_S",
         "ru": "Общий бюджет запроса для T1–T3."},
        {"key": "t4_total_timeout_s", "value": tp.t4_total_s,
         "source": "env GW_T4_TOTAL_TIMEOUT_S / код (timeouts)", "default": tp.t4_total_s,
         "configurable": True, "config_hint": "переменная окружения GW_T4_TOTAL_TIMEOUT_S",
         "ru": "Общий бюджет запроса для T4 (критические задачи)."},
        {"key": "pre_first_token_failover_budget_s", "value": tp.pre_first_failover_s,
         "source": "env GW_PRE_FIRST_FAILOVER_TIMEOUT_S / код (timeouts)", "default": tp.pre_first_failover_s,
         "configurable": True, "config_hint": "переменная окружения GW_PRE_FIRST_FAILOVER_TIMEOUT_S",
         "ru": "Бюджет failover до первого токена: за это время пробуются запасные маршруты."},
    ]
    return _CODE_PARAMS


@router.get("/routing/effective")
async def routing_effective(request: Request):
    """R14 §10/§39: every parameter that affects route selection, with
    value / source / default. Mutable keys come from the active config
    revision; code constants are labelled honestly."""
    _check_auth(request)
    cfg = active_config()
    dflt = defaults()
    out = []
    for key, (ru, hint) in _POLICY_RU.items():
        value = cfg.get(key)
        v_default = dflt.get(key)
        if key == "quality_floors":
            out.append({
                "key": key, "value": value, "default": v_default,
                "ru": ru, "hint": hint,
                "source": "активная конфигурация (ревизия)",
                "configurable": True,
                "children": [{"key": f"{key}.{t}", "value": (value or {}).get(t),
                              "default": (v_default or {}).get(t)}
                             for t in ("T1", "T2", "T3", "T4")],
            })
            continue
        out.append({
            "key": key, "value": value, "default": v_default,
            "ru": ru, "hint": hint,
            "source": "активная конфигурация (ревизия)" if key in cfg else "default (config/defaults.yaml)",
            "configurable": key in POLICY_CONTRACT,
        })
    # provider floors
    for pname, pc in (cfg.get("providers") or {}).items():
        out.append({
            "key": f"providers.{pname}.min_discount", "value": pc.get("min_discount"),
            "default": cfg.get("min_discount"),
            "ru": f"Минимальная скидка — {pname}",
            "hint": "Флор скидки конкретного провайдера (перекрывает глобальный).",
            "source": "активная конфигурация (ревизия)" if pc.get("min_discount") is not None
                      else f"наследует глобальный ({cfg.get('min_discount')})",
            "configurable": True,
        })
    out.extend(_code_params())
    _, rid = store.get_active_config()
    return {"params": out, "revision_id": rid,
            "code_params_count": len(_code_params()),
            "policy_params_count": len(_POLICY_RU)}


# ── §11/§38: the real pipeline, exactly as selector.py implements it ──────

PIPELINE = [
    {"stage": 1, "title": "Запрос",
     "ru": "Приходит запрос: модель (алиас/canonical/main-auto), текст задачи, оценка контекста, сессия.",
     "detail": "main-auto = селектор сам выбирает модель. Конкретное имя модели = авторитетная подсказка.",
     "code": "app.py: _canonical_from_alias_or_mapping, _task_class_from_header"},
    {"stage": 2, "title": "Класс задачи",
     "ru": "Детерминированный классификатор по тексту (без LLM): 13 классов от SIMPLE до CRITICAL.",
     "detail": "Заголовок x-hermes-taCHANGE_ME перекрывает классификацию.",
     "code": "classifier.py: classify()"},
    {"stage": 3, "title": "Уровень качества (tier)",
     "ru": "Класс → уровень T1–T4. Уровень задаёт минимальное качество модели.",
     "detail": "T1 простой → T4 критический. Порог качества настраивается (quality_floors).",
     "code": "classifier.py: tier_for_class()"},
    {"stage": 4, "title": "Временные правила",
     "ru": "Административные переопределения: зафиксировать модель/маршрут, отключить провайдера/модель/маршрут.",
     "detail": "Сбой control-plane НЕ ломает маршрутизацию: правила просто не применяются.",
     "code": "overrides.py: resolve(), integration.py: apply_overrides_to_context()"},
    {"stage": 5, "title": "Кандидаты-модели (LEVEL A)",
     "ru": "Отбор моделей: необходимые возможности ⊆, качество ≥ порога уровня, UNKNOWN-качество отклоняется для T3/T4.",
     "detail": "Сортировка: качество ↓, достоверность ↓, имя. Подсказка модели авторитетнее.",
     "code": "selector.py: candidate_canonicals()"},
    {"stage": 6, "title": "Жёсткие фильтры маршрута (LEVEL B)",
     "ru": "Для каждой модели перебираются все маршруты: здоровье/цепь → скидка ≥ флор → контекст (safe context) → возможности → сертификация.",
     "detail": "Отвергнутые маршруты попадают в trace с причиной — ничего не исчезает молча.",
     "code": "selector.py: _route_passes(), _plan_for_canonical()"},
    {"stage": 7, "title": "Ранжирование выживших",
     "ru": "Порядок ключа: здоровье → свежий сбой (45с) → число успехов → надёжность → стоимость/успех → латентность p95.",
     "detail": "Имя провайдера НЕ участвует: Provider A и Provider B конкурируют одинаково.",
     "code": "selector.py: _key_score()"},
    {"stage": 8, "title": "Экономика кэша",
     "ru": "Тёплая сессия: менять маршрут только если выгода > потеря кэша × запас. Холодная — просто дешевле выигрывает.",
     "detail": "Неизвестная цена не может выиграть (не «бесплатно»).",
     "code": "selector.py: cache_switch_economics(), _apply_cache_affinity()"},
    {"stage": 9, "title": "Запасная модель",
     "ru": "После маршрутов выбранной модели добавляется ОДНА запасная (порог уровня − 0.20) для failover.",
     "detail": "Сначала маршруты той же модели, потом запасная — порядок failover.",
     "code": "selector.py: plan() alternate block"},
    {"stage": 10, "title": "Failover транспорта",
     "ru": "При сбое маршрута транспорт идёт по плану. До первого токена — бюджет 20с на перебор.",
     "detail": "Исчерпание плана = явная ошибка upstream_exhausted, не пустой ответ.",
     "code": "transport.py: stream_plan/execute_plan, timeouts.py"},
]

PIPELINE_SUMMARY = (
    "Запрос → класс → уровень → временные правила → кандидаты (качество/возможности) → "
    "жёсткие фильтры маршрута → ранжирование (стоимость/надёжность/латентность) → "
    "экономика кэша → запасная модель → failover"
)


@router.get("/routing/pipeline")
async def routing_pipeline(request: Request):
    _check_auth(request)
    return {"stages": PIPELINE, "summary": PIPELINE_SUMMARY,
            "note": "Порядок шагов соответствует коду gateway/selector.py; "
                    "каждая стадия кликабельна и содержит ссылку на исходник."}


# ── §12: actual request distribution ──────────────────────────────────────

def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _aggregate(rows: list[dict], field: str, total: int) -> list[dict]:
    c = Counter((r.get(field) or "—") for r in rows)
    return [{"name": k, "requests": v, "pct": _pct(v, total)}
            for k, v in c.most_common()]


@router.get("/routing/distribution")
async def routing_distribution(request: Request, hours: float = 24.0):
    """R14 §12/§18: ACTUAL distribution of real requests over a period,
    from the decision journal. Honest sample count always shown; a small
    sample is displayed as such, never extrapolated. Provider/model shares
    are observed facts, NOT a configured traffic split — the Router has no
    static provider weights."""
    _check_auth(request)
    hours = max(0.1, min(float(hours), 24 * 30))
    rows = store.list_decisions(limit=50000, hours=hours)
    total = len(rows)
    # dedupe retries: one row per decision (journal has one row per request)
    by_tier = {}
    for t in ("T0", "T1", "T2", "T3", "T4"):
        n = sum(1 for r in rows if (r.get("tier") or "") == t)
        by_tier[t] = {"requests": n, "pct": _pct(n, total)}
    return {
        "hours": hours,
        "sample_count": total,
        "window_from": (time.time() - hours * 3600.0) if rows else None,
        "by_tier": by_tier,
        "by_task_class": _aggregate(rows, "task_class", total),
        "by_model": _aggregate(rows, "canonical", total),
        "by_provider": _aggregate(rows, "provider", total),
        "no_route_count": sum(1 for r in rows if not r.get("canonical")),
        "journaling_since": "R14",
        "note": ("Фактическое распределение за период по журналу решений. "
                 "Router не имеет статических весов провайдеров: доли — "
                 "наблюдаемый результат экономики, не настройка."),
    }


# ── §13: last decisions (privacy-safe) ────────────────────────────────────

_REASON_RU = {
    "quality_first_selected": "лучшее качество среди подходящих",
    "hint_route": "модель задана явно в запросе",
    "hint_only_route": "единственный маршрут заданной модели",
    "cache_retained": "сохранён тёплый маршрут сессии (кэш)",
    "cache_switch": "выгоднее сменить маршрут с учётом кэша",
    "alternate_tier_fallback": "запасная модель для failover",
    "primary": "самый выгодный допущенный маршрут",
    "alternate": "запасной маршрут для failover",
    "no_eligible_route": "нет допущенного маршрута",
}


def _reason_ru(r: str | None) -> str:
    if not r:
        return "—"
    if r in _REASON_RU:
        return _REASON_RU[r]
    for k, v in _REASON_RU.items():
        if r.startswith(k):
            return v
    return r


@router.get("/routing/decisions")
async def routing_decisions(request: Request, limit: int = 50):
    """R14 §13: «Почему Router выбирает…» — last decisions with a
    human-readable reason. Click → full decision trace. No prompt content."""
    _check_auth(request)
    rows = store.list_decisions(limit=max(1, min(limit, 500)))
    out = []
    for r in rows:
        t = r.get("trace") or {}
        out.append({
            "id": r.get("id"),
            "ts": r.get("ts"),
            "iso": time.strftime("%d.%m %H:%M:%S", time.localtime(r.get("ts") or 0)),
            "task_class": r.get("task_class"),
            "tier": r.get("tier"),
            "canonical": r.get("canonical"),
            "provider": r.get("provider"),
            "provider_model_id": r.get("provider_model_id"),
            "plan_len": r.get("plan_len"),
            "reason": _reason_ru(r.get("reason")),
            "reason_raw": r.get("reason"),
            "required_context": t.get("required_context"),
            "cache_state": t.get("cache_state"),
            "candidates": t.get("candidate_canonicals"),
            "est_cost_usd": _est_cost(r),
        })
    return {"decisions": out, "journaling_since": "R14",
            "note": "Журналирование решений включено с версии R14. "
                    "Более ранние запросы в журнале отсутствуют."}


def _est_cost(r: dict) -> float | None:
    """Оценка стоимости решения: required_context * input + 1k output по цене
    выбранного маршрута (текущие цены реестра). Помечается как оценка."""
    from ..app import _registry
    if not r.get("provider") or not r.get("provider_model_id"):
        return None
    rec = _registry.get_any(r["provider"], r["provider_model_id"])
    if rec is None or not (rec.input_price or rec.output_price):
        return None
    t = (r.get("trace") or {})
    tok_in = int(t.get("required_context") or 0)
    cost = (tok_in / 1e6) * rec.input_price + (1024 / 1e6) * rec.output_price
    return round(cost, 8)


@router.get("/routing/decisions/{decision_id}")
async def routing_decision_one(decision_id: str, request: Request):
    """Полный трейс одного решения (privacy-safe)."""
    _check_auth(request)
    r = store.get_decision(decision_id)
    if r is None:
        raise HTTPException(status_code=404, detail="decision not found")
    r = dict(r)
    r["reason_ru"] = _reason_ru(r.get("reason"))
    r["est_cost_usd"] = _est_cost(r)
    return r


# ── §14/§15: task classes and tiers — code truth ──────────────────────────

_CLASS_RU = {
    "SIMPLE": ("Простые ответы", "арифметика, факты, короткие ответы"),
    "SEARCH": ("Поиск", "найти документацию, справку, определение"),
    "SUMMARIZE": ("Сжатие/саммари", "сжать текст, пересказать, summarize"),
    "COMPRESSION": ("Компрессия контекста", "сжатие контекста сессии"),
    "SIMPLE_EDIT": ("Простая правка", "малые правки текста/кода"),
    "REPOSITORY_INSPECTION": ("Осмотр репозитория", "чтение кодовой базы, поиск по файлам"),
    "NORMAL_CODING": ("Кодирование", "реализация, правки, деплой"),
    "DEBUG": ("Отладка", "починить баг, отладка"),
    "REFACTOR": ("Рефакторинг", "переработка кода без смены поведения"),
    "ARCHITECTURE": ("Архитектура", "проектирование, миграции"),
    "REVIEW": ("Ревью", "review кода/документа"),
    "RESEARCH": ("Исследование", "изучить, сравнить, проанализировать"),
    "CRITICAL": ("Критическое", "безопасность, критичные решения, hard reasoning"),
}

_TIER_RU = {
    "T0": ("Детерминированный", "арифметика/явный структурный поиск; в текущем classifier не выдаётся (классификатор даёт T1+)"),
    "T1": ("Простой", "простые ответы, поиск, сжатие, компрессия, осмотр репозитория"),
    "T2": ("Средний (код)", "кодирование, отладка, рефакторинг; требует tool_call"),
    "T3": ("Сложный", "архитектура, ревью, исследование; нужно подтверждённое качество"),
    "T4": ("Критический", "безопасность, критичные архитектурные решения, hard reasoning"),
}


@router.get("/taCHANGE_ME")
async def task_classes(request: Request):
    """R14 §14/§15: REAL classification table read from gateway/classifier.py.
    Class→tier mapping is code-level (not configurable via revisions) —
    reported honestly instead of faking editable fields."""
    _check_auth(request)
    from ..classifier import CLASSES, capabilities_for_class, tier_for_class
    cfg = active_config()
    floors = (cfg.get("quality_floors") or {})
    # candidate models per class (needs the canonical registry)
    candidates: dict[str, list[str]] = {}
    try:
        from .admin_api import _ensure_registry_built
        await _ensure_registry_built()
        from ..app import _canon
        if _canon is not None:
            for cls in CLASSES:
                tier = tier_for_class(cls)
                floor = float(floors.get(tier, 0.0))
                accepted, _rej = _canon.candidates_for_task(
                    task_class=cls, capabilities=capabilities_for_class(cls),
                    quality_floor=floor, allow_unknown=False)
                accepted = sorted(accepted, key=lambda m: (-m.quality_score, m.canonical_id))
                candidates[cls] = [m.canonical_id for m in accepted[:5]]
    except Exception:
        pass
    # 24h counts
    rows = store.list_decisions(limit=50000, hours=24.0)
    counts = Counter((r.get("task_class") or "—") for r in rows)
    total = len(rows)
    tc_policy = cfg.get("task_classes") or {}
    classes = []
    for cls in CLASSES:
        pol = tc_policy.get(cls) or {}
        overridden = bool(pol)
        tier = tier_for_class(cls) if not overridden else (
            "T1" if pol.get("enabled") is False else pol.get("tier") or tier_for_class(cls))
        ru, desc = _CLASS_RU.get(cls, (cls, ""))
        n = counts.get(cls, 0)
        classes.append({
            "class": cls, "ru": ru, "description": desc,
            "tier": tier,
            "enabled": pol.get("enabled", True),
            "source": "config" if overridden else "code",
            "min_quality": floors.get(tier),
            "quality_floor": floors.get(tier),
            "capabilities": sorted(capabilities_for_class(cls)),
            "candidates": candidates.get(cls, []),
            "requests_24h": n, "pct_24h": _pct(n, total),
            "configurable": True,
            "configurable_note": "Класс→уровень задаётся кодом (classifier.py); "
                                 "активная ревизия может переопределить уровень "
                                 "или отключить класс (задачи обрабатываются как простые).",
        })
    tiers = []
    for t in ("T0", "T1", "T2", "T3", "T4"):
        ru, when = _TIER_RU[t]
        n = sum(1 for r in rows if (r.get("tier") or "") == t)
        tclasses = [c["class"] for c in classes if c["tier"] == t]
        # candidate canonicals: любые модели, допущенные для этого уровня
        tcand: list[str] = []
        try:
            from ..app import _canon
            if _canon is not None:
                floor = float(floors.get(t, 0.0))
                accepted, _rej = _canon.candidates_for_task(
                    task_class=tclasses[0] if tclasses else "SIMPLE",
                    capabilities=frozenset({"text", "streaming"}),
                    quality_floor=floor, allow_unknown=False)
                tcand = sorted((m.canonical_id for m in accepted),)[:8]
        except Exception:
            pass
        tiers.append({
            "tier": t, "ru": ru, "title": ru, "when": when, "when_used": when,
            "what": "Минимальное качество моделей: " + str(floors.get(t, "—")),
            "quality_floor": floors.get(t),
            "classes": tclasses,
            "candidates": tcand,
            "requests_24h": n, "pct_24h": _pct(n, total),
        })
    return {"classes": classes, "tiers": tiers, "sample_count_24h": total,
            "source": "gateway/classifier.py (код) + активная конфигурация (пороги)"}


# ── §16: quality of models ────────────────────────────────────────────────

_CONF_RU = {
    "VERIFIED": "Проверенное",
    "PROVISIONAL": "Предварительное",
    "INCOMPLETE": "Неполное",
    "UNKNOWN": "Не оценено",
}


@router.get("/quality/models")
async def quality_models(request: Request):
    """R14 §16: quality evidence per canonical model, RU-mapped, with why a
    model is/isn't eligible for T3/T4."""
    _check_auth(request)
    from .admin_api import _ensure_registry_built
    await _ensure_registry_built()
    from ..app import _canon
    cfg = active_config()
    floors = (cfg.get("quality_floors") or {})
    models = []
    snap = (_canon.snapshot() if _canon is not None else {}) or {}
    for mid, m in (snap.get("models") or {}).items():
        conf = m.get("confidence")
        score = m.get("quality_score")
        eligible = []
        why = []
        for t in ("T1", "T2", "T3", "T4"):
            floor = float(floors.get(t, 0.0))
            if conf == "UNKNOWN":
                ok = t not in ("T3", "T4")
                if not ok:
                    why.append(f"T3/T4 требуют подтверждённого качества — у модели нет калиброванных данных")
            else:
                ok = (score or 0.0) >= floor
                if not ok:
                    why.append(f"{t}: качество {score:.3f} < порог {floor}")
            if ok:
                eligible.append(t)
        models.append({
            "canonical": mid,
            "confidence": conf,
            "confidence_ru": _CONF_RU.get(conf, conf),
            "quality_score": score,
            "tier_eligibility": m.get("tier_eligibility"),
            "eligible_tiers": eligible,
            "why_not": "; ".join(sorted(set(why)))[:300] or None,
            "capabilities": m.get("capabilities"),
            "evidence_count": len(m.get("evidence") or []),
            "quality_frontier": m.get("quality_frontier"),
        })
    models.sort(key=lambda x: (-(x["quality_score"] or 0.0), x["canonical"]))
    return {"models": models, "floors": floors,
            "confidence_ru": _CONF_RU,
            "source": "качественные доказательства из бенчмарков (PERF/fq-v2), не ручной рейтинг"}


# ── §41: routing replay / impact ──────────────────────────────────────────

@router.post("/routing/replay-impact")
async def routing_replay_impact(request: Request):
    """R14 §41: replay the last N real decisions under a candidate policy
    patch (dry-run). Reports how many decisions would keep/switch provider
    or model, and which would lose their eligible route. Cost comparison is
    estimated from current route prices (labelled as estimate)."""
    _check_auth(request)
    body = await request.json()
    patch = body.get("config") or {}
    if body.get("revision_id"):
        rev_row = store.get_revision(str(body["revision_id"]))
        if rev_row is None:
            raise HTTPException(status_code=404, detail="revision not found")
        base = active_config()
        patch = {k: v for k, v in (rev_row.get("config") or {}).items()
                 if base.get(k) != v}
    limit = int(body.get("limit") or 100)
    limit = max(1, min(limit, 500))
    from . import revisions as rev
    errors = rev.validate_config(patch)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    from .admin_api import _ensure_registry_built
    await _ensure_registry_built()
    import dataclasses
    from ..app import _registry, _selector
    from ..config import load_config
    from ..selector import SelectionContext
    from ..classifier import capabilities_for_class

    records = store.list_decisions(limit=limit)
    if not records:
        return {"replayed": 0, "note": "Журнал решений пуст — impact недоступен. "
                "Журналирование решений включено с версии R14."}

    before_cfg = _registry.config()
    merged = dataclasses.replace(before_cfg)
    merged.apply_fields({k: v for k, v in patch.items() if k != "providers"})
    for pname, pc in (patch.get("providers") or {}).items():
        prov = merged.providers.get(pname)
        if prov is not None:
            prov.enabled = bool(pc.get("enabled", prov.enabled))
            if pc.get("min_discount") is not None:
                prov.min_discount = float(pc["min_discount"])

    def _choose_all(ctx_cfg) -> dict[int, tuple]:
        # temporarily swap the live config (control loop is single-threaded;
        # choose() is synchronous) — restored in finally.
        _registry._cfg = ctx_cfg
        try:
            out = {}
            for r in records:
                cls = r.get("task_class") or "NORMAL_CODING"
                t = r.get("trace") or {}
                ctx = SelectionContext(
                    required_context=int(t.get("required_context") or 0),
                    capabilities_required=capabilities_for_class(cls),
                    tier=r.get("tier"),
                    task_class=cls,
                    cache_state="UNKNOWN",
                )
                try:
                    primary, plan, _tr = _selector.choose(ctx)
                except Exception:
                    primary, plan = None, []
                out[r["id"]] = (primary, plan)
            return out
        finally:
            _registry._cfg = before_cfg

    before = _choose_all(before_cfg)
    after = _choose_all(merged)

    same = provider_changed = model_changed = lost = 0
    cost_before = cost_after = 0.0
    examples = []
    for r in records:
        rid = r["id"]
        pb, planb = before.get(rid, (None, []))
        pa, plana = after.get(rid, (None, []))
        if pb is None and pa is None:
            same += 1
            continue
        if pb is None and pa is not None:
            model_changed += 1
            continue
        if pa is None:
            lost += 1
            examples.append({"was": f"{pb.canonical}/{pb.provider}",
                             "now": None, "task_class": r.get("task_class")})
            continue
        cost_before += (pb and getattr(pb, "_est", 0) or 0)
        if pb.canonical == pa.canonical and pb.provider == pa.provider:
            same += 1
        elif pb.canonical == pa.canonical:
            provider_changed += 1
        else:
            model_changed += 1
        if (pb.canonical != pa.canonical or pb.provider != pa.provider) and len(examples) < 20:
            examples.append({"was": f"{pb.canonical}/{pb.provider}",
                             "now": f"{pa.canonical}/{pa.provider}",
                             "task_class": r.get("task_class")})
    # estimated cost via current route prices (input+output per 1M as proxy)
    def _route_cost(p) -> float:
        if p is None:
            return 0.0
        rec = _registry.get_any(p.provider, p.provider_model_id)
        if rec is None or not (rec.input_price or rec.output_price):
            return 0.0
        return rec.input_price + rec.output_price
    cost_before = sum(_route_cost(before.get(r["id"], (None, []))[0]) for r in records)
    cost_after = sum(_route_cost(after.get(r["id"], (None, []))[0]) for r in records)
    return {
        "replayed": len(records),
        "same": same, "provider_changed": provider_changed,
        "model_changed": model_changed, "no_eligible_route": lost,
        "estimated_cost": {
            "before_index": round(cost_before, 6),
            "after_index": round(cost_after, 6),
            "note": "сумма цен input+output за 1M по выбранным маршрутам — "
                    "прокси-индекс стоимости, не точный счёт",
        },
        "examples": examples,
        "diff": rev.diff_configs(active_config(), {**active_config(), **patch}),
        "note": f"Проиграно {len(records)} последних решений из журнала (dry-run, "
                "без реальных запросов).",
    }


# ═══ R15: Production Guards + Observability endpoints ═════════════════════

@router.get("/obs/readiness")
async def obs_readiness(request: Request):
    """§9: GREEN/YELLOW/RED production readiness."""
    _check_auth(request)
    from . import observability as obs
    rt = await _runtime_get("/health")
    rt_metrics = {}
    try:
        m = await _runtime_get("/metrics")
        if m.get("ok"):
            rt_metrics = m.get("data") or {}
    except Exception:  # noqa: BLE001
        pass
    return obs.readiness(runtime_health=rt, control_ok=True,
                         runtime_metrics=rt_metrics)


@router.get("/obs/alerts")
async def obs_alerts(request: Request):
    """§10: alert center — merged categorized alerts with ack state."""
    _check_auth(request)
    from . import observability as obs
    rt_metrics = {}
    try:
        m = await _runtime_get("/metrics")
        if m.get("ok"):
            rt_metrics = m.get("data") or {}
    except Exception:  # noqa: BLE001
        pass
    return obs.alert_center(runtime_metrics=rt_metrics)


@router.post("/obs/alerts/{alert_key:path}/ack")
async def obs_alert_ack(request: Request, alert_key: str):
    _check_auth(request)
    from . import store as _st
    ok = _st.ack_alert(alert_key, actor="admin")
    _st.audit("admin", "obs.alert_ack", alert_key, {"acked": ok})
    return {"ok": ok}


@router.get("/obs/providers")
async def obs_providers(request: Request):
    """§4: provider degradation with mandatory reason."""
    _check_auth(request)
    from . import observability as obs
    rt_metrics = {}
    try:
        m = await _runtime_get("/metrics")
        if m.get("ok"):
            rt_metrics = m.get("data") or {}
    except Exception:  # noqa: BLE001
        pass
    return {"providers": obs.provider_degradation(rt_metrics)}


@router.get("/obs/models")
async def obs_models(request: Request):
    """§5: model degradation for active pool."""
    _check_auth(request)
    from . import observability as obs
    rt_metrics = {}
    try:
        m = await _runtime_get("/metrics")
        if m.get("ok"):
            rt_metrics = m.get("data") or {}
    except Exception:  # noqa: BLE001
        pass
    return {"models": obs.model_degradation(runtime_metrics=rt_metrics)}


@router.get("/obs/budgets")
async def obs_budgets(request: Request):
    """§3: daily/monthly warning budgets + forecast."""
    _check_auth(request)
    from . import observability as obs
    return obs.budgets()


@router.put("/obs/budgets")
async def obs_budgets_put(request: Request):
    """R15 §3: set warning budgets (hard limit stays OFF by default)."""
    _check_auth(request)
    import json as _json
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    from . import observability as obs
    out = obs.set_budgets(body)
    from . import store as _st
    _st.audit("admin", "obs.budgets_set", "budgets", {
        "daily_budget_usd": out.get("daily_budget_usd"),
        "monthly_budget_usd": out.get("monthly_budget_usd"),
        "hard_limit": out.get("hard_limit_usd"),
    })
    return out


@router.get("/obs/economic-anomalies")
async def obs_economic(request: Request):
    """§1: possible routing inefficiencies from the decision journal."""
    _check_auth(request)
    from . import observability as obs
    return obs.economic_anomalies()


@router.get("/obs/shadow")
async def obs_shadow(request: Request):
    """§6: shadow candidates dry-run over decision records."""
    _check_auth(request)
    from . import observability as obs
    return obs.shadow_candidates()


@router.post("/obs/shadow/{canonical}/toggle")
async def obs_shadow_toggle(request: Request, canonical: str):
    """§6: mark/unmark a model as shadow candidate (NOT a routing change —
    candidates never receive production inference)."""
    _check_auth(request)
    body = await request.json()
    on = bool(body.get("candidate"))
    from . import store as _st
    ok = _st.set_candidate(canonical, on)
    _st.audit("admin", "shadow.candidate_toggle", canonical,
              {"candidate": on, "ok": ok})
    from . import observability as obs
    return {"ok": ok, "canonical": canonical, "candidate": on,
            "stats": obs.shadow_candidates() if ok else None}


@router.get("/obs/canary")
async def obs_canary(request: Request):
    """§7: canary experiment status + threshold evaluation."""
    _check_auth(request)
    from . import observability as obs
    st = obs.canary_status()
    return st


@router.post("/obs/canary/config")
async def obs_canary_config(request: Request):
    """§7: create/stop the optional canary experiment. Default disabled.
    The canary is a SEPARATE mechanism — it never edits routing policy."""
    _check_auth(request)
    body = await request.json()
    from . import store as _st
    import json as _json
    if body.get("stop"):
        raw = _st.get_kv("canary")
        st = _json.loads(raw) if raw else {}
        st = {**st, "enabled": False, "stopped_at": time.time()}
        _st.set_kv("canary", _json.dumps(st))
        _st.audit("admin", "canary.stop", st.get("model") or "", {})
        return {"ok": True, "canary": st}
    # validate the experiment shape
    try:
        exp = {
            "enabled": True,
            "model": str(body["model"]),
            "task_classes": [str(c) for c in (body.get("task_classes") or [])],
            "traffic_pct": max(0.0, min(100.0, float(body.get("traffic_pct") or 5.0))),
            "started_at": time.time(),
            "duration_s": max(60, int(body.get("duration_s") or 3600)),
            "rollback": {
                "error_rate": float(body.get("error_rate", 0.2)),
                "ttft_ms": float(body.get("ttft_ms", 20000)),
                "cost_multiplier": float(body.get("cost_multiplier", 2.0)),
            },
        }
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, f"invalid canary config: {e}")
    _st.set_kv("canary", _json.dumps(exp))
    _st.audit("admin", "canary.start", exp["model"], {
        "traffic_pct": exp["traffic_pct"], "classes": exp["task_classes"]})
    return {"ok": True, "canary": exp}


@router.get("/obs/events")
async def obs_events(request: Request, limit: int = 100):
    """R15 §11: system events history (anomalies/alerts/refresh), separate
    from the configuration revision log (Audit page)."""
    _check_auth(request)
    from . import store as _st
    return {"events": _st.list_system_events(limit=min(int(limit), 500))}
