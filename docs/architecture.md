# Architecture

```
Client ([OI]-compatible: Hermes, scripts, any SDK)
        |
        v  http://127.0.0.1:4100/v1
+--------------------------------------------------+
| Model Router (FastAPI, gateway/app.py)           |
|                                                  |
|  auth (disabled|bearer)      inference token     |
|  classifier.py      task class -> tier           |
|  policy/canonical   alias -> canonical            |
|  registry.py        provider adapters -> routes   |
|  selector.py        health->reliability->cost,    |
|                     cache affinity, failover plan |
|  transport.py       plan execution, SSE passthru |
|  context_manager.py 65%/78% compression policy    |
|  lifecycle.py       CORE/WATCH/UNAVAILABLE        |
|  metrics.py         per-route/provider metrics    |
|  model_pool.py      canonical pool view           |
+------------+------------------+------------------+
             |                  |
             v                  v
   Provider A adapter     Provider B adapter     [OI]Compat adapter (plugin)
   (providers/          (providers/
    provider_a.py)         provider_b.py)        (providers/openai_compat.py)

Control plane (separate ASGI app, :4111 by default):
  gateway/control/ — store (SQLite, revisioned), revisions (draft ->
  validate -> simulate -> apply -> rollback), overrides, admin_api,
  serve_control + admin UI. Failure of the control plane NEVER breaks
  inference (import-guarded, dependency direction control -> runtime).
```

## Separation of concerns

- Inference path (app.py -> classifier/selector/transport) has no
  dependency on the control plane; control changes are applied through
  runtime appliers and an atomic runtime config snapshot
  (gateway-v2.json).
- Providers implement the ProviderAdapter protocol only. The selector,
  economics, context manager and lifecycle core contain no
  provider-specific logic.
- Mutable state lives outside the code tree (MODEL_ROUTER_STATE_DIR);
  control DB is shared across instances by design (WAL, short
  transactions), per-instance telemetry is isolated via GATEWAY_STATE_DIR.

## Instances

| Instance | Port | State | Purpose |
|---|---|---|---|
| prod | 4100 | data/prod | production inference |
| canary | 4101 | data/canary | pre-production validation |
| control | 4111 | shared control.db | admin API + UI |
| release-test | 4210/4211 | data/runtime/release-test | clean-room install test |

## Schema versioning

control.db carries schema_meta.schema_version. Newer-schema DBs make the
service refuse to start (SchemaVersionError) instead of writing garbage;
migrations are idempotent SQL steps applied on connect.
