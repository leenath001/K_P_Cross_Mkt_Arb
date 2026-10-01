"""
trade/core/execution/market_data.py — Market data: top-of-book prices, orderbook depth, and the resting price on a market's own tick grid.
"""
import requests
import pandas as pd
from typing import Optional

from KALSHI import client as kalshi
from applog import get_logger
from trade.core.pricing import rest_price_cents, to_cents, parse_ranges


log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split


def get_market_price(ticker: str) -> Optional[int]:
    """Fetch the current yes_ask for a Kalshi market in cents. Returns None if unavailable (logged)."""
    try:
        resp = kalshi.get(f'/markets/{ticker}')
    except requests.exceptions.RequestException as exc:
        log.warning('get_market_price network failure for %s: %s', ticker, exc)
        return None
    if resp.ok:
        m = resp.json().get('market', {})
        ask = m.get('yes_ask_dollars')
        return round(float(ask) * 100) if ask else None
    log.warning('get_market_price failed for %s: %s %s', ticker, resp.status_code, resp.reason)
    return None


def get_market_prices(ticker: str) -> dict:
    """
    Fetch current yes_ask, yes_bid, no_ask, no_bid for a market in cents.
    Returns a dict with whichever keys are available, or {} on failure (logged).
    """
    try:
        resp = kalshi.get(f'/markets/{ticker}')
    except requests.exceptions.RequestException as exc:
        log.warning('get_market_prices network failure for %s: %s', ticker, exc)
        return {}
    if resp.ok:
        m = resp.json().get('market', {})
        result = {}
        for key, field in [
            ('yes_ask', 'yes_ask_dollars'), ('yes_bid', 'yes_bid_dollars'),
            ('no_ask',  'no_ask_dollars'),  ('no_bid',  'no_bid_dollars'),
        ]:
            v = m.get(field)
            if v:
                result[key] = to_cents(v)
        result['ranges'] = parse_ranges(m.get('price_ranges'))
        return result
    log.warning('get_market_prices failed for %s: %s %s', ticker, resp.status_code, resp.reason)
    return {}


def get_orderbook_depth(ticker: str, levels: int = 2) -> dict:
    """
    Fetch the top `levels` YES-side bid and ask levels for a market, each with
    the resting notional (price × quantity, in dollars), plus the last traded
    price. For the click-to-inspect order panel in the live dashboard.

    Kalshi's V2 orderbook (GET /markets/{ticker}/orderbook) only carries two
    ladders — 'yes_dollars' (YES bids) and 'no_dollars' (NO bids), both
    ascending by price with the LAST entry being the best (highest, i.e.
    top-of-book). There's no separate YES-ask ladder because an order to sell
    YES at price P is economically identical to a bid to buy NO at (1 - P), so
    YES asks are derived from the NO-bid ladder: yes_ask = 1 - no_bid, same
    quantity, best NO bid -> best (lowest) YES ask.

    Returns {'last_price_cents': int|None, 'bids': [...], 'asks': [...]} where
    each level is {'price_cents', 'qty', 'dollars'}, best-of-book first. On
    any failure (network/HTTP/malformed body) returns {} (logged) — callers
    should treat that as "depth unavailable right now", not a fatal error.
    """
    try:
        resp = kalshi.get(f'/markets/{ticker}/orderbook', params={'depth': max(levels, 5)})
    except requests.exceptions.RequestException as exc:
        log.warning('get_orderbook_depth network failure for %s: %s', ticker, exc)
        return {}
    if not resp.ok:
        log.warning('get_orderbook_depth failed for %s: %s %s', ticker, resp.status_code, resp.reason)
        return {}

    try:
        book    = resp.json().get('orderbook_fp') or {}
        yes_raw = book.get('yes_dollars') or []
        no_raw  = book.get('no_dollars')  or []

        def _parse(raw_levels):
            out = []
            for p_str, q_str in raw_levels:
                try:
                    out.append((float(p_str), float(q_str)))
                except (TypeError, ValueError):
                    continue
            return out

        yes_bids = sorted(_parse(yes_raw), key=lambda t: -t[0])[:levels]
        no_bids  = sorted(_parse(no_raw),  key=lambda t: -t[0])[:levels]
        yes_asks = sorted(((round(1 - p, 2), q) for p, q in no_bids), key=lambda t: t[0])

        def _fmt(pairs):
            return [{'price_cents': round(p * 100), 'qty': round(q, 2), 'dollars': round(p * q, 2)}
                    for p, q in pairs]

        result = {'last_price_cents': None, 'bids': _fmt(yes_bids), 'asks': _fmt(yes_asks)}
    except Exception:
        log.exception('get_orderbook_depth: malformed orderbook response for %s', ticker)
        return {}

    try:
        m_resp = kalshi.get(f'/markets/{ticker}')
        if m_resp.ok:
            lp = m_resp.json().get('market', {}).get('last_price_dollars')
            if lp is not None:
                result['last_price_cents'] = round(float(lp) * 100)
    except Exception:
        log.warning('get_orderbook_depth: last-price lookup failed for %s (depth still returned)', ticker)

    return result


def rest_price_dollars(prices: dict, side: str) -> Optional[float]:
    """Top-of-book resting price (dollars) from get_market_prices() output: one tick below the ask on the market's own grid."""
    bid, ask = prices.get(f'{side}_bid'), prices.get(f'{side}_ask')
    if ask is None:
        return None
    return rest_price_cents(bid, ask, prices.get('ranges')) / 100


def signal_rest_price(signal_row, side: str) -> float:
    """Resting price (dollars) for a signal row — the value kalshi_odds() precomputed, else derived from its bid/ask."""
    v = signal_row.get(f'rest_price_{side}')
    if v is not None and not pd.isna(v):
        return float(v)
    bid, ask = signal_row.get(f'{side}_bid'), signal_row.get(f'{side}_ask')
    return rest_price_cents(None if bid is None or pd.isna(bid) else bid * 100, ask * 100) / 100
