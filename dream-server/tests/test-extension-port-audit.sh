#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TOOL="$PROJECT_DIR/scripts/extension-port-audit.py"

PASS=0
FAIL=0

pass() {
    echo "PASS  $1"
    PASS=$((PASS + 1))
}

fail() {
    echo "FAIL  $1"
    [[ -n "${2:-}" ]] && echo "  $2"
    FAIL=$((FAIL + 1))
}

make_root() {
    local root
    root="$(mktemp -d)"
    mkdir -p "$root/extensions/services"
    echo "$root"
}

write_manifest() {
    local root="$1"
    local sid="$2"
    local body="$3"
    mkdir -p "$root/extensions/services/$sid"
    printf "schema_version: dream.services.v1\nservice:\n  id: %s\n%b" "$sid" "$body" > "$root/extensions/services/$sid/manifest.yaml"
}

write_compose() {
    local root="$1"
    local sid="$2"
    local body="$3"
    mkdir -p "$root/extensions/services/$sid"
    printf "services:\n%b" "$body" > "$root/extensions/services/$sid/compose.yaml"
}

run_expect() {
    local expected="$1"
    shift
    set +e
    output="$($TOOL "$@" 2>&1)"
    code=$?
    set -e
    [[ "$code" -eq "$expected" ]]
}

trap 'rm -rf "${root1:-}" "${root2:-}" "${root3:-}" "${root4:-}"' EXIT

root1="$(make_root)"
write_manifest "$root1" "n8n" "  name: n8n\n  compose_file: compose.yaml\n  external_port_env: N8N_PORT\n  external_port_default: 5678\n"
write_compose "$root1" "n8n" "  n8n:\n    image: n8nio/n8n:latest\n    ports:\n      - \"127.0.0.1:\${N8N_PORT:-5678}:5678\"\n"
if run_expect 0 --root "$root1"; then
    pass "valid manifest and compose port wiring succeeds"
else
    fail "valid manifest and compose port wiring should succeed" "$output"
fi

root2="$(make_root)"
write_manifest "$root2" "svc-a" "  name: A\n  compose_file: compose.yaml\n  external_port_env: A_PORT\n  external_port_default: 7000\n"
write_manifest "$root2" "svc-b" "  name: B\n  compose_file: compose.yaml\n  external_port_env: B_PORT\n  external_port_default: 7000\n"
write_compose "$root2" "svc-a" "  svc-a:\n    image: nginx\n    ports:\n      - \"127.0.0.1:\${A_PORT:-7000}:80\"\n"
write_compose "$root2" "svc-b" "  svc-b:\n    image: nginx\n    ports:\n      - \"127.0.0.1:\${B_PORT:-7000}:80\"\n"
if run_expect 1 --root "$root2"; then
    if echo "$output" | grep -q "port.collision"; then
        pass "duplicate host ports are rejected"
    else
        fail "duplicate host ports should emit collision code" "$output"
    fi
else
    fail "duplicate host ports should fail" "$output"
fi

root3="$(make_root)"
write_manifest "$root3" "svc-a" "  name: A\n  compose_file: compose.yaml\n  external_port_env: A_PORT\n  external_port_default: 7010\n"
write_compose "$root3" "svc-a" "  svc-a:\n    image: nginx\n    ports:\n      - \"127.0.0.1:\${A_PORT:-7020}:80\"\n"
if run_expect 1 --root "$root3"; then
    if echo "$output" | grep -q "compose.port_mismatch"; then
        pass "manifest and compose port drift is rejected"
    else
        fail "port drift should emit mismatch code" "$output"
    fi
else
    fail "port drift should fail" "$output"
fi

root4="$(make_root)"
write_manifest "$root4" "svc-a" "  name: A\n  compose_file: compose.yaml\n  external_port_env: bad-port\n  external_port_default: 7001\n"
write_compose "$root4" "svc-a" "  svc-a:\n    image: nginx\n"
if run_expect 1 --root "$root4" --format json; then
    if echo "$output" | grep -q '"manifest.port_invalid"'; then
        pass "invalid env var names are reported in json output"
    else
        fail "invalid env vars should be visible in json output" "$output"
    fi
else
    fail "invalid env vars should fail" "$output"
fi

echo ""
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [[ "$FAIL" -eq 0 ]]; then
    exit 0
fi

exit 1
