#!/usr/bin/env bash
# R8 §14 — rollback: restore the previous release.
# Same-host pattern: the LEGACY tree ($HOME/hermes-router) is the
# R7 rollback target; for future releases, `git checkout <previous-tag>`
# in the product tree + scripts/upgrade.sh.
set -euo pipefail

MODE="${1:-help}"

case "$MODE" in
  r7-legacy)
    echo "[rollback] R7 legacy tree: $HOME/hermes-router"
    echo "[rollback] tag model-router-v1-pre-release ($(git -C $HOME/hermes-router rev-parse --short model-router-v1-pre-release 2>/dev/null))"
    echo "[rollback] 1. stop new units"
    systemctl --user stop model-router.service 2>/dev/null || true
    echo "[rollback] 2. start legacy units"
    systemctl --user start hermes-router.service hermes-router-control.service 2>/dev/null || true
    echo "[rollback] 3. verify"
    sleep 3
    curl -sf http://127.0.0.1:4100/health | head -c 120; echo
    ;;
  git-tag)
    TAG="${2:?usage: rollback.sh git-tag <tag>}"
    cd "${MODEL_ROUTER_DIR:-$HOME/model-router}"
    git checkout "$TAG"
    echo "[rollback] checked out $TAG — run scripts/upgrade.sh to reinstall/restart"
    ;;
  *)
    cat <<'EOF'
usage: rollback.sh <mode>
  r7-legacy   restore R7 deployment from $HOME/hermes-router (units hermes-router*)
  git-tag T   checkout previous release tag in the product tree, then run scripts/upgrade.sh
EOF
    ;;
esac
