"""
trade/core/positions.py — live Kalshi state, the single dedup source for every
strategy.

Previously each strategy (bot.py, prospect.py, nothing.py) had its own
already_bet_tickers(), reading its own CSV trade log for rows with
result=='PENDING'. That log is a snapshot written once when an order was placed
— nothing ever went back and corrected it if the order later filled, canceled, or
expired outside that snapshot. Confirmed empirically: of 145 tickers the old
CSV-based check was blocking, every single one sampled was already closed on
Kalshi's side (canceled or aged-out), 0 were actually still open. The bot was
refusing to re-trade markets based on stale bookkeeping, not real exposure.

open_tickers() asks Kalshi directly instead: current positions + current resting
orders. No CSV involved, so a malformed or stale log row can never block a real
trade opportunity again.
"""

import os, sys
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from KALSHI.k_helpers import kalshi_headers
from applog import get_logger

log = get_logger(__name__)

BASE_URL = 'https://api.elections.kalshi.com/trade-api/v2'


def _open_position_tickers() -> set:
    """Tickers with a non-zero live position (GET /portfolio/positions), paginated."""
    tickers = set()
    cursor = None
    path = '/trade-api/v2/portfolio/positions'
    while True:
        params = {'count_filter': 'position', 'limit': 200}
        if cursor:
            params['cursor'] = cursor
        try:
            resp = requests.get(f'{BASE_URL}/portfolio/positions',
                                headers=kalshi_headers('GET', path), params=params)
        except requests.exceptions.RequestException:
            log.exception('_open_position_tickers: network failure')
            return tickers
        if not resp.ok:
            log.warning('_open_position_tickers failed: %s %s', resp.status_code, resp.reason)
            return tickers
        data = resp.json()
        for p in data.get('market_positions', []):
            try:
                if float(p.get('position_fp', 0) or 0) != 0:
                    tickers.add(p['ticker'])
            except (TypeError, ValueError):
                continue
        cursor = data.get('cursor')
        if not cursor:
            break
    return tickers


def _resting_order_tickers() -> set:
    """Tickers with a currently-resting order (GET /portfolio/orders?status=resting)."""
    path = '/trade-api/v2/portfolio/orders'
    try:
        resp = requests.get(f'{BASE_URL}/portfolio/orders',
                            headers=kalshi_headers('GET', path),
                            params={'status': 'resting', 'limit': 200})
    except requests.exceptions.RequestException:
        log.exception('_resting_order_tickers: network failure')
        return set()
    if not resp.ok:
        log.warning('_resting_order_tickers failed: %s %s', resp.status_code, resp.reason)
        return set()
    return {o['ticker'] for o in resp.json().get('orders', []) if o.get('ticker')}


def open_tickers() -> set:
    """
    Tickers to treat as "already have exposure here, don't signal again" — the
    union of current positions and current resting orders, read live from Kalshi.
    Call once per batch run (same cadence the old already_bet_tickers() used),
    not per-row — two cheap GETs regardless of how many signals are being scanned.
    """
    return _open_position_tickers() | _resting_order_tickers()
