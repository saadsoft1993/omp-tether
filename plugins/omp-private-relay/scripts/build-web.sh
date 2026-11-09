#!/usr/bin/env bash
# Builds the patched collab-web client from an omp source checkout, on the Bun that the installed omp embeds.
#
#   build-web.sh --omp-src DIR --tag vX.Y.Z --omp-bin /abs/omp --out DIR
#
#   --omp-src  an omp git checkout that has the release tag (git fetch --tags). It is only read: the tag is exported
#              with `git archive` into a temporary directory, so the checkout's branch, index and files are untouched.
#   --tag      the omp release to build, for example v18.18.0. Its minor must equal the minor of the installed omp:
#              the page and the sessions have to speak the same Collab protocol version.
#   --omp-bin  absolute path of the omp binary to build with.
#   --out      directory that receives the finished client (it must not exist yet).
#
# BUN_BE_BUN is set only for the commands that must be Bun. A shim named `bun` (collab-web's build script calls `bun`
# itself) is put first on PATH for those commands only; a symlink to omp would not do, because it would still behave
# as omp. The build needs network access once, for `bun install` of the checkout's locked dependencies.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="" tag="" omp="" out=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--omp-src) src="${2:?--omp-src needs a value}"; shift 2 ;;
	--tag) tag="${2:?--tag needs a value}"; shift 2 ;;
	--omp-bin) omp="${2:?--omp-bin needs a value}"; shift 2 ;;
	--out) out="${2:?--out needs a value}"; shift 2 ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[[ -n "$src" && -n "$tag" && -n "$omp" && -n "$out" ]] || { echo "usage: build-web.sh --omp-src DIR --tag vX.Y.Z --omp-bin /abs/omp --out DIR" >&2; exit 2; }
[[ "$tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "tag must look like v18.18.0" >&2; exit 2; }
minor="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
[[ "$omp" == /* && -x "$omp" ]] || { echo "--omp-bin must be an absolute path to an executable" >&2; exit 2; }
[[ ! -e "$out" ]] || { echo "$out already exists" >&2; exit 2; }

patch="$here/patches/collab-web-$minor.patch"
[[ -f "$patch" ]] || { echo "no patch for omp $minor: $patch is missing" >&2; exit 1; }

# The omp binary prints its own version only when BUN_BE_BUN is not set.
installed="$(env -u BUN_BE_BUN "$omp" --version | sed -n 's#^omp/\([0-9]*\.[0-9]*\)\..*#\1#p')"
if [[ "$installed" != "$minor" ]]; then
	echo "installed omp is $installed.x but the tag is $tag: build with the matching omp" >&2
	exit 1
fi
git -C "$src" rev-parse -q --verify "refs/tags/$tag" >/dev/null || { echo "tag $tag not found in $src (git fetch --tags)" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/collab-web-build.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir "$work/src" "$work/shim"
git -C "$src" archive "$tag" | tar -x -C "$work/src"

# collab-web's build script calls plain `bun`; this shim is omp's embedded Bun.
printf '#!/bin/sh\nBUN_BE_BUN=1 exec %q "$@"\n' "$omp" >"$work/shim/bun"
chmod +x "$work/shim/bun"

have="$(PATH="$work/shim:$PATH" bun --version)"
need="$(sed -n 's/.*"bun": ">=\([0-9.]*\)".*/\1/p' "$work/src/packages/collab-web/package.json")"
if [[ -n "$need" && "$(printf '%s\n%s\n' "$need" "$have" | sort -V | head -n1)" != "$need" ]]; then
	echo "the Bun inside omp is $have, older than collab-web's engines.bun >= $need" >&2
	exit 1
fi

(cd "$work/src" && git init -q . && git apply "$patch")
rm -rf "$work/src/.git"
(cd "$work/src" && PATH="$work/shim:$PATH" bun install --frozen-lockfile --ignore-scripts --filter @oh-my-pi/collab-web >/dev/null)
(cd "$work/src/packages/collab-web" && PATH="$work/shim:$PATH" bun run build >/dev/null)
dist="$work/src/packages/collab-web/dist"

# The patch must have applied, and the bundle must come from a production build.
shopt -s nullglob
js=("$dist"/*.js)
[[ ${#js[@]} -gt 0 ]] || { echo "no JavaScript bundle was produced" >&2; exit 1; }
if grep -q 'um.can.ac' "$dist/index.html"; then echo "third-party analytics script still in index.html: the patch did not apply" >&2; exit 1; fi
if grep -q 'void 0,!1,void 0,this)' "${js[@]}"; then echo "bundle uses development JSX calls: wrong Bun" >&2; exit 1; fi
grep -q 'this browser is not paired with the relay' "${js[@]}" || { echo "close 4401 text missing from the bundle: the patch did not apply" >&2; exit 1; }
grep -q 'location.hostname' "${js[@]}" || { echo "loopback rewrite missing from the bundle: the patch did not apply" >&2; exit 1; }

cp -R "$dist" "$out"
echo "collab-web $tag built with Bun $have -> $out"
