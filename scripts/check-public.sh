#!/usr/bin/env bash
# The publication gate for this repository (see tools/public-tree-check/release_gate.py): the committed tree and every
# ref's history, a credential scan of that history, a fresh single-commit export of the candidate commit, and the same
# checks on that exact artifact. It fails closed; gitleaks is required.
#
#   scripts/check-public.sh [--commit REF]
#
# Environment:
#   PUBLIC_IDENTITY       'Name <email>' every commit must carry (default: git's configured user)
#   PRIVATE_TERMS_FILE    optional file of private regexes (hostnames, other workspaces' names), kept outside the repo
#   BINARY_ALLOW_FILE     optional file of reviewed binary paths
#   EXEMPT_FILE           per-file exemptions of the content rules (default: tools/public-tree-check/exemptions.json, committed:
#                         exact paths, each with a one-line reason; it may exempt product names and nothing else)
# Uncommitted changes are not part of the candidate: commit first.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/tools/public-tree-check"
args=(--repo "$ROOT" --allowlist "$TOOL/allowlist.txt")
[[ -n "${PUBLIC_IDENTITY:-}" ]] && args+=(--identity "$PUBLIC_IDENTITY")
[[ -n "${PRIVATE_TERMS_FILE:-}" ]] && args+=(--private-terms "$PRIVATE_TERMS_FILE")
[[ -n "${BINARY_ALLOW_FILE:-}" ]] && args+=(--binary-allow "$BINARY_ALLOW_FILE")
EXEMPT_FILE="${EXEMPT_FILE:-$TOOL/exemptions.json}"
[[ -f "$EXEMPT_FILE" ]] && args+=(--exempt "$EXEMPT_FILE")
exec python3 "$TOOL/release_gate.py" "${args[@]}" "$@"
