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

        return fresh_ev > 0, our_fair
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# Steps 7 & 8 — Monitor Loop
# ---------------------------------------------------------------------------

def _monitor(order_id: str, ticker: str, event_id: str, sport: str, outcome: str,
             order_price: float, fee_rate: float, commence_utc: datetime,
             side: str = 'yes',
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

    side='yes' (default): Cross-or-rest YES logic unchanged.
      1. Check EV at yes_ask with taker_fee — if positive, cross the book.
      2. Else check EV at yes_bid+1¢ with maker_fee — if positive, rest.
      3. Else skip.

    side='no': Resting NO order at no_ask, always maker (post_only=True).
      Signal: EV > 0 using (1-fair_prob) vs no_ask at maker_fee.
      No cross attempt — NO orders always rest.
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

    # ── NO side: resting limit just below no_ask (top of book), maker fee ───
    if side == 'no':
        if no_ask is None:
            return {'status': 'skipped', 'reason': 'no_ask_unavailable',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        fair_prob_no = 1 - fair_prob
        # Gate uses the fee that will actually be charged
        _gate_fee = taker_fee if force_cross else maker_fee
        if _ev(fair_prob_no, no_ask, _gate_fee) <= 0:
            return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        # Cross at no_ask (taker) or rest 1¢ below it (maker)
        if force_cross:
            order_price = no_ask
            fee_rate    = taker_fee
            order_type  = 'no_cross'
        else:
            order_price = round(no_ask - 0.01, 2)
            fee_rate    = maker_fee
            order_type  = 'no_rest'
        if order_price < 0.01:
            return {'status': 'skipped', 'reason': 'no_ask_too_low',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
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
                                   price_cents, fair_prob_no, ev)

        reason = _monitor(
            order_id=order_id, ticker=ticker, event_id=event_id,
            sport=sport, outcome=outcome,
            order_price=order_price, fee_rate=fee_rate,
            commence_utc=commence_utc, side='no',
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

    # ── YES side (default): cross-or-rest ────────────────────────────────────
    taker_ev = _ev(fair_prob, yes_ask, taker_fee)
    if not limit_only and taker_ev > 0:
        order_price = yes_ask
        fee_rate    = taker_fee
        order_type  = 'cross'
    elif yes_bid is not None and _ev(fair_prob, round(yes_bid + 0.01, 2), maker_fee) > 0:
        order_price = round(yes_bid + 0.01, 2)
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

    if order_type == 'cross':
        live_ask_cents = get_market_price(ticker)
        if live_ask_cents is not None:
            order_price = live_ask_cents / 100
            ev          = _ev(fair_prob, order_price, fee_rate)
            if ev <= 0:
                return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                        'ticker': ticker, 'order_id': None, 'contracts': 0}
            price_cents = live_ask_cents
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
                               price_cents, fair_prob, ev)

    reason = _monitor(
        order_id=order_id, ticker=ticker, event_id=event_id,
        sport=sport, outcome=outcome,
        order_price=order_price, fee_rate=fee_rate,
        commence_utc=commence_utc,
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
    signal_col = 'signal_no' if side == 'no' else 'signal'
    active = (signals_df[signals_df.get(signal_col, signals_df['signal'])]
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
            result = {
                'status':  'error',
                'ticker':  row.get('k_ticker', ''),
                'outcome': row.get('outcome', ''),
                'reason':  str(exc),
                'order_id': None,
                'contracts': 0,
            }
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
        print('\n[shutdown] Ctrl+C received — canceling orders...')
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
        # Safety net: cancel any order this run placed that isn't already closed
        n = _force_cancel_all(order_registry)
        if n > 0:
            print(f'[shutdown] force-canceled {n} open order(s)')

    return results
