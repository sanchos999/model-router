"""Product version metadata (R8 §12).

MODEL_ROUTER_VERSION is the product semver. The build commit is injected by
scripts/install.sh (or the deployment environment) via MODEL_ROUTER_BUILD_COMMIT;
"unknown" means the code is not running from a tagged install.
"""
from __future__ import annotations

import os

MODEL_ROUTER_VERSION = os.environ.get("MODEL_ROUTER_VERSION", "1.0.0")
API_VERSION = "v1"

# Bumped when control.db layout changes; must match gateway.control.store.
STATE_SCHEMA_VERSION = 1


def build_commit() -> str:
    return os.environ.get("MODEL_ROUTER_BUILD_COMMIT", "unknown")


def instance_id() -> str:
    return os.environ.get("MODEL_ROUTER_INSTANCE_ID", "default")


def version_payload() -> dict:
    from .control import store as _store

    return {
        "version": MODEL_ROUTER_VERSION,
        "build_commit": build_commit(),
        "schema_version": _store.db_schema_version(),
        "api_version": API_VERSION,
        "instance_id": instance_id(),
        # R8.1C spec aliases (no private paths/secrets either way):
        "build": build_commit(),
        "instance": instance_id(),
    }
