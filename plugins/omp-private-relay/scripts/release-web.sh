#!/usr/bin/env bash
# Builds the web client for one omp release tag, packs it as a release asset, pins it in web-builds.json and writes
# SHA256SUMS next to it. Run by the release job (or by hand) on a machine that has the matching omp and an omp source
# checkout with tags; publishing the asset (a GitHub release) is a separate, deliberate step.
#
#   release-web.sh --omp-src DIR --tag vX.Y.Z --omp-bin /abs/omp --release-tag omp-private-relay-vA.B.C --out DIR
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="" tag="" omp="" release="" out=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--omp-src) src="${2:?}"; shift 2 ;;
	--tag) tag="${2:?}"; shift 2 ;;
	--omp-bin) omp="${2:?}"; shift 2 ;;
	--release-tag) release="${2:?}"; shift 2 ;;
	--out) out="${2:?}"; shift 2 ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[[ -n "$src" && -n "$tag" && -n "$omp" && -n "$release" && -n "$out" ]] || { echo "usage: release-web.sh --omp-src DIR --tag vX.Y.Z --omp-bin /abs/omp --release-tag TAG --out DIR" >&2; exit 2; }

work="$(mktemp -d "${TMPDIR:-/tmp}/release-web.XXXXXX")"
trap 'rm -rf "$work"' EXIT
"$here/build-web.sh" --omp-src "$src" --tag "$tag" --omp-bin "$omp" --out "$work/dist"
bun_version="$(BUN_BE_BUN=1 "$omp" --version)"
mkdir -p "$out"
BUN_BE_BUN=1 "$omp" "$here/web-asset.ts" pack --dist "$work/dist" --tag "$tag" --out "$out" >"$work/pack.json"
asset="$(sed -n 's/.*"asset":"\([^"]*\)".*/\1/p' "$work/pack.json")"
BUN_BE_BUN=1 "$omp" "$here/web-asset.ts" record --asset "$asset" --tag "$tag" --bun "$bun_version" --release-tag "$release"
(cd "$out" && if command -v sha256sum >/dev/null; then sha256sum "$(basename "$asset")"; else shasum -a 256 "$(basename "$asset")"; fi >SHA256SUMS)
echo "asset: $asset"
cat "$out/SHA256SUMS"
