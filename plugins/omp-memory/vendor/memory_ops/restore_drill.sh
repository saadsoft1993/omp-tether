#!/bin/bash
# Restore drill: prove a backup made by backup_mnemopi.sh can be restored and matches the live banks.
#
#   restore_drill.sh --backup DIR/<stamp> [--src BANKS_DIR] [--scratch DIR] [--keep] [--strict]
#
# 1. verifies SHA256SUMS of the backup,
# 2. restores every <bank>.db into <work>/banks/<bank>/mnemopi.db (the layout omp reads), and the copy of the
#    database that sits next to the banks directory into <work>/mnemopi.db,
# 3. on each restored copy: PRAGMA integrity_check and, for every FTS5 table, the FTS5 'integrity-check' command,
# 4. compares per-table row counts (every non-internal table, FTS shadow tables included) with the backup file
#    itself (any difference is a failure) and with a fresh read-only snapshot of the live source bank.
# Live banks keep changing while sessions run, so a source difference is reported as DRIFT: it is only a failure
# with --strict (use that after the overlay sessions are ended, i.e. at the final verify). A bank that exists
# in the source but not in the backup (or the reverse) is DRIFT too.
# The source is only read (read-only connection + VACUUM INTO to scratch); the backup is only read (read-only,
# immutable connections); only the disposable copies in <work> are ever opened for writing.
# <work> is always a new private directory made by this run, inside --scratch DIR (or inside $TMPDIR when there is
# no --scratch). It is deleted at the end unless --keep, and nothing else is ever deleted. DIR must be a separate
# directory: one that equals, contains or lies inside the source banks directory or the backup directory (after
# resolving symlinks) is refused.
# Which live database a backup file is compared with: <bank>.db with the bank under BANKS_DIR/<bank>, and the
# reserved name _parent-mnemopi.db with BANKS_DIR/../mnemopi.db. The backup's MANIFEST.tsv records the kind of each
# copy (sixth column: bank or parent). Older backups have no such column and named the parent database default.db:
# a default.db without that column is the parent database, unless its recorded source is a .../default/mnemopi.db.
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

# Backup file name (without .db) of the database next to the banks directory. Keep in sync with backup_mnemopi.sh.
PARENT_NAME="_parent-mnemopi"

src="$HOME/.omp/agent/memories/mnemopi/banks"
backup=""
scratch=""
keep=0
strict=0
while (($#)); do
	case "$1" in
	--backup) backup="${2:?--backup needs a directory}"; shift 2 ;;
	--src) src="${2:?--src needs a directory}"; shift 2 ;;
	--scratch) scratch="${2:?--scratch needs a directory}"; shift 2 ;;
	--keep) keep=1; shift ;;
	--strict) strict=1; shift ;;
	-h | --help) sed -n '2,25p' "$0"; exit 0 ;;
	*) shown name "$1"; echo "unknown argument: $shown_text" >&2; exit 2 ;;
	esac
done
[[ -d "$backup" ]] || { echo "--backup must be a backup directory (the <stamp> one)" >&2; exit 2; }
[[ -f "$backup/SHA256SUMS" ]] || { shown folder "$backup"; echo "no SHA256SUMS in $shown_text" >&2; exit 2; }
[[ -d "$src" ]] || { shown folder "$src"; echo "no banks directory: $shown_text" >&2; exit 2; }
command -v sqlite3 >/dev/null || { echo "sqlite3 not found" >&2; exit 2; }

# --- helpers: paths, SQL text, URIs -------------------------------------------------------------------------------
resolve_path() { # absolute path with symlinks resolved; a part that does not exist yet is appended as written
	local p="$1" tail="" r
	[[ "$p" == /* ]] || p="$PWD/$p"
	while [[ "$p" == */ && "$p" != / ]]; do p="${p%/}"; done
	while [[ ! -e "$p" && "$p" != / ]]; do
		tail="/${p##*/}$tail"
		p="${p%/*}"
		[[ -n "$p" ]] || p=/
	done
	case "$tail" in */.. | */../* | */. | */./*) return 1 ;; esac
	p="$(cd -P -- "$p" 2>/dev/null && pwd -P)" || return 1
	r="${p%/}$tail"
	printf '%s' "${r:-/}"
}
inside() { # inside A B: path A equals B or lies under B (both resolved)
	[[ "$1" == "$2" || "$2" == / || "$1" == "${2%/}"/* ]]
}
overlaps() { inside "$1" "$2" || inside "$2" "$1"; }
sql_ident() { local q='"'; printf '"%s"' "${1//$q/$q$q}"; } # "name" with embedded double quotes doubled
sql_lit() { local q="'"; printf "'%s'" "${1//$q/$q$q}"; }   # 'text' with embedded apostrophes doubled
uri_path() { # percent-encode a file path for use inside a file: URI (everything but unreserved characters and /)
	local s="$1" out="" c i
	for ((i = 0; i < ${#s}; i++)); do
		c="${s:i:1}"
		case "$c" in
		[A-Za-z0-9/._~-]) out+="$c" ;;
		*) out+="$(printf '%%%02X' "'$c")" ;;
		esac
	done
	printf '%s' "$out"
}
ro_uri() { printf 'file:%s?immutable=1' "$(uri_path "$1")"; } # for files nobody has open: never written, no -shm/-wal needed

real_src="$(resolve_path "$src")" || { shown folder "$src"; echo "cannot resolve --src $shown_text" >&2; exit 2; }
real_backup="$(resolve_path "$backup")" || { shown folder "$backup"; echo "cannot resolve --backup $shown_text" >&2; exit 2; }

if [[ -n "$scratch" ]]; then
	parent="$(resolve_path "$scratch")" || { shown folder "$scratch"; echo "--scratch $shown_text cannot be resolved (is a part of it a file, or does it contain ..?)" >&2; exit 2; }
	for guarded in "$real_src" "$real_backup"; do
		if overlaps "$parent" "$guarded"; then
			shown folder "$scratch"
			scratch_label="$shown_text"
			shown folder "$guarded"
			echo "--scratch $scratch_label overlaps $shown_text: the restore must not share a directory with live data or the backup; use a separate directory" >&2
			exit 2
		fi
	done
else
	parent="$(resolve_path "${TMPDIR:-/tmp}")" || { shown folder "${TMPDIR:-/tmp}"; echo "cannot resolve the temporary directory $shown_text" >&2; exit 2; }
	for guarded in "$real_src" "$real_backup"; do
		if inside "$parent" "$guarded"; then
			shown folder "$parent"
			scratch_label="$shown_text"
			shown folder "$guarded"
			echo "the temporary directory $scratch_label lies inside $shown_text: pass --scratch with a separate directory" >&2
			exit 2
		fi
	done
fi

fail=0
drift=0

echo "== checksums"
(cd "$backup" && if command -v shasum >/dev/null 2>&1; then shasum -a 256 -c SHA256SUMS; else sha256sum -c SHA256SUMS; fi >/dev/null) || { shown folder "$backup"; echo "FAIL checksum mismatch in $shown_text" >&2; exit 1; }
echo "ok"

# The only directory this run may write to or delete: a new private directory made by mktemp.
mkdir -p "$parent"
parent="$(cd -P -- "$parent" && pwd -P)"
work="$(mktemp -d "${parent%/}/memops-restore.XXXXXX")"
[[ -d "$work" && ! -L "$work" ]] || { shown folder "$parent"; echo "could not create a scratch directory in $shown_text" >&2; exit 2; }
cleanup() {
	if ((keep)); then shown folder "$work"; echo "scratch kept: $shown_text"; else rm -rf "$work"; fi
}
trap cleanup EXIT
mkdir "$work/banks" "$work/snap"

vacuum_into() { # read-only VACUUM INTO; immutable only when no -wal exists (see backup_mnemopi.sh)
	local uri
	uri="$(uri_path "$1")"
	sqlite3 -readonly "file:$uri?mode=ro" "VACUUM INTO $(sql_lit "$2")" 2>/dev/null && return 0
	[[ ! -s "$1-wal" ]] || return 1
	rm -f "$2"
	sqlite3 -readonly "file:$uri?immutable=1" "VACUUM INTO $(sql_lit "$2")"
}
counts() { # counts <db>: "table<TAB>rows" lines; read-only. The statements are built by SQLite itself (%w doubles quotes).
	local db="$1" script
	script="$(sqlite3 -readonly "$(ro_uri "$db")" "SELECT group_concat(printf('SELECT %Q || char(9) || count(*) FROM \"%w\";', name, name), ' ') FROM (SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name)")"
	[[ -z "$script" ]] || sqlite3 -readonly "$(ro_uri "$db")" "$script"
}
fts_check() { # fts_check <db>: prints failures, returns 1 if any. Writes, so <db> must be a disposable copy.
	local db="$1" t n i bad=0 where="type='table' AND sql LIKE '%fts5%'"
	n="$(sqlite3 "$db" "SELECT count(*) FROM sqlite_master WHERE $where")"
	for ((i = 0; i < n; i++)); do
		t="$(sqlite3 "$db" "SELECT name FROM sqlite_master WHERE $where ORDER BY name LIMIT 1 OFFSET $i")"
		if ! sqlite3 "$db" "INSERT INTO $(sql_ident "$t")($(sql_ident "$t")) VALUES('integrity-check')" 2>/dev/null; then
			shown name "$t"
			echo "    FTS integrity-check failed: $shown_text"
			bad=1
		fi
	done
	return "$bad"
}
default_kind() { # default_kind: what the backup's default.db is: bank (BANKS_DIR/default) or parent (BANKS_DIR/../mnemopi.db)
	local row kind source
	row="$(NAME=default awk -F '\t' 'NR > 1 && $1 == ENVIRON["NAME"] { print $6 "\t" $2; exit }' "$backup/MANIFEST.tsv" 2>/dev/null)" || row=""
	kind="${row%%$'\t'*}"
	source="${row#*$'\t'}"
	case "$kind" in
	bank | parent) echo "$kind" ;;
	*) if [[ "$source" == */default/mnemopi.db ]]; then echo bank; else echo parent; fi ;; # older backup: see the header
	esac
}

echo "== restore + compare"
printf '%-34s %6s %-9s %-9s %s\n' bank tables integrity fts "vs-backup / vs-source"
restored=" "
for file in "$backup"/*.db; do
	[[ -e "$file" ]] || { echo "FAIL no .db files in backup" >&2; exit 1; }
	bank="$(basename "$file" .db)"
	if [[ ! "$bank" =~ ^[A-Za-z0-9._-]+$ || "$bank" == . || "$bank" == .. ]]; then
		shown bank "$bank"
		echo "FAIL not a usable bank name in the backup: $shown_text (letters, digits, . _ - only)" >&2
		fail=1
		continue
	fi
	kind=bank
	if [[ "$bank" == "$PARENT_NAME" ]]; then kind=parent; elif [[ "$bank" == default ]]; then kind="$(default_kind)"; fi
	if [[ "$kind" == parent ]]; then
		db="$work/mnemopi.db"
		live="$(dirname "$src")/mnemopi.db"
	else
		restored+="$bank "
		mkdir -p "$work/banks/$bank"
		db="$work/banks/$bank/mnemopi.db"
		live="$src/$bank/mnemopi.db"
	fi
	cp "$file" "$db"
	integ="$(sqlite3 "$db" 'PRAGMA integrity_check' | tr '\n' ' ' | sed 's/ $//')"
	ftsres=ok
	fts_check "$db" >"$work/fts.out" || ftsres=FAIL
	[[ "$integ" == ok && "$ftsres" == ok ]] || { fail=1; cat "$work/fts.out"; }
	counts "$db" >"$work/restored.tsv"
	counts "$file" >"$work/backup.tsv"
	vsb=same
	cmp -s "$work/restored.tsv" "$work/backup.tsv" || { vsb=DIFF; fail=1; }
	vss=same
	if [[ -f "$live" ]]; then
		snap="$work/snap/$bank.db"
		rm -f "$snap"
		vacuum_into "$live" "$snap"
		counts "$snap" >"$work/source.tsv"
		if ! cmp -s "$work/restored.tsv" "$work/source.tsv"; then
			vss=DRIFT
			drift=1
			diff "$work/restored.tsv" "$work/source.tsv" | grep '^[<>]' | sed 's/^/    /' | head -6 || true
		fi
		fts_check "$snap" >"$work/fts.out" || { vss="$vss+SRC-FTS-FAIL"; fail=1; cat "$work/fts.out"; }
		rm -f "$snap"
	else
		vss=MISSING-IN-SOURCE
		drift=1
	fi
	shown bank "$bank"
	printf '%-34s %6s %-9s %-9s %s / %s\n' "$shown_text" "$(wc -l <"$work/restored.tsv" | tr -d ' ')" "${integ:0:9}" "$ftsres" "$vsb" "$vss"
done
for dir in "$src"/*/; do
	[[ -f "${dir}mnemopi.db" ]] || continue
	b="$(basename "$dir")"
	[[ "$restored" == *" $b "* ]] || { shown bank "$b"; echo "DRIFT: bank $shown_text exists in source but not in the backup"; drift=1; }
done

if ((fail)); then echo "RESULT: FAIL"; exit 1; fi
if ((drift && strict)); then echo "RESULT: FAIL (drift, --strict)"; exit 1; fi
if ((drift)); then echo "RESULT: OK, restored copies are intact; source drift since the backup (live banks changed)"; else echo "RESULT: OK, identical to source"; fi
