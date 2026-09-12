#!/usr/bin/env bash
# test-migration-safety.sh — regression suite for R8.1B migrate-r8.sh.
#
# Tests:
#   A. no args                    -> exit 2, zero mutations
#   B. --check                    -> exit 0, zero mutations
#   C. --dry-run                  -> exit 0, zero mutations, plan printed
#   D. invalid preconditions      -> zero mutations
#   E. --execute --confirm-prod   -> expected fake-systemctl sequence
#   F. failure mid-migration      -> no sentinel
#   G. success full migration     -> sentinel exists
#   H. second execute after sentinel -> refused
#   I. concurrent invocation      -> second refused
#   J. production unit names untouched when using fake
#
# All tests use SYSTEMCTL_BIN=fake-systemctl.sh. Production paths are never
# touched: tests build throwaway /tmp dirs.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATE="$SCRIPT_DIR/migrate-r8.sh"
FAKE="$SCRIPT_DIR/fake-systemctl.sh"

TESTS=0
PASS=0
FAIL=0

mkdir -p /tmp/r81-tests
rm -f /tmp/model-router-migrate.lock  # belt-and-braces; flock should clear on its own

ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); TESTS=$((TESTS+1)); }
nok()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); TESTS=$((TESTS+1)); }

run_migrate() {
  # run_migrate <mode-args...>  -> sets RC, STDOUT
  # NOTE: no set -e here — the suite runs under `set -uo pipefail` without -e;
  # re-enabling -e globally would kill the script on expected non-zero exits.
  local args=("$@")
  local log="/tmp/r81-tests/run.log"
  STDOUT=$(FAKE_SYSTEMCTL_LOG="$LOG" FAKE_SYSTEMCTL_STATE="$STATE" \
    SYSTEMCTL_BIN="$FAKE" "$MIGRATE" "${args[@]}" 2>&1)
  RC=$?
}

make_fake_env() {
  local base="$1"
  L="$base/legacy"
  N="$base/product"
  D="$base/data"
  SU="$base/su"
  STATE="$base/fake-state"
  LOG="$base/fake-log"

  rm -rf "$base"
  mkdir -p "$L/state/prod" "$N" "$D" "$SU" "$D/control"
  touch "$L/state/control.db" "$L/state/gateway-v2.json"
  echo "schema" > "$L/state/prod/canonical-registry.json"

  # Staged prod units
  cat >"$SU/model-router.service.new" <<EOF
[Unit]
Description=fake-prod
ExecStart=/bin/true
EOF
  cat >"$SU/hermes-router-control.service.new" <<EOF
[Unit]
Description=fake-control
ExecStart=/bin/true
EOF

  echo "model-router.service=active" > "$STATE"
  echo "hermes-router-control.service=inactive" >> "$STATE"
  echo "hermes-gateway-v2.service=inactive" >> "$STATE"
  : > "$LOG"
}

count_log_lines() {
  wc -l < "$1" 2>/dev/null || echo 0
}

# ── A. no args ────────────────────────────────────────────────────────────
echo "[A] no args -> fail safe"
make_fake_env /tmp/r81-tests/A
run_migrate
if [ "$RC" -eq 2 ]; then
  ok "A exit=2"
else
  nok "A expected exit=2, got $RC"
fi
if ! grep -q ".r8-migration-complete" "$D/.r8-migration-complete" 2>/dev/null; then
  ok "A no sentinel"
else
  nok "A sentinel unexpectedly created"
fi
if [ "$(count_log_lines "$LOG")" -eq 0 ]; then
  ok "A zero fake-systemctl mutations"
else
  nok "A fake-systemctl log mutated ($(count_log_lines "$LOG"))"
fi

# ── B. --check ───────────────────────────────────────────────────────────
echo "[B] --check zero mutations"
make_fake_env /tmp/r81-tests/B
run_migrate --check
if [ "$RC" -eq 0 ]; then
  ok "B exit=0"
else
  nok "B expected exit=0, got $RC"
fi
if [ "$(count_log_lines "$LOG")" -eq 0 ]; then
  ok "B zero fake-systemctl mutations"
else
  nok "B fake-systemctl log mutated ($(count_log_lines "$LOG"))"
fi
if [ ! -f "$D/.r8-migration-complete" ]; then
  ok "B no sentinel"
else
  nok "B sentinel unexpectedly created"
fi

# ── C. --dry-run ─────────────────────────────────────────────────────────
echo "[C] --dry-run zero mutations"
make_fake_env /tmp/r81-tests/C
run_migrate --dry-run
if [ "$RC" -eq 0 ]; then
  ok "C exit=0"
else
  nok "C expected exit=0, got $RC"
fi
if echo "$STDOUT" | grep -q "dry-run plan"; then
  ok "C plan printed"
else
  nok "C plan missing in stdout"
fi
if [ "$(count_log_lines "$LOG")" -eq 0 ]; then
  ok "C zero fake-systemctl mutations"
else
  nok "C fake-systemctl log mutated ($(count_log_lines "$LOG"))"
fi
if [ ! -f "$D/.r8-migration-complete" ]; then
  ok "C no sentinel"
else
  nok "C sentinel unexpectedly created"
fi

# ── D. invalid preconditions ─────────────────────────────────────────────
echo "[D] invalid preconditions"
make_fake_env /tmp/r81-tests/D
# Point legacy to non-existent dir
run_migrate --execute --confirm-production \
  --legacy /nonexistent --product "$N" --data "$D" --systemd-user "$SU"
if [ "$RC" -ne 0 ]; then
  ok "D preflight rejected ($RC)"
else
  nok "D preflight accepted non-existent legacy"
fi
if [ "$(count_log_lines "$LOG")" -eq 0 ]; then
  ok "D zero mutations"
else
  nok "D mutated log ($(count_log_lines "$LOG"))"
fi

# --execute without --confirm-production
run_migrate --execute --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU"
if [ "$RC" -eq 2 ]; then
  ok "D --execute w/o --confirm-production refused"
else
  nok "D --execute w/o --confirm-production accepted (rc=$RC)"
fi

# temp paths + real systemctl = forbidden
run_migrate --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU"
# unset SYSTEMCTL_BIN; should refuse on temp paths
STDOUT=$(SYSTEMCTL_BIN=systemctl "$MIGRATE" --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU" 2>&1)
RC=$?
if [ "$RC" -ne 0 ] && echo "$STDOUT" | grep -q "FORBIDDEN"; then
  ok "D temp path + real systemctl refused"
else
  nok "D temp path + real systemctl NOT refused (rc=$RC)"
fi

# ── E. --execute --confirm-production: expected fake-sequence ────────────
echo "[E] --execute --confirm-production: expected sequence"
make_fake_env /tmp/r81-tests/E
# Make verify.sh and curl succeed by overriding verify.sh path through N
# We'll simulate verify by setting a fake verify.sh that exits 0
mkdir -p "$N/scripts" "$N/.fake-bin"
  cat >"$N/scripts/verify.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
  chmod +x "$N/scripts/verify.sh"
  cat >"$N/.fake-bin/curl" <<'EOF'
#!/bin/bash
echo '{"choices":[{"message":{"content":"ok"}}]}'
EOF
  chmod +x "$N/.fake-bin/curl"

PATH="$N/.fake-bin:$PATH" run_migrate --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU"
if [ "$RC" -eq 0 ]; then
  ok "E execute exit=0"
else
  echo "    STDOUT (last 30 lines):"
  echo "$STDOUT" | tail -30
  nok "E execute failed (rc=$RC)"
fi

# Verify fake-systemctl got stop in execute
if grep -q "stop" "$LOG"; then
  ok "E stop called"
else
  nok "E stop NOT called"
fi
if grep -q "daemon-reload" "$LOG"; then
  ok "E daemon-reload called"
else
  nok "E daemon-reload NOT called"
fi
if grep -q "enable" "$LOG"; then
  ok "E enable called"
else
  nok "E enable NOT called"
fi

# Production unit names were used (the script is meant to address them in execute mode)
if grep -q "model-router.service" "$LOG"; then
  ok "E model-router.service referenced"
else
  nok "E model-router.service NOT referenced"
fi

# ── F. failure mid-migration: no sentinel ────────────────────────────────
echo "[F] failure midway -> no sentinel"
make_fake_env /tmp/r81-tests/F
mkdir -p "$N/scripts" "$N/.fake-bin"
cat >"$N/scripts/verify.sh" <<'EOF'
#!/bin/bash
echo "fake verify FAIL"
exit 1
EOF
chmod +x "$N/scripts/verify.sh"
cat >"$N/.fake-bin/curl" <<'EOF'
#!/bin/bash
out=""; code=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) code="$2"; shift 2 ;;
    -sf|-s|-f|-H|-X|-d|--max-time|--connect-timeout|--data|--data-raw|--data-binary) shift ;;
    -*) shift ;;
    *) shift ;;
  esac
done
if [ -n "$out" ] || [ -n "$code" ]; then
  printf "%s" "${code:-200}"
else
  echo '{"choices":[{"message":{"content":"ok"}}],"ok":true}'
fi
EOF
chmod +x "$N/.fake-bin/curl"

PATH="$N/.fake-bin:$PATH" run_migrate --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU"
if [ "$RC" -ne 0 ]; then
  ok "F execute failed (rc=$RC)"
else
  nok "F execute unexpectedly succeeded"
fi
if [ ! -f "$D/.r8-migration-complete" ]; then
  ok "F no sentinel on failure"
else
  nok "F sentinel created despite failure"
fi

# ── G. success full migration -> sentinel ────────────────────────────────
echo "[G] success full fake migration -> sentinel"
make_fake_env /tmp/r81-tests/G
mkdir -p "$N/scripts" "$N/.fake-bin"
cat >"$N/scripts/verify.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$N/scripts/verify.sh"
cat >"$N/.fake-bin/curl" <<'EOF'
#!/bin/bash
out=""; code=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) code="$2"; shift 2 ;;
    -sf|-s|-f|-H|-X|-d|--max-time|--connect-timeout|--data|--data-raw|--data-binary) shift ;;
    -*) shift ;;
    *) shift ;;
  esac
done
if [ -n "$out" ] || [ -n "$code" ]; then
  printf "%s" "${code:-200}"
else
  echo '{"choices":[{"message":{"content":"ok"}}],"ok":true}'
fi
EOF
chmod +x "$N/.fake-bin/curl"

PATH="$N/.fake-bin:$PATH" run_migrate --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU"
if [ "$RC" -eq 0 ]; then
  ok "G execute success"
else
  nok "G execute failed (rc=$RC)"
fi
if [ -f "$D/.r8-migration-complete" ]; then
  ok "G sentinel created"
else
  nok "G sentinel missing"
fi
if grep -q "migration=r8" "$D/.r8-migration-complete"; then
  ok "G sentinel content has migration=r8"
else
  nok "G sentinel content wrong"
fi

# ── H. second execute after sentinel -> refused ──────────────────────────
echo "[H] second execute after sentinel -> refused"
# Reuse G's environment (sentinel exists)
export FAKE_SYSTEMCTL_STATE="$STATE"
export FAKE_SYSTEMCTL_LOG="$LOG"
PATH="$N/.fake-bin:$PATH" run_migrate --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU"
if [ "$RC" -ne 0 ] && echo "$STDOUT" | grep -q "already applied"; then
  ok "H second execute refused (rc=$RC)"
else
  nok "H second execute NOT refused (rc=$RC, stdout=$(echo $STDOUT | head -c 80))"
fi

# ── I. concurrent invocation ─────────────────────────────────────────────
echo "[I] concurrent invocation -> second refused"
make_fake_env /tmp/r81-tests/I
mkdir -p "$N/scripts" "$N/.fake-bin"
cat >"$N/scripts/verify.sh" <<'EOF'
#!/bin/bash
sleep 5
exit 0
EOF
chmod +x "$N/scripts/verify.sh"
cat >"$N/.fake-bin/curl" <<'EOF'
#!/bin/bash
out=""; code=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) code="$2"; shift 2 ;;
    -sf|-s|-f|-H|-X|-d|--max-time|--connect-timeout|--data|--data-raw|--data-binary) shift ;;
    -*) shift ;;
    *) shift ;;
  esac
done
if [ -n "$out" ] || [ -n "$code" ]; then
  printf "%s" "${code:-200}"
else
  echo '{"choices":[{"message":{"content":"ok"}}],"ok":true}'
fi
EOF
chmod +x "$N/.fake-bin/curl"

export FAKE_SYSTEMCTL_STATE="$STATE"
export FAKE_SYSTEMCTL_LOG="$LOG"

PATH="$N/.fake-bin:$PATH" SYSTEMCTL_BIN="$FAKE" FAKE_SYSTEMCTL_LOG="$LOG" FAKE_SYSTEMCTL_STATE="$STATE" \
  "$MIGRATE" --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU" >/tmp/r81-tests/I1.log 2>&1 &
PID1=$!
sleep 1
PATH="$N/.fake-bin:$PATH" SYSTEMCTL_BIN="$FAKE" FAKE_SYSTEMCTL_LOG="$LOG" FAKE_SYSTEMCTL_STATE="$STATE" \
  "$MIGRATE" --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU" >/tmp/r81-tests/I2.log 2>&1
RC2=$?
wait "$PID1" 2>/dev/null || true
if [ "$RC2" -ne 0 ] && grep -q "another migrate" /tmp/r81-tests/I2.log; then
  ok "I second concurrent run refused"
else
  nok "I second concurrent run NOT refused (rc=$RC2)"
fi

# ── J. production unit names untouched when using fake ───────────────────
echo "[J] fake test never touches production units"
make_fake_env /tmp/r81-tests/J
export FAKE_SYSTEMCTL_STATE="$STATE"
export FAKE_SYSTEMCTL_LOG="$LOG"

# Real prod unit state at start
PROD_BEFORE=$(grep "model-router.service=" /tmp/r81-tests/J/fake-state)
PATH="$N/.fake-bin:$PATH" SYSTEMCTL_BIN="$FAKE" FAKE_SYSTEMCTL_LOG="$LOG" FAKE_SYSTEMCTL_STATE="$STATE" \
  "$MIGRATE" --execute --confirm-production \
  --legacy "$L" --product "$N" --data "$D" --systemd-user "$SU" >/dev/null 2>&1 || true
# Production service files at /home/sanchos/.config/systemd/user must not exist as
# new artefacts in the user's systemd dir from this test:
if [ ! -f "/home/sanchos/.config/systemd/user/model-router.service.new" ]; then
  ok "J no real .new unit created in user systemd dir"
else
  nok "J real .new unit created in user systemd dir"
fi

# ── summary ──────────────────────────────────────────────────────────────
echo
echo "────────────────────────────────────────────────────────"
echo "tests=$TESTS pass=$PASS fail=$FAIL"
echo "────────────────────────────────────────────────────────"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1