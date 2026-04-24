"""
bot.py — Trading execution module.

Implements strategy steps 6-9:
  6. Place resting YES limit order at Kalshi ask (limit order, no market crossing)
  7. Re-ping Pinnacle every 2 min while order is live; cancel if signal flips
  8. Cancel if 30 min elapsed OR event start is imminent (<5 min away)
  9. Size with partial Kelly, fraction scaled by edge magnitude
"""

import os, sys, time, uuid, signal, threading
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from math import floor
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from KALSHI.k_helpers import kalshi_headers
from theODDS.p_helpers import pinnacle_odds, get_api_usage
from logger import log_trade, LOG_PATH, NO_LOG_PATH

BASE_URL          = 'https://api.elections.kalshi.com/trade-api/v2'
TAKER_FEE         = 0.07   # 7% of winnings — fee when crossing the book
MAKER_FEE         = 0.03   # 3% of winnings — fee when resting in the book
MIN_CROSS_EV      = 0.005  # minimum EV required to fire or execute a taker (cross) order
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
    Kalshi fee = fee_rate * price * (1-price), charged at entry regardless of outcome.
      EV = fair_prob - price - fee_rate * price * (1-price)
    """
    return fair_prob - price - fee_rate * price * (1 - price)


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

    Kalshi fee model: fee = fee_rate * price * (1-price) per contract (charged at entry).
    Net win per contract = (1-price) - fee = (1-price)*(1 - fee_rate*price)
        f* = EV / win_amount   [full Kelly fraction of bankroll]
    Scaled by a ROI-adjusted partial fraction.
    Each contract costs `price` dollars.
    """
    ev = _ev(fair_prob, price, fee_rate)
    if ev <= 0:
        return 0
    win_amount = (1 - price) * (1 - fee_rate * price)
    full_kelly = ev / win_amount
    partial    = _kelly_fraction(ev / price)   # ROI = ev per dollar at risk
    dollar_bet = bankroll * full_kelly * partial
    return max(floor(dollar_bet / price), 1)


# ---------------------------------------------------------------------------
# Step 6 — Order Placement, Cancellation, Status
# ---------------------------------------------------------------------------

def place_order(ticker: str, price_cents: int, count: int,
                side: str = 'yes',
                expiration_ts: Optional[int] = None,
                post_only: bool = False) -> dict:
    """
    Place a limit buy order on Kalshi for YES or NO contracts.
    post_only=True guarantees the order rests (maker fee) — rejected if it would cross.
    `price_cents` is interpreted as yes_price for side='yes', no_price for side='no'.
    """
    path = '/trade-api/v2/portfolio/orders'
    # Kalshi always uses yes_price regardless of side.
    # For NO orders the yes_price is the complement: 100 - no_price_cents.
    yes_price = (100 - price_cents) if side == 'no' else price_cents
    body: dict = {
        'ticker':          ticker,
        'client_order_id': str(uuid.uuid4()),
        'type':            'limit',
        'action':          'buy',
        'side':            side,
        'count':           count,
        'yes_price':       yes_price,
    }
    if expiration_ts:
        body['expiration_ts'] = expiration_ts
    if post_only:
        body['post_only'] = True
    print(f'[place_order] body={body}')
    resp = requests.post(
        f'{BASE_URL}/portfolio/orders',
        headers={**kalshi_headers('POST', path), 'Content-Type': 'application/json'},
        json=body,
    )
    if not resp.ok:
        raise requests.HTTPError(
            f'{resp.status_code} {resp.reason} — {resp.text}', response=resp
        )
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


def get_market_price(ticker: str) -> Optional[int]:
    """Fetch the current yes_ask for a Kalshi market in cents. Returns None if unavailable."""
    path = f'/trade-api/v2/markets/{ticker}'
    resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                        headers=kalshi_headers('GET', path))
    if resp.ok:
        m = resp.json().get('market', {})
        ask = m.get('yes_ask_dollars')
        return round(float(ask) * 100) if ask else None
    return None


def get_market_prices(ticker: str) -> dict:
    """
    Fetch current yes_ask and no_ask for a Kalshi market in cents.
    Returns {'yes_ask': int, 'no_ask': int} or {} if unavailable.
    """
    path = f'/trade-api/v2/markets/{ticker}'
    resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                        headers=kalshi_headers('GET', path))
    if resp.ok:
        m = resp.json().get('market', {})
        ya = m.get('yes_ask_dollars')
        na = m.get('no_ask_dollars')
        result = {}
        if ya:
            result['yes_ask'] = round(float(ya) * 100)
        if na:
            result['no_ask'] = round(float(na) * 100)
        return result
    return {}


def cross_and_cancel_order(ticker: str, order_id: str, contracts: int,
                           taker_fee: float, side: str = 'yes',
                           event_id: str = '', sport: str = '',
                           outcome: str = '',
                           fair_override: Optional[float] = None) -> dict:
    """
    Cancel a resting order then re-place as a taker cross if a fresh Pinnacle
    fair prob still gives EV >= MIN_CROSS_EV at the current Kalshi ask.

    fair_override: pass when the caller already has a freshly-fetched fair prob
                   (e.g. from _recheck_signal in the monitor loop). When None,
                   this function pings Pinnacle itself using event_id/sport/outcome.
    """
    ask_key = 'no_ask' if side == 'no' else 'yes_ask'

    # ── 1. Get current Kalshi ask ────────────────────────────────────────────
    prices    = get_market_prices(ticker)
    ask_cents = prices.get(ask_key)
    if ask_cents is None:
        return {'action': 'error', 'ticker': ticker, 'reason': 'no ask price available'}
    ask = ask_cents / 100

    # ── 2. Get fresh Pinnacle fair prob ──────────────────────────────────────
    if fair_override is not None:
        fair = fair_override
    elif event_id and sport and outcome:
        try:
            fresh_df = pinnacle_odds(sports=[sport], hrs=72, live=False)
            match = fresh_df[
                (fresh_df['event_id'] == event_id) &
                (fresh_df['outcome']  == outcome)
            ]
            if match.empty:
                cancel_order(order_id)
                return {'action': 'canceled', 'ticker': ticker,
                        'reason': 'outcome not found in Pinnacle — canceled without cross'}
            yes_fair = float(match.iloc[0]['fair_prob'])
            fair = (1 - yes_fair) if side == 'no' else yes_fair
        except Exception as exc:
            return {'action': 'error', 'ticker': ticker,
                    'reason': f'Pinnacle ping failed: {exc}'}
    else:
        return {'action': 'error', 'ticker': ticker,
                'reason': 'no fair prob source (pass fair_override or event_id+sport+outcome)'}

    # ── 3. EV check with fresh fair prob ────────────────────────────────────
    ev = _ev(fair, ask, taker_fee)

    # ── 4. Always cancel the resting order ───────────────────────────────────
    if not cancel_order(order_id):
        return {'action': 'error', 'ticker': ticker, 'reason': 'cancel failed'}

    if ev < MIN_CROSS_EV:
        return {
            'action': 'canceled', 'ticker': ticker,
            'ask': ask, 'fair': fair, 'ev': round(ev, 4),
            'reason': f'EV {ev:+.4f} < {MIN_CROSS_EV} — not worth crossing at {ask:.2f}',
        }

    # ── 5. Re-fetch ask immediately before placing (cancel may move book) ───
    prices2    = get_market_prices(ticker)
    ask_cents2 = prices2.get(ask_key, ask_cents)
    ask2       = ask_cents2 / 100
    ev2        = _ev(fair, ask2, taker_fee)
    if ev2 < MIN_CROSS_EV:
        return {
            'action': 'canceled', 'ticker': ticker,
            'ask': ask2, 'fair': fair, 'ev': round(ev2, 4),
            'reason': f'EV {ev2:+.4f} < {MIN_CROSS_EV} after re-fetch',
        }

    try:
        # 5-minute expiry prevents orphaned resting orders if the cross doesn't fill immediately
        _exp_ts = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
        order   = place_order(ticker, ask_cents2, contracts, side=side, post_only=False,
                              expiration_ts=_exp_ts)
        new_oid = order.get('order_id')
        return {
            'action': 'crossed', 'ticker': ticker,
            'ask': ask2, 'fair': fair, 'ev': round(ev2, 4),
            'new_order_id': new_oid, 'contracts': contracts,
        }
    except Exception as exc:
        return {'action': 'error', 'ticker': ticker, 'reason': str(exc)}


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
                    dashboard=None, order_id: Optional[str] = None,
                    side: str = 'yes') -> tuple:
    """
    Re-fetch Pinnacle odds and check if EV at the order price is still
    positive. For side='no', the relevant probability is (1 − fair_prob)
    and the price is a NO price — so we flip both inputs.
    Returns (signal_valid, fresh_fair_prob_of_our_side).
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

        fresh_yes_fair = float(match.iloc[0]['fair_prob'])
        our_fair       = (1 - fresh_yes_fair) if side == 'no' else fresh_yes_fair
        fresh_ev       = _ev(our_fair, order_price, fee_rate)

        if dashboard is not None and order_id is not None:
            dashboard.update(order_id, fair_prob=our_fair, edge=fresh_ev)

        # Cross orders (taker fee) require MIN_CROSS_EV to stay alive;
        # REST orders (maker fee) only need EV > 0.
        min_ev = MIN_CROSS_EV if fee_rate >= TAKER_FEE else 0
        return fresh_ev >= min_ev, our_fair
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# Steps 7 & 8 — Monitor Loop
# ---------------------------------------------------------------------------

def _monitor(order_id: str, ticker: str, event_id: str, sport: str, outcome: str,
             order_price: float, fee_rate: float, commence_utc: datetime,
             side: str = 'yes',
             contracts: int = 1,
             taker_fee: float = TAKER_FEE,
             kalshi_poll: int = KALSHI_POLL,
             pinnacle_poll: int = PINNACLE_POLL,
             max_duration: int = MAX_DURATION,
             pre_event_buffer: int = PRE_EVENT_BUFFER,
             dashboard=None,
             stop_event: Optional[threading.Event] = None) -> str:
    """
    Two independent polling rates:
      - Kalshi  : every `kalshi_poll`   seconds — status, live price, remaining cts
      - Pinnacle: every `pinnacle_poll` seconds — fair prob, edge update

    Kill conditions:
      - Kalshi confirms order closed (executed / canceled / expired)
      - 30-min hard cap elapsed → cross & cancel (cross if EV positive, else cancel)
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
        order      = get_order_status(order_id)
        status     = order.get('status', 'unknown')
        live_price = order.get('yes_price_dollars')
        fill_fp    = order.get('fill_count_fp')

        market_ask = get_market_price(ticker)
        if dashboard:
            dashboard.update(
                order_id,
                status=status,
                filled=round(float(fill_fp)) if fill_fp is not None else None,
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
            # Re-ping Pinnacle for fresh fair prob, then cross if EV >= MIN_CROSS_EV
            _, _fair = _recheck_signal(event_id, sport, outcome, order_price,
                                       fee_rate, dashboard=dashboard,
                                       order_id=order_id, side=side)
            if _fair is not None:
                _xc = cross_and_cancel_order(
                    ticker, order_id, contracts, taker_fee, side,
                    fair_override=_fair,
                )
                _action = _xc.get('action', 'error')
            else:
                cancel_order(order_id)
                _action = 'exceeded'
            if dashboard:
                dashboard.update(order_id, status='canceled')
            return f'max_duration_{_action}'

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
                                       order_id=order_id, side=side)
            last_pinnacle = time.time()
            if not valid:
                cancel_order(order_id)
                if dashboard:
                    dashboard.update(order_id, status='signal_flipped')
                return 'signal_flipped'

        # Interruptible sleep — wakes immediately when stop_event is set
        if stop_event:
            if stop_event.wait(kalshi_poll):
                continue
        else:
            time.sleep(kalshi_poll)


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def run_trade(signal_row: pd.Series, bankroll: float,
              taker_fee: float = TAKER_FEE,
              maker_fee: float = MAKER_FEE,
              limit_only: bool = False,
              force_cross: bool = False,
              side: str = 'yes',
              max_duration: int = MAX_DURATION,
              pre_event_buffer: int = PRE_EVENT_BUFFER,
              dashboard=None,
              stop_event: Optional[threading.Event] = None,
              order_registry: Optional[list] = None) -> dict:
    """
    Execute a single trade for one signaled row from kalshi_odds().

    Both sides share the same logic structure:
      AUTO  : cross at ask (taker fee) if EV > 0, else rest at ask-1¢ (maker fee) if EV > 0, else skip.
      CROSS : cross at ask (taker fee); skip if EV <= 0.
      REST  : rest at ask-1¢ (maker fee); skip if EV <= 0.

    YES: ask = yes_ask,  fair = fair_prob
    NO : ask = no_ask,   fair = 1 - fair_prob
    """
    ticker    = signal_row['k_ticker']
    fair_prob = float(signal_row['fair_prob'])
    yes_ask   = float(signal_row['yes_ask'])
    yes_bid   = float(signal_row['yes_bid']) if signal_row['yes_bid'] is not None else None
    no_ask    = float(signal_row['no_ask'])  if signal_row.get('no_ask') is not None else None
    no_bid    = float(signal_row['no_bid'])  if signal_row.get('no_bid') is not None else None
    commence  = signal_row['commence']
    event_id  = signal_row['event_id']
    sport     = signal_row['sport']
    outcome   = signal_row['outcome']

    # ── NO side: same AUTO/CROSS/REST logic as YES, using no_ask ────────────
    if side == 'no':
        if no_ask is None:
            return {'status': 'skipped', 'reason': 'no_ask_unavailable',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        fair_prob_no = 1 - fair_prob
        rest_price_no = round(no_ask - 0.01, 2)
        taker_ev_no   = _ev(fair_prob_no, no_ask,      taker_fee)
        maker_ev_no   = _ev(fair_prob_no, rest_price_no, maker_fee)
        if force_cross:
            if taker_ev_no < MIN_CROSS_EV:
                return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                        'ticker': ticker, 'order_id': None, 'contracts': 0}
            order_price = no_ask
            fee_rate    = taker_fee
            order_type  = 'no_cross'
        elif not limit_only and taker_ev_no >= MIN_CROSS_EV:
            order_price = no_ask
            fee_rate    = taker_fee
            order_type  = 'no_cross'
        elif maker_ev_no > 0:
            order_price = rest_price_no
            fee_rate    = maker_fee
            order_type  = 'no_rest'
        else:
            return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        if order_price < 0.01:
            return {'status': 'skipped', 'reason': 'no_ask_too_low',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        # Re-fetch live ask before placing to avoid post_only rejection on stale price
        if order_type == 'no_rest':
            live_prices = get_market_prices(ticker)
            live_na = live_prices.get('no_ask')
            if live_na is not None:
                live_rest = round(live_na / 100 - 0.01, 2)
                live_ev   = _ev(fair_prob_no, live_rest, fee_rate)
                if live_ev <= 0:
                    return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                            'ticker': ticker, 'order_id': None, 'contracts': 0}
                order_price = live_rest
        elif order_type == 'no_cross':
            live_prices = get_market_prices(ticker)
            live_na = live_prices.get('no_ask')
            if live_na is not None:
                live_cross = round(live_na / 100, 2)
                live_ev    = _ev(fair_prob_no, live_cross, fee_rate)
                if live_ev <= 0:
                    return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                            'ticker': ticker, 'order_id': None, 'contracts': 0}
                order_price = live_cross

        ev          = _ev(fair_prob_no, order_price, fee_rate)
        price_cents = round(order_price * 100)
        contracts   = kelly_contracts(fair_prob_no, order_price, bankroll, fee_rate)
        if contracts <= 0:
            return {'status': 'skipped', 'reason': 'zero_contracts',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        now_utc      = datetime.now(timezone.utc)
        commence_utc = pd.Timestamp(commence).tz_convert('UTC').to_pydatetime()
        expiry_dt    = min(now_utc + timedelta(seconds=max_duration),
                           commence_utc - timedelta(seconds=pre_event_buffer))
        if expiry_dt <= now_utc:
            return {'status': 'skipped', 'reason': 'event_too_soon',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        order    = place_order(ticker, price_cents, contracts, side='no',
                               expiration_ts=int(expiry_dt.timestamp()),
                               post_only=not force_cross)
        order_id = order.get('order_id')
        if order_registry is not None and order_id:
            order_registry.append(order_id)
        print(f'[no order] {ticker}  no_ask={no_ask}  contracts={contracts}  ev={ev:.4f}')

        if dashboard:
            dashboard.add_position(order_id, ticker, f'NO:{outcome}', contracts,
                                   price_cents, fair_prob_no, ev,
                                   event_id=event_id, sport=sport,
                                   raw_outcome=outcome, fee_rate=fee_rate)

        reason = _monitor(
            order_id=order_id, ticker=ticker, event_id=event_id,
            sport=sport, outcome=outcome,
            order_price=order_price, fee_rate=fee_rate,
            commence_utc=commence_utc, side='no',
            contracts=contracts, taker_fee=taker_fee,
            max_duration=max_duration, pre_event_buffer=pre_event_buffer,
            dashboard=dashboard, stop_event=stop_event,
        )
        final        = get_order_status(order_id)
        final_status = final.get('status', 'unknown')
        log_trade(
            order_id=order_id, sport=sport, outcome=f'NO:{outcome}',
            k_ticker=ticker, commence=commence, order_type=order_type,
            fair_prob=fair_prob_no, yes_ask_at_signal=yes_ask,
            entry_price=order_price, fee_rate=fee_rate,
            ev_per_contract=ev, contracts=contracts,
            final_status=final_status, close_reason=reason,
            side='no',
        )
        return {
            'order_id':   order_id,
            'ticker':     ticker,
            'outcome':    f'NO:{outcome}',
            'contracts':  contracts,
            'no_price':   price_cents,
            'fair_prob':  fair_prob_no,
            'ev':         round(ev, 4),
            'order_type': order_type,
            'status':     final_status,
            'reason':     reason,
        }

    # ── YES side: same AUTO/CROSS/REST logic as NO, using yes_ask ───────────
    rest_price_yes = round(yes_ask - 0.01, 2)
    taker_ev_yes   = _ev(fair_prob, yes_ask,       taker_fee)
    maker_ev_yes   = _ev(fair_prob, rest_price_yes, maker_fee)
    if force_cross:
        if taker_ev_yes < MIN_CROSS_EV:
            if dashboard:
                skip_id = f'skip_{ticker}'
                dashboard.add_position(skip_id, ticker, outcome, 0,
                                       round(yes_ask * 100), fair_prob, taker_ev_yes)
                dashboard.update(skip_id, status='skipped')
            return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        order_price = yes_ask
        fee_rate    = taker_fee
        order_type  = 'cross'
    elif not limit_only and taker_ev_yes >= MIN_CROSS_EV:
        order_price = yes_ask
        fee_rate    = taker_fee
        order_type  = 'cross'
    elif maker_ev_yes > 0:
        order_price = rest_price_yes
        fee_rate    = maker_fee
        order_type  = 'rest'
    else:
        if dashboard:
            skip_id = f'skip_{ticker}'
            dashboard.add_position(skip_id, ticker, outcome, 0,
                                   round(yes_ask * 100), fair_prob, taker_ev_yes)
            dashboard.update(skip_id, status='skipped')
        return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    ev            = _ev(fair_prob, order_price, fee_rate)
    price_cents   = round(order_price * 100)
    contracts     = kelly_contracts(fair_prob, order_price, bankroll, fee_rate)

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

    # Re-fetch live ask before placing to avoid post_only rejection on stale price
    live_ask_cents = get_market_price(ticker)
    if live_ask_cents is not None:
        if order_type == 'cross':
            order_price = live_ask_cents / 100
        else:  # rest: top of book = ask - 1¢
            order_price = round(live_ask_cents / 100 - 0.01, 2)
        ev = _ev(fair_prob, order_price, fee_rate)
        if ev <= 0:
            return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        price_cents = round(order_price * 100)
        contracts   = kelly_contracts(fair_prob, order_price, bankroll, fee_rate)

    if contracts <= 0:
        return {'status': 'skipped', 'reason': 'zero_contracts',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    order    = place_order(ticker, price_cents, contracts, side='yes',
                           expiration_ts=int(expiry_dt.timestamp()),
                           post_only=(order_type == 'rest' or limit_only))
    order_id = order.get('order_id')
    if order_registry is not None and order_id:
        order_registry.append(order_id)

    actual_taker_fee = float(order.get('taker_fees_dollars') or 0)
    actual_maker_fee = float(order.get('maker_fees_dollars') or 0)
    print(f'[fee check] actual taker=${actual_taker_fee:.4f}  maker=${actual_maker_fee:.4f}'
          f'  assumed_rate={fee_rate*100:.0f}%  contracts={contracts}  price={price_cents}¢')

    if dashboard:
        dashboard.add_position(order_id, ticker, outcome, contracts,
                               price_cents, fair_prob, ev,
                               event_id=event_id, sport=sport,
                               raw_outcome=outcome, fee_rate=fee_rate)

    reason = _monitor(
        order_id=order_id, ticker=ticker, event_id=event_id,
        sport=sport, outcome=outcome,
        order_price=order_price, fee_rate=fee_rate,
        commence_utc=commence_utc,
        contracts=contracts, taker_fee=taker_fee,
        max_duration=max_duration, pre_event_buffer=pre_event_buffer,
        dashboard=dashboard, stop_event=stop_event,
    )

    final        = get_order_status(order_id)
    final_status = final.get('status', 'unknown')

    log_trade(
        order_id          = order_id,
        sport             = sport,
        outcome           = outcome,
        k_ticker          = ticker,
        commence          = commence,
        order_type        = order_type,
        fair_prob         = fair_prob,
        yes_ask_at_signal = yes_ask,
        entry_price       = order_price,
        fee_rate          = fee_rate,
        ev_per_contract   = ev,
        contracts         = contracts,
        final_status      = final_status,
        close_reason      = reason,
    )

    return {
        'order_id':   order_id,
        'ticker':     ticker,
        'outcome':    outcome,
        'contracts':  contracts,
        'yes_price':  price_cents,
        'fair_prob':  fair_prob,
        'ev':         round(ev, 4),
        'order_type': order_type,
        'status':     final_status,
        'reason':     reason,
    }


# ---------------------------------------------------------------------------
# Batch Runner (threaded — one thread per signal)
# ---------------------------------------------------------------------------

def _force_cancel_all(order_ids: list) -> int:
    """Cancel any order_id in the list that isn't already in a terminal state."""
    canceled = 0
    for oid in list(order_ids):
        if not oid:
            continue
        try:
            status = get_order_status(oid).get('status', 'unknown')
            if status in _CLOSED_STATUSES:
                continue
            if cancel_order(oid):
                canceled += 1
        except Exception:
            # Best-effort — try remaining orders even if one fails
            pass
    return canceled


def already_bet_tickers() -> set:
    """
    Tickers with an open or pending position across both log files.
    Blocks re-trading any ticker where result=PENDING and the order is
    still active (resting / executed / filled).
    Canceled/expired rows and settled WIN/LOSS rows are not blocked.
    """
    import csv as _csv
    blocked = set()
    for path in (LOG_PATH, NO_LOG_PATH):
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            continue
        with open(path, newline='') as f:
            for row in _csv.DictReader(f):
                if (row.get('result') == 'PENDING' and
                        row.get('final_status', '') in ('resting', 'executed', 'filled')):
                    ticker = row.get('k_ticker') or row.get('ticker', '')
                    if ticker:
                        blocked.add(ticker)
    return blocked


def run_all_signals(signals_df: pd.DataFrame, bankroll: float,
                    taker_fee: float = TAKER_FEE,
                    maker_fee: float = MAKER_FEE,
                    limit_only: bool = False,
                    force_cross: bool = False,
                    side: str = 'yes',
                    max_duration: int = MAX_DURATION,
                    dashboard=None,
                    stop_event: Optional[threading.Event] = None) -> list:
    """
    Run trades in parallel (one thread per signal).
    Deduplicates on k_ticker — each Kalshi market is traded at most once.

    Ctrl+C behavior: sets stop_event, waits for monitors to cancel their own
    orders, then runs a force-cancel pass over any tracked order_id that is
    still open. A second Ctrl+C during cleanup is ignored so cancellation
    always completes.
    """
    # Build the same mask web_app uses so REST/CROSS/AUTO modes are consistent
    def _scol(name):
        if name in signals_df.columns:
            return signals_df[name]
        return pd.Series(False, index=signals_df.index)

    if side == 'no':
        if force_cross:
            sig_mask = _scol('signal_no_cross')
        elif limit_only:
            sig_mask = _scol('signal_no')
        else:
            sig_mask = _scol('signal_no_cross') | _scol('signal_no')
    else:
        if force_cross:
            sig_mask = _scol('signal')
        elif limit_only:
            sig_mask = _scol('signal_yes_rest')
        else:
            sig_mask = _scol('signal') | _scol('signal_yes_rest')

    active = (signals_df[sig_mask]
              .drop_duplicates(subset='k_ticker')
              .copy())

    # Drop tickers with existing open/pending positions (dedup across runs)
    _open = already_bet_tickers()
    if _open:
        before = len(active)
        active = active[~active['k_ticker'].isin(_open)].copy()
        dropped = before - len(active)
        if dropped:
            print(f'  [dedup] Skipped {dropped} ticker(s) with existing open positions')

    # Owned by run_all_signals — every order this run places gets tracked here
    if stop_event is None:
        stop_event = threading.Event()
    order_registry: list = []
    results = [None] * len(active)
    lock    = threading.Lock()

    def _trade(row, idx):
        try:
            result = run_trade(row, bankroll=bankroll,
                               taker_fee=taker_fee, maker_fee=maker_fee,
                               limit_only=limit_only, force_cross=force_cross,
                               side=side, max_duration=max_duration,
                               dashboard=dashboard, stop_event=stop_event,
                               order_registry=order_registry)
        except Exception as exc:
            print(f'  [error] {row.get("k_ticker", "?")}  {type(exc).__name__}: {exc}')
            result = {
                'status':  'error',
                'ticker':  row.get('k_ticker', ''),
                'outcome': row.get('outcome', ''),
                'reason':  str(exc),
                'order_id': None,
                'contracts': 0,
            }
        if result.get('status') in ('skipped', 'error'):
            print(f'  [skip]  {result.get("ticker", "?")}  reason={result.get("reason", "?")}')
        with lock:
            results[idx] = result

    BATCH_SIZE  = 10   # orders fired per wave
    BATCH_DELAY = 10   # seconds to wait between waves

    rows_list = list(active.iterrows())
    threads   = [
        threading.Thread(target=_trade, args=(row, i), daemon=True)
        for i, (_, row) in enumerate(rows_list)
    ]

    try:
        # Fire each batch then immediately move on — don't wait for monitors to finish.
        # All threads run concurrently once started; we join ALL at the end.
        for batch_start in range(0, len(threads), BATCH_SIZE):
            batch   = threads[batch_start : batch_start + BATCH_SIZE]
            n_total = len(threads)
            print(f'  [batch] firing {len(batch)} order(s)  '
                  f'({batch_start}/{n_total} sent so far)')
            for t in batch:
                t.start()
            if batch_start + BATCH_SIZE < len(threads):
                time.sleep(BATCH_DELAY)

        # Wait for every thread (all batches) to complete
        for t in threads:
            t.join()

    except KeyboardInterrupt:
        print('\n[shutdown] Ctrl+C received — canceling ALL orders...')
        # Block further SIGINTs so cleanup always completes
        prev_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            stop_event.set()
            for t in threads:
                t.join(timeout=15)
        finally:
            signal.signal(signal.SIGINT, prev_handler)
        raise
    finally:
        # Cancel every order placed this run, across all batches
        n = _force_cancel_all(order_registry)
        if n > 0:
            print(f'[shutdown] force-canceled {n} open order(s)')

    return results
