#!/bin/bash
# Backup every mnemopi bank database with VACUUM INTO (a consistent, WAL-merged copy made through SQLite; a plain
# cp of a live WAL database can miss committed pages or tear). The source is only read: the connection is opened
# read-only and nothing under the banks directory is written.
#
#   backup_mnemopi.sh --dest DIR [--src BANKS_DIR]
#
# Writes DIR/<UTC yyyymmddThhmmssZ>/<bank>.db (0600, dir 0700) plus:
#   SHA256SUMS    `sha256sum -c` / `shasum -a 256 -c` format, one line per copy
#   MANIFEST.tsv  bank (the file name without .db), source file, bytes, seconds, integrity_check result, kind
# Runs on macOS and Linux (BSD and GNU userland): it needs bash, sqlite3 (3.27 or newer for VACUUM INTO) and
# either shasum or sha256sum; it does not need Python.
# `--src` defaults to ~/.omp/agent/memories/mnemopi/banks. Every <src>/<bank>/mnemopi.db becomes <bank>.db (kind
# `bank`; a bank name is letters, digits, `.`, `_` and `-`, anything else fails), so the importer's global bank,
# <src>/default/mnemopi.db, is default.db. A <src>/../mnemopi.db (the older global database next to the banks
# directory) is a different database and is stored as _parent-mnemopi.db (kind `parent`); restore_drill.sh restores
# it to its own place. Backups made before this naming stored that parent database as default.db: restore_drill.sh
# still reads those. A copy never replaces one an earlier step of the same run wrote: a name clash fails the run.
# The last stdout line is `BACKUP_DIR=<path>`. Exit 1 if any copy fails its integrity_check or cannot be made. The
# caller chooses --dest (the cutover uses ~/Backups/omp-memory); an existing --dest keeps its mode, a directory
# this run creates is made 0700.
# With no bank database at all (a machine where omp has not written memory yet) there is nothing to back up: it says so on
# stderr, creates nothing, prints no BACKUP_DIR line and exits 0.
set -euo pipefail
umask 077
export LC_ALL=C # bytewise string handling: uri_path below encodes byte by byte

# --- names in messages ---------------------------------------------------------------------------------------------
# A bank name, a folder or a path is not secret by intent, but a PIN assignment (`password=1234`) or an API key pasted into
# one must not be echoed into a terminal or a log. `shown KIND TEXT` leaves the answer in $shown_text: TEXT with control
# characters made `?`, or, when TEXT carries a secret, an index label (`bank#1`, `folder#2`) numbered by first appearance in
# this run, with nothing of the value in it. TEXT is judged as written and percent-decoded (twice at most), in lower case, by
# token prefixes (sk-, ghp_, github_pat_, glpat-, xox, AKIA), URL credentials, private-key headers, `Bearer` tokens and a key
# word (password, token, secret, api key, credential, auth ...) followed by a value. This is the short form of the rule in
# python/omp_memory/detect.py, which also filters everything the omp-memory command prints. Keep the copies in the three
# scripts in step.
shown_kinds=()
shown_values=()
shown_text=""
value_in_name() { # three or more characters with a digit, or four or more with a capital after the first letter
	local v="$1"
	if ((${#v} >= 3)) && [[ "$v" =~ [0-9] ]]; then return 0; fi
	if ((${#v} >= 4)) && [[ "$v" =~ [a-z] && "${v:1}" =~ [A-Z] ]]; then return 0; fi
	return 1
}
secret_form() { # secret_form TEXT: succeeds when TEXT carries a secret as written
	local s="$1" lower i n w nxt k rest
	local prefix='(^|[^a-z0-9])(sk-[a-z0-9_-]{10,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|glpat-[a-z0-9_-]{10,}|xox[bp]-[a-z0-9-]{10,}|akia[a-z0-9]{12,})'
	local url='://[^/@:[:space:]]+:[^/@[:space:]]{3,}@'
	local bearer='bearer[[:space:]]+[a-z0-9._~+/=-]{20,}'
	local -a orig low
	lower="$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]')"
	[[ "$lower" =~ $prefix || "$s" =~ $url || "$lower" =~ $bearer || "$lower" == *"-----begin"*"private key"* ]] && return 0
	set -f
	# shellcheck disable=SC2206  # word splitting on purpose: the names were made of letters and digits and spaces just above
	orig=(${s//[^A-Za-z0-9]/ })
	# shellcheck disable=SC2206
	low=(${lower//[^a-z0-9]/ })
	set +f
	n=${#low[@]}
	for ((i = 0; i < n; i++)); do
		w="${low[i]}"
		nxt=$((i + 1))
		if [[ "$w" == api && "${low[nxt]:-}" == key ]]; then
			w=apikey
			nxt=$((nxt + 1))
		fi
		case "$w" in
		password | passwd | pwd | passphrase | secret | token | apikey | credential | credentials | auth)
			case "${low[nxt]:-}" in is | are | was | equals | eq) nxt=$((nxt + 1)) ;; esac
			if value_in_name "${orig[nxt]:-}"; then return 0; fi
			;;
		*)
			for k in password passwd pwd passphrase secret token apikey credentials credential auth; do
				if [[ "$w" == "$k"* ]]; then
					rest="${w#"$k"}"
					if [[ "$rest" =~ ^[0-9]{3,}$ ]]; then return 0; fi
					break
				fi
			done
			;;
		esac
	done
	return 1
}
url_decoded() { local s="${1//\\/\\\\}"; printf '%b' "${s//%/\\x}"; }
shown() { # shown KIND TEXT
	local kind="$1" text dec1 dec2 i n=0
	text="$(printf '%s' "$2" | tr '\000-\037\177' '?')"
	dec1="$(url_decoded "$text")"
	dec2="$(url_decoded "$dec1")"
	if secret_form "$text" || secret_form "$dec1" || secret_form "$dec2"; then
		for ((i = 0; i < ${#shown_kinds[@]}; i++)); do
			if [[ "${shown_kinds[i]}" == "$kind" ]]; then
				n=$((n + 1))
				if [[ "${shown_values[i]}" == "$text" ]]; then
					shown_text="$kind#$n"
					return 0
				fi
			fi
		done
		shown_kinds+=("$kind")
		shown_values+=("$text")
		shown_text="$kind#$((n + 1))"
	else
		shown_text="$text"
	fi
}

# File name (without .db) of the database next to the banks directory. Keep in sync with restore_drill.sh.
PARENT_NAME="_parent-mnemopi"

src="$HOME/.omp/agent/memories/mnemopi/banks"
dest=""
while (($#)); do
	case "$1" in
	--dest) dest="${2:?--dest needs a directory}"; shift 2 ;;
	--src) src="${2:?--src needs a directory}"; shift 2 ;;
	-h | --help) sed -n '2,23p' "$0"; exit 0 ;;
	*) shown name "$1"; echo "unknown argument: $shown_text" >&2; exit 2 ;;
	esac
done
[[ -n "$dest" ]] || { echo "--dest is required" >&2; exit 2; }
command -v sqlite3 >/dev/null || { echo "sqlite3 not found" >&2; exit 2; }
# Nothing to back up: no banks directory (or an empty one) and no global bank next to it.
have_banks=0
for dir in "$src"/*/; do [[ -f "${dir}mnemopi.db" ]] && have_banks=1; done
if [[ "$have_banks" == 0 && ! -f "$(dirname "$src")/mnemopi.db" ]]; then
	shown folder "$src"
	echo "nothing to back up: no bank database under $shown_text" >&2
	exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dest/$stamp"
[[ -d "$dest" ]] || { mkdir -p "$dest" && chmod 700 "$dest"; }
mkdir "$out"
printf 'bank\tsource\tbytes\tseconds\tintegrity\tkind\n' >"$out/MANIFEST.tsv"

# SQL text and URIs built from names that come from the file system.
sql_lit() { local q="'"; printf "'%s'" "${1//$q/$q$q}"; } # 'text' with embedded apostrophes doubled
uri_path() { # percent-encode a file path for use inside a file: URI (everything but unreserved characters and /)
	local s="$1" enc="" c i
	for ((i = 0; i < ${#s}; i++)); do
		c="${s:i:1}"
		case "$c" in
		[A-Za-z0-9/._~-]) enc+="$c" ;;
		*) enc+="$(printf '%%%02X' "'$c")" ;;
		esac
	done
	printf '%s' "$enc"
}

# Read-only VACUUM INTO. A WAL database with no -shm/-wal files (nobody has it open) cannot be opened mode=ro;
# with no -wal there is nothing to merge, so immutable=1 reads the main file safely (never used when a -wal exists).
vacuum_into() { # vacuum_into <source db> <target file>
	local uri
	uri="$(uri_path "$1")"
	sqlite3 -readonly "file:$uri?mode=ro" "VACUUM INTO $(sql_lit "$2")" 2>"$2.err" && { rm -f "$2.err"; return 0; }
	if [[ ! -s "$1-wal" ]]; then
		rm -f "$2"
		sqlite3 -readonly "file:$uri?immutable=1" "VACUUM INTO $(sql_lit "$2")" 2>"$2.err" && { rm -f "$2.err"; return 0; }
	fi
	return 1
}

now() { date +%s; }
file_size() { wc -c <"$1" | tr -d ' '; }
sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }

failed=0
total_bytes=0
count=0
t_all="$(now)"
backup_one() { # backup_one <file name without .db> <source db> <kind: bank|parent>
	local name="$1" db="$2" kind="$3" t0 t1 bytes check label
	shown bank "$name"
	label="$shown_text"
	if [[ ! "$name" =~ ^[A-Za-z0-9._-]+$ || "$name" == . || "$name" == .. ]]; then
		echo "FAIL  $label: not a usable bank name (letters, digits, . _ - only); not backed up" >&2
		failed=1
		return
	fi
	if [[ -e "$out/$name.db" ]]; then
		echo "FAIL  $label: its backup file was already written by an earlier copy of this run; not replacing it" >&2
		failed=1
		return
	fi
	t0="$(now)"
	if ! vacuum_into "$db" "$out/$name.db"; then
		shown name "$(head -c 200 "$out/$name.db.err")"
		echo "FAIL  $label: VACUUM INTO: $shown_text" >&2
		rm -f "$out/$name.db" "$out/$name.db.err"
		failed=1
		return
	fi
	t1="$(now)"
	check="$(sqlite3 -readonly "file:$(uri_path "$out/$name.db")?immutable=1" 'PRAGMA integrity_check' | tr '\n' ' ' | sed 's/ $//')"
	bytes="$(file_size "$out/$name.db")"
	[[ "$check" == ok ]] || { echo "FAIL  $label: integrity_check: ${check:0:200}" >&2; failed=1; }
	printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$db" "$bytes" "$((t1 - t0))" "$check" "$kind" >>"$out/MANIFEST.tsv"
	total_bytes=$((total_bytes + bytes))
	count=$((count + 1))
}

for dir in "$src"/*/; do
	[[ -f "${dir}mnemopi.db" ]] || continue
	backup_one "$(basename "$dir")" "${dir}mnemopi.db" bank
done
global="$(dirname "$src")/mnemopi.db"
[[ ! -f "$global" ]] || backup_one "$PARENT_NAME" "$global" parent

(cd "$out" && sha256_of -- *.db >SHA256SUMS)
chmod 600 "$out"/*
echo "databases: $count, bytes: $total_bytes, seconds: $(($(now) - t_all))"
echo "BACKUP_DIR=$out"
exit "$failed"
