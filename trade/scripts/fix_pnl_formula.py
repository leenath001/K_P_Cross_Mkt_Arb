"""
trade/scripts/fix_pnl_formula.py — one-time recompute of actual_pnl for
already-settled WIN/LOSS rows after settle.py's compute_pnl() fee-formula fix.

The old formula charged fee as `entry_price * fee_rate` on wins only ((1-price)
* (1-fee_rate)) and nothing at all on losses. The correct model — the same one
trade.core.execution._ev() already used everywhere else — charges
fee_rate * price * (1-price) at entry, on every filled contract, win or lose.
Every already-settled WIN/LOSS row's actual_pnl was computed with the old,
wrong formula and needs recomputing from what's already in the row
(contracts, entry_price, fee_rate, result) — no API calls needed for those.

SCALAR rows need the original settlement_value, which isn't stored in the
CSV — re-fetched live from Kalshi per row instead (there are typically very
few of these).

CLOSED_EARLY rows are untouched: their PnL already comes from real observed
fill prices (trade.settle.fetch_realized_pnl_from_fills), not this formula,
so they were never affected by the bug.

Usage:
    python trade/scripts/fix_pnl_formula.py            # dry run
    python trade/scripts/fix_pnl_formula.py --live      # apply
"""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from trade.settle import compute_pnl, compute_scalar_pnl, fetch_scalar_settlement_value

LOG_PATHS = [
    (os.path.join('trade', 'logs', 'trades.csv'),         'yes'),
    (os.path.join('trade', 'logs', 'no_trades.csv'),      'no'),
    # nothing.py only ever buys NO — same side='no' settle.py already uses for it.
    (os.path.join('trade', 'logs', 'nothing_trades.csv'), 'no'),
]


def fix_file(path: str, side: str, live: bool) -> int:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print(f'{path}: no log')
        return 0
    df = pd.read_csv(path)
    if df.empty:
        print(f'{path}: empty')
        return 0

    changed = 0
    for idx, row in df.iterrows():
        result = row.get('result')
        if result not in ('WIN', 'LOSS', 'SCALAR'):
            continue   # PENDING, VOID (already 0), CLOSED_EARLY (real fills, untouched)

        old_pnl = row.get('actual_pnl')
        try:
            contracts   = int(row['contracts'])
            entry_price = float(row['entry_price'])
            fee_rate    = float(row['fee_rate'])
        except (KeyError, ValueError, TypeError):
            print(f'  SKIP {row.get("k_ticker")}: malformed contracts/entry_price/fee_rate')
            continue

        if result == 'SCALAR':
            ticker = row.get('k_ticker')
            sv = fetch_scalar_settlement_value(ticker)
            if sv is None:
                print(f'  SKIP {ticker}: SCALAR — could not re-fetch settlement_value live')
                continue
            new_pnl = compute_scalar_pnl(contracts, entry_price, fee_rate, sv, side=side)
        else:
            # compute_pnl() takes the RAW Kalshi outcome ('yes'/'no'), not this
            # CSV's own derived WIN/LOSS label — translate back via the same
            # side-aware mapping _settle_file() used to produce that label in
            # the first place.
            if result == 'WIN':
                kalshi_result = 'no' if side == 'no' else 'yes'
            else:  # LOSS
                kalshi_result = 'yes' if side == 'no' else 'no'
            new_pnl = compute_pnl(kalshi_result, contracts, entry_price, fee_rate, side=side)

        if old_pnl is None or abs(float(old_pnl) - new_pnl) > 0.0001:
            print(f'  {row.get("k_ticker")}  {result:5s}  '
                  f'old={old_pnl}  ->  new={new_pnl:+.4f}')
            if live:
                df.at[idx, 'actual_pnl'] = new_pnl
            changed += 1

    if changed and live:
        df.to_csv(path, index=False)
        print(f'{path}: wrote {changed} corrected row(s)')
    elif changed:
        print(f'{path}: {changed} row(s) would change (dry run)')
    else:
        print(f'{path}: no changes needed')
    return changed


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true')
    args = p.parse_args()

    total = 0
    for path, side in LOG_PATHS:
        total += fix_file(path, side, args.live)

    print(f'\nTOTAL changed: {total}' + ('' if args.live else '  (DRY RUN — pass --live to apply)'))
