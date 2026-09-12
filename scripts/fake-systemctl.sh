#!/usr/bin/env bash
# fake-systemctl.sh — minimal systemctl stand-in for migrate-r8.sh tests.
#
# Behaviour:
#   - All calls append "<args>" to a log file (default $FAKE_SYSTEMCTL_LOG).
#   - Reads state from a key/value file (default $FAKE_SYSTEMCTL_STATE):
#       model-router.service=active|inactive|failed
#       hermes-router-control.service=active|inactive|failed
#       hermes-gateway-v2.service=active|inactive|failed
#   - Subcommands:
#       is-active <unit>...         -> 0 if all active, else 3
#       show <unit>... -p ...       -> prints "Id=... ActiveState=..."
#       list-unit-files             -> prints "model-router.service" etc.
#       stop <unit>...              -> sets state to inactive, log, exit 0
#       start <unit>...             -> sets state to active, log, exit 0
#       restart <unit>...           -> log, exit 0
#       enable [--now] <unit>...    -> log, exit 0
#       disable <unit>...           -> log, exit 0
#       daemon-reload               -> log, exit 0
#
# Failure injection:
#   FAKE_SYSTEMCTL_FAIL_ON=<comma-separated-subcmds>
#       Each subcmd in the list exits with FAKE_SYSTEMCTL_FAIL_CODE (default 1).
#
#   FAKE_SYSTEMCTL_FAIL_CODE=<int>   default 1
#
# Defaults: state file = $FAKE_SYSTEMCTL_STATE (default /tmp/fake-sctl.state)
#           log    file = $FAKE_SYSTEMCTL_LOG    (default /tmp/fake-sctl.log)

set -e
LOG="${FAKE_SYSTEMCTL_LOG:-/tmp/fake-sctl.log}"
STATE="${FAKE_SYSTEMCTL_STATE:-/tmp/fake-sctl.state}"

# Ensure state file exists with sane defaults.
if [ ! -f "$STATE" ]; then
  cat >"$STATE" <<'EOF'
model-router.service=active
hermes-router-control.service=inactive
hermes-gateway-v2.service=inactive
EOF
fi

# Init log
echo "$(date -u +%H:%M:%S) FAKE $* " >>"$LOG"

# Skip --user, accept whatever
while [ "${1:-}" = "--user" ]; do shift; done
SUBCMD="${1:-}"; shift || true

# Failure injection
if [ -n "${FAKE_SYSTEMCTL_FAIL_ON:-}" ]; then
  IFS=',' read -r -a fail_list <<<"$FAKE_SYSTEMCTL_FAIL_ON"
  for f in "${fail_list[@]}"; do
    if [ "$f" = "$SUBCMD" ]; then
      echo "$(date -u +%H:%M:%S) FAKE FAIL $SUBCMD" >>"$LOG"
      exit "${FAKE_SYSTEMCTL_FAIL_CODE:-1}"
    fi
  done
fi

set_state() {
  local unit="$1" val="$2"
  # rewrite state file replacing the line for that unit
  python3 - "$STATE" "$unit" "$val" <<'PY'
import sys, re
path, unit, val = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines()
found = False
out = []
for ln in lines:
    if ln.startswith(unit + "="):
        out.append(f"{unit}={val}")
        found = True
    else:
        out.append(ln)
if not found:
    out.append(f"{unit}={val}")
open(path, "w").write("\n".join(out) + "\n")
PY
}

case "$SUBCMD" in
  is-active)
    rc=0
    for u in "$@"; do
      v=$(grep -E "^${u}=" "$STATE" | cut -d= -f2 || true)
      if [ "$v" != "active" ]; then rc=3; echo "inactive"; else echo "active"; fi
    done
    exit "$rc"
    ;;
  show)
    for u in "$@"; do
      case "$u" in
        -p) continue ;;
        *=*) echo "$u" ;;
        *)
          v=$(grep -E "^${u}=" "$STATE" | cut -d= -f2 || echo "inactive")
          echo "Id=$u ActiveState=$v MainPID=0"
          ;;
      esac
    done
    ;;
  list-unit-files)
    grep -E "\.service$" "$STATE" | cut -d= -f1
    ;;
  stop)
    for u in "$@"; do set_state "$u" inactive; done
    ;;
  start)
    for u in "$@"; do set_state "$u" active; done
    ;;
  restart)
    for u in "$@"; do set_state "$u" active; done
    ;;
  enable)
    for u in "$@"; do
      [ "$u" = "--now" ] && continue
      set_state "$u" active
    done
    ;;
  disable)
    for u in "$@"; do set_state "$u" inactive; done
    ;;
  daemon-reload)
    :
    ;;
  *)
    echo "fake-systemctl: unknown subcommand: $SUBCMD" >&2
    exit 2
    ;;
esac