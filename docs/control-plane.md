# Control plane

Separate ASGI app (gateway/control/serve_control.py), default
127.0.0.1:4111. The control plane is OPTIONAL for inference: its import
or crash never breaks the inference path (dependency direction
control -> runtime appliers, guarded in app.py).

State: SQLite control.db (WAL, busy_timeout; shared across prod/canary
instances by design), schema-versioned (schema_meta). A DB stamped with a
future schema version makes the service refuse to start safely.

## API (prefix /admin, auth: loopback allowed; ADMIN_TOKEN required for
non-loopback callers — separate from the inference token)

Config revisions:
- GET  /admin/config/active
- GET  /admin/config/revisions, POST /admin/config/revisions (draft)
- POST /admin/config/revisions/{rid}/validate | simulate | apply | rollback
- POST /admin/config/impact-preview
- GET  /admin/config/export — secret-free snapshot (R8 §11)
- POST /admin/config/import — {"payload":..., "_dry_run": true|false}

Providers:
- GET/POST /admin/providers, PATCH /admin/providers/{name},
  POST /admin/providers/{name}/enable|disable, DELETE (archive)

Model policy: GET /admin/models/policy, POST /admin/models/{canonical}/policy
Overrides: GET/POST /admin/overrides, DELETE /admin/overrides/{oid}
Simulation: POST /admin/simulate
Audit: GET /admin/audit
Health: GET /admin/healthz

## Revision lifecycle

draft -> validate (schema + key contract) -> simulate (impact preview:
eligible routes before/after, affected core models, blockers) -> apply
(atomic: control.db commit + runtime hot-apply + gateway-v2.json snapshot
write) -> rollback (creates a new revision restoring the target config).

All changes are audited (actor, action, entity, fields, revision).

## Config export/import (R8 §11)

Export never contains secrets — provider records keep secret_ref NAMES
only; scrubbing covers key-like fields. Import validates first; apply goes
through the normal revision pipeline. Redacted payloads are rejected.
