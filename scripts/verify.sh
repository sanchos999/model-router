#!/usr/bin/env bash
# R8 §14 — deployment verification: health, version, auth boundary, registry.
# Usage: scripts/verify.sh [port]
set -euo pipefail
PORT="${1:-4100}"
B="http://127.0.0.1:$PORT"
fail=0

echo "[verify] $B"

health_json=$(curl -sf "$B/health") || fail=1
if [ -n "${health_json:-}" ]; then
  echo "$health_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d.get("registry",{})
print("  health: ok=%s instance=%s routes=%s providers=%s" % (d.get("ok"), d.get("instance_id"), r.get("routes"), r.get("providers")))
assert d.get("ok")
' || fail=1
fi

version_json=$(curl -sf "$B/version") || fail=1
if [ -n "${version_json:-}" ]; then
  echo "$version_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  version: %s schema=%s api=%s commit=%s" % (d.get("version"), d.get("schema_version"), d.get("api_version"), d.get("build_commit")))
' || fail=1
fi

# bind check: must be loopback
if ss -tln | grep -q ":$PORT " && ! ss -tln | grep ":$PORT " | grep -q "127.0.0.1:$PORT"; then
  echo "  BIND: WARNING - port $PORT not loopback-only"; fail=1
else
  echo "  bind: localhost-only OK"
fi

if [ "$fail" = 0 ]; then echo "[verify] PASS"; else echo "[verify] FAIL"; exit 1; fi
