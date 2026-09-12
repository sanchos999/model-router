#!/usr/bin/env bash
# R8 §16/§20 — release tree secret/sensitivity scan.
# Exit 0 only with zero findings (or explicit allow-list hits).
set -uo pipefail
DIR="${1:-$(pwd)}"
cd "$DIR"

findings=0
note() { echo "  $1"; }

echo "[scan] real API keys / bearer tokens"
pat_key='sk-[A-Za-z0-9]{20,}'
pat_bearer='Bearer[[:space:]]+[A-Za-z0-9._-]{15,}'
pat_authz='Authorization:[[:space:]]*Bearer'
out=$(grep -rInE "$pat_key|$pat_bearer" --include='*.py' --include='*.md' --include='*.yaml' --include='*.sh' --include='*.service' --include='*.json' --include='*.example' . 2>/dev/null | grep -v '\.venv/' | grep -vE 'CHANGE_ME-replace-me|Bearer \$|Bearer \$TOKEN|super-secret-token|example' || true)
[ -n "$out" ] && { findings=$((findings+1)); note "FOUND: $out"; } || note "  clean"

echo "[scan] private hosts/paths"
# Reviewed allow-list (docs/security.md):
#  - gateway/providers/{provider_a,provider_b}.py default base URLs are the
#    marketplaces' PUBLIC API endpoints (not credentials, not internal
#    hosts); both are env-overridable.
out=$(grep -rInE 'provider_bintelligence\.ai|provider_a\.dev|/home/sanchos|\.hermes/|192\.168\.' \
  --include='*.py' --include='*.md' --include='*.yaml' --include='*.sh' --include='*.service' --include='*.json' --include='*.example' . 2>/dev/null \
  | grep -v '\.venv/' \
  | grep -vE 'provider-a|provider-b|youruser|docs/|README' \
  | grep -v 'scripts/secret_scan.sh' \
  | grep -v 'examples/clients/hermes_client.md' \
  | grep -vE 'gateway/providers/(provider_a|provider_b)\.py' \
  | grep -vE 'super-secret-token' \
  || true)
if [ -n "$out" ]; then findings=$((findings+1)); note "FOUND: $out"; else note "  clean (public provider API endpoints allow-listed, see docs/security.md)"; fi

echo "[scan] real user names / telegram ids"
out=$(grep -rInE 'sanchos|alexandrovich|397237510|Гуссамов' \
  --include='*.py' --include='*.md' --include='*.yaml' --include='*.sh' --include='*.service' --include='*.json' --include='*.example' . 2>/dev/null \
  | grep -v '\.venv/' | grep -vE 'docs/|README' | grep -v 'scripts/secret_scan.sh' || true)
[ -n "$out" ] && { findings=$((findings+1)); note "FOUND: $out"; } || note "  clean"

echo "[scan] sqlite/state/telemetry/backup artifacts"
out=$(find . -path ./.venv -prune -o -path ./.git -prune -o \
  \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.jsonl' -o -name '*.log' -o -name 'state' -o -name 'backups' -o -name '__pycache__' -o -name '.pytest_cache' -o -name '.env' -o -name '.env.gateway' \) -print 2>/dev/null \
  | while read -r f; do git check-ignore -q "$f" 2>/dev/null || echo "$f"; done)
[ -n "$out" ] && { findings=$((findings+1)); note "FOUND: $out"; } || note "  clean"

echo "[scan] raw provider catalog / billing / price-agreement data"
out=$(grep -rInE 'x-si-buyer-cost|cost_per_success.*[0-9]+\.[0-9]+[0-9]|billing' --include='*.json' . 2>/dev/null | grep -v '\.venv/' | grep -v example || true)
[ -n "$out" ] && { findings=$((findings+1)); note "FOUND: $out"; } || note "  clean (docs explain concepts generically only)"

echo "[scan] env files with real values"
[ -f .env ] && { findings=$((findings+1)); note "FOUND: .env in tree"; } || note "  clean (.env.example only)"

if [ "$findings" = 0 ]; then echo "[scan] PASS — 0 sensitive findings"; else echo "[scan] FAIL — $findings finding group(s)"; exit 1; fi
