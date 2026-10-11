"""
tools/setup_keys.py — ask for the API keys, write .env, and check them against the live APIs.

Run by KP-Arb.command (macOS) and KP-Arb.bat (Windows) from the project's virtual environment:
    python tools/setup_keys.py            # prompt only if .env is missing a key, then check
    python tools/setup_keys.py --change   # always prompt (replace existing keys), then check
    python tools/setup_keys.py --check    # just check the keys in .env

Exit code 0 = keys saved and both checks passed; 1 = something failed (the message says what).
The Kalshi private key is stored the way KALSHI/client.py reads it: the base64 body of the PEM file, header/footer
lines and line breaks removed. .env is written readable only by you (and is git-ignored).
"""
import base64
import binascii
import getpass
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, '.env')
KEYS = ('ODDS_API_KEY', 'API_KEY', 'API_PRIVATE')
GREEN, RED, BOLD, OFF = ('\033[32m', '\033[31m', '\033[1m', '\033[0m') if sys.stdout.isatty() else ('',) * 4
if os.name == 'nt':
    os.system('')                      # turns on ANSI colours in the Windows console


def ok(msg): print(f'{GREEN}✓{OFF} {msg}')
def bad(msg): print(f'{RED}✗ {msg}{OFF}')


def read_env() -> dict:
    vals = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding='utf-8', errors='ignore'):
            k, sep, v = line.strip().partition('=')
            if sep:
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def pick_key_file() -> str:
    """Native file picker (Finder on macOS, Explorer on Windows); falls back to typing / dragging the path in."""
    path = ''
    try:
        if sys.platform == 'darwin':
            path = subprocess.run(['osascript', '-e', 'POSIX path of (choose file with prompt '
                                   '"Select your Kalshi private key file (.key / .pem / .txt)")'],
                                  capture_output=True, text=True, timeout=600).stdout.strip()
        else:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk(); root.withdraw(); root.attributes('-topmost', True)
            path = filedialog.askopenfilename(title='Select your Kalshi private key file (.key / .pem / .txt)')
            root.destroy()
    except Exception:
        path = ''
    if not path:
        path = input('  No file chosen — drag the key file into this window (or type its path), then press Enter: ')
    return path.strip().strip('"').strip("'").strip()


def key_body(path: str) -> str:
    raw = open(path, encoding='utf-8', errors='ignore').read()
    body = re.sub(r'\s+', '', ''.join(l for l in raw.splitlines() if l.strip() and not l.startswith('-----')))
    try:
        base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('that file does not look like a private key (expected a PEM / base64 key file)')
    if len(body) < 200:
        raise ValueError('that file is too short to be a private key')
    return body


def prompt_and_save():
    print('Secret keys are hidden while you type or paste — press Enter when done.\n')
    print(f'{BOLD}The Odds API key{OFF} (Pinnacle odds) — from https://the-odds-api.com/account/')
    odds = getpass.getpass('  Odds API key: ').strip()
    print(f'\n{BOLD}Kalshi API key ID{OFF} — Kalshi → Account & security → API keys (looks like a UUID)')
    kid = input('  Kalshi API key ID: ').strip()
    print(f'\n{BOLD}Kalshi private key file{OFF} — the .key / .pem / .txt file Kalshi gave you with that key')
    path = pick_key_file()
    if not odds or not kid:
        raise ValueError('a key was left empty')
    if not os.path.isfile(path):
        raise ValueError(f'key file not found: {path}')
    body = key_body(path)
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(f'ODDS_API_KEY={odds}\nAPI_KEY={kid}\nAPI_PRIVATE={body}\n')
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass
    ok("saved to .env (only readable by you; it's git-ignored, never committed)")


def check() -> bool:
    import requests
    env = read_env()
    passed = True
    # Kalshi: a signed read of the balance (read-only)
    try:
        os.environ.update({k: env.get(k, '') for k in KEYS})
        sys.path.insert(0, ROOT)
        from KALSHI import client as kalshi
        r = kalshi.get('/portfolio/balance')
        if r.status_code == 401:
            bad('Kalshi rejected the key (401) — check the key ID and that the private key file matches it.')
            passed = False
        else:
            r.raise_for_status()
            ok(f"Kalshi: connected, cash ${r.json().get('balance', 0) / 100:,.2f}")
    except Exception as exc:
        bad(f'Kalshi check failed: {exc}')
        passed = False
    # The Odds API: /sports is free (doesn't use credits) and returns the usage headers
    try:
        r = requests.get('https://api.the-odds-api.com/v4/sports', params={'apiKey': env.get('ODDS_API_KEY')}, timeout=15)
        if r.status_code == 401:
            bad('The Odds API rejected the key (401) — check it at https://the-odds-api.com/account/')
            passed = False
        else:
            r.raise_for_status()
            ok(f"The Odds API: connected, {int(r.headers.get('x-requests-remaining', 0)):,} requests left this month")
    except Exception as exc:
        bad(f'The Odds API check failed: {exc}')
        passed = False
    return passed


def main(argv):
    change, only_check = '--change' in argv, '--check' in argv
    have = all(read_env().get(k) for k in KEYS)
    if not only_check and (change or not have):
        try:
            prompt_and_save()
        except (ValueError, OSError) as exc:
            bad(f'Could not save the keys: {exc}')
            return 1
        except (KeyboardInterrupt, EOFError):
            print(); bad('Cancelled.')
            return 1
    print(f'\n{BOLD}Checking the keys…{OFF}')
    return 0 if check() else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
