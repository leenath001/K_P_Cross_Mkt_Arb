"""
bot.py — Trading execution module.

Implements strategy steps 6-9:
  6. Place resting YES limit order at Kalshi ask (limit order, no market crossing)
  7. Re-ping Pinnacle every 2 min while order is live; cancel if signal flips
  8. Cancel if 30 min elapsed OR event start is imminent (<5 min away)
  9. Size with partial Kelly, fraction scaled by edge magnitude
"""

import os, sys, time, uuid, threading
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from math import floor
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from KALSHI.k_helpers import kalshi_headers
from theODDS.p_helpers import pinnacle_odds, get_api_usage

BASE_URL          = 'https://api.elections.kalshi.com/trade-api/v2'
TAKER_FEE         = 0.07   # 7% of winnings — fee when crossing the book
MAKER_FEE         = 0.03   # 3% of winnings — fee when resting in the book
KALSHI_POLL       = 10     # seconds between Kalshi status checks
PINNACLE_POLL     = 120    # seconds between Pinnacle re-checks
MAX_DURATION      = 1800   # 30 min max order lifetime (seconds)
PRE_EVENT_BUFFER  = 300    # cancel 5 min before event start (seconds)

# Statuses Kalshi uses to indicate an order is no longer open
_CLOSED_STATUSES = {'filled', 'executed', 'canceled', 'expired'}


# ---------------------------------------------------------------------------
# Step 9 — Kelly Sizing + Edge Calculation
# ---------------------------------------------------------------------------

def _ev(fair_prob: float, price: float, fee_rate: float) -> float:
    """
    Expected value per contract in dollars.
    Kalshi charges fee_rate * winnings on the winning side.
      Win (prob=fair_prob): receive (1-price)*(1-fee_rate)
      Lose               : lose price
    """
    return fair_prob * (1 - price) * (1 - fee_rate) - (1 - fair_prob) * price


def _kelly_fraction(roi: float) -> float:
    """
    Partial Kelly multiplier based on return-on-investment (ev/price).
    Higher ROI → rarer opportunity → size more aggressively.
    """
    if roi < 0.02:
        return 0.10
    elif roi < 0.05:
        return 0.20
    elif roi < 0.10:
        return 0.33
    else:
        return 0.50


def kelly_contracts(fair_prob: float, price: float, bankroll: float,
                    fee_rate: float) -> int:
    """
    Returns the number of YES contracts to buy using partial Kelly.

    Proper binary Kelly with fee on winnings:
        win_amount = (1 - price) * (1 - fee_rate)
        f* = EV / win_amount   [full Kelly fraction of bankroll]
    Scaled by a ROI-adjusted partial fraction.
    Each contract costs `price` dollars.
    """
    ev = _ev(fair_prob, price, fee_rate)
    if ev <= 0:
        return 0
    win_amount = (1 - price) * (1 - fee_rate)
    full_kelly = ev / win_amount
    partial    = _kelly_fraction(ev / price)   # ROI = ev per dollar at risk
    dollar_bet = bankroll * full_kelly * partial
    return max(floor(dollar_bet / price), 1)


# ---------------------------------------------------------------------------
# Step 6 — Order Placement, Cancellation, Status
# ---------------------------------------------------------------------------

def place_order(ticker: str, yes_price_cents: int, count: int,
                expiration_ts: int | None = None,
                post_only: bool = False) -> dict:
    """
    Place a YES limit buy order on Kalshi.
    post_only=True guarantees the order rests in the book —
    if it would cross immediately, the exchange rejects it instead of filling.
    """
    path = '/trade-api/v2/portfolio/orders'
    body: dict = {
        'ticker':          ticker,
        'client_order_id': str(uuid.uuid4()),
        'type':            'limit',
        'action':          'buy',
        'side':            'yes',
        'count':           count,
        'yes_price':       yes_price_cents,
    }
    if expiration_ts:
        body['expiration_ts'] = expiration_ts
    if post_only:
        body['post_only'] = True
    resp = requests.post(
        f'{BASE_URL}/portfolio/orders',
        headers={**kalshi_headers('POST', path), 'Content-Type': 'application/json'},
        json=body,
    )
    resp.raise_for_status()
    return resp.json().get('order', {})


def get_balance() -> float:
    """
    Fetch available balance from Kalshi portfolio (returns dollars).
    Raises RuntimeError with a clear message on auth failure.
    """
    path = '/trade-api/v2/portfolio/balance'
    resp = requests.get(f'{BASE_URL}/portfolio/balance',
                        headers=kalshi_headers('GET', path))
    if resp.status_code == 401:
        raise RuntimeError(
            'Kalshi returned 401 for /portfolio/balance.\n'
            '  Possible causes:\n'
            '    1. API key does not have portfolio/trading permissions\n'
            '    2. Wrong API_KEY in .env\n'
            '    3. API_PRIVATE key is incorrect\n'
            '  Use --bankroll <amount> to skip this check.'
        )
    resp.raise_for_status()
    return resp.json().get('balance', 0) / 100


def cancel_order(order_id: str) -> bool:
    """Cancel an open Kalshi order. Returns True on success."""
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    resp = requests.delete(f'{BASE_URL}/portfolio/orders/{order_id}',
                           headers=kalshi_headers('DELETE', path))
    return resp.status_code in (200, 204)


def get_market_price(ticker: str) -> int | None:
    """
    Fetch the current yes_ask for a Kalshi market in cents.
    Returns None if unavailable.
    """
    path = f'/trade-api/v2/markets/{ticker}'
    resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                        headers=kalshi_headers('GET', path))
    if resp.ok:
        m = resp.json().get('market', {})
        ask = m.get('yes_ask_dollars')
        return round(float(ask) * 100) if ask else None
    return None


def get_order_status(order_id: str) -> dict:
    """
    Fetch the current state of an order directly from Kalshi.
    Tries the single-order endpoint first; falls back to searching
    the open orders list if that returns 404.
    Status is always taken verbatim from Kalshi — never assumed.
    """
    # 1. Try direct lookup
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    resp = requests.get(f'{BASE_URL}/portfolio/orders/{order_id}',
                        headers=kalshi_headers('GET', path))
    if resp.ok:
        return resp.json().get('order', {})

    # 2. Fallback: scan all recent orders for this order_id
    list_path = '/trade-api/v2/portfolio/orders'
    resp2 = requests.get(f'{BASE_URL}/portfolio/orders',
                         headers=kalshi_headers('GET', list_path),
                         params={'limit': 100})
    if resp2.ok:
        for o in resp2.json().get('orders', []):
            if o.get('order_id') == order_id:
                return o

    # 3. Could not confirm status — return unknown so monitor keeps running
    return {'status': 'unknown', 'order_id': order_id}


# ---------------------------------------------------------------------------
# Step 7 — Signal Re-validation
# ---------------------------------------------------------------------------

def _recheck_signal(event_id: str, sport: str, outcome: str,
                    order_price: float, fee_rate: float,
                    dashboard=None, order_id: str | None = None) -> tuple[bool, float | None]:
    """
    Re-fetch Pinnacle odds and check if EV at the original order price
    is still positive given the fee rate.
    Returns (signal_valid, fresh_fair_prob).
    Conservative: any fetch error returns (False, None).
    """
    try:
        fresh = pinnacle_odds(sports=[sport], hrs=72, live=False)

        if dashboard is not None:
            used, remaining = get_api_usage()
            dashboard.set_api_usage(used, remaining)

        match = fresh[
            (fresh['event_id'] == event_id) &
            (fresh['outcome']  == outcome)
        ]
        if match.empty:
            return False, None

        fresh_fair = float(match.iloc[0]['fair_prob'])
        fresh_ev   = _ev(fresh_fair, order_price, fee_rate)

        if dashboard is not None and order_id is not None:
            dashboard.update(order_id, fair_prob=fresh_fair, edge=fresh_ev)

        return fresh_ev > 0, fresh_fair
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# Steps 7 & 8 — Monitor Loop
# ---------------------------------------------------------------------------

def _monitor(order_id: str, ticker: str, event_id: str, sport: str, outcome: str,
             order_price: float, fee_rate: float, commence_utc: datetime,
             kalshi_poll: int = KALSHI_POLL,
             pinnacle_poll: int = PINNACLE_POLL,
             max_duration: int = MAX_DURATION,
             pre_event_buffer: int = PRE_EVENT_BUFFER,
             dashboard=None,
             stop_event: threading.Event | None = None) -> str:
    """
    Two independent polling rates:
      - Kalshi  : every `kalshi_poll`   seconds — status, live price, remaining cts
      - Pinnacle: every `pinnacle_poll` seconds — fair prob, edge update

    Kill conditions:
      - Kalshi confirms order closed (executed / canceled / expired)
      - 30-min hard cap elapsed
      - Event starts in < pre_event_buffer seconds
      - Pinnacle signal has flipped (EV gone negative)
    """
    start          = time.time()
    last_pinnacle  = start  # first Pinnacle re-check after pinnacle_poll seconds

    while True:
        now     = time.time()
        elapsed = now - start
        now_utc = datetime.now(timezone.utc)

        # ── Kalshi ping (every kalshi_poll seconds) ──────────────────────────
        order     = get_order_status(order_id)
        status    = order.get('status', 'unknown')
        remaining = order.get('remaining_count_fp')
        live_price = order.get('yes_price_dollars')

        market_ask = get_market_price(ticker)
        if dashboard:
            dashboard.update(
                order_id,
                status=status,
                contracts=round(float(remaining)) if remaining is not None else None,
                market_ask=market_ask,
            )

        if status in _CLOSED_STATUSES:
            return f'order_{status}'

        # ── User-initiated shutdown ──────────────────────────────────────────
        if stop_event and stop_event.is_set():
            cancel_order(order_id)
            if dashboard:
                dashboard.update(order_id, status='canceled')
            return 'user_canceled'

        # ── Time-based kill conditions ───────────────────────────────────────
        if elapsed >= max_duration:
            cancel_order(order_id)
            if dashboard:
                dashboard.update(order_id, status='canceled')
            return 'max_duration_exceeded'

        to_event = (commence_utc - now_utc).total_seconds()
        if to_event <= pre_event_buffer:
            cancel_order(order_id)
            if dashboard:
                dashboard.update(order_id, status='canceled')
            return 'event_imminent'

        # ── Pinnacle ping (every pinnacle_poll seconds) ──────────────────────
        if now - last_pinnacle >= pinnacle_poll:
            valid, _ = _recheck_signal(event_id, sport, outcome, order_price,
                                       fee_rate, dashboard=dashboard,
                                       order_id=order_id)
            last_pinnacle = time.time()
            if not valid:
                cancel_order(order_id)
                if dashboard:
                    dashboard.update(order_id, status='signal_flipped')
                return 'signal_flipped'

        time.sleep(kalshi_poll)


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def run_trade(signal_row: pd.Series, bankroll: float,
              taker_fee: float = TAKER_FEE,
              maker_fee: float = MAKER_FEE,
              limit_only: bool = False,
              max_duration: int = MAX_DURATION,
              pre_event_buffer: int = PRE_EVENT_BUFFER,
              dashboard=None,
              stop_event: threading.Event | None = None) -> dict:
    """
    Execute a single trade for one signaled row from kalshi_odds().

    Cross-or-rest logic:
      1. Check EV at yes_ask with taker_fee — if positive, cross the book.
      2. Else check EV at yes_bid with maker_fee — if positive, rest at bid.
      3. Else skip — no edge after fees either way.
    """
    ticker    = signal_row['k_ticker']
    fair_prob = float(signal_row['fair_prob'])
    yes_ask   = float(signal_row['yes_ask'])
    yes_bid   = float(signal_row['yes_bid']) if signal_row['yes_bid'] is not None else None
    commence  = signal_row['commence']
    event_id  = signal_row['event_id']
    sport     = signal_row['sport']
    outcome   = signal_row['outcome']

    # Determine price and fee_rate (cross vs rest)
    taker_ev = _ev(fair_prob, yes_ask, taker_fee)
    if not limit_only and taker_ev > 0:
        order_price = yes_ask
        fee_rate    = taker_fee
        order_type  = 'cross'
    elif yes_bid is not None and _ev(fair_prob, yes_bid, maker_fee) > 0:
        order_price = yes_bid
        fee_rate    = maker_fee
        order_type  = 'rest'
    else:
        ev_at_ask = taker_ev
        if dashboard:
            skip_id = f'skip_{ticker}'
            dashboard.add_position(skip_id, ticker, outcome, 0,
                                   round(yes_ask * 100), fair_prob, ev_at_ask)
            dashboard.update(skip_id, status='skipped')
        return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    ev            = _ev(fair_prob, order_price, fee_rate)
    price_cents   = round(order_price * 100)
    contracts     = kelly_contracts(fair_prob, order_price, bankroll, fee_rate)

    # Compute server-side expiry
    now_utc      = datetime.now(timezone.utc)
    commence_utc = pd.Timestamp(commence).tz_convert('UTC').to_pydatetime()
    expiry_dt    = min(now_utc + timedelta(seconds=max_duration),
                       commence_utc - timedelta(seconds=pre_event_buffer))

    if expiry_dt <= now_utc:
        if dashboard:
            skip_id = f'skip_{ticker}'
            dashboard.add_position(skip_id, ticker, outcome, 0,
                                   price_cents, fair_prob, ev)
            dashboard.update(skip_id, status='skipped')
        return {'status': 'skipped', 'reason': 'event_too_soon',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    # Step 6: Place order
    order    = place_order(ticker, price_cents, contracts,
                           int(expiry_dt.timestamp()),
                           post_only=limit_only)
    order_id = order.get('order_id')

    # Log actual fees charged vs assumed — helps calibrate fee assumptions
    actual_taker_fee = float(order.get('taker_fees_dollars') or 0)
    actual_maker_fee = float(order.get('maker_fees_dollars') or 0)
    assumed_fee_cost = ev * -1 * 0  # placeholder
    print(f'[fee check] actual taker=${actual_taker_fee:.4f}  maker=${actual_maker_fee:.4f}'
          f'  assumed_rate={fee_rate*100:.0f}%  contracts={contracts}  price={price_cents}¢')

    if dashboard:
        dashboard.add_position(order_id, ticker, outcome, contracts,
                               price_cents, fair_prob, ev)

    # Steps 7 & 8: Monitor — always run, status comes live from Kalshi
    reason = _monitor(
        order_id=order_id, ticker=ticker, event_id=event_id,
        sport=sport, outcome=outcome,
        order_price=order_price, fee_rate=fee_rate,
        commence_utc=commence_utc,
        max_duration=max_duration, pre_event_buffer=pre_event_buffer,
        dashboard=dashboard, stop_event=stop_event,
    )

    final = get_order_status(order_id)
    return {
        'order_id':   order_id,
        'ticker':     ticker,
        'outcome':    outcome,
        'contracts':  contracts,
        'yes_price':  price_cents,
        'fair_prob':  fair_prob,
        'ev':         round(ev, 4),
        'order_type': order_type,
        'status':     final.get('status'),
        'reason':     reason,
    }


# ---------------------------------------------------------------------------
# Batch Runner (threaded — one thread per signal)
# ---------------------------------------------------------------------------

def run_all_signals(signals_df: pd.DataFrame, bankroll: float,
                    taker_fee: float = TAKER_FEE,
                    maker_fee: float = MAKER_FEE,
                    limit_only: bool = False,
                    dashboard=None,
                    stop_event: threading.Event | None = None) -> list[dict]:
    """
    Run trades in parallel (one thread per signal).
    Deduplicates on k_ticker — each Kalshi market is traded at most once.
    Pass a threading.Event as stop_event to cancel all orders on demand.
    """
    active = (signals_df[signals_df['signal']]
              .drop_duplicates(subset='k_ticker')
              .copy())

    results = [None] * len(active)
    lock    = threading.Lock()

    def _trade(row, idx):
        result = run_trade(row, bankroll=bankroll,
                           taker_fee=taker_fee, maker_fee=maker_fee,
                           limit_only=limit_only, dashboard=dashboard,
                           stop_event=stop_event)
        with lock:
            results[idx] = result

    threads = [
        threading.Thread(target=_trade, args=(row, i), daemon=True)
        for i, (_, row) in enumerate(active.iterrows())
    ]
    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        # Signal all monitors to cancel their orders
        if stop_event:
            stop_event.set()
        # Wait for every thread to finish canceling before returning
        for t in threads:
            t.join()
        raise

    return results
