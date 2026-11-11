#!/usr/bin/env bash
# Refresh vendor/ from the source tree (the source of the importer, the backup scripts and the memory modules).
#
#   OMP_MEMORY_SOURCE_DIR=DIR scripts/sync-vendor.sh [--source DIR] [--check]
#
# --check writes nothing and exits 1 when vendor/ differs from the source. The files that are copied are listed in
# scripts/vendor_manifest.txt; vendor/SOURCE.sha256 records what was copied. Run it, review `git diff`, commit.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$here/vendor_sync.py" "$@"
