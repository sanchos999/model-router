#!/usr/bin/env bash
# R8.1B — safe migration script.
#
# Defaults are FAIL SAFE:
#   - no arguments           -> exit 2, zero mutations
#   - --check                -> read-only validation, zero mutations
#   - --dry-run              -> print full execution plan, zero mutations
#   - --execute --confirm-production
#                            -> destructive operations allowed
#                               (systemctl stop/start/restart/enable/disable,
#                                mv/cp/remove production state,
#                                install production units,
#                                daemon-reload, cutover).
# Any other combination refuses.
#
# All systemctl calls go through run_systemctl() so tests can intercept them
# via SYSTEMCTL_BIN. Mutation helpers additionally require
# MODE=execute AND CONFIRM_PRODUCTION=true.
#
# Concurrency: a single-instance flock protects against overlapping runs.
#
# Sentinel $DATA_ROOT/.r8-migration-complete is written ONLY at the very end
# after every success step (files migrated, units installed, daemon-reload,
# target service started, readiness 200, smoke 200). No sentinel on failure.
#
# Production paths must NOT look like temp paths while SYSTEMCTL_BIN is real.
#
# Usage:
#   migrate-r8.sh --check [--legacy DIR --product DIR --data DIR --systemd-user DIR]
#   migrate-r8.sh --dry-run [--legacy DIR --product DIR --data DIR --systemd-user DIR]
#   migrate-r8.sh --execute --confirm-production \
#       --legacy DIR --product DIR --data DIR --systemd-user DIR
set -euo pipefail

# ── argument parsing ──────────────────────────────────────────────────────
MODE=""
CONFIRM_PRODUCTION="false"
L=""
N=""
D=""
SU=""

usage() {
  cat <<'EOF'
usage:
  migrate-r8.sh --check [--legacy L --product N --data D --systemd-user SU]
  migrate-r8.sh --dry-run [--legacy L --product N --data D --systemd-user SU]
  migrate-r8.sh --execute --confirm-production \
      --legacy L --product N --data D --systemd-user SU
EOF
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)            MODE="check"; shift ;;
    --dry-run)          MODE="dry-run"; shift ;;
    --execute)          MODE="execute"; shift ;;
    --confirm-production) CONFIRM_PRODUCTION="true"; shift ;;
    --legacy)           L="${2:-}"; shift 2 ;;
    --product)          N="${2:-}"; shift 2 ;;
    --data)             D="${2:-}"; shift 2 ;;
    --systemd-user)     SU="${2:-}"; shift 2 ;;
    -h|--help)          usage ;;
    *)                  echo "ERROR: unknown argument: $1" >&2; usage ;;
  esac
done

# Default = fail safe.
if [ -z "$MODE" ]; then
  echo "ERROR: no mode specified. Use --check | --dry-run | --execute --confirm-production" >&2
  echo "Refusing to run. No mutations performed." >&2
  exit 2
fi

# Production execution requires BOTH flags.
if [ "$MODE" = "execute" ] && [ "$CONFIRM_PRODUCTION" != "true" ]; then
  echo "ERROR: --execute requires --confirm-production" >&2
  echo "Refusing to run. No mutations performed." >&2
  exit 2
fi

# --execute requires positional arguments; --check/--dry-run do not.
if [ "$MODE" = "execute" ]; then
  if [ -z "$L" ] || [ -z "$N" ] || [ -z "$D" ] || [ -z "$SU" ]; then
    echo "ERROR: --execute requires --legacy --product --data --systemd-user" >&2
    exit 2
  fi
fi

# ── environment / abstraction ─────────────────────────────────────────────
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
READINESS_TIMEOUT_S="${READINESS_TIMEOUT_S:-30}"
READINESS_INTERVAL_S="${READINESS_INTERVAL_S:-1}"

SENTINEL_FILE="${D:-/tmp/.placeholder}/.r8-migration-complete"
LOCK_FILE="/tmp/model-router-migrate.lock"

# Decide whether a path looks like a temp/test path.
path_is_temp() {
  case "$1" in
    /tmp/*|/var/tmp/*|./tmp/*) return 0 ;;
    *) return 1 ;;
  esac
}

# Forbidden: temp paths + real systemctl + destructive mode.
if [ "$MODE" = "execute" ]; then
  if path_is_temp "$L" || path_is_temp "$N" || path_is_temp "$D" || path_is_temp "$SU"; then
    if [ "$SYSTEMCTL_BIN" = "systemctl" ]; then
      echo "ERROR: destructive execute with temp path AND real systemctl is FORBIDDEN." >&2
      echo "  legacy=$L product=$N data=$D systemd-user=$SU systemctl=$SYSTEMCTL_BIN" >&2
      exit 2
    fi
  fi
fi

# ── systemctl abstraction ─────────────────────────────────────────────────
# Read-only: is-active, show, list-unit-files always allowed.
# Mutating: stop/start/restart/enable/disable/daemon-reload require execute+confirm.
MUTATION_SUBCOMMANDS="stop start restart enable disable daemon-reload"

run_systemctl() {
  "$SYSTEMCTL_BIN" --user "$@"
}

is_mutation() {
  local cmd="$1"
  for m in $MUTATION_SUBCOMMANDS; do
    if [ "$cmd" = "$m" ]; then return 0; fi
  done
  return 1
}

sc() {
  # wrapper around run_systemctl that enforces mutation policy.
  local subcmd="$1"; shift
  if is_mutation "$subcmd"; then
    if [ "$MODE" != "execute" ] || [ "$CONFIRM_PRODUCTION" != "true" ]; then
      echo "ERROR: systemctl --user $subcmd refused (mode=$MODE confirm=$CONFIRM_PRODUCTION)" >&2
      return 99
    fi
  fi
  run_systemctl "$subcmd" "$@"
}

# ── common helpers ────────────────────────────────────────────────────────
log_step() { echo "[$MODE] $1"; }
die()      { echo "ERROR: $1" >&2; exit 1; }

require_path() {
  local p="$1" what="$2"
  [ -n "$p" ] || die "$what path is empty"
}

resolve_paths() {
  # In check/dry-run, fall back to safe placeholders.
  if [ -z "$L" ]; then L="$N/legacy"; fi   # never used in check/dry-run
  if [ -z "$N" ]; then N="$HOME/model-router"; fi
  if [ -z "$D" ]; then D="$HOME/model-router-data"; fi
  if [ -z "$SU" ]; then SU="$HOME/.config/systemd/user"; fi
  SENTINEL_FILE="$D/.r8-migration-complete"
}

sentinel_exists() {
  [ -f "$SENTINEL_FILE" ]
}

acquire_lock() {
  # flock with timeout. fd 9. Try non-blocking first, then retry briefly.
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "ERROR: another migrate-r8.sh is already running (lock=$LOCK_FILE)" >&2
    exit 1
  fi
}

write_sentinel_atomic() {
  local tmp="$SENTINEL_FILE.tmp.$$"
  cat >"$tmp" <<EOF
migration=r8
completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
source_commit=${SOURCE_COMMIT:-unknown}
target_commit=${TARGET_COMMIT:-unknown}
schema_version=${SCHEMA_VERSION:-1}
EOF
  mv -f "$tmp" "$SENTINEL_FILE"
}

# Bounded readiness waiter. PASS only when /health returns 200 within timeout.
wait_for_ready() {
  local url="$1" timeout="$READINESS_TIMEOUT_S" interval="$READINESS_INTERVAL_S"
  local elapsed=0 code
  while [ "$elapsed" -lt "$timeout" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
      echo "    ready after ${elapsed}s ($url)"
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  echo "    readiness FAILED after ${timeout}s ($url, last code=$code)" >&2
  return 1
}

# ── preflight (shared by all modes) ───────────────────────────────────────
preflight() {
  log_step "preflight checks"
  resolve_paths
  log_step "  legacy=$L product=$N data=$D systemd-user=$SU"
  log_step "  systemctl=$SYSTEMCTL_BIN mode=$MODE confirm=$CONFIRM_PRODUCTION"

  if [ "$MODE" = "execute" ]; then
    require_path "$L" "legacy";  [ -d "$L" ] || die "legacy dir not found: $L"
    require_path "$N" "product"; [ -d "$N" ] || die "product dir not found: $N"
    [ -d "$D" ] || die "data dir not found: $D"
    [ -d "$SU" ] || die "systemd-user dir not found: $SU"
  fi

  if sentinel_exists; then
    log_step "  sentinel present at $SENTINEL_FILE"
    cat "$SENTINEL_FILE"
    case "$MODE" in
      check)
        log_step "  ALREADY_MIGRATED. Refusing further checks." ;;
      dry-run)
        log_step "  execute would be REFUSED." ;;
      execute)
        die "migration already applied. Recovery requires explicit procedure." ;;
    esac
    return 10
  fi
  return 0
}

# ── backup (execute only) ─────────────────────────────────────────────────
do_backup() {
  log_step "[1/8] backup"
  local TS BK
  TS=$(date +%Y%m%d-%H%M%S)
  BK="$D/backups/migration-$TS"
  mkdir -p "$BK" || die "cannot create backup dir $BK"
  sc show model-router.service hermes-router-control.service hermes-gateway-v2.service \
    -p Id,ActiveState,MainPID,ExecMainStartTimestamp >"$BK/services-before.txt" 2>/dev/null || true
  cp -a "$L/state/control.db" "$BK/control.db" || die "backup control.db failed"
  [ -f "$L/state/control.db-wal" ] && cp -a "$L/state/control.db-wal" "$BK/" || true
  cp -a "$L/state/gateway-v2.json" "$BK/gateway-v2.json" || true
  [ -d "$L/state/prod" ] && cp -a "$L/state/prod" "$BK/prod-state" || true
  [ -f "$L/.env.gateway" ] && cp -a "$L/.env.gateway" "$BK/env.gateway" || true
  cp "$SU/model-router.service" "$BK/model-router.service.old" 2>/dev/null || true
  cp "$SU/hermes-router-control.service" "$BK/control.service.old" 2>/dev/null || true
  log_step "  backup=$BK"
  echo "$BK" >"$D/.r8-last-backup"
}

# ── destructive steps (execute only) ──────────────────────────────────────
do_stop() {
  log_step "[2/8] stop services"
  sc stop model-router.service hermes-router-control.service hermes-gateway-v2.service 2>/dev/null || true
}

do_migrate_state() {
  log_step "[3/8] migrate state"
  mkdir -p "$D"/{prod,canary,control,metrics,runtime}
  "$SYSTEMCTL_BIN" --user show >/dev/null 2>&1 || true   # touch — read-only, but uses real systemctl
  cp -a "$L/state/control.db" "$D/control/control.db" || die "cp control.db failed"
  cp -a "$L/state/gateway-v2.json" "$D/control/gateway-v2.json" || true
  for f in canonical-registry.json context-compression.jsonl model-aliases.json \
           model-lifecycle.json model-registry.json; do
    [ -f "$L/state/prod/$f" ] && cp -a "$L/state/prod/$f" "$D/prod/$f" || true
  done
}

do_install_units() {
  log_step "[4/8] install units"
  test -f "$SU/model-router.service.new" || die "model-router.service.new not staged"
  mv "$SU/model-router.service.new" "$SU/model-router.service" || die "mv unit failed"
  if [ -f "$SU/hermes-router-control.service.new" ]; then
    mv "$SU/hermes-router-control.service.new" "$SU/hermes-router-control.service" || true
  fi
  sc disable model-router.service hermes-router-control.service 2>/dev/null || true
}

do_start() {
  log_step "[5/8] daemon-reload + start"
  sc daemon-reload || die "daemon-reload failed"
  sc enable --now model-router.service model-router-control.service || die "enable+start failed"
}

do_verify() {
  log_step "[6/8] verify (bounded readiness, no fixed sleep)"
  bash "$N/scripts/verify.sh" 4100 || die "verify.sh failed"
}

do_smoke() {
  log_step "[7/8] inference smoke"
  curl -sf http://127.0.0.1:4100/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"main-auto","messages":[{"role":"user","content":"Reply with the single word ok"}],"max_tokens":20}' \
    | python3 -c 'import json,sys; json.load(sys.stdin)["choices"][0]["message"]["content"]' \
    || die "main-auto smoke failed"
  curl -sf http://127.0.0.1:4100/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"compression-auto","messages":[{"role":"user","content":"Summarize: test"}],"max_tokens":30}' \
    | python3 -c 'import json,sys; json.load(sys.stdin)["choices"][0]["message"]["content"]' \
    || die "compression-auto smoke failed"
}

# ── mode dispatch ─────────────────────────────────────────────────────────
acquire_lock
preflight
rc=$?
if [ "$rc" -eq 10 ]; then
  # sentinel already exists; preflight printed mode-specific refusal.
  exit 1
fi

case "$MODE" in
  check)
    log_step "check: no mutations performed."
    exit 0
    ;;
  dry-run)
    log_step "dry-run plan:"
    cat <<EOF
  [1/8] backup                    -> $D/backups/migration-<TS>
  [2/8] systemctl stop            -> model-router, hermes-router-control, hermes-gateway-v2
  [3/8] migrate state             -> legacy -> $D
  [4/8] mv staged *.new -> final  -> $SU
           systemctl disable      -> model-router, hermes-router-control
  [5/8] systemctl daemon-reload
         systemctl enable --now    -> model-router, model-router-control
  [6/8] verify.sh 4100             -> health/version/bind
  [7/8] smoke                      -> main-auto, compression-auto
  [8/8] sentinel                   -> $SENTINEL_FILE (atomic write)
EOF
    log_step "dry-run: zero mutations performed."
    exit 0
    ;;
  execute)
    SOURCE_COMMIT=$(git -C "$L" rev-parse HEAD 2>/dev/null || echo unknown)
    TARGET_COMMIT=$(git -C "$N" rev-parse HEAD 2>/dev/null || echo unknown)
    SCHEMA_VERSION="1"
    do_backup
    do_stop
    do_migrate_state
    do_install_units
    do_start
    do_verify
    do_smoke
    write_sentinel_atomic
    log_step "[8/8] MIGRATION DONE. sentinel=$SENTINEL_FILE"
    exit 0
    ;;
esac