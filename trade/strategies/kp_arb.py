"""
trade/strategies/kp_arb.py — K/P cross-market arbitrage strategy.

Prices Kalshi sports markets against Pinnacle's de-vigged fair probability
(KALSHI.k_helpers.kalshi_odds) and crosses or rests depending on mode. Built on
trade/core/ for execution, dedup, and logging — see trade/strategies/base.py for
the shared contract.
"""

import os, sys, csv, time, signal, threading
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from KALSHI.k_helpers import kalshi_odds, fee_rate_for
from trade.core.execution import (
    place_order, get_order_status, _final_order_status, filled_count, get_market_price,
    get_market_prices, _rest_price_cents, cross_and_cancel_order, _monitor,
    resolve_contracts, _ev, _exact_ev_ok, force_cancel_all,
    TAKER_FEE, MAKER_FEE, MIN_CROSS_EV, MAX_DURATION, PRE_EVENT_BUFFER,
)
from trade.core.positions import (open_tickers, opposite_leg_blocked,
                                  drop_same_event_duplicates)
from trade.core.logging_io import write_row, log_unfilled_attempt, is_filled
from applog import get_logger

log = get_logger(__name__)

LOG_DIR     = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
LOG_PATH    = os.path.join(LOG_DIR, 'trades.csv')
NO_LOG_PATH = os.path.join(LOG_DIR, 'no_trades.csv')

FIELDS = [
    'logged_at', 'order_id', 'sport', 'outcome', 'k_ticker', 'commence',
    'order_type', 'fair_prob', 'yes_ask_at_signal', 'entry_price',
    'entry_price_cents', 'fee_rate', 'ev_per_contract', 'edge', 'contracts',
    'total_cost', 'ev_total', 'final_status', 'close_reason', 'result', 'actual_pnl',
    'settled_at',
]


def generate_signals(pinnacle_df: pd.DataFrame, threshold: float = 0.6,
                     max_delta: float = 0.25) -> pd.DataFrame:
    """Signal generation for this strategy — see KALSHI.k_helpers.kalshi_odds."""
    return kalshi_odds(pinnacle_df, threshold=threshold, max_delta=max_delta)


def log_trade(*, order_id: str, sport: str, outcome: str, k_ticker: str,
              commence, order_type: str, fair_prob: float,
              yes_ask_at_signal: float, entry_price: float, fee_rate: float,
              ev_per_contract: float, contracts: int,
              final_status: str, close_reason: str,
              side: str = 'yes') -> dict:
    """
    Log a REAL fill (executed/filled) to trades.csv (side='yes') or
    no_trades.csv (side='no'). Callers must check is_filled(final_status) first —
    see run_trade(), which routes non-fills to log_unfilled_attempt() instead.
    """
    path = NO_LOG_PATH if side == 'no' else LOG_PATH
    row = {
        'logged_at':         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'order_id':          order_id,
        'sport':             sport,
        'outcome':           outcome,
        'k_ticker':          k_ticker,
        'commence':          str(commence),
        'order_type':        order_type,
        'fair_prob':         round(fair_prob, 4),
        'yes_ask_at_signal': round(yes_ask_at_signal, 4),
        'entry_price':       round(entry_price, 4),
        'entry_price_cents': round(entry_price * 100),
        'fee_rate':          fee_rate,
        'ev_per_contract':   round(ev_per_contract, 4),
        'edge':              round(fair_prob - entry_price, 4),
        'contracts':         contracts,
        'total_cost':        round(contracts * entry_price, 4),
        'ev_total':          round(ev_per_contract * contracts, 4),
        'final_status':      final_status,
        'close_reason':      close_reason,
        'result':            'PENDING',
        'actual_pnl':        '',
        'settled_at':        '',
    }
    return write_row(path, FIELDS, row)


def _log_result(*, order_id, sport, outcome, k_ticker, commence, order_type,
                fair_prob, yes_ask_at_signal, entry_price, fee_rate,
                ev_per_contract, contracts, final_status, close_reason, side,
                filled: int = None):
    """
    Route to the real trade log if any contracts actually filled, else the
    unfilled-attempts log.

    `filled` (from Kalshi's fill_count_fp on the final order read) is the
    source of truth here, not `final_status` alone — an order can be
    canceled/expired by Kalshi's own status field while still holding a
    partial fill (e.g. resting, 1 of 5 contracts trade, then the rest gets
    canceled at max_duration). Routing on final_status alone silently dropped
    that partial fill into log_unfilled_attempt() as a total miss, leaving a
    real Kalshi position with no row in trades.csv/no_trades.csv at all —
    invisible to settle.py and to every downstream PnL/positions check.
    `filled` defaults to is_filled(final_status) ? contracts : 0 for callers
    that haven't been updated to pass it explicitly.
    """
    if filled is None:
        filled = contracts if is_filled(final_status) else 0
    if filled > 0:
        log_trade(order_id=order_id, sport=sport, outcome=outcome, k_ticker=k_ticker,
                  commence=commence, order_type=order_type, fair_prob=fair_prob,
                  yes_ask_at_signal=yes_ask_at_signal, entry_price=entry_price,
                  fee_rate=fee_rate, ev_per_contract=ev_per_contract, contracts=filled,
                  final_status=final_status, close_reason=close_reason, side=side)
    else:
        log_unfilled_attempt(strategy='kp_arb', side=side, order_id=order_id,
                             k_ticker=k_ticker, sport=sport, outcome=outcome,
                             entry_price=entry_price, contracts=contracts,
                             final_status=final_status, close_reason=close_reason)


def resume_monitoring(*, order_id: str, ticker: str, event_id: str, sport: str,
                      outcome: str, order_price: float, fee_rate: float,
                      commence: str, side: str, contracts: int,
                      fair_prob: float, ev_per_contract: float,
                      taker_fee: float = TAKER_FEE,
                      max_duration: int = MAX_DURATION,
                      pre_event_buffer: int = PRE_EVENT_BUFFER,
                      dashboard=None,
                      stop_event: Optional[threading.Event] = None) -> None:
    """
    Pick up full run_trade()-style monitoring for an order that already exists
    on Kalshi but wasn't placed through run_trade() in this call stack —
    specifically a resized order (trade.core.execution.resize_resting_order).
    The cancel+replace there happens synchronously on a UI click and has to
    return immediately, so without this the new order would get none of the
    usual care: no periodic status/fill refresh in the dashboard, no
    auto-cancel-before-event safety net, and critically no eventual
    trades.csv/unfilled_attempts.csv entry when it resolves — it would just
    sit there orphaned. Meant to run in its own thread; blocks on _monitor()
    until the order closes one way or another, same as run_trade() does.
    """
    if dashboard:
        dashboard.add_position(order_id, ticker, outcome, contracts,
                               round(order_price * 100), fair_prob, ev_per_contract,
                               event_id=event_id, sport=sport, raw_outcome=outcome,
                               fee_rate=fee_rate, side=side, commence=commence)

    try:
        commence_utc = pd.Timestamp(commence).tz_convert('UTC').to_pydatetime()
    except Exception:
        log.exception('resume_monitoring: bad commence %r for %s — falling back to +1h '
                      'so this order still gets pre-event-buffer protection eventually',
                      commence, ticker)
        commence_utc = datetime.now(timezone.utc) + timedelta(hours=1)

    reason = _monitor(
        order_id=order_id, ticker=ticker, event_id=event_id,
        sport=sport, outcome=outcome, order_price=order_price,
        fee_rate=fee_rate, commence_utc=commence_utc, side=side,
        contracts=contracts, taker_fee=taker_fee, max_duration=max_duration,
        pre_event_buffer=pre_event_buffer, dashboard=dashboard, stop_event=stop_event,
    )
    final        = _final_order_status(order_id)
    final_status = final.get('status', 'unknown')
    order_type   = 'no_rest' if side == 'no' else 'rest'   # resize always re-rests, never crosses
    _log_result(
        order_id=order_id, sport=sport, outcome=outcome, k_ticker=ticker,
        commence=commence, order_type=order_type, fair_prob=fair_prob,
        yes_ask_at_signal=order_price, entry_price=order_price, fee_rate=fee_rate,
        ev_per_contract=ev_per_contract, contracts=contracts,
        final_status=final_status, close_reason=reason, side=side,
        filled=filled_count(final),
    )


def run_trade(signal_row: pd.Series, bankroll: float,
              taker_fee: float = TAKER_FEE,
              maker_fee: float = MAKER_FEE,
              limit_only: bool = False,
              force_cross: bool = False,
              side: str = 'yes',
              max_duration: int = MAX_DURATION,
              pre_event_buffer: int = PRE_EVENT_BUFFER,
              size_mult: float = 1.0,
              dashboard=None,
              stop_event: Optional[threading.Event] = None,
              order_registry: Optional[list] = None) -> dict:
    """
    Execute a single trade for one signaled row from generate_signals().

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

    # Real fee rates vary per series (see KALSHI/k_helpers.fee_rate_for) — override
    # whatever taker_fee/maker_fee was passed in (e.g. a UI slider) with the live rate
    # for THIS ticker's series, so execution can't diverge from the signal it's acting
    # on. Prefer the rate kalshi_odds() already computed for this exact row when present.
    _series = str(ticker).split('-')[0]
    if signal_row.get('taker_fee_rate') is not None:
        taker_fee = float(signal_row['taker_fee_rate'])
    else:
        taker_fee = fee_rate_for(_series, maker=False)
    if signal_row.get('maker_fee_rate') is not None:
        maker_fee = float(signal_row['maker_fee_rate'])
    else:
        maker_fee = fee_rate_for(_series, maker=True)

    # ── NO side: same AUTO/CROSS/REST logic as YES, using no_ask ────────────
    if side == 'no':
        if no_ask is None:
            return {'status': 'skipped', 'reason': 'no_ask_unavailable',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        fair_prob_no  = 1 - fair_prob
        no_bid_c      = round(no_bid * 100) if no_bid is not None else None
        no_ask_c      = round(no_ask * 100)
        rest_price_no = _rest_price_cents(no_bid_c, no_ask_c) / 100
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

        # Re-fetch live prices before placing to use fresh spread-aware rest price
        live_prices = get_market_prices(ticker)
        live_na = live_prices.get('no_ask')
        if live_na is not None:
            if order_type == 'no_rest':
                live_nb   = live_prices.get('no_bid')
                live_rest = _rest_price_cents(live_nb, live_na) / 100
                live_ev   = _ev(fair_prob_no, live_rest, fee_rate)
                if live_ev <= 0:
                    return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                            'ticker': ticker, 'order_id': None, 'contracts': 0}
                order_price = live_rest
            else:  # no_cross
                live_cross = round(live_na / 100, 2)
                live_ev    = _ev(fair_prob_no, live_cross, fee_rate)
                if live_ev <= 0:
                    return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                            'ticker': ticker, 'order_id': None, 'contracts': 0}
                order_price = live_cross

        ev          = _ev(fair_prob_no, order_price, fee_rate)
        price_cents = round(order_price * 100)
        contracts   = resolve_contracts(signal_row, fair_prob_no, order_price, bankroll,
                                        fee_rate, size_mult)
        if contracts is None:
            return {'status': 'skipped', 'reason': 'zero_contracts',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        if contracts * order_price > bankroll:
            return {'status': 'skipped', 'reason': 'insufficient_cash',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        _min_ev = MIN_CROSS_EV if order_type == 'no_cross' else 0
        if not _exact_ev_ok(fair_prob_no, order_price, contracts, _series,
                            maker=(order_type == 'no_rest'), min_ev=_min_ev):
            return {'status': 'skipped', 'reason': 'no_edge_after_exact_fee_rounding',
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
            order_registry.append((order_id, ticker))
        log.debug('[no order] %s no_ask=%s contracts=%s ev=%.4f', ticker, no_ask, contracts, ev)

        if dashboard:
            dashboard.add_position(order_id, ticker, f'NO:{outcome}', contracts,
                                   price_cents, fair_prob_no, ev,
                                   event_id=event_id, sport=sport,
                                   raw_outcome=outcome, fee_rate=fee_rate,
                                   side='no', commence=str(commence))

        reason = _monitor(
            order_id=order_id, ticker=ticker, event_id=event_id,
            sport=sport, outcome=outcome,
            order_price=order_price, fee_rate=fee_rate,
            commence_utc=commence_utc, side='no',
            contracts=contracts, taker_fee=taker_fee,
            max_duration=max_duration, pre_event_buffer=pre_event_buffer,
            dashboard=dashboard, stop_event=stop_event,
        )
        final        = _final_order_status(order_id)
        final_status = final.get('status', 'unknown')
        _log_result(
            order_id=order_id, sport=sport, outcome=f'NO:{outcome}',
            k_ticker=ticker, commence=commence, order_type=order_type,
            fair_prob=fair_prob_no, yes_ask_at_signal=yes_ask,
            entry_price=order_price, fee_rate=fee_rate,
            ev_per_contract=ev, contracts=contracts,
            final_status=final_status, close_reason=reason,
            side='no', filled=filled_count(final),
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
    yes_bid_c      = round(yes_bid * 100) if yes_bid is not None else None
    yes_ask_c      = round(yes_ask * 100)
    rest_price_yes = _rest_price_cents(yes_bid_c, yes_ask_c) / 100
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
    contracts     = resolve_contracts(signal_row, fair_prob, order_price, bankroll,
                                      fee_rate, size_mult)
    if contracts is None:
        if dashboard:
            skip_id = f'skip_{ticker}'
            dashboard.add_position(skip_id, ticker, outcome, 0, price_cents, fair_prob, ev)
            dashboard.update(skip_id, status='skipped')
        return {'status': 'skipped', 'reason': 'zero_contracts',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

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

    # Re-fetch live prices before placing to use fresh spread-aware rest price
    live_prices = get_market_prices(ticker)
    live_ya = live_prices.get('yes_ask')
    if live_ya is not None:
        if order_type == 'cross':
            order_price = live_ya / 100
        else:  # rest: spread-aware — 2¢ wide → bid+1, 1¢ wide → bid
            live_yb     = live_prices.get('yes_bid')
            order_price = _rest_price_cents(live_yb, live_ya) / 100
        ev = _ev(fair_prob, order_price, fee_rate)
        if ev <= 0:
            return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        price_cents = round(order_price * 100)
        contracts   = resolve_contracts(signal_row, fair_prob, order_price, bankroll,
                                        fee_rate, size_mult)

    if contracts is None:
        return {'status': 'skipped', 'reason': 'zero_contracts',
                'ticker': ticker, 'order_id': None, 'contracts': 0}
    if contracts * order_price > bankroll:
        return {'status': 'skipped', 'reason': 'insufficient_cash',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    _min_ev = MIN_CROSS_EV if order_type == 'cross' else 0
    if not _exact_ev_ok(fair_prob, order_price, contracts, _series,
                        maker=(order_type == 'rest'), min_ev=_min_ev):
        return {'status': 'skipped', 'reason': 'no_edge_after_exact_fee_rounding',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    order    = place_order(ticker, price_cents, contracts, side='yes',
                           expiration_ts=int(expiry_dt.timestamp()),
                           post_only=(order_type == 'rest' or limit_only))
    order_id = order.get('order_id')
    if order_registry is not None and order_id:
        order_registry.append((order_id, ticker))

    actual_taker_fee = float(order.get('taker_fees_dollars') or 0)
    actual_maker_fee = float(order.get('maker_fees_dollars') or 0)
    log.debug('[fee check] actual taker=$%.4f maker=$%.4f assumed_rate=%.0f%% contracts=%s price=%s¢',
             actual_taker_fee, actual_maker_fee, fee_rate * 100, contracts, price_cents)

    if dashboard:
        dashboard.add_position(order_id, ticker, outcome, contracts,
                               price_cents, fair_prob, ev,
                               event_id=event_id, sport=sport,
                               raw_outcome=outcome, fee_rate=fee_rate,
                               side='yes', commence=str(commence))

    reason = _monitor(
        order_id=order_id, ticker=ticker, event_id=event_id,
        sport=sport, outcome=outcome,
        order_price=order_price, fee_rate=fee_rate,
        commence_utc=commence_utc,
        contracts=contracts, taker_fee=taker_fee,
        max_duration=max_duration, pre_event_buffer=pre_event_buffer,
        dashboard=dashboard, stop_event=stop_event,
    )

    final        = _final_order_status(order_id)
    final_status = final.get('status', 'unknown')

    _log_result(
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
        side              = 'yes',
        filled            = filled_count(final),
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


def run_all_signals(signals_df: pd.DataFrame, bankroll: float,
                    taker_fee: float = TAKER_FEE,
                    maker_fee: float = MAKER_FEE,
                    limit_only: bool = False,
                    force_cross: bool = False,
                    side: str = 'yes',
                    max_duration: int = MAX_DURATION,
                    size_mult: float = 1.0,
                    dashboard=None,
                    stop_event: Optional[threading.Event] = None) -> list:
    """
    Run trades in parallel (one thread per signal).
    Deduplicates against LIVE Kalshi state (trade.core.positions.open_tickers) —
    not the CSV log — so a stale/unwritten log row can never block a real signal.

    Ctrl+C behavior: sets stop_event, waits for monitors to cancel their own
    orders, then runs a force-cancel pass over any tracked (order_id, ticker) that
    is still open. A second Ctrl+C during cleanup is ignored so cancellation
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

    # Drop tickers with existing open/pending positions — live Kalshi state, not CSV
    _open = open_tickers()
    if _open:
        before = len(active)
        active = active[~active['k_ticker'].isin(_open)].copy()
        dropped = before - len(active)
        if dropped:
            log.info('[dedup] Skipped %d ticker(s) with existing open positions', dropped)

    # Same-event dedup: YES A and NO B are the same bet on two tickers. Skip any
    # signal whose 2-way event already has exposure on the other ticker (open or
    # resting), and within this batch keep only the best leg per event.
    if not active.empty:
        _blocked = opposite_leg_blocked(active['k_ticker'].tolist(), _open)
        if _blocked:
            log.info('[dedup] Skipped %d ticker(s) — opposite leg of the event already held: %s',
                     len(_blocked), sorted(_blocked))
            active = active[~active['k_ticker'].isin(_blocked)].copy()
        if side == 'no':
            active['_score'] = (1 - active['fair_prob']) - active['no_ask']
        else:
            active['_score'] = active['fair_prob'] - active['yes_ask']
        _before = len(active)
        active = drop_same_event_duplicates(active).drop(columns='_score').copy()
        if len(active) < _before:
            log.info('[dedup] Dropped %d same-event opposite leg(s) within this batch',
                     _before - len(active))

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
                               size_mult=size_mult,
                               dashboard=dashboard, stop_event=stop_event,
                               order_registry=order_registry)
        except Exception as exc:
            log.exception('run_trade failed for %s', row.get('k_ticker', '?'))
            result = {
                'status':  'error',
                'ticker':  row.get('k_ticker', ''),
                'outcome': row.get('outcome', ''),
                'reason':  str(exc),
                'order_id': None,
                'contracts': 0,
            }
        if result.get('status') in ('skipped', 'error'):
            log.info('[skip] %s  reason=%s', result.get('ticker', '?'), result.get('reason', '?'))
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
            log.info('[batch] firing %d order(s)  (%d/%d sent so far)',
                     len(batch), batch_start, n_total)
            for t in batch:
                t.start()
            if batch_start + BATCH_SIZE < len(threads):
                time.sleep(BATCH_DELAY)

        # Wait for every thread (all batches) to complete
        for t in threads:
            t.join()

    except KeyboardInterrupt:
        log.warning('[shutdown] Ctrl+C received — canceling ALL orders...')
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
        n = force_cancel_all(order_registry)
        if n > 0:
            log.info('[shutdown] force-canceled %d open order(s)', n)

    return results
