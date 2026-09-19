"""
trade/scripts/backfill_settled_at.py — one-time backfill of the new
`settled_at` column for already-settled rows (WIN/LOSS/SCALAR/CLOSED_EARLY),
and a header rewrite so the column exists at all — the log_trade() FIELDS
lists were just updated to include it, but csv.DictWriter's incremental
appends don't retroactively add a column to a file whose header was already
written without it (that's a straight column-count mismatch waiting to
happen), so this has to be a full rewrite via pandas, not an append.

For WIN/LOSS/SCALAR rows, re-fetches each ticker's market object and uses
Kalshi's own settlement_ts (falling back to updated_time, then close_time) —
the real moment the market resolved, not a guess. Some older markets may
404 (Kalshi's markets endpoint doesn't keep everything forever) — those are
left blank rather than guessed at.

CLOSED_EARLY rows have no Kalshi settlement event at all (they were closed
manually, not by the market resolving) — left blank; there's no timestamp to
recover for these from Kalshi's side.

Usage:
    python trade/scripts/backfill_settled_at.py            # dry run
    python trade/scripts/backfill_settled_at.py --live     # apply
"""
import os, sys, argparse, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import requests
from KALSHI.k_helpers import kalshi_headers

BASE_URL = 'https://api.elections.kalshi.com/trade-api/v2'
LOG_PATHS = [
    os.path.join('trade', 'logs', 'trades.csv'),
    os.path.join('trade', 'logs', 'no_trades.csv'),
    os.path.join('trade', 'logs', 'nothing_trades.csv'),
]


def fetch_settlement_ts(ticker: str):
    path = f'/trade-api/v2/markets/{ticker}'
    try:
        resp = requests.get(f'{BASE_URL}/markets/{ticker}', headers=kalshi_headers('GET', path))
    except requests.exceptions.RequestException as exc:
        print(f'    network error for {ticker}: {exc}')
        return None
    if not resp.ok:
        print(f'    {ticker}: {resp.status_code} {resp.reason}')
        return None
    m = resp.json().get('market', {})
    return m.get('settlement_ts') or m.get('updated_time') or m.get('close_time')


def backfill_file(path: str, live: bool) -> int:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print(f'{path}: no log')
        return 0
    df = pd.read_csv(path)
    if df.empty:
        print(f'{path}: empty')
        return 0

    if 'settled_at' not in df.columns:
        df['settled_at'] = ''

    changed = 0
    for idx, row in df.iterrows():
        result = row.get('result')
        if result not in ('WIN', 'LOSS', 'SCALAR'):
            continue   # PENDING (nothing to backfill), CLOSED_EARLY (no Kalshi event)
        existing = row.get('settled_at')
        if pd.notna(existing) and str(existing).strip():
            continue   # already has one (e.g. from a settle.py run since the field was added)

        ticker = row.get('k_ticker')
        ts = fetch_settlement_ts(ticker)
        time.sleep(0.05)   # be gentle — this is ~300 GETs in one run
        if ts is None:
            print(f'  SKIP {ticker}: could not recover settlement time')
            continue

        print(f'  {ticker}  {result}  settled_at -> {ts}')
        if live:
            df.at[idx, 'settled_at'] = ts
        changed += 1

    if live:
        df.to_csv(path, index=False)   # full rewrite — this is what actually adds the column
        print(f'{path}: rewrote with settled_at column, backfilled {changed} row(s)')
    else:
        print(f'{path}: {changed} row(s) would be backfilled (dry run — column not added yet)')
    return changed


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true')
    args = p.parse_args()

    total = 0
    for path in LOG_PATHS:
        total += backfill_file(path, args.live)

    print(f'\nTOTAL backfilled: {total}' + ('' if args.live else '  (DRY RUN — pass --live to apply)'))
