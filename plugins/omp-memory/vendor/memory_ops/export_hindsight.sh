#!/bin/bash
# OPTIONAL. Only for people who keep memories in a Hindsight server and want them imported into omp's local banks.
# Nothing else in omp-memory runs or needs this script. It never writes to Hindsight (it only reads) and it has no
# defaults: which banks, which server, where to write are all given by flags or environment variables.
#
#   export_hindsight.sh --banks "a b" --out DIR (--overlay-dir DIR | --api-url URL --token-file FILE)
#                       [--mode pages|bank|both] [--ssh-host HOST --remote-export-cmd CMD]
#                       [--timeout SECONDS] [--nice N] [--dry-run]
#
# Flag                    Environment variable                      Meaning
#   --banks LIST          OMP_MEMORY_HINDSIGHT_BANKS                Hindsight bank names, space or comma separated (required)
#   --out DIR             OMP_MEMORY_HINDSIGHT_OUT                  where the export files go (required; made 0700 if new, an existing one keeps its mode)
#   --overlay-dir DIR     OMP_MEMORY_HINDSIGHT_OVERLAY_DIR          directory of <bank>.yml overlay files (api url + token per bank)
#   --api-url URL         OMP_MEMORY_HINDSIGHT_API_URL              one bank's api url instead of overlays (https; http only for localhost)
#   --token-file FILE     OMP_MEMORY_HINDSIGHT_TOKEN_FILE           file holding that bank's token (never printed)
#   --mode M              (none)                                    pages (default), bank or both
#   --ssh-host HOST       OMP_MEMORY_HINDSIGHT_SSH_HOST             host that runs the Hindsight admin tool (modes bank and both)
#   --remote-export-cmd C OMP_MEMORY_HINDSIGHT_REMOTE_EXPORT_CMD    command run on that host to write one bank's archive;
#                                                                   `{bank}` and `{out}` are replaced (out = a path relative to
#                                                                   the remote home that the command must write)
#   --timeout S --nice N  (none)                                    limits for the remote command (default 900 s, nice 10)
#   --dry-run             (none)                                    print the plan and the arguments, touch nothing
#
# Two exports per bank, one bank at a time:
#   pages (default): hindsight_pages.py pages GET memories/list into <bank>-<stamp>.jsonl + .meta.json and, when every
#      bank succeeded, hindsight-export-<stamp>.json: that file is what `omp-memory import --source hindsight` reads.
#   bank: the remote command (an archive made by your Hindsight admin tool, run over ssh), copied here as
#      export-<bank>-<stamp>.zip with a sha256 file. It is a restorable backup; it has no memory ids, so the importer
#      cannot use it. Needs ssh, scp and unzip.
# Exit 1 if either part fails for any bank; exit 2 for bad arguments.
set -euo pipefail
umask 077

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

out="${OMP_MEMORY_HINDSIGHT_OUT:-}"
banks="${OMP_MEMORY_HINDSIGHT_BANKS:-}"
overlay_dir="${OMP_MEMORY_HINDSIGHT_OVERLAY_DIR:-}"
api_url="${OMP_MEMORY_HINDSIGHT_API_URL:-}"
token_file="${OMP_MEMORY_HINDSIGHT_TOKEN_FILE:-}"
host="${OMP_MEMORY_HINDSIGHT_SSH_HOST:-}"
remote_cmd="${OMP_MEMORY_HINDSIGHT_REMOTE_EXPORT_CMD:-}"
mode=pages
tmo=900
nice_n=10
dry=0
while (($#)); do
	case "$1" in
	--out) out="${2:?--out needs a directory}"; shift 2 ;;
	--banks) banks="${2:?--banks needs a list}"; shift 2 ;;
	--overlay-dir) overlay_dir="${2:?--overlay-dir needs a directory}"; shift 2 ;;
	--api-url) api_url="${2:?--api-url needs a url}"; shift 2 ;;
	--token-file) token_file="${2:?--token-file needs a file}"; shift 2 ;;
	--mode) mode="${2:?--mode needs both|bank|pages}"; shift 2 ;;
	--ssh-host) host="${2:?--ssh-host needs a host}"; shift 2 ;;
	--remote-export-cmd) remote_cmd="${2:?--remote-export-cmd needs a command}"; shift 2 ;;
	--timeout) tmo="${2:?--timeout needs seconds}"; shift 2 ;;
	--nice) nice_n="${2:?--nice needs a number}"; shift 2 ;;
	--dry-run) dry=1; shift ;;
	-h | --help) sed -n '2,30p' "$0"; exit 0 ;;
	*) shown name "$1"; echo "unknown argument: $shown_text" >&2; exit 2 ;;
	esac
done
banks="${banks//,/ }"
case "$mode" in both | bank | pages) ;; *) echo "--mode must be both, bank or pages" >&2; exit 2 ;; esac
[[ "$tmo" =~ ^[0-9]+$ && "$nice_n" =~ ^[0-9]+$ ]] || { echo "--timeout and --nice must be integers" >&2; exit 2; }
[[ -n "${banks// /}" ]] || { echo "--banks is required (or OMP_MEMORY_HINDSIGHT_BANKS): there is no default" >&2; exit 2; }
[[ -n "$out" ]] || { echo "--out is required (or OMP_MEMORY_HINDSIGHT_OUT): there is no default" >&2; exit 2; }
for b in $banks; do [[ "$b" =~ ^[A-Za-z0-9_.-]{1,80}$ ]] || { shown bank "$b"; echo "bad bank name: $shown_text" >&2; exit 2; }; done
if [[ "$mode" != bank ]]; then
	[[ -n "$overlay_dir" || -n "$api_url" ]] || { echo "pages mode needs --overlay-dir, or --api-url with --token-file" >&2; exit 2; }
fi
if [[ "$mode" != pages ]]; then
	[[ -n "$host" && -n "$remote_cmd" ]] || { echo "modes bank and both need --ssh-host and --remote-export-cmd: there is no default host or command" >&2; exit 2; }
	[[ "$host" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$ ]] || { shown name "$host"; echo "bad ssh host: $shown_text" >&2; exit 2; }
fi

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
csv="${banks// /,}"

# portable helpers: BSD and GNU differ in stat and shasum
file_size() { wc -c <"$1" | tr -d ' '; }
sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }

pages_args=(--banks "$csv" --out "$out")
[[ -z "$overlay_dir" ]] || pages_args+=(--overlay-dir "$overlay_dir")
[[ -z "$api_url" ]] || pages_args+=(--api-url "$api_url")
[[ -z "$token_file" ]] || pages_args+=(--token-file "$token_file")

if ((dry)); then
	shown_banks=""
	for b in $banks; do
		shown bank "$b"
		shown_banks+="${shown_banks:+ }$shown_text"
	done
	shown folder "$out"
	shown_out="$shown_text"
	shown name "${host:-(none)}"
	shown_host="$shown_text"
	echo "plan: mode=$mode banks=$shown_banks out=$shown_out host=$shown_host timeout=${tmo}s nice=$nice_n stamp=$stamp"
	for b in $banks; do
		[[ "$mode" == pages ]] && continue
		shown bank "$b"
		shown_bank="$shown_text"
		shown name "${remote_cmd//\{bank\}/$b}"
		echo "  remote export $shown_bank: ssh $shown_host nice -n $nice_n timeout $tmo $shown_text; scp -> $shown_out/"
	done
	[[ "$mode" == bank ]] || (cd "$root" && python3 -m memory_ops.hindsight_pages --dry-run "${pages_args[@]}")
	exit 0
fi

[[ -d "$out" ]] || { mkdir -p "$out" && chmod 700 "$out"; }
failed=0

if [[ "$mode" != pages ]]; then
	command -v unzip >/dev/null || { echo "unzip is required for modes bank and both" >&2; exit 2; }
	# Each archive is received and checked in a private directory this run makes inside --out (same file system, so
	# publishing is a hard link). Nothing in --out is written, replaced or deleted except by that link: a name that
	# is already taken (a file, a symlink, a dangling symlink) is skipped by adding a counter. Cleanup removes only
	# this directory (and, when the second link fails, the first link this run just made).
	work="$(mktemp -d "${out%/}/.export-work.XXXXXX")"
	[[ -d "$work" && ! -L "$work" ]] || { shown folder "$out"; echo "could not create a work directory in $shown_text" >&2; exit 2; }
	trap 'rm -rf "$work"' EXIT
	taken() { [[ -e "$1" || -L "$1" ]]; }
	publish() { # publish <bank> <received zip> <digest>: prints the final file name; fails if none could be created
		local b="$1" zip="$2" digest="$3" n=1 cand
		while ((n <= 1000)); do
			cand="export-$b-$stamp"
			((n == 1)) || cand+="-$n"
			cand+=".zip"
			if ! taken "$out/$cand" && ! taken "$out/$cand.sha256"; then
				printf '%s  %s\n' "$digest" "$cand" >"$work/$cand.sha256"
				if ln -- "$zip" "$out/$cand" 2>/dev/null; then
					if ln -- "$work/$cand.sha256" "$out/$cand.sha256" 2>/dev/null; then
						printf '%s' "$cand"
						return 0
					fi
					rm -f -- "$out/$cand" # the link this run just made, so it is ours
				fi
			fi
			n=$((n + 1))
		done
		return 1
	}
	for b in $banks; do
		rname="export-$b-$stamp-$$.zip" # relative to the remote home; unique per run
		remote="omp-memory-export/$rname"
		got="$work/$rname"
		cmd="${remote_cmd//\{bank\}/$b}"
		cmd="${cmd//\{out\}/\$HOME/$remote}"
		shown bank "$b"
		bank_label="$shown_text"
		echo "remote export $bank_label (one at a time)..."
		# shellcheck disable=SC2029  # the variables are meant to expand here, locally
		if ssh -o ConnectTimeout=15 -o BatchMode=yes -- "$host" \
			"mkdir -p omp-memory-export && t=\$(command -v timeout || command -v gtimeout) && nice -n $nice_n \$t $tmo $cmd >/dev/null" &&
			scp -q -o ConnectTimeout=15 -o BatchMode=yes -- "$host:$remote" "$got"; then
			if unzip -tq "$got" >/dev/null; then
				read -r digest _ < <(sha256_of "$got")
				if final="$(publish "$b" "$got" "$digest")"; then
					shown name "$final"
					echo "ok   $shown_text ($(file_size "$got") bytes)"
				else
					shown folder "$out"
					echo "FAIL export $bank_label: could not publish into $shown_text without replacing an existing file" >&2
					failed=1
				fi
			else
				echo "FAIL export $bank_label: zip test failed" >&2
				failed=1
			fi
		else
			echo "FAIL remote export $bank_label (ssh/timeout/scp)" >&2
			failed=1
		fi
		rm -f -- "$got"
	done
fi

if [[ "$mode" != bank ]]; then
	(cd "$root" && python3 -m memory_ops.hindsight_pages "${pages_args[@]}") || failed=1
fi
exit "$failed"
