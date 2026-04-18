"""
cancel_all.py — Cancel every open resting order on Kalshi.

Usage:
    python trade/cancel_all.py            # cancel all resting/open orders
    python trade/cancel_all.py --dry-run  # list them without canceling
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from KALSHI.k_helpers import kalshi_headers
from bot import cancel_order

BASE_URL = 'https://api.elections.kalshi.com/trade-api/v2'
OPEN_STATUSES = {'resting', 'open', 'pending'}


def list_open_orders() -> list:
    path = '/trade-api/v2/portfolio/orders'
    resp = requests.get(f'{BASE_URL}/portfolio/orders',
                        headers=kalshi_headers('GET', path),
                        params={'status': 'resting', 'limit': 200})
    resp.raise_for_status()
    return resp.json().get('orders', [])


def main(dry_run: bool):
    orders = list_open_orders()
    orders = [o for o in orders if o.get('status') in OPEN_STATUSES]
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
        oid = o.get('order_id')
        if cancel_order(oid):
            print(f'  canceled {oid}')
            ok += 1
        else:
            print(f'  FAILED  {oid}')
    print(f'\nCanceled {ok}/{len(orders)} orders.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    main(dry_run=args.dry_run)
