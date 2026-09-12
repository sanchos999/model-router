# Model Router PROJECT STATE

Updated: 2026-09-11 14:40 MSK (R9 DONE — Admin UI/API contract + failover fixes)

## Phases

- R1-R3 (router core, selection, transport): DONE
- R4 (model pool): DONE
- R5 (routing economics): DONE
- R6 (control plane, revisioned config): DONE
- R7 (streaming/lifecycle hardening): DONE (2fa2be7)
- R8 (release hardening, same-host clean deploy, sanitization): DONE
- R8.1 (migration safety rewrite, 0449277 + 4009c79 rc1): DONE
- R8.2 (final service-only production cutover): DONE
- R9 (Admin UI/API contract fix + same-canonical failover fix): DONE (327f227, b962392)

## R8 outcome

Production (since 2026-09-11 09:42 MSK):
- code root:  /home/sanchos/model-router (git: 0e12205, sanitized history,
  3 commits, no secrets)
- state root: /home/sanchos/model-router-data (prod/ canary/ control/
  metrics/ runtime/; control.db schema v1, WAL, shared)
- private:    /home/sanchos/.config/model-router/router.env (0600)
- units:      model-router.service :4100, model-router-control.service :4111
  (enabled, localhost-only)
- rollback:   /home/sanchos/hermes-router FROZEN_LEGACY_ROLLBACK
  (scripts/rollback.sh r7-legacy; backup: model-router-data/backups/
  migration-20260911-094204/)

Verified live on :4100 after migration: health (34 routes, 2 providers),
/version 1.0.0, main-auto (glm-5.3), compression-auto (gpt-5.6-luna),
streaming SSE, control plane with 7 preserved revisions, localhost-only
binds, no writes into the legacy tree, standalone operation with Hermes
stopped (then restored).

R8 gates: clean venv via uv, fresh empty-state bootstrap (twice), release
instance :4210/:4211 (now stopped, units retained), load smoke 40/40,
router overhead p50 1.7ms, secret scan 0 findings, tests 159 passed /
4 skipped (live opt-in), docs complete, provider plugin + client examples.

## Model Router V1: PRODUCTION READY

R8.2 cutover (2026-09-11 12:02 MSK): stop hermes-router.service → start
model-router.service. Readiness 2.0s. No migration rerun, no state copy,
no Hermes source changes, no Memory/MegaBrain changes.

Production runtime (post-R8.2):
- code root:       /home/sanchos/model-router (tag model-router-v1.0.0
  at 4009c79; rc1 kept historically; not pushed)
- data root:       /home/sanchos/model-router-data
- private config:  /home/sanchos/.config/model-router (0600)
- inference:       model-router.service :4100 (active, enabled)
- control:         model-router-control.service :4111 (active; active
  revision rev-f249d62915cd preserved)
- old router:      /home/sanchos/hermes-router = LEGACY_ROLLBACK_ONLY
  (hermes-router.service stopped + disabled, tree preserved)

R8.2 verified gates: /health /version /v1/models 200; main-auto
(cb/glm-5.3) and compression-auto (cb/gpt-5.6-luna) 200; SSE stream with
data: [DONE]; tool-call (get_weather) PASS; Provider A explicit
ih:cb/glm-5.3 200; Provider B sp:grok-4.3 200 (no policy bypass); AUTO
routing headers (tier/plan/safe-context); Hermes compression config
threshold 0.65 / threshold_tokens 614250 / effective trigger 614250 (180k
did not return); control independence (inference :4100 200 with control
stopped); graceful restart readiness 3.0s with state/revision preserved;
gateway tests 168 passed / 4 skipped; verify ok=true (Model Router
recipe); single listener on :4100.

## NEXT PROJECT

MEGABRAIN M0 — separate service/repo (/home/sanchos/megabrain/), network-
API contract only (docs/megabrain-future-contract.md).


## R9 outcome (2026-09-11 14:40 MSK)

Admin UI/API contract (327f227):
- UI called /router/metrics-summary (registered as /admin/router/... -> 404)
  and /models/pool (not on :4111 at all -> 404 Polniy snimok Not Found).
- Control process never got metrics: bind_dashboard_metrics() runs only in
  the inference process; :4111 is a separate uvicorn. Runtime telemetry now
  proxied from :4100 (GW_RUNTIME_URL) - /admin/router/metrics-summary,
  /admin/models/pool, /admin/runtime/health. Control DB stays control-plane only.
- Providers list merges control-DB rows with active-config providers;
  enable/disable for config-based providers goes through an atomic config revision.
- UI shows explicit error boxes (endpoint + HTTP status) instead of empty
  tables; dead /admin-ui/metrics-summary-proxy removed; apply_revision now
  returns the post-apply status (APPLIED, was stale VALIDATED).
- pyyaml was missing from the venv -> defaults() was {} on fresh DB;
  added to requirements.txt.

Failover fix (b962392):
- Streaming plan exhaustion yielded a silent empty data: [DONE] - Hermes
  saw a dead stream (terminal failed) with no structured error. Now
  stream_plan emits a terminal event and gen() relays a structured SSE
  upstream_exhausted error. Non-stream path already 502 correctly.
- Recent-failure ranking penalty in _key_score (45s window, cleared by a
  success): the next request no longer blindly re-selects the route that
  just timed out.
- Privacy-safe failover decision trace: state/<instance>/failover-trace.jsonl
  (request_id, canonical, per-attempt route/failure_class/reason/elapsed_ms,
  winner, terminal reason). No prompts/keys.
- Tests: test_r9_admin_ui_contract.py (17), test_r9_failover.py (11);
  suite 196 passed / 4 skipped.
- Known pre-existing (not touched): provider raw SSE [DONE] + router [DONE]
  both relayed in stream tail (clients stop at the first).

## R12 — Admin UI product for the administrator (2026-09-11)

R11 backend/data model kept as-is; discovery/pricing NOT rebuilt.

Backend (gateway/control/):
- insights.py (new): value rating (transparent arithmetic: discount vs
  floor + market activity, tooltip "почему"), why-router checks, economics
  anomaly detector (§29: cheaper equal-quality model not selected ->
  diagnostic only, never auto-applied), opportunities (§30), alerts (§31:
  discount below policy, liquidity loss, price spike vs 24h history).
- store.py: price_history table + aggregates (now/1h/24h/7d/30d min/avg/max,
  §28), immutable baselines (FACTORY_DEFAULT/KNOWN_GOOD, SHA-256,
  schema_version, §25), record_price_history from discovery refresh.
- discovery.py: price snapshots per refresh (offers vs sellers never
  double-counted; Provider A asks = price offers, seller identity unknown).
- inventory.py: liquidity aggregation (offers_count/sellers_count per
  canonical with kind labels), probe-based latency evidence (TTFT only
  from inference probes), reconciliation now RU reasons + on_market +
  in_pool counts (§19 Provider B explanation).
- admin_api.py: /admin/insights/{why,economics,opportunities,alerts,
  provider,usage}, /admin/price-history/{canonical}, /admin/baselines
  (+diff/restore via revision pipeline — no raw DB writes),
  /admin/dashboard/summary (semantic counters: routes vs market offers
  separated, catalog per provider, honest spend label "с момента запуска"),
  /admin/unmatched v2 (search + family groups), /admin/search/catalog
  (wizard step 1), /admin/policy/inheritance/{canonical} (§24),
  /admin/model-lifecycle/{canonical} (hide/unhide/remove_auto/restore/
  disable), /admin/audit human format (RU render + "Для разработчика"),
  cost-preview v2 (per-provider comparison, actual last cost from runtime
  billing, price precision 2/4/6dp).
- store.py FIX (regression-tested): partial pool-policy update (only
  min_discount_override) on a model without a model_pool row no longer
  silently sets in_pool=0 (drops it from the pool). Implicit default True.
- list_discovered now returns provider/provider_model_id from the row,
  not only from raw_json.

Frontend (gateway/control/static/admin/):
- i18n.js: single RU/EN dictionary (~200 keys), RU default; forbidden
  terms (inPool/onMarket/bestNow/myMin/eligible/…) only in diagnostics
  mode (default OFF).
- page_models.js rewritten: main table (Модель/Используется/Доступность/
  Провайдеры/Предложения/Контекст/Официальная/Цена сейчас/Скидка/Мой
  минимум/Скорость/Статус/Действия), model drawer with 6 tabs, «Почему?»
  checklist, add-model wizard (5 steps), human removal actions with
  confirmations, unmatched v2 (search/groups/filters), cost calculator
  with presets, per-model policy with live preview (§22) and inheritance
  (§24), price history tab.
- page_dashboard.js rewritten: semantic counters, spend with honest
  period label, provider «Не использовался — Почему?» drawer (§19),
  opportunities + attention blocks.
- core.js: drawer v2 — #drawer-bg backdrop (e.target === backdrop),
  Escape/✕/Back close, click inside stays, URL hash state, new drawer
  replaces old.
- styles.css: drawer clamp(440px,42vw,720px), breakpoint column hiding,
  word-break for IDs, tabs/kv-cards/audit styles. No horizontal page
  scroll (verified 1920/1366/1280/1024).

Tests: gateway_tests/test_r12_admin_product.py (12) — liquidity fields,
price history roundtrip, calculator precision + per-provider, policy
inheritance, lifecycle actions, unmatched grouping, search, dashboard
semantics, baseline create/diff/restore, audit format, economics anomaly,
partial-policy regression. Suite: 209 passed / 4 skipped.

E2E: state/e2e/e2e_r12.py, 36/36 PASS (two consecutive runs), real
Chromium (nodriver 0.42 scratch venv /tmp/mr-e2e-venv): all 9 pages 0
forbidden terms + 0 console errors + 0 unexpected 4xx/5xx; drawer
click-outside matrix; responsive no-hscroll at 4 breakpoints; journeys
A–E (find/read/why, min-discount 80->90->reset with live preview,
unmatched map+cleanup, calculator, baseline create/mutate/diff/restore).
Screenshots: state/e2e-shots/r12-*.png.

Ops: control.db backup before migration (control.db.bak-r12-*); two
E2E-artifact rows cleaned (e2e-test-model-r12 mapping + pool row);
deepseek-v4-pro pool membership restored to implicit. Backups:
control.db.bak-r11, control.db.bak-r12-20260911-220620.

## R13 — RELEASE / DOCUMENTATION / RECOVERY (2026-09-12)

R12-B принят как функционально завершённый; новых функций не добавлялось.

- KNOWN-GOOD baseline base-knowngood-5748bc15 (SHA-256 2e9d51a9..., source
  commit ff75cef), restore dry-run diff=0. Config export
  docs/reference/current-known-good-config.json (без секретов, import
  dry-run PASS).
- Discovery shrink-guard: падение каталога провайдера >50% блокирует
  деструктивный persist, создаёт аномалию (никакого silent collapse).
- Price sanity guards: negative/NaN/inf reject, $0 только при доказанном
  FREE, unit-jump guard для Provider B µUSD, discount >99.99% warning,
  official<market фиксируется.
- Auto-refresh scheduler (control plane): catalog 6h / market 1h /
  availability 30min — availability только pool-модели, бесплатные
  пробы, платные inference-пробы никогда не запускаются автоматически.
  /admin/refresh/schedule + баннер в Models (последнее/следующее
  обновление/ошибка).
- Cold-start smoke: stop both -> listeners closed -> runtime 3s -> control
  2s; после старта providers=2 pool=19 unmatched=537 routes=34, discovery
  state/baseline/audit/price-history не потеряны.
- Документация: docs/RUKOVODSTVO-R13.md (35 разделов + словарь),
  docs/QUICK-START-RU.md, DOCX (237 параграфов, 13 скриншотов) и PDF
  (12 страниц, 13 изображений) — Model_Router_v1_0_Rukovodstvo_FINAL.*.
- Verify ok=true; tests 218 passed / 4 skipped; E2E r12b 19/19 + tm5 8/8
  (0 network 4xx/5xx, 0 console errors); live reconciliation PASS.
