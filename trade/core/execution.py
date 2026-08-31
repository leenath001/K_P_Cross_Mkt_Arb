"""
trade/core/execution.py — Kalshi order execution primitives.

Strategy-agnostic: placing/canceling/monitoring orders, Kelly sizing, and EV math.
Every strategy in trade/strategies/ builds on these rather than reimplementing them
— the three-way drift between bot.py/prospect.py/nothing.py (three different
already_bet_tickers() implementations, inconsistent fee handling before this
module existed) is exactly the bug class this consolidation removes.
"""

import os, sys, time, uuid, threading, random
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from math import floor
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from KALSHI.k_helpers import kalshi_headers, fee_rate_for, kalshi_fee_dollars
from theODDS.p_helpers import pinnacle_odds, get_api_usage
from applog import get_logger

log = get_logger(__name__)

BASE_URL          = 'https://api.elections.kalshi.com/trade-api/v2'
# Fallback-only defaults, used if a series' live fee lookup fails. Real fee rates are
# fetched per-series via fee_rate_for() (KALSHI/k_helpers.py) — verified to genuinely
# vary: e.g. KXMLBGAME has a 0.5x fee_multiplier, several soccer/boxing series charge
# NO maker fee at all. See kalshi.com/docs/kalshi-fee-schedule.pdf for the source formula.
TAKER_FEE         = 0.07   # Kalshi's general-table taker rate
MAKER_FEE         = 0.0175 # Kalshi's general-table maker rate (0.25x taker)
MIN_CROSS_EV      = 0.005  # minimum EV required to fire or execute a taker (cross) order
KALSHI_POLL       = 10     # seconds between Kalshi status checks
PINNACLE_POLL     = 120    # seconds between Pinnacle re-checks
MAX_DURATION      = 1800   # 30 min max order lifetime (seconds)
PRE_EVENT_BUFFER  = 300    # cancel 5 min before event start (seconds)
MIN_NOTIONAL      = 5.0    # floor $ risked per trade — see kelly_contracts() docstring

# Statuses Kalshi uses to indicate an order is no longer open
_CLOSED_STATUSES = {'filled', 'executed', 'canceled', 'expired'}


# ---------------------------------------------------------------------------
# Kelly Sizing + Edge Calculation
# ---------------------------------------------------------------------------

def _exact_ev_ok(fair_prob: float, price: float, contracts: int, series: str,
                 maker: bool, min_ev: float) -> bool:
    """
    Final go/no-go check using Kalshi's REAL ceil-to-cent fee (kalshi_fee_dollars),
    not the smooth per-contract rate. The smooth rate _ev()/kelly_contracts() use is
    exact only as contracts -> infinity; cent-rounding can matter a lot at contracts=1
    (e.g. a 5c longshot: smooth fee ~$0.003, real fee rounds up to $0.01 — 3x higher).
    Call this once contracts is known, right before placing the order.
    """
    fee_total  = kalshi_fee_dollars(contracts, price, series, maker)
    ev_exact   = fair_prob - price - fee_total / contracts
    return ev_exact >= min_ev


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

    dollar_bet is floored at MIN_NOTIONAL: real Kalshi fees round UP to the next
    cent per order (see KALSHI/k_helpers.kalshi_fee_dollars), so a 1-contract order
    pays a wildly higher effective rate than the smooth per-contract estimate — e.g.
    a 5c longshot's real fee is ~3x the smooth estimate at 1 contract. The
    monitoring/execution overhead per signal is roughly fixed regardless of size, so
    capturing a real edge at a few dollars notional instead of a few cents is close
    to free extra expected value on trades already being taken (caller's existing
    insufficient_cash check still applies — this floor never forces a trade past
    what the bankroll can afford).
    """
    ev = _ev(fair_prob, price, fee_rate)
    if ev <= 0:
        return 0
    win_amount = (1 - price) * (1 - fee_rate * price)
    full_kelly = ev / win_amount
    partial    = _kelly_fraction(ev / price)   # ROI = ev per dollar at risk
    dollar_bet = max(bankroll * full_kelly * partial, MIN_NOTIONAL)
    return max(floor(dollar_bet / price), 1)


# ---------------------------------------------------------------------------
# Order Placement, Cancellation, Status
# ---------------------------------------------------------------------------

def place_order(ticker: str, price_cents: int, count: int,
                side: str = 'yes',
                expiration_ts: Optional[int] = None,
                post_only: bool = False) -> dict:
    """
    Place a limit buy order on Kalshi for YES or NO contracts.
    post_only=True guarantees the order rests (maker fee) — rejected if it would cross.
    `price_cents` is interpreted as yes_price for side='yes', no_price for side='no'.

    Uses the V2 order endpoint (the legacy /portfolio/orders POST returns 410 Gone).
    V2 quotes everything in YES terms: buying NO is submitted as `ask` (sell YES) at
    the complementary price, since selling YES at P is economically buying NO at 1-P.
    """
    path = '/trade-api/v2/portfolio/events/orders'
    # Kalshi V2 always quotes from the YES side.
    # For NO orders the yes-equivalent price is the complement: 100 - no_price_cents.
    yes_price_cents = (100 - price_cents) if side == 'no' else price_cents
    body: dict = {
        'ticker':                     ticker,
        'client_order_id':            str(uuid.uuid4()),
        'side':                       'ask' if side == 'no' else 'bid',
        'count':                      f'{count:.2f}',
        'price':                      f'{yes_price_cents / 100:.2f}',
        'time_in_force':              'good_till_canceled',
        'self_trade_prevention_type': 'taker_at_cross',
    }
    if expiration_ts:
        body['expiration_time'] = expiration_ts
    if post_only:
        body['post_only'] = True
    log.debug('place_order body=%s', body)
    try:
        resp = requests.post(
            f'{BASE_URL}/portfolio/events/orders',
            headers={**kalshi_headers('POST', path), 'Content-Type': 'application/json'},
            json=body,
        )
    except requests.exceptions.RequestException as exc:
        log.error('place_order network failure for %s: %s', ticker, exc)
        raise
    if not resp.ok:
        if resp.status_code == 404 and 'user_not_found' in resp.text:
            # Kalshi shards some categories onto separate exchange indices (e.g. shard 3
            # = Tennis & Baseball) and requires collateral pre-funded on that specific
            # shard before it will accept an order there — see
            # https://docs.kalshi.com/getting_started/exchange_sharding. This is NOT a
            # transient failure; it recurs on every order for that category until the
            # shard is funded via the Intra-Account Transfer endpoint
            # (POST /portfolio/intra_exchange_instance_transfer).
            log.error('place_order failed for %s: shard-funding gap (404 user_not_found) — '
                     'this ticker routes to an exchange shard with no collateral. Fund it via '
                     'POST /portfolio/intra_exchange_instance_transfer. Response: %s',
                     ticker, resp.text)
        else:
            log.error('place_order failed for %s: %s %s — %s',
                      ticker, resp.status_code, resp.reason, resp.text)
        raise requests.HTTPError(
            f'{resp.status_code} {resp.reason} — {resp.text}', response=resp
        )
    return resp.json()


def get_balance() -> float:
    """
    Fetch available balance from Kalshi portfolio (returns dollars).
    Raises RuntimeError with a clear message on auth failure or network error.
    """
    path = '/trade-api/v2/portfolio/balance'
    try:
        resp = requests.get(f'{BASE_URL}/portfolio/balance',
                            headers=kalshi_headers('GET', path))
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


def cancel_order(ticker: str, order_id: str, max_retries: int = 4) -> bool:
    """
    Cancel an open Kalshi order. Returns True on success, False on any failure (logged).

    `ticker` is REQUIRED and passed as the `market_ticker` query param so Kalshi can
    auto-route the DELETE to the correct exchange shard. Without it, Kalshi defaults
    to shard 0 and 404s on anything else — confirmed live: MLB/Tennis lives on shard 3,
    Combos on 1, Crypto on 2, and every cancel attempt against those was silently
    failing (this is what made cancel_all.py look "broken for MLB games" — it wasn't
    MLB-specific, every non-default-shard order was affected the same way).

    Retries with exponential backoff + jitter on 429 (rate limit). Bulk-cancel paths
    ("Cancel all" waking every _monitor() thread at once, _force_cancel_all() looping
    over many orders) can burst past Kalshi's write-token budget — observed in
    production as repeated 429s on cancel_order. Without a retry, that 429 was final:
    cancel_order returned False and the order stayed open, silently, even though the
    user believed "Cancel all" had closed it.
    """
    path = f'/trade-api/v2/portfolio/events/orders/{order_id}'
    for attempt in range(max_retries + 1):
        try:
            resp = requests.delete(f'{BASE_URL}/portfolio/events/orders/{order_id}',
                                   headers=kalshi_headers('DELETE', path),
                                   params={'market_ticker': ticker})
        except requests.exceptions.RequestException as exc:
            log.error('cancel_order network failure for %s (%s): %s', order_id, ticker, exc)
            return False
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get('Retry-After')
            try:
                delay = float(retry_after) if retry_after else (0.4 * (2 ** attempt))
            except ValueError:
                delay = 0.4 * (2 ** attempt)
            delay += random.uniform(0, 0.3)  # jitter — desyncs threads that got rate-limited together
            log.warning('cancel_order rate-limited for %s — retrying in %.1fs (attempt %d/%d)',
                       order_id, delay, attempt + 1, max_retries)
            time.sleep(delay)
            continue
        log.warning('cancel_order failed for %s (%s): %s %s — %s',
                    order_id, ticker, resp.status_code, resp.reason, resp.text)
        return False
    return False


def ensure_canceled(ticker: str, order_id: str, max_attempts: int = 5,
                    poll_delay: float = 0.5) -> bool:
    """
    Cancel an order and VERIFY it actually closed — don't just trust cancel_order()'s
    return value as proof the order is dead. A 200/204 means Kalshi accepted the
    cancel REQUEST; eventual consistency means a follow-up GET can still show it
    resting for a beat (see _final_order_status). Re-issues the cancel if a
    confirming GET still shows the order open, instead of firing one DELETE and
    hoping. Checks status FIRST each loop so an already-closed order costs one GET,
    not a wasted DELETE.

    Use this (not bare cancel_order) anywhere the caller is about to act on the
    assumption the order is gone — e.g. re-placing as a cross, re-resting at a new
    price. A false "canceled" there risks double exposure (old + new order both live).

    Returns True once Kalshi confirms closed, False if it gives up after
    max_attempts (logged as an error — this is a real "still can't confirm cancel"
    situation, not a routine retry).
    """
    status = 'unknown'
    for attempt in range(max_attempts):
        status = get_order_status(order_id).get('status', 'unknown')
        if status in _CLOSED_STATUSES:
            return True
        cancel_order(ticker, order_id)
        time.sleep(poll_delay * (attempt + 1))
    status = get_order_status(order_id).get('status', 'unknown')
    if status in _CLOSED_STATUSES:
        return True
    log.error('ensure_canceled: gave up on %s (%s) after %d attempts — still %s',
             order_id, ticker, max_attempts, status)
    return False


def get_market_price(ticker: str) -> Optional[int]:
    """Fetch the current yes_ask for a Kalshi market in cents. Returns None if unavailable (logged)."""
    path = f'/trade-api/v2/markets/{ticker}'
    try:
        resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                            headers=kalshi_headers('GET', path))
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
    path = f'/trade-api/v2/markets/{ticker}'
    try:
        resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                            headers=kalshi_headers('GET', path))
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
                result[key] = round(float(v) * 100)
        return result
    log.warning('get_market_prices failed for %s: %s %s', ticker, resp.status_code, resp.reason)
    return {}


def _rest_price_cents(bid_cents: Optional[int], ask_cents: int) -> int:
    """
    Spread-aware rest price (in cents).
      2¢ spread: bid+1 (= ask-1) — tightens spread to 1¢
      1¢ spread: bid   (= ask-1) — join the best bid
      >2¢ spread: ask-1          — stay near top of book
    Always returns a price that will not cross the book.
    """
    if bid_cents is not None:
        spread = ask_cents - bid_cents
        if spread == 2:
            return ask_cents - 1   # = bid + 1
        if spread == 1:
            return bid_cents       # = ask - 1, join bid
    return ask_cents - 1           # fallback: top of book


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

    `taker_fee` is IGNORED in favor of the live per-series rate (fee_rate_for) —
    callers (including the "Cross & Cancel" button, which passes a UI slider value)
    can't be trusted to know the correct series-specific rate; deriving it here from
    `ticker` makes this function correct regardless of what's passed in.
    """
    taker_fee = fee_rate_for(str(ticker).split('-')[0], maker=False)
    ask_key = 'no_ask' if side == 'no' else 'yes_ask'

    # ── 1. Get current Kalshi ask ────────────────────────────────────────────
    prices    = get_market_prices(ticker)
    ask_cents = prices.get(ask_key)
    if ask_cents is None:
        log.warning('cross_and_cancel_order: no ask price available for %s', ticker)
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
                ensure_canceled(ticker, order_id)
                return {'action': 'canceled', 'ticker': ticker,
                        'reason': 'outcome not found in Pinnacle — canceled without cross'}
            yes_fair = float(match.iloc[0]['fair_prob'])
            fair = (1 - yes_fair) if side == 'no' else yes_fair
        except Exception as exc:
            log.exception('cross_and_cancel_order: Pinnacle ping failed for %s', ticker)
            return {'action': 'error', 'ticker': ticker,
                    'reason': f'Pinnacle ping failed: {exc}'}
    else:
        return {'action': 'error', 'ticker': ticker,
                'reason': 'no fair prob source (pass fair_override or event_id+sport+outcome)'}

    # ── 3. EV check with fresh fair prob ────────────────────────────────────
    ev = _ev(fair, ask, taker_fee)

    # ── 4. Always cancel the resting order — verified, not just requested, since
    #        we're about to place a NEW order and can't risk both being live ──
    if not ensure_canceled(ticker, order_id):
        return {'action': 'error', 'ticker': ticker, 'reason': 'could not confirm cancel'}

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
    if not _exact_ev_ok(fair, ask2, contracts, str(ticker).split('-')[0],
                        maker=False, min_ev=MIN_CROSS_EV):
        return {
            'action': 'canceled', 'ticker': ticker,
            'ask': ask2, 'fair': fair, 'ev': round(ev2, 4),
            'reason': 'EV negative after exact (ceil-to-cent) fee at this contract count',
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
        log.exception('cross_and_cancel_order: cross re-place failed for %s', ticker)
        return {'action': 'error', 'ticker': ticker, 'reason': str(exc)}


def cancel_and_rerest(
    ticker: str, order_id: str, remaining_contracts: int,
    price_cents: int, side: str, commence_str: str,
    buffer_minutes: int = 30,
) -> dict:
    """
    Cancel a resting order and re-place it with GTC = event_start − buffer_minutes.
    post_only=True so the order never crosses (stays as a resting maker order).
    Call stop_event.set() after this to halt the Pinnacle polling loop.
    """
    if remaining_contracts <= 0:
        return {'action': 'skipped', 'ticker': ticker, 'reason': 'no unfilled contracts'}

    try:
        commence_utc = pd.Timestamp(commence_str).tz_convert('UTC').to_pydatetime()
    except Exception as exc:
        return {'action': 'error', 'ticker': ticker, 'reason': f'bad commence: {exc}'}

    new_expiry = commence_utc - timedelta(minutes=buffer_minutes)
    now_utc    = datetime.now(timezone.utc)
    if new_expiry <= now_utc:
        return {'action': 'error', 'ticker': ticker,
                'reason': f'event starts in under {buffer_minutes}min — too close to re-rest'}

    if not ensure_canceled(ticker, order_id):
        return {'action': 'error', 'ticker': ticker, 'reason': 'could not confirm cancel'}

    try:
        order  = place_order(ticker, price_cents, remaining_contracts, side=side,
                             expiration_ts=int(new_expiry.timestamp()), post_only=True)
        new_id = order.get('order_id')
        return {
            'action':       'rested',
            'ticker':       ticker,
            'new_order_id': new_id,
            'price_cents':  price_cents,
            'expiry':       new_expiry.strftime('%Y-%m-%d %H:%M UTC'),
        }
    except Exception as exc:
        log.exception('cancel_and_rerest: re-place failed for %s', ticker)
        return {'action': 'error', 'ticker': ticker, 'reason': f'place failed: {exc}'}


def get_order_status(order_id: str) -> dict:
    """
    Fetch the current state of an order directly from Kalshi.
    Tries the single-order endpoint first; falls back to searching
    the open orders list if that returns 404.
    Status is always taken verbatim from Kalshi — never assumed.

    Both underlying calls are unaffected by the exchange-sharding issue that hits
    cancel_order(): the list endpoint returns cross-shard results by default
    (verified live), so no ticker/exchange_index is needed here.
    """
    try:
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
    except requests.exceptions.RequestException as exc:
        log.warning('get_order_status network failure for %s: %s', order_id, exc)

    # 3. Could not confirm status — return unknown so monitor keeps running
    return {'status': 'unknown', 'order_id': order_id}


def _final_order_status(order_id: str, retries: int = 4, delay: float = 0.5) -> dict:
    """
    Fetch the definitive post-monitor order status for logging.

    _monitor() may have just canceled moments before returning — Kalshi's
    cancellation can lag behind the DELETE response by a beat, so a GET issued
    immediately after can catch a stale 'resting' snapshot. If that gets written to
    log_trade() as the "final" status, the CSV permanently shows
    result=PENDING/final_status=resting even though the order is long since
    canceled. (Confirmed empirically: every "PENDING+resting" row sampled from the
    log showed 'canceled' or aged-out 'unknown' when queried live, never resting.)
    ensure_canceled() at the call site now makes this far less likely, but this
    retry stays as a second line of defense for the final status read itself.

    Retry briefly until the status is no longer 'resting', or give up and
    return the last read after `retries` attempts.
    """
    order = {}
    for attempt in range(retries):
        order = get_order_status(order_id)
        if order.get('status') != 'resting':
            return order
        time.sleep(delay)
    log.warning('_final_order_status: %s still showing resting after %d retries '
               '(%.1fs) — logging as-is', order_id, retries, retries * delay)
    return order


# ---------------------------------------------------------------------------
# Signal Re-validation
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
        log.exception('_recheck_signal failed for event=%s outcome=%s', event_id, outcome)
        return False, None


# ---------------------------------------------------------------------------
# Monitor Loop
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
    consecutive_errors = 0

    while True:
        try:
            now     = time.time()
            elapsed = now - start
            now_utc = datetime.now(timezone.utc)

            # ── Kalshi ping (every kalshi_poll seconds) ──────────────────────
            order      = get_order_status(order_id)
            status     = order.get('status', 'unknown')
            fill_fp    = order.get('fill_count_fp')
            filled     = round(float(fill_fp)) if fill_fp is not None else None

            # VWAP fill price INCLUDING fees = (fill cost + fees paid) / contracts
            # filled. Kalshi reports cost and fees as separate fields on the order
            # (taker_fill_cost_dollars/maker_fill_cost_dollars are the raw notional;
            # taker_fees_dollars/maker_fees_dollars are the fee charged on top) — cost
            # alone understates true cost basis. Confirmed empirically: a $0.37 rest
            # fill with $0.0041 in fees shows "Avg price" as 37.41c in Kalshi's own
            # UI, not 37.00c — this matches their convention.
            avg_fill_price = None
            if filled:
                try:
                    total_cost = (float(order.get('taker_fill_cost_dollars') or 0) +
                                  float(order.get('maker_fill_cost_dollars') or 0) +
                                  float(order.get('taker_fees_dollars') or 0) +
                                  float(order.get('maker_fees_dollars') or 0))
                    avg_fill_price = round(total_cost / filled, 4)
                except (TypeError, ValueError):
                    log.debug('_monitor: could not compute avg fill price for order %s', order_id)

            market_ask = get_market_price(ticker)
            if dashboard:
                dashboard.update(
                    order_id,
                    status=status,
                    filled=filled,
                    market_ask=market_ask,
                    avg_fill_price=avg_fill_price,
                )

            if status in _CLOSED_STATUSES:
                return f'order_{status}'

            # ── User-initiated shutdown ────────────────────────────────────
            if stop_event and stop_event.is_set():
                # A single "Cancel all" click sets stop_event once, waking every
                # _monitor() thread on the same tick — without spreading them out,
                # they all fire their DELETE within the same instant and can trip
                # Kalshi's write-token rate limit (cancel_order() retries on 429, but
                # avoiding the burst in the first place is cheaper and faster).
                time.sleep(random.uniform(0, 0.5))
                ensure_canceled(ticker, order_id)
                if dashboard:
                    dashboard.update(order_id, status='canceled')
                return 'user_canceled'

            # ── Time-based kill conditions ─────────────────────────────────
            # effective_max_duration is re-read every iteration (not captured once at
            # thread start) so an "Extend time" click in the UI pushes back every
            # running order's deadline immediately, not just new ones.
            extra_seconds = dashboard.get_extra_seconds() if dashboard else 0
            if elapsed >= max_duration + extra_seconds:
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
                    ensure_canceled(ticker, order_id)
                    _action = 'exceeded'
                if dashboard:
                    dashboard.update(order_id, status='canceled')
                return f'max_duration_{_action}'

            to_event = (commence_utc - now_utc).total_seconds()
            if to_event <= pre_event_buffer:
                ensure_canceled(ticker, order_id)
                if dashboard:
                    dashboard.update(order_id, status='canceled')
                return 'event_imminent'

            # ── Pinnacle ping (every pinnacle_poll seconds) ─────────────────
            if now - last_pinnacle >= pinnacle_poll:
                valid, _ = _recheck_signal(event_id, sport, outcome, order_price,
                                           fee_rate, dashboard=dashboard,
                                           order_id=order_id, side=side)
                last_pinnacle = time.time()
                if not valid:
                    ensure_canceled(ticker, order_id)
                    if dashboard:
                        dashboard.update(order_id, status='signal_flipped')
                    return 'signal_flipped'

            consecutive_errors = 0

        except Exception:
            # A monitor thread dying silently orphans a live/resting order with
            # nothing watching it — always log and keep polling instead of exiting.
            consecutive_errors += 1
            log.exception('_monitor iteration failed for order %s (ticker=%s, '
                          'consecutive_errors=%d)', order_id, ticker, consecutive_errors)
            if consecutive_errors >= 20:
                # ~ same order of magnitude as max_duration at the default poll rate —
                # something is persistently broken, not transient. Cancel rather than
                # leave an unmonitored resting order alive indefinitely.
                log.error('_monitor giving up on order %s after %d consecutive errors — canceling',
                         order_id, consecutive_errors)
                ensure_canceled(ticker, order_id)
                if dashboard:
                    dashboard.update(order_id, status='canceled')
                return 'monitor_error_giveup'

        # Interruptible sleep — wakes immediately when stop_event is set
        if stop_event:
            if stop_event.wait(kalshi_poll):
                continue
        else:
            time.sleep(kalshi_poll)


def force_cancel_all(order_registry: list) -> int:
    """
    Cancel every (order_id, ticker) in the registry that isn't already terminal.
    order_registry: list of (order_id, ticker) tuples.
    """
    canceled = 0
    for i, entry in enumerate(list(order_registry)):
        if not entry:
            continue
        oid, ticker = entry
        if not oid:
            continue
        try:
            status = get_order_status(oid).get('status', 'unknown')
            if status in _CLOSED_STATUSES:
                continue
            if ensure_canceled(ticker, oid):
                canceled += 1
        except Exception:
            # Best-effort — try remaining orders even if one fails
            log.exception('force_cancel_all: failed to cancel order %s (%s)', oid, ticker)
        # Small throttle between orders — this loop can run 20-30+ cancels back to
        # back (e.g. Ctrl+C mid-session), which was enough on its own to trip
        # Kalshi's write-token rate limit even before cancel_order()'s own retry logic.
        if i < len(order_registry) - 1:
            time.sleep(0.15)
    return canceled
