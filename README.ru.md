# Model Router

Автономный LLM-шлюз и роутер. Располагается между [OI]-совместимыми клиентами и N провайдерами моделей; классифицирует запросы, выбирает канонические модели и маршрутизирует по здоровью, надёжности и стоимости за успешный ответ, управляет сжатием контекста и предоставляет control plane с админ-UI.

Model Router — независимый продукт. Он НЕ требует (и не импортирует) Hermes или какой-либо конкретный агентский фреймворк — подходит любой клиент, говорящий по стандартному протоколу [OI] chat-completions. Hermes — один из примеров клиента (см. examples/clients/hermes_client.md).

## Возможности

- [OI]-совместимый inference: /v1/chat/completions (non-stream + SSE-стриминг + tool calls), /v1/models, /health, /version
- Классификация задач (SIMPLE / NORMAL_CODING / DEBUG / RESEARCH / ARCHITECTURE / CRITICAL ...) -> выбор канонической модели по тиру
- Динамическое обнаружение провайдеров: обновление каталога, порог скидки, фильтрация по возможностям и контексту, статус сертификации
- Экономика: ранжирование health -> reliability -> cost_per_success; переключение маршрутов с учётом кэша (WARM / LIKELY_WARM / COLD / UNKNOWN); стоимость ожидаемых ретраев и потерь кэша; сентинелы FREE/UNKNOWN (никогда не «бесплатно» по умолчанию)
- Context manager: расчёт безопасного контекста, мягкий порог 65% / жёсткий 78%, anti-thrash, маршрутизация сжатия с цепочкой fallback, приватная телеметрия (без сырых промптов)
- Жизненный цикл моделей: CORE / WATCH / UNAVAILABLE, пороги качества по тиру
- Control plane: ревизионный конфиг (draft -> validate -> simulate -> apply -> rollback), записи провайдеров, политика на модель, временные переопределения, аудит-лог; экспорт/импорт конфига без секретов
- Несколько инстансов на одном хосте: prod / canary / release-test с изолированным состоянием и общей ревизионной БД control plane
- Плагины провайдеров: добавление маркетплейса без правки кода selector/economics/context/lifecycle (см. docs/provider-plugin.md)

## Быстрый старт (тот же хост)

    scripts/install.sh                     # venv + зависимости + каталоги данных
    cp .env.example ~/.config/model-router/router.env  # ключи провайдеров; chmod 600
    systemctl --user start model-router.service

Проверка:

    scripts/verify.sh 4100

## Структура

    <checkout>/            код, примеры конфигов, тесты, документация (этот репозиторий)
    ~/model-router-data/   изменяемое состояние: prod/ canary/ control/ metrics/ runtime/
    ~/.config/model-router/ приватный env (секреты), 0600

## Документация

docs/architecture.md, docs/install-same-host.md, docs/configuration.md, docs/providers.md, docs/provider-plugin.md, docs/routing-policy.md, docs/control-plane.md, docs/admin-ui.md, docs/security.md, docs/upgrade.md, docs/rollback.md, docs/troubleshooting.md

## Тесты

    PYTHONPATH=. .venv/bin/python -m pytest gateway_tests -q
    RUN_LIVE_TESTS=1 ... # опциональные живые пробы провайдеров

Версия: 1.0.0 (см. GET /version).

*(English: см. [README.md](README.md))*
