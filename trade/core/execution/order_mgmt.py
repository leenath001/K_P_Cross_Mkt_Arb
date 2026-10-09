"""
trade/core/execution/order_mgmt.py — Managing live orders: re-quote on post-only cross, cross-and-cancel to take liquidity, extend a resting order's expiry, resize, and cancel-and-re-rest.
"""
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Optional

from KALSHI.k_helpers import fee_rate_for
from theODDS.p_helpers import pinnacle_odds
from applog import get_logger
from trade.core.pricing import to_cents
from trade.core.execution.constants import MAX_DURATION, MIN_CROSS_EV, OPEN_ORDER_STATUSES, PRE_EVENT_BUFFER
from trade.core.execution.sizing import _ev, _exact_ev_ok, resolve_contracts
from trade.core.execution.orders import ensure_canceled, get_order_status, place_order
from trade.core.execution.market_data import get_market_prices, rest_price_dollars

log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split


def place_rest_with_requote(ticker: str, side: str, price_cents: float, contracts: int, order_price: float,
                            ev: float, expiration_ts: int, fair_prob: float, fee_rate: float, bankroll: float,
                            signal_row, size_mult: float = 1.0) -> tuple:
    """
    Place a post_only REST order; if Kalshi rejects it with 'post only cross' (the book moved between our last
    price check and the order actually landing — normal under real latency, not a bug), re-quote ONE more time
    from a fresh top-of-book instead of aborting the whole trade attempt. This is the same live-price recompute
    run_trade() already does before its first attempt, just repeated once on this specific rejection.

    Returns (order_dict, order_price, price_cents, contracts, ev) — all four numbers reflect what was ACTUALLY
    placed, which the caller must use for dashboard/log entries (they may differ from the inputs if re-quoted).
    Raises the original requests.HTTPError for any other rejection, or if the re-quote no longer has a valid
    price/edge/size (caller's existing except-and-skip handling covers that unchanged).
    """
    try:
        order = place_order(ticker, price_cents, contracts, side=side,
                            expiration_ts=expiration_ts, post_only=True)
        return order, order_price, price_cents, contracts, ev
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response is not None else ''
        if 'post only cross' not in body:
            raise
        log.info('place_rest_with_requote: %s crossed at placement (price moved in flight) — re-quoting once from a fresh book', ticker)
        live_prices = get_market_prices(ticker)
        rest = rest_price_dollars(live_prices, side)
        if rest is None or rest < 0.01:
            raise
        new_ev = _ev(fair_prob, rest, fee_rate)
        if new_ev <= 0:
            raise
        new_contracts = resolve_contracts(signal_row, fair_prob, rest, bankroll, fee_rate, size_mult)
        if new_contracts is None or new_contracts * rest > bankroll:
            raise
        new_price_cents = to_cents(rest)
        order = place_order(ticker, new_price_cents, new_contracts, side=side,
                            expiration_ts=expiration_ts, post_only=True)
        return order, rest, new_price_cents, new_contracts, new_ev


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


def extend_resting_order(ticker: str, order_id: str, commence_str: str,
                         extend_seconds: int = 900,
                         pre_event_buffer: int = PRE_EVENT_BUFFER) -> dict:
    """
    Push a resting order's REAL Kalshi expiration out by `extend_seconds` (default 15 min) — reads the order's current
    expiration_time from Kalshi, adds the extension, caps it at commence − pre_event_buffer (never later than the normal
    pre-event cutoff), then cancels and re-places at the same price for whatever remains unfilled.

    This exists because Kalshi has no amend-in-place for an order's expiration, and the "+15 min" button previously only
    pushed back an in-memory counter the monitor loop checks against — the order's actual Good-till time on Kalshi never
    moved, so the order could still expire on Kalshi before the app's own bookkeeping said it should.
    """
    order = get_order_status(order_id)
    status = order.get('status', 'unknown')
    if status not in OPEN_ORDER_STATUSES:
        return {'action': 'skipped', 'ticker': ticker, 'reason': f'order is {status}, not resting'}
    try:
        cur_exp = pd.Timestamp(order['expiration_time']).tz_convert('UTC').to_pydatetime()
    except Exception:
        cur_exp = datetime.now(timezone.utc)
    try:
        commence_utc = pd.Timestamp(commence_str).tz_convert('UTC').to_pydatetime()
    except Exception as exc:
        return {'action': 'error', 'ticker': ticker, 'reason': f'bad commence: {exc}'}
    new_exp = min(cur_exp + timedelta(seconds=extend_seconds), commence_utc - timedelta(seconds=pre_event_buffer))
    now_utc = datetime.now(timezone.utc)
    if new_exp <= now_utc:
        return {'action': 'error', 'ticker': ticker,
                'reason': f'event starts in under {pre_event_buffer // 60}min — too close to extend'}
    if new_exp <= cur_exp:
        return {'action': 'skipped', 'ticker': ticker, 'reason': 'already at the pre-event cutoff'}
    side = 'yes' if order.get('book_side') != 'ask' and order.get('outcome_side') != 'no' else 'no'
    # place_order() takes the price in the order's OWN side (it converts NO to the YES-equivalent itself). Passing the
    # YES price for a NO order re-rested it at 100 − price: a NO bid at 58¢ came back at 42¢ (and one that would then
    # cross is rejected as post-only, leaving the order cancelled and not replaced).
    price_field = 'no_price_dollars' if side == 'no' else 'yes_price_dollars'
    price_cents = round(float(order[price_field]) * 100, 3)
    total = round(float(order.get('initial_count_fp') or 0))
    res = resize_resting_order(ticker, order_id, side, total, price_cents, expiration_ts=int(new_exp.timestamp()))
    if res['action'] == 'resized':
        res['new_expiry'] = new_exp.strftime('%Y-%m-%d %H:%M UTC')
    return res


def resize_resting_order(ticker: str, order_id: str, side: str,
                         new_total_contracts: int, price_cents: int,
                         expiration_ts: Optional[int] = None) -> dict:
    """
    Resize a resting order to a new TOTAL contract count. Kalshi has no
    amend-in-place, so this cancels the current resting order and re-places
    one for (new_total - already_filled) at the same price, still post_only
    (stays a resting maker order, never crosses).

    new_total_contracts is the desired FINAL size, not "add N more" — e.g. an
    order resting for 10 with 3 already filled, resized to 8, places a new
    order for 5 (8 - 3), not 8. The already-filled 3 are a done trade; you
    can't shrink below what's already filled, and this refuses to try rather
    than silently placing something that doesn't match what was asked for.

    If expiration_ts is left unset, the new order gets a fresh MAX_DURATION
    window from now — Kalshi's order response doesn't reliably echo back the
    original expiration_time to reuse it, and giving the resized order no TTL
    at all would leave it resting indefinitely with none of the bot's usual
    auto-cancel-before-event-start monitoring (this was placed manually, so
    there's no monitor thread watching it).
    """
    order    = get_order_status(order_id)
    fill_fp  = order.get('fill_count_fp')
    already_filled = round(float(fill_fp)) if fill_fp is not None else 0
    if expiration_ts is None:
        expiration_ts = int(time.time()) + MAX_DURATION

    if new_total_contracts <= already_filled:
        return {'action': 'error', 'ticker': ticker,
                'reason': f'{already_filled} already filled — cannot resize to '
                         f'{new_total_contracts} (must be greater)'}

    new_remaining = new_total_contracts - already_filled

    if not ensure_canceled(ticker, order_id):
        return {'action': 'error', 'ticker': ticker, 'reason': 'could not confirm cancel'}

    try:
        new_order = place_order(ticker, price_cents, new_remaining, side=side,
                                expiration_ts=expiration_ts, post_only=True)
        return {
            'action':         'resized',
            'ticker':         ticker,
            'new_order_id':   new_order.get('order_id'),
            'already_filled': already_filled,
            'new_remaining':  new_remaining,
            'new_total':      new_total_contracts,
        }
    except Exception as exc:
        log.exception('resize_resting_order: re-place failed for %s', ticker)
        return {'action': 'error', 'ticker': ticker, 'reason': f'place failed: {exc}'}


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
