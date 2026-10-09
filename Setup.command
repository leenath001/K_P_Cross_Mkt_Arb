#!/bin/bash
# Setup.command — one-click setup on macOS. Double-click it in Finder.
#   1. finds Python 3.10+          2. creates .venv and installs requirements.txt
#   3. asks for your API keys and writes .env (only readable by you)
#   4. checks both keys against the live APIs, then offers to start the app
# Safe to re-run: it reuses .venv and asks before replacing existing keys.
cd "$(dirname "$0")" || exit 1
bold=$'\e[1m'; green=$'\e[32m'; red=$'\e[31m'; yellow=$'\e[33m'; off=$'\e[0m'
step() { echo; echo "${bold}==> $1${off}"; }
fail() { echo "${red}✗ $1${off}"; echo; read -r -p "Press Enter to close."; exit 1; }

echo "${bold}K/P Cross-Market Arb — setup${off}"

# ── 1. Python ─────────────────────────────────────────────────────────────────
step "Finding Python 3.10 or newer"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 /opt/anaconda3/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.1*/bin/python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  osascript -e 'display dialog "Python 3.10 or newer is needed. Install it from python.org (the macOS installer), then double-click Setup.command again." buttons {"Open python.org", "Cancel"} default button 1' \
    -e 'if button returned of result is "Open python.org" then open location "https://www.python.org/downloads/macos/"' >/dev/null 2>&1
  fail "Python 3.10+ not found."
fi
echo "${green}✓${off} $("$PY" --version) ($PY)"

# ── 2. virtual environment + packages ────────────────────────────────────────
step "Setting up the virtual environment (.venv)"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || fail "Could not create .venv."
fi
.venv/bin/python -m pip install --quiet --upgrade pip || fail "Could not upgrade pip (check your internet connection)."
echo "Installing packages (first run takes a few minutes)…"
.venv/bin/python -m pip install --quiet -r requirements.txt || fail "Package install failed — see the messages above."
echo "${green}✓${off} packages installed"

# ── 3. API keys → .env ───────────────────────────────────────────────────────
step "API keys"
have_keys=$(.venv/bin/python - <<'PY'
from dotenv import dotenv_values
v = dotenv_values('.env')
print('yes' if all(v.get(k) for k in ('ODDS_API_KEY', 'API_KEY', 'API_PRIVATE')) else 'no')
PY
)
ask_keys=yes
if [ "$have_keys" = "yes" ]; then
  read -r -p "Keys are already set up in .env. Replace them? [y/N] " ans
  [[ "$ans" =~ ^[Yy] ]] || ask_keys=no
fi

if [ "$ask_keys" = "yes" ]; then
  echo "Typing is hidden for secret keys — paste, then press Enter."
  echo
  echo "${bold}The Odds API key${off} (Pinnacle odds) — from https://the-odds-api.com/account/"
  read -r -s -p "  Odds API key: " ODDS; echo
  [ -n "$ODDS" ] || fail "No Odds API key entered."
  echo
  echo "${bold}Kalshi API key ID${off} — Kalshi → Account & security → API keys (looks like a UUID)"
  read -r -p "  Kalshi API key ID: " KID
  [ -n "$KID" ] || fail "No Kalshi API key ID entered."
  echo
  echo "${bold}Kalshi private key file${off} — the .key / .pem / .txt file Kalshi gave you when you created the key."
  KEYFILE=$(osascript -e 'POSIX path of (choose file with prompt "Select your Kalshi private key file (.key / .pem / .txt)")' 2>/dev/null)
  if [ -z "$KEYFILE" ]; then
    read -r -p "  No file chosen — drag the key file into this window (or type its path), then press Enter: " KEYFILE
    KEYFILE="${KEYFILE%\"}"; KEYFILE="${KEYFILE#\"}"; KEYFILE="${KEYFILE%\'}"; KEYFILE="${KEYFILE#\'}"; KEYFILE="${KEYFILE% }"
  fi
  [ -f "$KEYFILE" ] || fail "Key file not found: $KEYFILE"

  # Written by Python so no key ever passes through shell quoting; the private key is stored the way the app reads it
  # (KALSHI/client.py): the base64 body with the PEM header/footer lines and line breaks removed.
  ODDS="$ODDS" KID="$KID" KEYFILE="$KEYFILE" .venv/bin/python - <<'PY' || fail "Could not save .env."
import base64, binascii, os, re, sys
raw = open(os.environ['KEYFILE'], encoding='utf-8', errors='ignore').read()
body = ''.join(l.strip() for l in raw.splitlines() if l.strip() and not l.startswith('-----'))
body = re.sub(r'\s+', '', body)
try:
    base64.b64decode(body, validate=True)
except (binascii.Error, ValueError):
    sys.exit('That file does not look like a private key (expected a PEM / base64 key file).')
lines = {'ODDS_API_KEY': os.environ['ODDS'].strip(), 'API_KEY': os.environ['KID'].strip(), 'API_PRIVATE': body}
fd = os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(''.join(f'{k}={v}\n' for k, v in lines.items()))
os.chmod('.env', 0o600)
PY
  echo "${green}✓${off} saved to .env (readable only by you; never committed — it's in .gitignore)"
fi

# ── 4. check the keys against the live APIs ──────────────────────────────────
step "Checking the keys"
.venv/bin/python - <<'PY'
import sys
ok = True
try:
    from KALSHI import client as kalshi
    r = kalshi.get('/portfolio/balance')
    if r.status_code == 401:
        print('\033[31m✗ Kalshi rejected the key (401) — check the key ID and that the private key file matches it.\033[0m'); ok = False
    else:
        r.raise_for_status()
        print(f"\033[32m✓\033[0m Kalshi: connected, cash ${r.json().get('balance', 0) / 100:,.2f}")
except Exception as exc:
    print(f'\033[31m✗ Kalshi check failed: {exc}\033[0m'); ok = False
try:
    import requests
    from dotenv import dotenv_values
    r = requests.get('https://api.the-odds-api.com/v4/sports',            # free endpoint: costs no credits
                     params={'apiKey': dotenv_values('.env').get('ODDS_API_KEY')}, timeout=15)
    if r.status_code == 401:
        print('\033[31m✗ The Odds API rejected the key (401) — check it at https://the-odds-api.com/account/\033[0m'); ok = False
    else:
        r.raise_for_status()
        print(f"\033[32m✓\033[0m The Odds API: connected, {int(r.headers.get('x-requests-remaining', 0)):,} requests left this month")
except Exception as exc:
    print(f'\033[31m✗ The Odds API check failed: {exc}\033[0m'); ok = False
sys.exit(0 if ok else 1)
PY
checks=$?
echo
if [ $checks -ne 0 ]; then
  echo "${yellow}Setup finished, but a key check failed. Double-click Setup.command again to re-enter the keys.${off}"
  read -r -p "Press Enter to close."
  exit 1
fi

echo "${green}${bold}All set.${off} From now on, double-click ${bold}Start.command${off} to open the dashboard."
read -r -p "Start the dashboard now? [Y/n] " go
if [[ ! "$go" =~ ^[Nn] ]]; then
  exec ./Start.command
fi
