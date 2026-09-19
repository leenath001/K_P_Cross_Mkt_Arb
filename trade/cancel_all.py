"""
cancel_all.py — Cancel every open resting order on Kalshi.

Usage:
    python trade/cancel_all.py            # cancel all resting/open orders
    python trade/cancel_all.py --dry-run  # list them without canceling
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trade.core.execution import ensure_canceled, list_resting_orders


def main(dry_run: bool):
    orders = list_resting_orders()
    if not orders:
        print('No open orders.')
        return
    print(f'Found {len(orders)} open order(s):')
    for o in orders:
        print(f'  {o.get("ticker")}  {o.get("side")}  {o.get("yes_price")}¢  x{o.get("remaining_count_fp")}'
              f'  [{o.get("status")}]  id={o.get("order_id")}')
    if dry_run:
        print('\n(dry run — nothing canceled)')
        return
    print()
    ok = 0
    for o in orders:
        oid    = o.get('order_id')
        ticker = o.get('ticker')
        # ensure_canceled passes ticker as market_ticker so Kalshi routes the
        # DELETE to the correct exchange shard (MLB/Tennis = shard 3, Combos = 1,
        # Crypto = 2) instead of silently defaulting to shard 0, and verifies the
        # cancel actually took by re-polling status rather than trusting one call.
        if ensure_canceled(ticker, oid):
            print(f'  canceled {oid}  {ticker}')
            ok += 1
        else:
            print(f'  FAILED  {oid}  {ticker}')
    print(f'\nCanceled {ok}/{len(orders)} orders.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    main(dry_run=args.dry_run)
