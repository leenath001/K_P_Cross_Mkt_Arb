"""
trade/core/execution/monitor.py — The per-order monitor loop: re-checks Pinnacle and Kalshi on a cadence and cancels or crosses when the edge flips, the order ages out, or the event is about to start.
"""
import time, threading, random
from datetime import datetime, timezone
from typing import Optional

from theODDS.p_helpers import pinnacle_odds, get_api_usage
from applog import get_logger
from trade.core.execution.constants import KALSHI_POLL, MAX_DURATION, MIN_CROSS_EV, PINNACLE_POLL, PRE_EVENT_BUFFER, TAKER_FEE, _CLOSED_STATUSES
from trade.core.execution.sizing import _ev
from trade.core.execution.orders import ensure_canceled
from trade.core.execution.order_mgmt import cross_and_cancel_order
from trade.core.execution import live_cache

log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split


# ---------------------------------------------------------------------------
# Signal Re-validation
# ---------------------------------------------------------------------------

PINNACLE_SHARE_SEC = 30      # monitors re-check every PINNACLE_POLL (120 s); sharing a sport's odds this long is fresh enough
_pin_cache: dict = {}        # sport -> (fetched_at, DataFrame)
_pin_locks: dict = {}
_pin_guard = threading.Lock()


def _pinnacle_shared(sport: str):
    """pinnacle_odds() for one sport, shared across every monitor for PINNACLE_SHARE_SEC. A 200-order batch otherwise
    made 200 separate Odds API calls (credits) every 2 minutes for the same handful of sports."""
    with _pin_guard:
        lock = _pin_locks.setdefault(sport, threading.Lock())
    with lock:
        hit = _pin_cache.get(sport)
        if hit and time.time() - hit[0] < PINNACLE_SHARE_SEC:
            return hit[1]
        fresh = pinnacle_odds(sports=[sport], hrs=72, live=False)   # errors propagate: caller treats them as no-signal
        _pin_cache[sport] = (time.time(), fresh)
        return fresh


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
        fresh = _pinnacle_shared(sport)

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
            order      = live_cache.order_status(order_id)    # shared snapshot, not one GET per order (see live_cache)
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

            market_ask = live_cache.market_ask(ticker)
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
