#!/usr/bin/env bash
# ============================================================================
# Dream Server — Safe environment loading (no eval)
# ============================================================================
# Parses KEY="value" lines (with \" and \\ escapes) from stdin and exports
# them in the current shell. Use instead of eval for output from
# build-capability-profile.sh, preflight-engine.sh, resolve-compose-stack.sh,
# load-backend-contract.sh, etc.
# ============================================================================

load_env_from_output() {
    local line key value
    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=\"(.*)\"$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            # Unescape: \\ -> \, \" -> "
            value="${value//\\\\/\\}"
            value="${value//\\\"/\"}"
            export "$key=$value"
        fi
    done
}
