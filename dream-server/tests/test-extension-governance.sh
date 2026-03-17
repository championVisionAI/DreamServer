#!/bin/bash
# Dream Server Extension Governance Toolkit tests

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TOOL="$PROJECT_DIR/scripts/extension-governance.py"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

pass() {
  echo -e "  ${GREEN}PASS${NC}  $1"
  PASS=$((PASS + 1))
}

fail() {
  echo -e "  ${RED}FAIL${NC}  $1"
  [[ -n "${2:-}" ]] && echo -e "        ${RED}→ $2${NC}"
  FAIL=$((FAIL + 1))
}

skip() {
  echo -e "  ${YELLOW}SKIP${NC}  $1"
  SKIP=$((SKIP + 1))
}

section() {
  echo ""
  echo -e "${BLUE}[$1]${NC} $2"
}

mkroot() {
  local root
  root="$(mktemp -d)"
  mkdir -p "$root/extensions/services"
  echo "$root"
}

write_manifest() {
  local root="$1"
  local sid="$2"
  local body="$3"
  local svc_dir="$root/extensions/services/$sid"
  mkdir -p "$svc_dir"
  cat > "$svc_dir/manifest.yaml" <<EOF
schema_version: dream.services.v1
service:
  id: $sid
$body
EOF
}

write_compose() {
  local root="$1"
  local sid="$2"
  local compose_body="$3"
  local svc_dir="$root/extensions/services/$sid"
  mkdir -p "$svc_dir"
  cat > "$svc_dir/compose.yaml" <<EOF
services:
$compose_body
EOF
}

run_tool_expect_code() {
  local expected="$1"
  shift
  set +e
  output="$("$TOOL" "$@" 2>&1)"
  code=$?
  set -e
  if [[ "$code" -eq "$expected" ]]; then
    return 0
  fi
  echo "$output"
  return 1
}

if [[ ! -f "$TOOL" ]]; then
  echo -e "${RED}Tool not found: $TOOL${NC}"
  exit 1
fi

if ! python3 -c "import yaml" >/dev/null 2>&1; then
  skip "PyYAML not installed; governance tool requires yaml support"
  echo ""
  echo -e "${YELLOW}Skipped:${NC} $SKIP"
  exit 0
fi

section "1/6" "Audit passes on valid extension graph"
root1="$(mkroot)"
trap 'rm -rf "$root1" "${root2:-}" "${root3:-}" "${root4:-}" "${root5:-}" "${root6:-}"' EXIT

write_manifest "$root1" "llama-server" "  name: Llama
  category: core
  container_name: dream-llama
  health: /health
  gpu_backends: [amd, nvidia]
  external_port_default: 8080
  external_port_env: LLAMA_SERVER_PORT
"

write_manifest "$root1" "n8n" "  name: N8N
  category: optional
  compose_file: compose.yaml
  container_name: dream-n8n
  health: /healthz
  gpu_backends: [amd, nvidia]
  depends_on: [llama-server]
  aliases: [workflows]
  external_port_default: 5678
  external_port_env: N8N_PORT
"

write_compose "$root1" "n8n" "  n8n:
    image: n8nio/n8n:latest
    ports:
      - \"127.0.0.1:\${N8N_PORT:-5678}:5678\"
"

if run_tool_expect_code 0 audit --root "$root1"; then
  pass "audit succeeds for valid graph"
else
  fail "audit should pass for valid graph" "$output"
fi

section "2/6" "Missing dependency is reported as error"
root2="$(mkroot)"
write_manifest "$root2" "alpha" "  name: Alpha
  category: optional
  compose_file: compose.yaml
  container_name: dream-alpha
  health: /health
  depends_on: [missing-svc]
"
write_compose "$root2" "alpha" "  alpha:
    image: alpine
"

if run_tool_expect_code 1 audit --root "$root2"; then
  if echo "$output" | grep -q "service.dep_missing"; then
    pass "missing dependency detected"
  else
    fail "missing dependency code not present" "$output"
  fi
else
  fail "audit should fail with missing dependency" "$output"
fi

section "3/6" "Alias collisions are detected"
root3="$(mkroot)"
write_manifest "$root3" "svc-a" "  name: Service A
  category: optional
  compose_file: compose.yaml
  container_name: dream-svc-a
  aliases: [shared]
  health: /health
"
write_manifest "$root3" "svc-b" "  name: Service B
  category: optional
  compose_file: compose.yaml
  container_name: dream-svc-b
  aliases: [shared]
  health: /health
"
write_compose "$root3" "svc-a" "  svc-a:
    image: alpine
"
write_compose "$root3" "svc-b" "  svc-b:
    image: alpine
"

if run_tool_expect_code 1 audit --root "$root3"; then
  if echo "$output" | grep -q "service.alias_collision"; then
    pass "alias collision detected"
  else
    fail "alias collision code not present" "$output"
  fi
else
  fail "audit should fail for alias collision" "$output"
fi

section "4/6" "Dependency cycles are reported"
root4="$(mkroot)"
write_manifest "$root4" "a" "  name: A
  category: optional
  compose_file: compose.yaml
  container_name: dream-a
  depends_on: [b]
  health: /health
"
write_manifest "$root4" "b" "  name: B
  category: optional
  compose_file: compose.yaml
  container_name: dream-b
  depends_on: [a]
  health: /health
"
write_compose "$root4" "a" "  a:
    image: alpine
"
write_compose "$root4" "b" "  b:
    image: alpine
"

if run_tool_expect_code 1 audit --root "$root4"; then
  if echo "$output" | grep -q "service.dep_cycle"; then
    pass "dependency cycle detected"
  else
    fail "cycle code not present" "$output"
  fi
else
  fail "audit should fail for cycle" "$output"
fi

section "5/6" "Port collisions fail ports command"
root5="$(mkroot)"
write_manifest "$root5" "web-a" "  name: Web A
  category: optional
  compose_file: compose.yaml
  container_name: dream-web-a
  health: /health
  external_port_default: 7001
  external_port_env: WEB_A_PORT
"
write_manifest "$root5" "web-b" "  name: Web B
  category: optional
  compose_file: compose.yaml
  container_name: dream-web-b
  health: /health
  external_port_default: 7001
  external_port_env: WEB_B_PORT
"
write_compose "$root5" "web-a" "  web-a:
    image: nginx
    ports:
      - \"127.0.0.1:7001:80\"
"
write_compose "$root5" "web-b" "  web-b:
    image: nginx
    ports:
      - \"127.0.0.1:7001:80\"
"

if run_tool_expect_code 1 ports --root "$root5"; then
  if echo "$output" | grep -q "Port collisions"; then
    pass "ports command reports collisions"
  else
    fail "ports collision summary missing" "$output"
  fi
else
  fail "ports should fail for collisions" "$output"
fi

section "6/6" "Matrix JSON includes all services"
root6="$(mkroot)"
write_manifest "$root6" "core-api" "  name: Core API
  category: core
  container_name: dream-core-api
  health: /health
  gpu_backends: [all]
"
write_manifest "$root6" "gpu-addon" "  name: GPU Addon
  category: optional
  compose_file: compose.yaml
  container_name: dream-gpu-addon
  health: /health
  depends_on: [core-api]
  gpu_backends: [nvidia]
"
write_compose "$root6" "gpu-addon" "  gpu-addon:
    image: alpine
"

if run_tool_expect_code 0 matrix --root "$root6" --format json; then
  if echo "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["service_count"]==2; print("ok")' >/dev/null 2>&1; then
    pass "matrix json has expected service_count"
  else
    fail "matrix json did not have expected service_count" "$output"
  fi
else
  fail "matrix should succeed" "$output"
fi

echo ""
echo -e "${BLUE}Summary${NC}"
echo "  Passed:  $PASS"
echo "  Failed:  $FAIL"
echo "  Skipped: $SKIP"
echo ""

if [[ "$FAIL" -eq 0 ]]; then
  echo -e "${GREEN}All extension governance tests passed.${NC}"
  exit 0
fi

echo -e "${RED}Some extension governance tests failed.${NC}"
exit 1
