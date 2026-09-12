#!/usr/bin/env bash
# R8 §14 — upgrade pattern: new code -> deps -> migrations -> tests ->
# switch/restart -> readiness -> smoke. NO auto-pull from GitHub.
# Usage: scripts/upgrade.sh [CODE_DIR]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODE_DIR="${1:-$SCRIPT_DIR}"
cd "$CODE_DIR"

echo "[upgrade 1/6] current commit: $(git rev-parse --short HEAD 2>/dev/null || echo 'no-git')"
echo "[upgrade 2/6] dependencies"
.venv/bin/pip install -q -r requirements.txt 2>/dev/null || \
  uv pip install --python .venv/bin/python -r requirements.txt

echo "[upgrade 3/6] migrations (schema versioning runs on service start;"
echo "               a future-schema DB makes the service refuse to start)"
echo "[upgrade 4/6] tests"
PYTHONPATH="$CODE_DIR" .venv/bin/python -m pytest gateway_tests -q || {
  echo "[upgrade] TESTS FAILED — aborting, nothing switched"; exit 1; }

echo "[upgrade 5/6] switch: restart instances running from $CODE_DIR"
for u in model-router.service model-router-release-test.service model-router-release-control-test.service; do
  if systemctl --user is-active --quiet "$u" 2>/dev/null; then
    systemctl --user restart "$u"
    echo "  restarted $u"
  fi
done
if systemctl --user is-active --quiet hermes-router-control.service 2>/dev/null; then
  echo "  NOTE: hermes-router-control.service still points at the legacy tree — see docs/upgrade.md"
fi

echo "[upgrade 6/6] readiness + inference smoke"
sleep 3
for port in 4100 4210; do
  if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "  :$port health OK"
    curl -s "http://127.0.0.1:$port/version" | head -c 120; echo
  fi
done
echo "[upgrade] DONE. On failure: scripts/rollback.sh"
