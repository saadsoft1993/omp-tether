#!/usr/bin/env bash
# Loads the exact artifact that would be published: a fresh single-commit export of the committed candidate, made with the
# same exporter the publication gate uses, goes through the extension-load gate (install from the export as a local
# marketplace, load, run, uninstall). `make gate` tests the working tree; this tests what leaves it.
#
#   scripts/export-gate.sh [--commit REF]
#
# Environment: OMP_BIN as for the gate. The author is git's configured user. Uncommitted changes are not part of it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
NAME="$(git -C "$ROOT" config user.name)"
EMAIL="$(git -C "$ROOT" config user.email)"
python3 "$ROOT/tools/public-tree-check/fresh_history.py" "$ROOT" "$TMP/export" \
  --allowlist "$ROOT/tools/public-tree-check/allowlist.txt" --name "$NAME" --email "$EMAIL" "$@"
python3 "$ROOT/tools/extension-load-gate/gate.py" --repo "$TMP/export"
