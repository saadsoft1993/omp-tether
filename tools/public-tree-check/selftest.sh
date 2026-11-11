#!/usr/bin/env bash
# Throwaway-repo self-test for public_tree_check.py. Planted strings are assembled at runtime so this
# file never holds them literally. Usage: ./selftest.sh   (exit 0 when every scenario behaves as expected)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK=(python3 "$HERE/public_tree_check.py")
OPTS=(--allowlist "$HERE/allowlist.example.txt" --identities "$HERE/identities.example.txt")
ROOT="$(mktemp -d /tmp/ptc-selftest.XXXXXX)"
trap 'rm -rf "$ROOT"' EXIT
export GIT_AUTHOR_NAME="Example Author" GIT_AUTHOR_EMAIL="author@example.com"
export GIT_COMMITTER_NAME="Example Author" GIT_COMMITTER_EMAIL="author@example.com"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
NAME_A="Cl""aude"; AGENT_FILE="CLAU""DE.md"; TRAILER="Co-Auth""ored-""By: ${NAME_A} <noreply@vendor.invalid>"
HOST="box.tail""abc123.t""s.net"; HOMEP="/Us""ers/someone/project"
STRAY="someone@acme-corp"".com"; TAILIP="10""0.101.102.103"
pass=0; failn=0

new_repo() {  # builds a clean publishable repo at $ROOT/$1
  local d="$ROOT/$1"; mkdir -p "$d/plugins/example-plugin/src" && git -C "$d" init -q -b main
  printf 'Session control for omp.\n' >"$d/README.md"
  printf 'MIT\n' >"$d/LICENSE"
  printf '{"name":"example-plugin","version":"0.0.0"}\n' >"$d/plugins/example-plugin/package.json"
  printf 'export const ok = true;\n' >"$d/plugins/example-plugin/src/index.ts"
  git -C "$d" add -A && git -C "$d" commit -q -m "Initial import"
  echo "$d"
}
expect() {  # expect <label> <want-exit> <needle|-> <repo>
  local label="$1" want="$2" needle="$3" repo="$4" out rc
  out="$("${CHECK[@]}" "$repo" "${OPTS[@]}" 2>&1)"; rc=$?
  if [[ $rc -eq $want ]] && { [[ $needle == - ]] || grep -q -- "$needle" <<<"$out"; }; then
    printf 'ok   %-46s exit=%d\n' "$label" "$rc"; pass=$((pass+1))
  else
    printf 'BAD  %-46s exit=%d (wanted %d, needle %s)\n%s\n' "$label" "$rc" "$want" "$needle" "$out"; failn=$((failn+1))
  fi
  grep -E '^(FAIL|warn)' <<<"$out" | sed 's/^/       /' | head -4
}

d=$(new_repo clean);          expect "clean tree passes"                         0 "0 failing" "$d"
d=$(new_repo planted-agent);  printf 'rules\n' >"$d/$AGENT_FILE"; git -C "$d" add -A; git -C "$d" commit -q -m "Add notes"
                              expect "planted agent-instruction file caught"      1 "path-denied" "$d"
                              git -C "$d" rm -q "$AGENT_FILE"; git -C "$d" commit -q -m "Remove notes"
                              expect "same file deleted later: history still fails" 1 "history" "$d"
d=$(new_repo planted-trailer); printf 'x\n' >>"$d/README.md"; git -C "$d" commit -q -am "Update readme

$TRAILER"
                              expect "planted co-author trailer caught"           1 "ai-trailer" "$d"
d=$(new_repo planted-host);   printf 'Open https://%s/ to pair.\n' "$HOST" >>"$d/README.md"; git -C "$d" commit -q -am "Docs"
                              expect "planted tailnet hostname caught"            1 "tailnet-host" "$d"
d=$(new_repo planted-name);   printf '// pasted from %s\n' "$NAME_A" >>"$d/plugins/example-plugin/src/index.ts"; git -C "$d" commit -q -am "Tweak"
                              expect "planted AI-name word caught"                1 "ai-name" "$d"
d=$(new_repo unlisted-file);  printf 'x\n' >"$d/notes.txt"; git -C "$d" add -A; git -C "$d" commit -q -m "Add"
                              expect "file not on the allowlist caught"           1 "path-not-allowed" "$d"
d=$(new_repo wrong-author);   printf 'x\n' >>"$d/README.md"; GIT_AUTHOR_EMAIL="other@acme-corp"".com" git -C "$d" commit -q -am "Edit"
                              expect "wrong author identity caught"               1 "identity" "$d"
d=$(new_repo home-path);      printf 'cd %s\n' "$HOMEP" >>"$d/README.md"; git -C "$d" commit -q -am "Docs"
                              expect "absolute home path caught"                  1 "home-path" "$d"
d=$(new_repo stray-email);    printf 'mail %s\n' "$STRAY" >>"$d/README.md"; git -C "$d" commit -q -am "Docs"
                              expect "stray email caught"                         1 "email" "$d"
d=$(new_repo tailnet-ip);     printf 'host %s\n' "$TAILIP" >>"$d/README.md"; git -C "$d" commit -q -am "Docs"
                              expect "tailnet (CGNAT) address caught"             1 "cgnat-ip" "$d"
d=$(new_repo env-file);       printf 'K=v\n' >"$d/.env"; git -C "$d" add -f .env; git -C "$d" commit -q -m "Env"
                              expect "env file caught"                            1 "path-denied" "$d"
d=$(new_repo private-term);   printf 'internal host: bigbox-7\n' >>"$d/README.md"; git -C "$d" commit -q -am "Docs"
  printf '# private\nbigbox-\\d+\n' >"$ROOT/terms.txt"
  out="$("${CHECK[@]}" "$d" "${OPTS[@]}" --private-terms "$ROOT/terms.txt" 2>&1)"; rc=$?
  if [[ $rc -eq 1 ]] && grep -q private-term <<<"$out"; then echo "ok   private-terms file catches a custom hostname      exit=1"; pass=$((pass+1)); else echo "BAD  private terms"; failn=$((failn+1)); fi
  echo "tool checks itself:"; (cd "$ROOT" && rm -rf self && mkdir -p self/tools/public-tree-check && cp "$HERE"/public_tree_check.py "$HERE"/fresh_history.py "$HERE"/*.txt "$HERE"/selftest.sh self/tools/public-tree-check/ \
    && git -C self init -q -b main && git -C self add -A && git -C self commit -q -m "tool")
  expect "the tool's own directory passes the check"     0 "0 failing" "$ROOT/self"
d=$(new_repo dirty-history);  printf 'rules\n' >"$d/$AGENT_FILE"; printf 'K=v\n' >"$d/.env"; git -C "$d" add -f -A; git -C "$d" commit -q -m "Work

$TRAILER"
                              git -C "$d" rm -q "$AGENT_FILE" .env; git -C "$d" commit -q -m "Remove local files"
                              expect "dirty history fails even though HEAD is clean" 1 "history" "$d"
  python3 "$HERE/fresh_history.py" "$d" "$ROOT/fresh" --allowlist "$HERE/allowlist.example.txt" --name "Example Author" --email "author@example.com" >/dev/null
                              expect "fresh one-commit tree from it passes"       0 "0 failing" "$ROOT/fresh"
  n=$(git -C "$ROOT/fresh" rev-list --count HEAD); if [[ $n -eq 1 ]]; then echo "ok   fresh tree has exactly one commit"; pass=$((pass+1)); else echo "BAD  fresh tree has $n commits"; failn=$((failn+1)); fi

printf '\n%d ok, %d unexpected\n' "$pass" "$failn"; [[ $failn -eq 0 ]]
