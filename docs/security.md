# Security

## Bind defaults

All services bind 127.0.0.1 only. Remote exposure requires explicit
config (unit Environment HOST / GATEWAY_V2_HOST / GW_CONTROL_HOST).

## Auth (R8 §21)

- Inference: auth.mode disabled (default — localhost-only compatibility
  with existing clients like Hermes) or bearer
  (MODEL_ROUTER_PROVIDER_A_TOKEN). Bearer mode with no token configured
  fails CLOSED (401).
- Admin: loopback allowed; non-loopback requires x-admin-token ==
  ADMIN_TOKEN. The inference token and admin token are separate; neither
  works for the other.

## Secrets

- Provider keys live ONLY in the private EnvironmentFile
  (~/.config/model-router/router.env, 0600), referenced by systemd
  EnvironmentFile=. Never in the repo, the control DB, logs, telemetry or
  the admin UI.
- Config export is secret-free; secret refs are env var NAMES.
- Error responses are sanitized; logs and telemetry exclude prompts,
  completions and keys.

## Sanitization gate

scripts/secret_scan.sh must pass before any publication: API keys, bearer
tokens, private hosts/paths, real user identifiers, state/DB/telemetry
artifacts, raw catalogs, billing data. Allow-list (documented here and in
the scanner):
- gateway/providers/{provider_a,provider_b}.py default base URLs — the
  marketplaces' PUBLIC API endpoints (not credentials); env-overridable
- fake tokens in tests/docs (super-secret-token, CHANGE_ME-replace-me)

## File permissions

- Private env: 0600 (enforced by install.sh and tested)
- control.db: standard user permissions, WAL mode
- No world-writable paths in the runtime

## Update safety

No auto-pull from GitHub in production code; upgrades go through
scripts/upgrade.sh (deps -> tests -> restart -> readiness -> smoke) with
scripts/rollback.sh as the escape hatch.
