"""Per-instance state directory resolution (R7 §2).

GATEWAY_STATE_DIR overrides the state directory so canary (:4101) and
production (:4100) instances never write the same runtime files.

SHARED (intentional, control state, concurrent-safe via SQLite WAL):
  state/control.db        — policy revisions, providers, overrides, audit
  state/gateway-v2.json   — generated runtime config snapshot (written
                            atomically by control apply only)

PER-INSTANCE (runtime telemetry/registry mirrors):
  canonical-registry.json, context-compression.jsonl,
  model-registry.json, model-aliases.json, model-lifecycle.json
"""
from __future__ import annotations

import os
from pathlib import Path

# MODEL_ROUTER_STATE_DIR relocates the mutable data root (R8 §3);
# GATEWAY_STATE_DIR stays honoured as the legacy alias.
def _default_state_root() -> str:
    return os.environ.get(
        "MODEL_ROUTER_STATE_DIR", str(Path.home() / "model-router-data")
    )


def state_dir() -> str:
    return os.environ.get("GATEWAY_STATE_DIR", _default_state_root())


def state_file(name: str) -> str:
    return os.path.join(state_dir(), name)
