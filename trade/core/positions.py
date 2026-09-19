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


def position_open(ticker: str) -> bool:
    """
    True if we currently hold a non-zero position on this exact ticker, live
    from Kalshi. Used by settle.py to notice a position that was manually
    closed (sold back) before the market settled — something a pure
    "did the market settle" check would never catch on its own.
    """
    return ticker in _open_position_tickers()


# ── Opposite-leg (same-event) exposure ───────────────────────────────────────
# On a 2-outcome event (A vs B) Kalshi lists one market per side, so "YES A" and
# "NO B" are the SAME bet on different tickers (and YES A + YES B is a guaranteed
# partial loss). Ticker-keyed dedup can't see that. 3-way events (a -TIE market
# exists) are left alone — YES A and NO B are genuinely different bets there.

_EVENT_MARKET_COUNT: dict = {}


def event_of(ticker: str) -> str:
    return ticker.rsplit('-', 1)[0]


def is_two_way_event(event_ticker: str) -> bool:
    """True if the Kalshi event has exactly 2 markets. Fails CLOSED (True) if it can't be checked."""
    if event_ticker in _EVENT_MARKET_COUNT:
        return _EVENT_MARKET_COUNT[event_ticker] == 2
    path = '/trade-api/v2/markets'
    try:
        resp = requests.get(f'{BASE_URL}/markets', headers=kalshi_headers('GET', path),
                            params={'event_ticker': event_ticker, 'limit': 20})
        if not resp.ok:
            log.warning('is_two_way_event %s failed: %s', event_ticker, resp.status_code)
            return True
        n = len(resp.json().get('markets', []))
    except requests.exceptions.RequestException:
        log.exception('is_two_way_event: network failure for %s', event_ticker)
        return True
    _EVENT_MARKET_COUNT[event_ticker] = n
    return n == 2


def opposite_leg_blocked(tickers, open_set: set) -> set:
    """Subset of `tickers` whose 2-way event already has exposure on a DIFFERENT ticker."""
    open_events: dict = {}
    for t in open_set:
        open_events.setdefault(event_of(t), set()).add(t)
    blocked = set()
    for t in tickers:
        ev = event_of(t)
        if open_events.get(ev, set()) - {t} and is_two_way_event(ev):
            blocked.add(t)
    return blocked


def drop_same_event_duplicates(df, score_col: str = '_score'):
    """Within one batch, keep only the best-scoring row per 2-way event."""
    if df.empty:
        return df
    keep = []
    for _, grp in df.groupby(df['k_ticker'].map(event_of), sort=False):
        if len(grp) > 1 and is_two_way_event(event_of(grp['k_ticker'].iloc[0])):
            keep.append(grp[score_col].idxmax())
        else:
            keep.extend(grp.index)
    return df.loc[[i for i in df.index if i in set(keep)]]
