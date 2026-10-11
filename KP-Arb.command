#!/bin/bash
# KP-Arb.command — double-click to run the dashboard on macOS. The first run sets everything up:
#   finds Python 3.10+  →  creates .venv and installs requirements.txt  →  asks for your API keys (.env)  →  checks them
# Every run after that just starts the app (packages are reinstalled only when requirements.txt changes), with a
# 3-second chance to press K and change your keys. Keep the Terminal window open while trading; Ctrl+C stops the app.
cd "$(dirname "$0")" || exit 1
bold=$'\e[1m'; green=$'\e[32m'; red=$'\e[31m'; off=$'\e[0m'
step() { echo; echo "${bold}==> $1${off}"; }
fail() { echo "${red}✗ $1${off}"; echo; read -r -p "Press Enter to close."; exit 1; }
VPY=.venv/bin/python

echo "${bold}K/P Cross-Market Arb${off}"

# ── 1. Python + virtual environment (first run, or if .venv was deleted) ──────────
if [ ! -x "$VPY" ]; then
  step "First-time setup: finding Python 3.10 or newer"
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 /opt/anaconda3/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.1*/bin/python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$p"; break; fi
  done
  if [ -z "$PY" ]; then
    osascript -e 'display dialog "Python 3.10 or newer is needed. Install it from python.org (the macOS installer), then double-click KP-Arb.command again." buttons {"Open python.org", "Cancel"} default button 1' \
      -e 'if button returned of result is "Open python.org" then open location "https://www.python.org/downloads/macos/"' >/dev/null 2>&1
    fail "Python 3.10+ not found."
  fi
  echo "${green}✓${off} $("$PY" --version) ($PY)"
  step "Creating the virtual environment (.venv)"
  "$PY" -m venv .venv || fail "Could not create .venv."
fi

# ── 2. packages: install when requirements.txt changed since the last install ──────
req_hash=$(shasum requirements.txt | cut -d' ' -f1)
if [ "$(cat .venv/.requirements.sha 2>/dev/null)" != "$req_hash" ]; then
  step "Installing packages (first run takes a few minutes)"
  "$VPY" -m pip install --quiet --upgrade pip || fail "Could not upgrade pip (check your internet connection)."
  "$VPY" -m pip install --quiet -r requirements.txt || fail "Package install failed — see the messages above."
  echo "$req_hash" > .venv/.requirements.sha
  echo "${green}✓${off} packages installed"
fi

# ── 3. API keys: ask on first run (or when K is pressed), check after any change ────
keys_complete() { "$VPY" -c "import sys; sys.argv=['x']; sys.path.insert(0,'tools'); import setup_keys as s; sys.exit(0 if all(s.read_env().get(k) for k in s.KEYS) else 1)"; }
mode=""
if ! keys_complete; then
  step "API keys"; mode="--first"
else
  read -r -t 3 -n 1 -p "Starting… press K within 3 seconds to change your API keys. " k; echo
  [[ "$k" =~ ^[Kk]$ ]] && { step "API keys"; mode="--change"; }
fi
if [ -n "$mode" ]; then
  arg=""; [ "$mode" = "--change" ] && arg="--change"
  until "$VPY" tools/setup_keys.py $arg; do
    echo; read -r -p "Re-enter the keys? [Y/n] " again || again=n     # no input left (window closed): stop, don't loop
    [[ "$again" =~ ^[Nn] ]] && fail "Not starting until the keys check out. Double-click KP-Arb.command to try again."
    arg="--change"
  done
fi

# ── 4. start ────────────────────────────────────────────────────────────────────
step "Starting the dashboard — it opens in your browser. Keep this window open; Ctrl+C (or closing it) stops the app."
exec "$VPY" -m streamlit run trade/web_app.py
