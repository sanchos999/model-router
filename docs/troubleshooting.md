# Troubleshooting

Symptom -> check -> fix.

- Service won't start, log shows SchemaVersionError
  control.db was written by a NEWER product version. Restore the matching
  code version (scripts/rollback.sh git-tag) or migrate the DB explicitly.
  Do not delete control.db without a backup.

- /health 200 but registry.routes == 0
  Provider keys missing/invalid in the private env, or marketplaces
  unreachable. Check unit EnvironmentFile=, then curl the provider
  /v1/models directly with the key. POST /registry/refresh after fixing.

- HTTP 502 model_selection_unavailable
  No eligible route for the requested canonical (discount floor, context
  too large, all unhealthy). GET /v1/registry + /models/pool to see why;
  check min_discount and model lifecycle state.

- HTTP 401 on inference
  auth.mode=bearer is on. Send Authorization: Bearer
  $MODEL_ROUTER_PROVIDER_A_TOKEN. Bearer mode WITHOUT a configured token
  rejects everything (fail-closed) — set the token or switch the mode off.

- HTTP 401 on admin API from a remote host
  Non-loopback callers need x-admin-token: $ADMIN_TOKEN. The inference
  token does not work here.

- Compression not triggering
  GET /context/sessions — utilization must exceed 65% of safe_context AND
  the anti-thrash window (max(100k tokens, 20% safe_context)) since the
  last compression.

- venv creation fails (ensurepip unavailable)
  install.sh falls back to `uv venv`. Or: apt install python3-14-venv.

- Control plane down, inference still works
  By design (control is optional for inference). Restart
  the control unit; check control.db permissions/WAL files.

- Wrong code loaded (stale legacy tree)
  Check unit WorkingDirectory + PYTHONPATH point at the product tree;
  `GET /version` returns the build commit. Test:
  test_runtime_loads_from_product_root.
