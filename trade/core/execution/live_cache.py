"""
trade/core/execution/live_cache.py — shared, short-lived snapshots of Kalshi state for the per-order monitors.

Every live order runs its own monitor thread (see monitor.py), and each one used to read its order's status and its
market's ask straight from Kalshi every 10 s. With a 200-order batch that was ~40-55 reads/s against a ~15/s read
budget, so every read in the app (the live board's order books, the balance, the MM engine) queued behind them.

Here the same answers come from two shared snapshots, each refreshed by at most one thread at a time:
  - resting orders: ONE paginated GET /portfolio/orders?status=resting covers every resting order's status and fills
    (same fields as the single-order endpoint — verified). An order missing from the snapshot (it just closed, or
    Kalshi's list hasn't caught up with a brand-new order) falls back to the direct single-order lookup, so a closing
    order still gets its exact final state.
  - market asks: one GET /markets?tickers=... per 100 markets, for every ticker asked about recently.
Snapshots are at most MAX_AGE_SEC old — fresher than the monitors' own 10 s cadence.
"""
import threading
import time
from typing import Optional

import requests

from KALSHI import client as kalshi
from applog import get_logger
from trade.core.execution.orders import get_order_status

log = get_logger('trade.core.execution')

MAX_AGE_SEC = 2.0
TICKER_TTL_SEC = 60.0       # stop batching a market's price once nobody has asked for it in this long


class _Snapshot:
    """Single-flight cache: one caller refreshes, concurrent callers wait for that result instead of piling on."""
    def __init__(self, fetch):
        self._fetch, self._lock = fetch, threading.Lock()
        self.data, self.at = None, 0.0

    def get(self):
        if self.data is not None and time.time() - self.at < MAX_AGE_SEC:
            return self.data
        with self._lock:
            if self.data is None or time.time() - self.at >= MAX_AGE_SEC:
                try:
                    self.data, self.at = self._fetch(), time.time()
                except (requests.exceptions.RequestException, ValueError) as exc:
                    log.warning('live_cache: refresh failed: %s', exc)
                    return None          # callers fall back to a direct read
            return self.data


def _fetch_resting() -> dict:
    out, cursor = {}, None
    for _ in range(20):                                   # 20 x 200 = far more resting orders than we ever hold
        params = {'status': 'resting', 'limit': 200, **({'cursor': cursor} if cursor else {})}
        r = kalshi.get('/portfolio/orders', params=params)
        r.raise_for_status()
        body = r.json()
        out.update({o['order_id']: o for o in body.get('orders', [])})
        cursor = body.get('cursor')
        if not cursor:
            break
    return out


_resting = _Snapshot(_fetch_resting)
_watched: dict = {}                                       # ticker -> last time someone asked for its price
_watched_lock = threading.Lock()


def _fetch_asks() -> dict:
    now = time.time()
    with _watched_lock:
        for t in [t for t, ts in _watched.items() if now - ts > TICKER_TTL_SEC]:
            del _watched[t]
        tickers = list(_watched)
    asks = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        r = kalshi.get('/markets', params={'tickers': ','.join(chunk), 'limit': len(chunk)})
        r.raise_for_status()
        for m in r.json().get('markets', []):
            ask = m.get('yes_ask_dollars')
            asks[m['ticker']] = round(float(ask) * 100) if ask else None
    return asks


_asks = _Snapshot(_fetch_asks)


def order_status(order_id: str) -> dict:
    """Same contract as orders.get_order_status(), served from the shared resting-orders snapshot when possible."""
    snap = _resting.get()
    if snap is not None and order_id in snap:
        return snap[order_id]
    return get_order_status(order_id)                    # closed / not listed yet / snapshot unavailable


def market_ask(ticker: str) -> Optional[int]:
    """Same contract as market_data.get_market_price() (yes ask in cents, or None), batched across all monitors."""
    with _watched_lock:
        _watched[ticker] = time.time()                    # from the next refresh on, this market is in the batch
    snap = _asks.get()
    if snap is not None and ticker in snap:
        return snap[ticker]
    # not in the batch yet (first ask for this market) or the batch failed: one direct read, as before
    from trade.core.execution.market_data import get_market_price
    return get_market_price(ticker)
