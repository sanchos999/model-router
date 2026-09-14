# Model Router

Standalone LLM gateway and router. Sits between [OI]-compatible clients and
N model-marketplace providers; classifies requests, selects canonical
models and routes by health, reliability and cost-per-success, manages
context compression, and exposes a control plane with an admin UI.

Model Router is an independent product. It does NOT require (or import)
Hermes or any specific agent framework — any client speaking the standard
[OI] chat-completions protocol works. Hermes is one example client (see
examples/clients/hermes_client.md).

## Features

- [OI]-compatible inference: /v1/chat/completions (non-stream + SSE
  streaming + tool calls), /v1/models, /health, /version
- Task classification (SIMPLE / NORMAL_CODING / DEBUG / RESEARCH /
  ARCHITECTURE / CRITICAL ...) -> tiered canonical selection
- Dynamic provider discovery: catalog refresh, discount floor, capability
  and context filtering, certification status
- Economics: health -> reliability -> cost_per_success ranking; cache-aware
  route switching (WARM / LIKELY_WARM / COLD / UNKNOWN); expected-retry and
  cache-loss costs; FREE/UNKNOWN cost sentinels (never free by default)
- Context manager: safe-context calculation, 65% soft / 78% hard
  thresholds, anti-thrash, compression routing with fallback chain,
  privacy-safe telemetry (no raw prompts)
- Model lifecycle: CORE / WATCH / UNAVAILABLE, quality floors per tier
- Control plane: revisioned config (draft -> validate -> simulate -> apply
  -> rollback), provider records, per-model policy, temporary overrides,
  audit log; secret-free config export/import
- Multi-instance on one host: prod / canary / release-test with isolated
  state, shared revisioned control DB
- Provider plugins: add a marketplace without touching selector/economics/
  context/lifecycle code (see docs/provider-plugin.md)

## Quick start (same host)

    scripts/install.sh                     # venv + deps + data dirs
    cp .env.example ~/.config/model-router/router.env  # fill provider keys; chmod 600
    systemctl --user start model-router.service

Verify:

    scripts/verify.sh 4100

## Layout

    <checkout>/            code, config examples, tests, docs (this repo)
    ~/model-router-data/   mutable state: prod/ canary/ control/ metrics/ runtime/
    ~/.config/model-router/ private env (secrets), 0600

## Docs

docs/architecture.md, docs/install-same-host.md, docs/configuration.md,
docs/providers.md, docs/provider-plugin.md, docs/routing-policy.md,
docs/control-plane.md, docs/admin-ui.md, docs/security.md, docs/upgrade.md,
docs/rollback.md, docs/troubleshooting.md

## Tests

    PYTHONPATH=. .venv/bin/python -m pytest gateway_tests -q
    RUN_LIVE_TESTS=1 ... # opt-in live provider probes

Version: 1.0.0 (see GET /version).

*(Russian: см. [README.ru.md](README.ru.md))*
