# Configuration

Config layers (in precedence order):

1. config/defaults.yaml — immutable defaults + policy key contract
   (read-only; changes require a code change)
2. control DB (control.db, config_active) — persistent mutable policy,
   revisioned; applied atomically; writes the runtime snapshot
   state/gateway-v2.json
3. Private EnvironmentFile — SECRETS ONLY (~/.config/model-router/router.env,
   0600). Never stored in the DB, logs, UI or Git
4. state/gateway-v2.json — generated runtime snapshot (never hand-edit)

## Environment variables

Runtime topology:
- MODEL_ROUTER_STATE_DIR — mutable data root (default ~/model-router-data)
- GATEWAY_STATE_DIR — per-instance state dir (legacy alias; wins over the
  data root for prod/canary subdirectories)
- GW_CONTROL_DB — control DB path (shared across instances by design)
- GW_CONFIG — runtime snapshot path
- MODEL_ROUTER_INSTANCE_ID — prod | canary | release-test | ...
- GATEWAY_V2_HOST / GATEWAY_V2_PORT — inference bind (default 127.0.0.1:4101;
  units override to 4100/4210)
- GW_CONTROL_HOST / GW_CONTROL_PORT — control plane bind (default 4111)
- GW_REFRESH_INTERVAL_S — catalog refresh interval (default 120)

Providers:
- HERMES_ROUTER_PROVIDER_A_BASE — provider-a base URL
- HERMES_ROUTER_PROVIDER_B_BASE — provider-b base URL (discount floor is the
  /minN/ path segment)
- <NAME>_API_KEY — provider keys (private env only)

Auth (R8 §21):
- MODEL_ROUTER_AUTH_MODE — disabled (default, localhost-compatible) | bearer
- MODEL_ROUTER_PROVIDER_A_TOKEN — inference bearer token (required in bearer
  mode; missing token fails CLOSED)
- ADMIN_TOKEN — control-plane token for non-loopback callers (separate
  from the inference token)

Version/build:
- MODEL_ROUTER_VERSION (default 1.0.0), MODEL_ROUTER_BUILD_COMMIT

## Policy keys (defaults.yaml)

min_discount, quality_first, health_ttl_success_s / health_ttl_failure_s,
cache_switch_horizon / cache_switch_margin, reliability_min_samples,
price_evidence_ttl_s, cache_affinity_ttl_s, canonical_switch_margin_factor,
free_route_min_success_rate, quality_floors (T1..T4), per-provider
min_discount overrides.

An example (anonymized, provider-a/provider-b) is in config/example.yaml;
real deployment config lives outside the repo.

## Export / import

- GET /admin/config/export — secret-free snapshot (secret refs are env var
  NAMES only)
- POST /admin/config/import — {"payload": <export>, "_dry_run": true|false};
  dry-run validates only; apply goes through the revision pipeline
  (draft -> validate -> apply) and is audited.
