#!/bin/sh
# Bootstrap for omp-services: finds omp and Python 3.10+, makes sure the plugin is installed, then runs
# `omp-services init`, which shows exactly what it would do and asks before it touches anything.
#
#   curl -fsSLo install.sh https://raw.githubusercontent.com/saadsoft1993/omp-tether/omp-services-v0.1.0/plugins/omp-services/install.sh
#   shasum -a 256 -c install.sh.sha256   # the SHA-256 published in the release notes, not one fetched from the same place
#   sh install.sh
#
# Prefer a release tag to `main` (a tag is easier to audit, but a tag can be moved). The SHA-256 in the release notes is an
# integrity check against a corrupted download; it is not independent proof of origin, because the notes are served from
# the same place as the script. This verifies the installer only. The plugin it then installs comes from the marketplace source named
# below, which is NOT tied to this script's commit; to tie it to a commit you checked, use OMP_TETHER_SOURCE_DIR.
#
# Read it first if you like; it is short. What it does, in order:
#   1. looks for omp (OMP_BIN, PATH, ~/.local/bin/omp) and for a Python 3.10 or newer; says so and stops if either is missing
#   2. asks `omp plugin list --json` where omp-services is installed. If it is not, shows the two omp commands that
#      install it from the marketplace and asks before running them (the plugin comes from OMP_TETHER_REPO,
#      default saadsoft1993/omp-tether, marketplace name OMP_TETHER_MARKETPLACE, default omp-tether)
#   3. runs `omp-services init`, which prints a plan and asks again before it creates a file or loads a unit
# Every question is asked on the terminal (/dev/tty), also when this script is piped into sh. With no terminal at all it
# changes nothing and exits with status 3.
set -u

REPO=${OMP_TETHER_REPO:-saadsoft1993/omp-tether}
MARKET=${OMP_TETHER_MARKETPLACE:-omp-tether}

say() { printf '%s\n' "$*"; }
die() { printf 'omp-services bootstrap: %s\n' "$*" >&2; exit "${2:-2}"; }

# What these values may be is checked before anything runs: they end up as arguments of `omp`, so a value that starts
# with "-" would be an option, and anything but owner/name would not be a GitHub shorthand at all.
case $REPO in
  *[!A-Za-z0-9._/-]* | -* | */-* | .* | */.* | */*/* | /* | */) die "OMP_TETHER_REPO must look like owner/name (letters, digits, . _ -), not: $(printf '%s' "$REPO" | tr -c 'A-Za-z0-9._/-' '?')" ;;
  */*) ;;
  *) die "OMP_TETHER_REPO must look like owner/name" ;;
esac
# A local checkout (OMP_TETHER_SOURCE_DIR) replaces the GitHub shorthand: this is how to install a plugin that is tied to a
# commit you chose and checked (git clone, then git checkout <commit>), because the shorthand follows the branch the host
# serves today.
SOURCE=$REPO
if [ -n "${OMP_TETHER_SOURCE_DIR:-}" ]; then
  case $OMP_TETHER_SOURCE_DIR in
    /*) ;;
    *) die "OMP_TETHER_SOURCE_DIR must be an absolute path" ;;
  esac
  case $OMP_TETHER_SOURCE_DIR in
    *[[:cntrl:]]*) die "OMP_TETHER_SOURCE_DIR must not contain control characters" ;;
  esac
  [ -d "$OMP_TETHER_SOURCE_DIR/.omp-plugin" ] || die "OMP_TETHER_SOURCE_DIR is not a checkout of the marketplace (no .omp-plugin directory)"
  SOURCE=$OMP_TETHER_SOURCE_DIR
fi
case $MARKET in
  *[!A-Za-z0-9.-]* | -* | .* | "") die "OMP_TETHER_MARKETPLACE may hold only letters, digits, . and -, and must not start with - or ." ;;
esac

ask() { # ask "question": yes only when a person types y or yes
  [ -r /dev/tty ] || return 1
  printf '%s [y/N] ' "$1" >/dev/tty
  IFS= read -r answer </dev/tty || return 1
  case $answer in y | Y | yes | YES | Yes) return 0 ;; *) return 1 ;; esac
}

recent() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
  if [ -n "${OMP_SERVICES_PYTHON:-}" ]; then
    case $OMP_SERVICES_PYTHON in /*) ;; *) return 1 ;; esac
    recent "$OMP_SERVICES_PYTHON" && printf '%s\n' "$OMP_SERVICES_PYTHON" && return 0
    return 1
  fi
  for dir in "" /opt/homebrew/bin /usr/local/bin "${HOME:-/nonexistent}/.local/bin"; do
    for name in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
      if [ -z "$dir" ]; then
        candidate=$(command -v "$name" 2>/dev/null) || continue
      else
        candidate=$dir/$name
        [ -x "$candidate" ] || continue
      fi
      case $candidate in /*) ;; *) continue ;; esac
      if recent "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  done
  return 1
}

find_omp() {
  if [ -n "${OMP_BIN:-}" ] && [ -x "$OMP_BIN" ]; then
    printf '%s\n' "$OMP_BIN"
    return 0
  fi
  candidate=$(command -v omp 2>/dev/null) && case $candidate in /*) printf '%s\n' "$candidate"; return 0 ;; esac
  if [ -x "${HOME:-/nonexistent}/.local/bin/omp" ]; then
    printf '%s\n' "$HOME/.local/bin/omp"
    return 0
  fi
  return 1
}

OMP=$(find_omp) || die "omp was not found (OMP_BIN, PATH, ~/.local/bin/omp). Install omp first: https://github.com/can1357/oh-my-pi"
PY=$(find_python) || die "Python 3.10 or newer was not found. Install one (Homebrew: brew install python; Ubuntu: apt install python3) or set OMP_SERVICES_PYTHON to its absolute path. On macOS, /usr/bin/python3 is 3.9 and too old."
say "omp:    $OMP"
say "python: $PY"

installed_dir() { # prints the install path of omp-services@MARKET, if omp lists one
  "$OMP" plugin list --json 2>/dev/null | "$PY" -c '
import json, sys
want = "omp-services@" + sys.argv[1]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
for item in data.get("marketplace", []):
    if item.get("id") == want:
        for entry in item.get("entries", []):
            if entry.get("installPath"):
                print(entry["installPath"])
                sys.exit(0)
sys.exit(1)
' "$MARKET"
}

DIR=$(installed_dir) || DIR=
if [ -n "$DIR" ] && [ -n "${OMP_TETHER_SOURCE_DIR:-}" ]; then
  # An installation that already exists is not known to come from the checkout that was asked for, so it is not run.
  die "omp-services is already installed (at $DIR), and nothing says it came from $OMP_TETHER_SOURCE_DIR. Run '$OMP plugin uninstall omp-services@$MARKET' first so that it is installed from that checkout, or unset OMP_TETHER_SOURCE_DIR to use the installation that exists."
fi
if [ -z "$DIR" ]; then
  say ""
  say "The omp-services plugin is not installed. These two commands install it from the marketplace $MARKET:"
  say "  $OMP plugin marketplace add $SOURCE"
  if [ "$SOURCE" = "$REPO" ]; then
    say "(that source follows the repository's current default branch: it is not tied to the commit of this script."
    say " For a plugin tied to a commit you checked, clone it, check that commit out, and run this with OMP_TETHER_SOURCE_DIR=<the checkout>.)"
  fi
  say "  $OMP plugin install omp-services@$MARKET"
  ask "Run them now?" || die "not installed and not confirmed: nothing was changed" 3
  "$OMP" plugin marketplace add "$SOURCE" || die "omp plugin marketplace add failed"
  "$OMP" plugin install "omp-services@$MARKET" || die "omp plugin install failed"
  DIR=$(installed_dir) || die "omp does not list omp-services after the install"
fi

[ -x "$DIR/bin/omp-services" ] || die "$DIR/bin/omp-services is missing: reinstall the plugin"
say ""
say "Running: omp-services init (it shows a plan and asks before changing anything)"
OMP_SERVICES_PYTHON=$PY OMP_BIN=$OMP exec "$DIR/bin/omp-services" init
