# Upgrade (same host)

Pattern: new code -> fresh deps -> migrations -> tests -> switch/restart ->
readiness -> inference smoke. NO auto-pull from GitHub by production code.

    scripts/upgrade.sh

Steps performed:
1. Print current commit (git rev-parse) — deploy new code by explicitly
   updating the checkout first (git fetch/checkout), never automatically.
2. Dependencies: .venv pip install -r requirements.txt.
3. Migrations: schema versioning runs on service start. A control.db
   stamped with a FUTURE schema version makes the service refuse to start
   (safe, non-destructive) — downgrade the code or migrate explicitly.
4. Tests: pytest gateway_tests must pass or the upgrade aborts BEFORE any
   service is touched.
5. Switch: restarts the units running from this tree.
6. Readiness + smoke: /health + /version on known ports.

## Failure handling

Any step failing before the switch leaves the running deployment
untouched. After a bad switch: scripts/rollback.sh (see rollback.md).

## Backup before destructive migrations

    cp <data>/control.db <data>/control.db.bak-$(date +%Y%m%d-%H%M%S)

(control.db is small; WAL files should be checkpointed or copied together:
control.db, control.db-wal, control.db-shm.)
