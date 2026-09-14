# Changelog

Все значимые изменения проекта фиксируются здесь. Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/).

## [1.0.0] — 2026-09-14

### Исправлено
- **Per-attempt бюджет до первого токена.** Ранее `pre_first_failover_s` был ОБЩИМ окном на всю цепочку failover: один медленный TTFT (например, glm-5.3 за 20.001с) оставлял всем запасным маршрутам `deadline_exceeded` с нулевой задержкой. Теперь бюджет считается на каждую попытку, а цепочка ограничена общим бюджетом запроса (180с/240с). Исправлены `execute_plan` и `stream_plan`, обновлено описание в `routing_explain`, добавлены 2 regression-теста.

### Fixed (English)
- **Per-attempt pre-first-token budget.** Previously `pre_first_failover_s` was ONE shared window for the whole failover chain: a single slow TTFT (e.g. glm-5.3 at 20.001s) left every fallback with `deadline_exceeded` / zero latency. The budget is now per attempt; the chain is bounded by the total request budget (180s/240s). Fixed `execute_plan` and `stream_plan`, updated `routing_explain` description, added 2 regression tests.

## [1.0.0-rc1] — 2026-09-14

### Добавлено (серия R15)
- R15.8: `tier_model_policy` (T1–T4) в config и selector, `canonical_capabilities`, реестр `/admin/models/registry` (единый источник), lifecycle-gate (hidden/not-in-pool/disabled), вывод capabilities, исправление нормализатора matcher (`rsplit('/')`), разделение vocab `ROUTE_CAPABILITIES` и canonical caps.
- R15.5: политика моделей по классам задач — режимы AUTO/MANUAL/HYBRID.
- R15.4: поведение маршрутизации + скидка на модель + аудит качества + диверсификация.
- R15.3: мастер сопоставления моделей + исправление мониторинга + UI автоматизаций.
- R15.1–R15.2: полировка релиза, отзывчивая таблица моделей.
- R15: продакшен-гарды и наблюдаемость.

### Added (English)
- R15.8: `tier_model_policy` (T1–T4) in config and selector, `canonical_capabilities`, `/admin/models/registry` (single source), lifecycle-gate (hidden/not-in-pool/disabled), capabilities derivation, matcher normalizer fix (`rsplit('/')`), separate `ROUTE_CAPABILITIES` vocab from canonical caps.
- R15.5: task-class model policy — AUTO/MANUAL/HYBRID modes.
- R15.4: routing behavior + per-model discount + quality audit + diversification.
- R15.3: model matching wizard + monitoring fix + automations UI.
- R15.1–R15.2: release polish, responsive models table.
- R15: production guards and observability.
