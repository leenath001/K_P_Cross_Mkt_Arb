"""
trade/core/execution/account.py — Account state: available cash, portfolio value, and open positions marked to market.
"""
import requests
from typing import Optional

from KALSHI import client as kalshi
from applog import get_logger


log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split


def get_balance() -> float:
    """
    Fetch available balance from Kalshi portfolio (returns dollars).
    Raises RuntimeError with a clear message on auth failure or network error.
    """
    try:
        resp = kalshi.get('/portfolio/balance')
    except requests.exceptions.RequestException as exc:
        log.error('get_balance network failure: %s', exc)
        raise RuntimeError(f'Could not reach Kalshi to fetch balance: {exc}') from exc
    if resp.status_code == 401:
        log.error('get_balance got 401 — check API_KEY / API_PRIVATE')
        raise RuntimeError(
            'Kalshi returned 401 for /portfolio/balance.\n'
            '  Possible causes:\n'
            '    1. API key does not have portfolio/trading permissions\n'
            '    2. Wrong API_KEY in .env\n'
            '    3. API_PRIVATE key is incorrect\n'
            '  Use --bankroll <amount> to skip this check.'
        )
    if not resp.ok:
        log.error('get_balance failed: %s %s — %s', resp.status_code, resp.reason, resp.text)
    resp.raise_for_status()
    return resp.json().get('balance', 0) / 100


def get_portfolio_snapshot() -> dict:
    """
    {'cash': available balance $, 'positions_value': Kalshi's own value of every open position $} from
    /portfolio/balance in one call. Used for the small always-on Cash/Portfolio readout in the top bar.

    Kalshi's `portfolio_value` is the value of open positions ONLY (priced at last trade) — it does NOT include cash,
    so a total account value is cash + positions_value. `balance_dollars` is used for cash when present: the integer
    `balance` field is truncated to whole cents.
    """
    resp = kalshi.get('/portfolio/balance')
    resp.raise_for_status()
    body = resp.json()
    cash = float(body['balance_dollars']) if body.get('balance_dollars') else body.get('balance', 0) / 100
    return {'cash': cash, 'positions_value': body.get('portfolio_value', 0) / 100}


def _position_mark_price(mk: dict) -> Optional[float]:
    """
    Current per-contract YES value for a market: 1/0 once Kalshi has a result, else the bid/ask mid, else last trade.

    A decided market sits at bid 0 / ask 1 with no real book until Kalshi pays out (status determined → finalized,
    which can take a while), so a plain mid would mark every settled-but-unpaid position at 0.50. The result is
    checked first for exactly that window.
    """
    result = (mk.get('result') or '').lower()
    if result == 'yes':
        return 1.0
    if result == 'no':
        return 0.0
    if result == 'scalar':
        for f in ('settlement_value_dollars', 'expiration_value'):
            try:
                return float(mk[f])
            except (KeyError, TypeError, ValueError):
                pass
    yb, ya = mk.get('yes_bid_dollars'), mk.get('yes_ask_dollars')
    try:
        yb, ya = float(yb), float(ya)
        if 0 < yb <= ya < 1:   # a real two-sided book (an empty side shows as bid 0 / ask 1)
            return (yb + ya) / 2
    except (TypeError, ValueError):
        pass
    try:
        last = float(mk.get('last_price_dollars') or 0)
        return last if last > 0 else None
    except (TypeError, ValueError):
        return None


def get_position_marks() -> list:
    """
    Every open position, marked to the market's CURRENT value (not the fixed entry cost Kalshi's own
    market_exposure_dollars reports) — see _position_mark_price. Market data for all positions comes from one batched
    GET /markets?tickers=... per 100 tickers; a position whose market can't be priced falls back to its entry cost.

    Returns [{'ticker', 'side' ('YES'/'NO'), 'qty', 'mark_dollars', 'cost_dollars', 'unrealized_dollars',
    'status', 'result'}, ...]. Raises on a failed positions read (caller decides what to show).
    """
    positions, cursor = [], None
    while True:
        params = {'count_filter': 'position', 'limit': 200, **({'cursor': cursor} if cursor else {})}
        resp = kalshi.get('/portfolio/positions', params=params)
        resp.raise_for_status()
        body = resp.json()
        positions.extend(p for p in body.get('market_positions', []) if abs(float(p.get('position_fp') or 0)) > 1e-9)
        cursor = body.get('cursor')
        if not cursor:
            break

    markets = {}
    tickers = [p['ticker'] for p in positions]
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            m_resp = kalshi.get('/markets', params={'tickers': ','.join(chunk), 'limit': len(chunk)})
            m_resp.raise_for_status()
            markets.update({m['ticker']: m for m in m_resp.json().get('markets', [])})
        except Exception:
            log.exception('get_position_marks: could not fetch market data for %s', chunk)

    out = []
    for p in positions:
        qty = float(p['position_fp'])
        ticker = p['ticker']
        cost = float(p.get('market_exposure_dollars') or 0)
        side = 'YES' if qty > 0 else 'NO'
        mk = markets.get(ticker, {})
        yes_px = _position_mark_price(mk) if mk else None
        if yes_px is None:
            mark = cost
        else:
            mark = abs(qty) * (yes_px if side == 'YES' else 1 - yes_px)
        out.append({'ticker': ticker, 'side': side, 'qty': abs(qty), 'mark_dollars': round(mark, 3),
                    'cost_dollars': round(cost, 3), 'unrealized_dollars': round(mark - cost, 3),
                    'status': mk.get('status'), 'result': mk.get('result') or None})
    return out
