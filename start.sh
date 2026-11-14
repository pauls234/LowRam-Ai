#!/usr/bin/env bash
# Start LowRAM AI Studio from the source folder (macOS / Linux).
#
# First run: creates .venv with Python 3.10+ and installs the requirements.
# Later runs: reinstalls only when a requirements file (or Python) changed.
# Arguments are passed to the launcher, e.g.  ./start.sh --no-window
set -euo pipefail

cd "$(dirname "$0")"
REQS=(requirements.txt requirements-desktop.txt)
VENV=.venv
VENV_PY="$VENV/bin/python"
STAMP="$VENV/.lowram-requirements.sha256"
DOWNLOAD_URL="https://www.python.org/downloads/"

say() { printf '%s\n' "$*"; }

python_ok() {  # $1: interpreter; true if it is Python 3.10 or newer
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

need_python() {
  say ""
  say "LowRAM AI needs Python 3.10 or newer, which was not found on this computer."
  say "Install it from $DOWNLOAD_URL (macOS: the \"macOS 64-bit universal2 installer\"),"
  say "then start LowRAM AI again."
  if [[ "$(uname -s)" == Darwin ]] && command -v osascript >/dev/null 2>&1; then
    if osascript -e 'display dialog "LowRAM AI needs Python 3.10 or newer. Install it from python.org, then open LowRAM AI again." with title "LowRAM AI" buttons {"Cancel", "Open python.org"} default button 2 with icon caution' >/dev/null 2>&1; then
      open "$DOWNLOAD_URL" || true
    fi
  fi
  exit 1
}

find_python() {
  local candidates=()
  [[ -n "${LOWRAM_PYTHON:-}" ]] && candidates+=("$LOWRAM_PYTHON")
  for version in 3.13 3.12 3.11 3.10; do
    candidates+=("python$version" "/Library/Frameworks/Python.framework/Versions/$version/bin/python3")
  done
  candidates+=(/opt/homebrew/bin/python3 /usr/local/bin/python3 python3)
  for candidate in "${candidates[@]}"; do
    local path
    path="$(command -v "$candidate" 2>/dev/null)" || continue
    # macOS: /usr/bin/python3 is a stub that pops up an Xcode installer when the
    # command line tools are missing (and is too old anyway); skip it then.
    if [[ "$path" == /usr/bin/python3 && "$(uname -s)" == Darwin ]] && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi
    if python_ok "$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

if [[ ! -x "$VENV_PY" ]] || ! python_ok "$VENV_PY"; then
  PYTHON="$(find_python)" || need_python
  say "Setting up LowRAM AI (first start, takes a minute) with $("$PYTHON" --version 2>&1)..."
  rm -rf "$VENV"
  if ! "$PYTHON" -m venv "$VENV"; then
    say "Could not create the Python environment in $(pwd)/$VENV."
    say "On Debian/Ubuntu install the venv module first: sudo apt install python3-venv"
    exit 1
  fi
fi

HASH="$("$VENV_PY" -c 'import hashlib, sys
h = hashlib.sha256(sys.version.encode())
for name in sys.argv[1:]:
    with open(name, "rb") as f:
        h.update(f.read())
print(h.hexdigest())' "${REQS[@]}")"

if [[ ! -f "$STAMP" || "$(cat "$STAMP")" != "$HASH" ]]; then
  say "Installing LowRAM AI's components (only needed after an update)..."
  "$VENV_PY" -m pip install --disable-pip-version-check --quiet --upgrade pip
  if ! "$VENV_PY" -m pip install --disable-pip-version-check --quiet -r requirements.txt -r requirements-desktop.txt; then
    say "Installing the components failed. Check your internet connection and try again."
    exit 1
  fi
  printf '%s\n' "$HASH" > "$STAMP"
fi

exec "$VENV_PY" -m desktop.launcher "$@"
