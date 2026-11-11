#!/usr/bin/env bash
# An isolated end-to-end smoke of omp-memory on SYNTHETIC data, in a throwaway HOME under $TMPDIR (deleted at the end).
# Nothing real is read or written: the memory files, the folders and the banks are made here, the banks by the published
# Mnemopi library itself.
#
#   scripts/smoke.sh [--bun PATH] [--omp-version X.Y.Z] [--lib-dir DIR] [--model-cache DIR] [--keep]
#
#   --bun PATH           Bun 1.4.4 or newer (default: found by omp-memory)
#   --omp-version V      the omp version to use when no `omp` is on PATH: a stand-in `omp` that prints it is made (default 18.18.3)
#   --lib-dir DIR        an existing library install root (node_modules/@oh-my-pi/pi-mnemopi at that version) to reuse instead of
#                        running `omp-memory setup` (which downloads it)
#   --model-cache DIR    a copy of omp's embedding cache (fastembed/, fastembed-runtime/) to reuse instead of downloading it
#   --keep               keep the scratch directory (its path is printed)
#   --isolation-check    set up the throwaway HOME, check that every destination resolves inside it, print `isolation ok` and stop
#
# Runs through the launcher: backup with no bank yet (nothing to back up), doctor (library missing), setup (or reuse, then
# `doctor --deep`), doctor, banks, sources, config groups, an import refused for lack of a dry run and a backup, dry-run,
# backup, import --apply, verify, a second apply (idempotent), undo --apply into the new bank and into an existing bank
# created by the library (its native rows must be unchanged), the banks the import created being "new" again after the
# undo, a binary file and an oversize file being skipped with a reason, and restore-drill. Memory text of the synthetic files is not secret; a fake credential in one of them must be held back.
# Works on macOS and Linux (bash, python3, sqlite3 and shasum or sha256sum). Exit 1 on the first failed expectation.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
launcher="$here/bin/omp-memory"
bun=""
omp_version="18.18.3"
lib_dir=""
model_cache=""
keep=0
isolation_only=0
while (($#)); do
	case "$1" in
	--bun) bun="${2:?--bun needs a path}"; shift 2 ;;
	--omp-version) omp_version="${2:?--omp-version needs a version}"; shift 2 ;;
	--lib-dir) lib_dir="${2:?--lib-dir needs a directory}"; shift 2 ;;
	--model-cache) model_cache="${2:?--model-cache needs a directory}"; shift 2 ;;
	--keep) keep=1; shift ;;
	--isolation-check) isolation_only=1; shift ;;
	-h | --help) sed -n '2,22p' "$0"; exit 0 ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
command -v sqlite3 >/dev/null || { echo "sqlite3 is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }

real_home="$HOME"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/omp-memory-smoke.XXXXXX")"
scratch="$(cd "$scratch" && pwd -P)"
cleanup() {
	if ((keep)); then echo "scratch kept: $scratch"; else rm -rf "$scratch"; fi
}
trap cleanup EXIT
export HOME="$scratch/home"
bun="${bun:-${OMP_MEMORY_BUN:-}}"
# every destination or lookup override of the caller's shell goes: the scratch HOME alone does not redirect an absolute path
for var in $(env | sed -n 's/^\(OMP_MEMORY_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$var"; done
unset PI_CODING_AGENT_DIR XDG_CONFIG_HOME XDG_CACHE_HOME BUN_BE_BUN OMP_PLUGINS_DIR
mkdir -p "$HOME/.omp/cache" "$HOME/work" "$scratch/bin"
[[ -z "$bun" ]] || export OMP_MEMORY_BUN="$bun"
banks_dir="$HOME/.omp/agent/memories/mnemopi/banks"
mkdir -p "$banks_dir"
export OMP_MEMORY_BACKUPS_DIR="$scratch/backups"
export OMP_PLUGINS_DIR="$scratch/no-plugins" # an empty omp plugin directory: the library must come from setup or --lib-dir

# a stand-in omp when there is none (it prints its version; it carries no SQL, so it is not scanned for it)
if [[ -z "${OMP_BIN:-}" ]] && ! command -v omp >/dev/null 2>&1; then
	printf '#!/bin/sh\necho "omp/%s"\n' "$omp_version" >"$scratch/bin/omp"
	chmod 755 "$scratch/bin/omp"
	export OMP_BIN="$scratch/bin/omp"
elif [[ -z "${OMP_BIN:-}" ]]; then
	omp_version="$(env -u BUN_BE_BUN omp --version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
fi
[[ -z "$lib_dir" ]] || export OMP_MEMORY_LIB_DIR="$lib_dir"

fail() { echo "SMOKE FAILED: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
om() { "$launcher" "$@"; }
py() { python3 -c "$@"; } # py '<code>' reads JSON on stdin as `d`

# before the first command touches anything: every place omp-memory would read or write must be inside the scratch directory
om config show --json | python3 -c '
import json, os, sys
settings = json.load(sys.stdin)["settings"]
root = os.path.realpath(sys.argv[1])
bad = []
for key in ("banksDir", "ledger", "backupsDir", "stateFile", "labelsFile"):
    value = settings[key]["value"]
    if not value or not os.path.realpath(value).startswith(root + os.sep):
        bad.append(key + "=" + str(value))
if settings["banksConf"]["value"] or (settings["libDir"]["value"] and not sys.argv[2]):
    bad.append("banksConf or libDir")
if bad:
    sys.exit("SMOKE FAILED: outside the scratch directory: " + ", ".join(bad))
' "$scratch" "$lib_dir" || exit 1
if ((isolation_only)); then echo "isolation ok"; exit 0; fi

if [[ -n "$model_cache" ]]; then
	step "reuse a copy of the embedding cache (read from $model_cache, never written there)"
	for dir in fastembed fastembed-runtime; do
		[[ -d "$model_cache/$dir" ]] && cp -R "$model_cache/$dir" "$HOME/.omp/cache/$dir"
	done
	ls "$HOME/.omp/cache"
fi

step "backup before any bank exists: nothing to back up, no error"
om backup --apply >"$scratch/backup0.out" 2>&1 || fail "backup --apply failed on a machine without banks: $(cat "$scratch/backup0.out")"
grep -q "nothing to back up" "$scratch/backup0.out" || fail "backup did not say there is nothing to back up: $(cat "$scratch/backup0.out")"
cut -c1-200 "$scratch/backup0.out"

step "omp-memory --version and doctor before setup (the library is not installed: doctor says so and names the command)"
om --version
om doctor >"$scratch/doctor0.txt" 2>&1 || true
grep -E '^(OK|WARN|FAIL) +(python|bun|omp|library) ' "$scratch/doctor0.txt" | cut -c1-170
if [[ -z "$lib_dir" ]]; then
	grep -q 'fix (library): omp-memory setup' "$scratch/doctor0.txt" || fail "doctor did not name the setup command"
fi

step "setup (installs @oh-my-pi/pi-mnemopi@$omp_version once; reused when --lib-dir was given)"
if [[ -z "$lib_dir" ]]; then
	om setup --dry-run 2>&1 | cut -c1-200
	[[ ! -e "$HOME/.cache/omp-memory" ]] || fail "setup --dry-run wrote something"
	om setup 2>"$scratch/setup.err" | py 'import json,sys; d=json.load(sys.stdin); print({k:d[k] for k in ("ok","action","embeddings","problems")}); sys.exit(0 if d["ok"] else 1)' || { cat "$scratch/setup.err" >&2; fail "setup"; }
	tail -3 "$scratch/setup.err" | cut -c1-200
else
	echo "reusing $lib_dir"
	om doctor --json | py 'import json,sys; c={x["name"]:x for x in json.load(sys.stdin)["checks"]}; print("before doctor --deep:", c["library"]["status"], c["library"]["detail"][-60:]); sys.exit(0 if c["library"]["status"]=="warn" else 1)' || fail "an unverified library must not be reported ok"
	om doctor --deep >"$scratch/deep.txt" 2>&1 || { cat "$scratch/deep.txt" >&2; fail "doctor --deep"; }
fi
om doctor --json | py 'import json,sys; d=json.load(sys.stdin); c={x["name"]:x for x in d["checks"]}; print("library:", c["library"]["status"], c["library"]["detail"][:150]); sys.exit(0 if c["library"]["status"]=="ok" else 1)' || fail "library not ok after setup"
om doctor --json | py 'import json,sys; d=json.load(sys.stdin); print("bun:", [c["detail"] for c in d["checks"] if c["name"]=="bun"][0][:120]); print("omp:", [c["detail"] for c in d["checks"] if c["name"]=="omp"][0][:120])'

step "synthetic folders, Claude Code memory and two banks made by the library"
real() { (cd "$1" && pwd -P); }
mkdir -p "$HOME/work/smoke-a" "$HOME/work/smoke-b" "$HOME/work/smoke-c"
a="$(real "$HOME/work/smoke-a")"
b="$(real "$HOME/work/smoke-b")"
c="$(real "$HOME/work/smoke-c")"
make_project() { # make_project <folder> <file> <text...>
	local folder="$1" file="$2"
	shift 2
	local enc="${folder//[^A-Za-z0-9]/-}"
	local dir="$HOME/.claude/projects/$enc"
	mkdir -p "$dir/memory"
	[[ -f "$dir/11111111-1111-4111-8111-111111111111.jsonl" ]] || printf '{"type":"user","cwd":"%s"}\n' "$folder" >"$dir/11111111-1111-4111-8111-111111111111.jsonl"
	printf -- '---\nname: %s\ndescription: smoke memory\ntype: project\n---\n\n%s\n' "${file%.md}" "$*" >"$dir/memory/$file"
}
make_project "$a" one.md "Smoke note one for folder A: the build uses make test."
make_project "$a" two.md "Smoke note two for folder A: prefer small commits."
make_project "$b" one.md "Smoke note for folder B: deploys go out on Tuesdays."
make_project "$b" secret.md "The staging admin password is Tr0ub4dor&3x9 for the box."
make_project "$c" one.md "Smoke note for folder C, whose bank already exists with native rows."
echo "wrote 5 synthetic memory files in 3 projects"
# folder C already has a bank, created by the library, with one native-looking row recorded under C
cat >"$scratch/make-bank.ts" <<EOF
import path from "node:path";
import { DEFAULT_EMBEDDING_MODEL, libraryOptions, requireLibrary } from "$here/vendor/memory_import/lib.ts";
const [banksDir, folder] = process.argv.slice(2) as [string, string];
const { lib } = await requireLibrary({ libDir: process.env.OMP_MEMORY_LIB_DIR || undefined, ompBin: process.env.OMP_BIN ?? "omp" });
const bank = lib.projectBankSegment(folder);
const memory = new lib.Mnemopi(libraryOptions(path.join(banksDir, bank, "mnemopi.db"), bank, DEFAULT_EMBEDDING_MODEL));
memory.db.run(
	"INSERT INTO working_memory (id, content, source, timestamp, session_id, importance, metadata_json, scope, veracity, memory_type, trust_tier) VALUES ('native1', 'A native note omp wrote itself.', 'coding-agent-retain', '2020-01-01T00:00:00.000Z', ?, 0.75, ?, 'bank', 'tool', 'fact', 'STATED')",
	[bank, JSON.stringify({ tool: "retain", cwd: folder })],
);
memory.close();
console.log(bank);
EOF
bun_bin="$(python3 -B -c "import sys; sys.path.insert(0, sys.argv[1]); from omp_memory import config, runtime; print(runtime.find_bun(config.Config()).path)" "$here/python")"
bank_c="$("$bun_bin" "$scratch/make-bank.ts" "$banks_dir" "$c")"
echo "bank for folder C created by the library: ${bank_c:0:30}"
before_native="$(sqlite3 -readonly "file:$banks_dir/$bank_c/mnemopi.db?mode=ro" "select id, content from working_memory order by id" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])')"

step "banks, sources, groups"
om banks | cut -c1-170
om sources | cut -c1-170
[[ "$(om banks --json | py 'import json,sys; d=json.load(sys.stdin); print({b["folder"]["group"] for b in d["banks"]}.pop())')" == "work" ]] || fail "default group is not the first directory below home"
om config set-group Smoke work/smoke-c >/dev/null
[[ "$(om banks --json | py 'import json,sys; d=json.load(sys.stdin); print({b["folder"]["group"] for b in d["banks"]}.pop())')" == "Smoke" ]] || fail "group from config was not applied"
om config unset-group Smoke >/dev/null

summ() { py 'import json,sys; d=json.load(sys.stdin); c=d["counts"]; print({k:c[k] for k in ("found","imported","skippedDuplicate","skippedUnchanged","skippedHeld","errors")}); print("perBank:", [(b["bank"][:26], b.get("imported"), b.get("wouldImport")) for b in d["perBank"]]); print("held:", [(h["originKey"].split("#")[-1], h["kinds"]) for h in d.get("held", [])]); print("runId:", d["runId"])'; }

step "import --apply is refused without a dry run and a backup"
if om import import --source claude-memory --apply 2>"$scratch/refused.err"; then fail "an apply without a dry run was allowed"; fi
grep -q "no dry run with these exact options" "$scratch/refused.err" || fail "wrong refusal: $(cat "$scratch/refused.err")"
cut -c1-220 "$scratch/refused.err"

step "import dry-run (recorded), then still refused without a backup"
om import dry-run --source claude-memory 2>"$scratch/dry.err" | tee "$scratch/dry.json" | summ
py 'import json,sys; d=json.load(open(sys.argv[1])); c=d["counts"]; assert c["imported"]==0 and d["apply"] is False, "a dry run must write nothing"; assert c["skippedHeld"]==1, c; assert c["found"]==5, c' "$scratch/dry.json" || fail "dry-run expectations"
if om import import --source claude-memory --apply 2>"$scratch/refused2.err"; then fail "an apply without a backup was allowed"; fi
grep -q "no verified backup" "$scratch/refused2.err" || fail "wrong refusal: $(cat "$scratch/refused2.err")"
cut -c1-220 "$scratch/refused2.err"
before_banks="$(find "$banks_dir" -mindepth 1 -maxdepth 1 -type d | sort | wc -l | tr -d ' ')"

step "backup --apply (verified, recorded)"
om backup --apply 2>"$scratch/backup.err" | tail -2
tail -1 "$scratch/backup.err" | cut -c1-200

step "import import --apply (embeds with the library's own model; the first run may download it)"
om import import --source claude-memory --apply 2>"$scratch/apply.err" | tee "$scratch/apply.json" | summ
tail -3 "$scratch/apply.err" | cut -c1-200
run_id="$(py 'import json,sys; print(json.load(open(sys.argv[1]))["runId"])' "$scratch/apply.json")"
py 'import json,sys; d=json.load(open(sys.argv[1])); c=d["counts"]; assert c["imported"]==4 and c["errors"]==0, c' "$scratch/apply.json" || fail "apply expectations (4 imported, 1 held)"
echo "banks: $before_banks -> $(find "$banks_dir" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"

step "import verify --run $run_id"
om import verify --source claude-memory --run "$run_id" | py 'import json,sys; d=json.load(sys.stdin); print("ok:", d["ok"], "problems:", d.get("problems")); sys.exit(0 if d["ok"] else 1)' || fail "verify"

step "re-running the same apply is idempotent"
om import dry-run --source claude-memory >/dev/null 2>&1
om import import --source claude-memory --apply 2>/dev/null | tee "$scratch/apply2.json" | summ | head -1
py 'import json,sys; c=json.load(open(sys.argv[1]))["counts"]; assert c["imported"]==0 and c["skippedUnchanged"]==4, c' "$scratch/apply2.json" || fail "a second apply was not idempotent"

step "import undo --run $run_id (report, then --apply); the native row of the existing bank must be untouched"
om import undo --run "$run_id" 2>&1 | tail -3 | cut -c1-200
om import undo --run "$run_id" --apply 2>"$scratch/undo.err" | py 'import json,sys; d=json.load(sys.stdin); print({k:d[k] for k in d if k in ("removed","problems")}); sys.exit(1 if d["problems"] else 0)' || fail "undo"
total_rows="$(for d in "$banks_dir"/*/; do sqlite3 -readonly "file:${d}mnemopi.db?mode=ro" 'select count(*) from working_memory'; done | paste -sd+ - | python3 -c 'print(eval(input()))')"
[[ "$total_rows" == "1" ]] || fail "after undo the banks hold $total_rows rows, expected only the native one"
after_native="$(sqlite3 -readonly "file:$banks_dir/$bank_c/mnemopi.db?mode=ro" "select id, content from working_memory order by id" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])')"
[[ "$before_native" == "$after_native" ]] || fail "the native row changed"
echo "rows after undo: $total_rows (the native row), unchanged"

step "after the undo the banks the import created are new again (--only-new-banks), and the bank omp made is not"
om import dry-run --source claude-memory --only-new-banks >"$scratch/dry-new.json" 2>/dev/null
py 'import json,sys; d=json.load(open(sys.argv[1])); c=d["counts"]; print({k:c[k] for k in ("found","wouldImport","skippedNotTargeted","skippedHeld")}); assert c["wouldImport"]==3 and c["skippedNotTargeted"]==1, c' "$scratch/dry-new.json" || fail "banks created by the import were not new after the undo"

step "a binary file and an oversize file are skipped by key, with a reason, and are not errors"
mkdir -p "$HOME/work/smoke-d"
d="$(real "$HOME/work/smoke-d")"
make_project "$d" good.md "A readable note for folder D."
dmem="$HOME/.claude/projects/${d//[^A-Za-z0-9]/-}/memory"
head -c 4000 /dev/urandom >"$dmem/random.md"
printf -- '---\nname: big\n---\n\n' >"$dmem/big.md"
python3 -c 'import sys; sys.stdout.write("x" * 1000100)' >>"$dmem/big.md"
om import dry-run --source claude-memory --only-new-banks 2>"$scratch/dry-bad.err" >"$scratch/dry-bad.json" || fail "dry-run with binary and oversize files"
py 'import json,sys; d=json.load(open(sys.argv[1])); c=d["counts"]; print({k:c[k] for k in ("found","wouldImport","skippedBinary","skippedTooLarge","errors")}); print("tooLarge:", d["tooLarge"][0]["reason"]); assert c["skippedBinary"]==1 and c["skippedTooLarge"]==1 and c["errors"]==0, c; assert "1,000,000" in d["tooLarge"][0]["reason"]' "$scratch/dry-bad.json" || fail "binary or oversize file not skipped"
grep -q "skipped (binary)" "$scratch/dry-bad.err" || fail "the binary file was not listed"
grep -q "skipped (too large)" "$scratch/dry-bad.err" || fail "the oversize file was not listed"
cut -c1-200 "$scratch/dry-bad.err"

step "restore-drill on the backup"
om restore-drill 2>&1 | tail -4

step "nothing outside the scratch home was the target"
[[ "$HOME" == "$scratch/home" && "$real_home" != "$HOME" ]] || fail "home was not scratch"
echo "SMOKE OK"
