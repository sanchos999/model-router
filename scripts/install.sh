#!/usr/bin/env bash
# R8 §14 — same-host install for Model Router.
# Usage: scripts/install.sh [CODE_DIR] [DATA_DIR] [PRIVATE_ENV]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODE_DIR="${1:-$SCRIPT_DIR}"
DATA_DIR="${2:-$HOME/model-router-data}"
PRIVATE_ENV="${3:-$HOME/.config/model-router/router.env}"

echo "[install] code:   $CODE_DIR"
echo "[install] data:   $DATA_DIR"
echo "[install] secret: $PRIVATE_ENV"

# 1. deps: fresh venv (falls back to uv if ensurepip is missing)
if [ ! -x "$CODE_DIR/.venv/bin/python" ]; then
  if ! "$CODE_DIR/../../usr/bin/python3.14" -m venv "$CODE_DIR/.venv" 2>/dev/null; then
    if command -v uv >/dev/null 2>&1; then
      uv venv --python /usr/bin/python3.14 "$CODE_DIR/.venv"
      uv pip install --python "$CODE_DIR/.venv/bin/python" -r "$CODE_DIR/requirements.txt"
    else
      python3 -m venv "$CODE_DIR/.venv" && "$CODE_DIR/.venv/bin/pip" install -r "$CODE_DIR/requirements.txt"
    fi
  else
    "$CODE_DIR/.venv/bin/pip" install -q -r "$CODE_DIR/requirements.txt"
  fi
else
  "$CODE_DIR/.venv/bin/pip" install -q -r "$CODE_DIR/requirements.txt" || \
    uv pip install --python "$CODE_DIR/.venv/bin/python" -r "$CODE_DIR/requirements.txt"
fi

# 2. data root
mkdir -p "$DATA_DIR"/{prod,canary,control,metrics,runtime}

# 3. private config
if [ ! -f "$PRIVATE_ENV" ]; then
  echo "[install] WARNING: $PRIVATE_ENV missing — copy .env.example and fill it"
  exit 1
fi
chmod 600 "$PRIVATE_ENV"

echo "[install] DONE (services are deployed separately; see deploy/ units)"
