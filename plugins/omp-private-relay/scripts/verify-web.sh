#!/usr/bin/env bash
# Rebuilds the web client from an omp release tag and compares the result with what web-builds.json pins: the archive's
# sha256 and the content hash. Exit 0 when both are equal, 1 when not. Nothing in the repository is changed. Run it (through
# `make release-check`) for every omp minor you publish; a difference means the build is not reproducible, or the pin is stale.
#
#   verify-web.sh --omp-src DIR --tag vX.Y.Z --omp-bin /abs/omp
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="" tag="" omp=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--omp-src) src="${2:?}"; shift 2 ;;
	--tag) tag="${2:?}"; shift 2 ;;
	--omp-bin) omp="${2:?}"; shift 2 ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[[ -n "$src" && -n "$tag" && -n "$omp" ]] || { echo "usage: verify-web.sh --omp-src DIR --tag vX.Y.Z --omp-bin /abs/omp" >&2; exit 2; }

work="$(mktemp -d "${TMPDIR:-/tmp}/verify-web.XXXXXX")"
trap 'rm -rf "$work"' EXIT
"$here/build-web.sh" --omp-src "$src" --tag "$tag" --omp-bin "$omp" --out "$work/dist"
BUN_BE_BUN=1 "$omp" "$here/web-asset.ts" pack --dist "$work/dist" --tag "$tag" --out "$work/out" >"$work/pack.json"
asset="$(sed -n 's/.*"asset":"\([^"]*\)".*/\1/p' "$work/pack.json")"
BUN_BE_BUN=1 "$omp" "$here/web-asset.ts" verify --asset "$asset" --tag "$tag"
