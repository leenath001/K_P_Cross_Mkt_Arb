"""
trade/strategies/prospect.py — Prospect Theory arbitrage strategy.

Exploits cognitive bias (probability-weighting distortion) in Kalshi prediction
markets using Pinnacle fair_prob as the ground-truth anchor:

  Longshot zone (yes_ask $0.05–$0.15): retail traders overprice low-prob events
    → signal fires when NO EV >= MIN_CROSS_EV  (buy NO)

  Favorite zone (yes_ask $0.75–$0.92): retail traders underprice high-prob events
    → signal fires when YES EV >= MIN_CROSS_EV (buy YES)

Built on trade/core/ for execution, dedup, and logging — see
trade/strategies/base.py for the shared contract. CLI-only (no web_app.py tab);
run via trade/prospect_quickstart.py.
"""

import os, sys, threading, time, signal as _signal
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from KALSHI.k_helpers import kalshi_odds, prospect_signals, fee_rate_for
from trade.core.execution import (
    place_order, get_market_prices, _rest_price_cents,
    kelly_contracts, _ev, _monitor, _final_order_status,
    TAKER_FEE, MAKER_FEE, MIN_CROSS_EV, MAX_DURATION, PRE_EVENT_BUFFER,
    force_cancel_all,
)
from trade.core.positions import open_tickers
from trade.core.logging_io import write_row, log_unfilled_attempt, is_filled
from applog import get_logger

log = get_logger(__name__)

LOG_DIR           = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
PROSPECT_LOG_PATH = os.path.join(LOG_DIR, 'prospect_trades.csv')

PROSPECT_FIELDS = [
    'logged_at', 'order_id', 'sport', 'outcome', 'k_ticker', 'commence',
    'pt_zone', 'pt_side',
    'order_type', 'fair_prob', 'yes_ask_at_signal',
    'entry_price', 'entry_price_cents', 'fee_rate',
    'ev_per_contract', 'edge', 'contracts', 'total_cost', 'ev_total',
    'final_status', 'close_reason', 'result', 'actual_pnl',
]


def generate_signals(pinnacle_df: pd.DataFrame, threshold: float = 0.6,
                     longshot_lo: float = 0.05, longshot_hi: float = 0.15,
                     favorite_lo: float = 0.75, favorite_hi: float = 0.92) -> pd.DataFrame:
    """Signal generation for this strategy: kalshi_odds() + prospect-zone filter."""
    matched = kalshi_odds(pinnacle_df, threshold=threshold)
    return prospect_signals(matched, longshot_lo=longshot_lo, longshot_hi=longshot_hi,
                            favorite_lo=favorite_lo, favorite_hi=favorite_hi)


def log_prospect_trade(*, order_id: str, sport: str, outcome: str,
                       k_ticker: str, commence, pt_zone: str, pt_side: str,
                       order_type: str, fair_prob: float,
                       yes_ask_at_signal: float, entry_price: float,
                       fee_rate: float, ev_per_contract: float,
                       contracts: int, final_status: str,
                       close_reason: str) -> dict:
    """Log a REAL fill (executed/filled). Non-fills go through log_unfilled_attempt() instead."""
    row = {
        'logged_at':         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'order_id':          order_id,
        'sport':             sport,
        'outcome':           outcome,
        'k_ticker':          k_ticker,
        'commence':          str(commence),
        'pt_zone':           pt_zone,
        'pt_side':           pt_side,
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
    }
    return write_row(PROSPECT_LOG_PATH, PROSPECT_FIELDS, row)


def _log_result(*, order_id, sport, outcome, k_ticker, commence, pt_zone, pt_side,
                order_type, fair_prob, yes_ask_at_signal, entry_price, fee_rate,
                ev_per_contract, contracts, final_status, close_reason):
    if is_filled(final_status):
        log_prospect_trade(order_id=order_id, sport=sport, outcome=outcome, k_ticker=k_ticker,
                           commence=commence, pt_zone=pt_zone, pt_side=pt_side,
                           order_type=order_type, fair_prob=fair_prob,
                           yes_ask_at_signal=yes_ask_at_signal, entry_price=entry_price,
                           fee_rate=fee_rate, ev_per_contract=ev_per_contract,
                           contracts=contracts, final_status=final_status,
                           close_reason=close_reason)
    else:
        log_unfilled_attempt(strategy='prospect', side=pt_side, order_id=order_id,
                             k_ticker=k_ticker, sport=sport, outcome=outcome,
                             entry_price=entry_price, contracts=contracts,
                             final_status=final_status, close_reason=close_reason)


def run_prospect_trade(signal_row: pd.Series, bankroll: float,
                       taker_fee: float = TAKER_FEE,
                       maker_fee: float = MAKER_FEE,
                       limit_only: bool = False,
                       force_cross: bool = False,
                       size_mult: float = 1.0,
                       max_duration: int = MAX_DURATION,
                       pre_event_buffer: int = PRE_EVENT_BUFFER,
                       dashboard=None,
                       stop_event: Optional[threading.Event] = None,
                       order_registry: Optional[list] = None) -> dict:
    """
    Execute one prospect theory trade. Reads pt_side and pt_zone from the row.
    Logic mirrors kp_arb.run_trade() but logs to prospect_trades.csv.
    """
    ticker    = signal_row['k_ticker']
    fair_prob = float(signal_row['fair_prob'])
    yes_ask   = float(signal_row['yes_ask'])
    no_ask    = float(signal_row['no_ask'])  if signal_row.get('no_ask')  is not None else None
    yes_bid   = float(signal_row['yes_bid']) if signal_row.get('yes_bid') is not None else None
    no_bid    = float(signal_row['no_bid'])  if signal_row.get('no_bid')  is not None else None
    commence  = signal_row['commence']
    event_id  = signal_row['event_id']
    sport     = signal_row['sport']
    outcome   = signal_row['outcome']
    pt_zone   = signal_row.get('pt_zone', '')
    pt_side   = signal_row.get('pt_side', 'yes')  # 'yes' or 'no'

    # Real fee rates vary per series (see KALSHI/k_helpers.fee_rate_for) — override
    # the passed-in taker_fee/maker_fee with the live per-series rate for this ticker.
    _series = str(ticker).split('-')[0]
    if signal_row.get('taker_fee_rate') is not None:
        taker_fee = float(signal_row['taker_fee_rate'])
    else:
        taker_fee = fee_rate_for(_series, maker=False)
    if signal_row.get('maker_fee_rate') is not None:
        maker_fee = float(signal_row['maker_fee_rate'])
    else:
        maker_fee = fee_rate_for(_series, maker=True)

    # ── NO side ──────────────────────────────────────────────────────────────
    if pt_side == 'no':
        if no_ask is None:
            return {'status': 'skipped', 'reason': 'no_ask_unavailable',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        fair_prob_no  = 1 - fair_prob
        no_bid_c      = round(no_bid * 100) if no_bid is not None else None
        no_ask_c      = round(no_ask * 100)
        rest_price_no = _rest_price_cents(no_bid_c, no_ask_c) / 100
        taker_ev_no   = _ev(fair_prob_no, no_ask, taker_fee)
        maker_ev_no   = _ev(fair_prob_no, rest_price_no, maker_fee)

        if force_cross:
            if taker_ev_no < MIN_CROSS_EV:
                return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                        'ticker': ticker, 'order_id': None, 'contracts': 0}
            order_price, fee_rate, order_type = no_ask, taker_fee, 'no_cross'
        elif not limit_only and taker_ev_no >= MIN_CROSS_EV:
            order_price, fee_rate, order_type = no_ask, taker_fee, 'no_cross'
        elif maker_ev_no > 0:
            order_price, fee_rate, order_type = rest_price_no, maker_fee, 'no_rest'
        else:
            return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        if order_price < 0.01:
            return {'status': 'skipped', 'reason': 'no_ask_too_low',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}

        live_prices = get_market_prices(ticker)
        live_na = live_prices.get('no_ask')
        if live_na is not None:
            if order_type == 'no_rest':
                live_nb     = live_prices.get('no_bid')
                order_price = _rest_price_cents(live_nb, live_na) / 100
            else:
                order_price = round(live_na / 100, 2)
            live_ev = _ev(fair_prob_no, order_price, fee_rate)
            if live_ev < (MIN_CROSS_EV if order_type == 'no_cross' else 0):
                return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                        'ticker': ticker, 'order_id': None, 'contracts': 0}

        ev          = _ev(fair_prob_no, order_price, fee_rate)
        price_cents = round(order_price * 100)
        contracts   = kelly_contracts(fair_prob_no, order_price, bankroll, fee_rate)
        if contracts <= 0:
            return {'status': 'skipped', 'reason': 'zero_contracts',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        contracts = max(1, round(contracts * size_mult))
        if contracts * order_price > bankroll:
            return {'status': 'skipped', 'reason': 'insufficient_cash',
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
                               post_only=(order_type == 'no_rest'))
        order_id = order.get('order_id')
        if order_registry is not None and order_id:
            order_registry.append((order_id, ticker))

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
        final_status = _final_order_status(order_id).get('status', 'unknown')
        _log_result(
            order_id=order_id, sport=sport, outcome=f'NO:{outcome}',
            k_ticker=ticker, commence=commence, pt_zone=pt_zone, pt_side='no',
            order_type=order_type, fair_prob=fair_prob_no,
            yes_ask_at_signal=yes_ask, entry_price=order_price,
            fee_rate=fee_rate, ev_per_contract=ev, contracts=contracts,
            final_status=final_status, close_reason=reason,
        )
        return {
            'order_id': order_id, 'ticker': ticker, 'outcome': f'NO:{outcome}',
            'contracts': contracts, 'no_price': price_cents, 'pt_zone': pt_zone,
            'pt_side': 'no', 'fair_prob': fair_prob_no, 'ev': round(ev, 4),
            'order_type': order_type, 'status': final_status, 'reason': reason,
        }

    # ── YES side ─────────────────────────────────────────────────────────────
    yes_bid_c      = round(yes_bid * 100) if yes_bid is not None else None
    yes_ask_c      = round(yes_ask * 100)
    rest_price_yes = _rest_price_cents(yes_bid_c, yes_ask_c) / 100
    taker_ev_yes   = _ev(fair_prob, yes_ask, taker_fee)
    maker_ev_yes   = _ev(fair_prob, rest_price_yes, maker_fee)

    if force_cross:
        if taker_ev_yes < MIN_CROSS_EV:
            return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        order_price, fee_rate, order_type = yes_ask, taker_fee, 'cross'
    elif not limit_only and taker_ev_yes >= MIN_CROSS_EV:
        order_price, fee_rate, order_type = yes_ask, taker_fee, 'cross'
    elif maker_ev_yes > 0:
        order_price, fee_rate, order_type = rest_price_yes, maker_fee, 'rest'
    else:
        return {'status': 'skipped', 'reason': 'no_edge_after_fees',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    ev          = _ev(fair_prob, order_price, fee_rate)
    price_cents = round(order_price * 100)
    contracts   = kelly_contracts(fair_prob, order_price, bankroll, fee_rate)

    now_utc      = datetime.now(timezone.utc)
    commence_utc = pd.Timestamp(commence).tz_convert('UTC').to_pydatetime()
    expiry_dt    = min(now_utc + timedelta(seconds=max_duration),
                      commence_utc - timedelta(seconds=pre_event_buffer))
    if expiry_dt <= now_utc:
        return {'status': 'skipped', 'reason': 'event_too_soon',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    live_prices = get_market_prices(ticker)
    live_ya = live_prices.get('yes_ask')
    if live_ya is not None:
        if order_type == 'cross':
            order_price = live_ya / 100
        else:
            live_yb     = live_prices.get('yes_bid')
            order_price = _rest_price_cents(live_yb, live_ya) / 100
        ev = _ev(fair_prob, order_price, fee_rate)
        if ev < (MIN_CROSS_EV if order_type == 'cross' else 0):
            return {'status': 'skipped', 'reason': 'signal_gone_at_execution',
                    'ticker': ticker, 'order_id': None, 'contracts': 0}
        price_cents = round(order_price * 100)
        contracts   = kelly_contracts(fair_prob, order_price, bankroll, fee_rate)

    if contracts <= 0:
        return {'status': 'skipped', 'reason': 'zero_contracts',
                'ticker': ticker, 'order_id': None, 'contracts': 0}
    contracts = max(1, round(contracts * size_mult))
    if contracts * order_price > bankroll:
        return {'status': 'skipped', 'reason': 'insufficient_cash',
                'ticker': ticker, 'order_id': None, 'contracts': 0}

    order    = place_order(ticker, price_cents, contracts, side='yes',
                           expiration_ts=int(expiry_dt.timestamp()),
                           post_only=(order_type == 'rest'))
    order_id = order.get('order_id')
    if order_registry is not None and order_id:
        order_registry.append((order_id, ticker))

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
        commence_utc=commence_utc, side='yes',
        contracts=contracts, taker_fee=taker_fee,
        max_duration=max_duration, pre_event_buffer=pre_event_buffer,
        dashboard=dashboard, stop_event=stop_event,
    )
    final_status = _final_order_status(order_id).get('status', 'unknown')
    _log_result(
        order_id=order_id, sport=sport, outcome=outcome,
        k_ticker=ticker, commence=commence, pt_zone=pt_zone, pt_side='yes',
        order_type=order_type, fair_prob=fair_prob,
        yes_ask_at_signal=yes_ask, entry_price=order_price,
        fee_rate=fee_rate, ev_per_contract=ev, contracts=contracts,
        final_status=final_status, close_reason=reason,
    )
    return {
        'order_id': order_id, 'ticker': ticker, 'outcome': outcome,
        'contracts': contracts, 'yes_price': price_cents, 'pt_zone': pt_zone,
        'pt_side': 'yes', 'fair_prob': fair_prob, 'ev': round(ev, 4),
        'order_type': order_type, 'status': final_status, 'reason': reason,
    }


def run_all_signals(signals_df: pd.DataFrame, bankroll: float,
                    taker_fee: float = TAKER_FEE,
                    maker_fee: float = MAKER_FEE,
                    limit_only: bool = False,
                    force_cross: bool = False,
                    size_mult: float = 1.0,
                    max_duration: int = MAX_DURATION,
                    dashboard=None,
                    stop_event: Optional[threading.Event] = None) -> list:
    """
    Run prospect trades in parallel (one thread per signal).
    Reads pt_side per row — handles mixed YES/NO in a single session.
    Dedup via live Kalshi state (trade.core.positions.open_tickers), not CSV.
    """
    active = signals_df[signals_df['pt_signal']].drop_duplicates('k_ticker').copy()

    _open = open_tickers()
    if _open:
        before = len(active)
        active = active[~active['k_ticker'].isin(_open)].copy()
        dropped = before - len(active)
        if dropped:
            log.info('[dedup] Skipped %d ticker(s) with existing open positions', dropped)

    if stop_event is None:
        stop_event = threading.Event()
    order_registry: list = []
    results = [None] * len(active)
    lock    = threading.Lock()

    def _trade(row, idx):
        try:
            result = run_prospect_trade(
                row, bankroll=bankroll,
                taker_fee=taker_fee, maker_fee=maker_fee,
                limit_only=limit_only, force_cross=force_cross,
                size_mult=size_mult, max_duration=max_duration,
                dashboard=dashboard, stop_event=stop_event,
                order_registry=order_registry,
            )
        except Exception as exc:
            log.exception('run_prospect_trade failed for %s', row.get('k_ticker', '?'))
            result = {'status': 'error', 'ticker': row.get('k_ticker', ''),
                      'outcome': row.get('outcome', ''), 'reason': str(exc),
                      'order_id': None, 'contracts': 0}
        if result.get('status') in ('skipped', 'error'):
            log.info('[skip] %s  reason=%s', result.get('ticker', '?'), result.get('reason', '?'))
        with lock:
            results[idx] = result

    BATCH_SIZE  = 10
    BATCH_DELAY = 10

    rows_list = list(active.iterrows())
    threads   = [
        threading.Thread(target=_trade, args=(row, i), daemon=True)
        for i, (_, row) in enumerate(rows_list)
    ]

    try:
        for batch_start in range(0, len(threads), BATCH_SIZE):
            batch = threads[batch_start : batch_start + BATCH_SIZE]
            log.info('[batch] firing %d order(s)  (%d/%d sent so far)',
                    len(batch), batch_start, len(threads))
            for t in batch:
                t.start()
            if batch_start + BATCH_SIZE < len(threads):
                time.sleep(BATCH_DELAY)
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.warning('[shutdown] Ctrl+C — canceling ALL prospect orders...')
        prev = _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
        try:
            stop_event.set()
            for t in threads:
                t.join(timeout=15)
        finally:
            _signal.signal(_signal.SIGINT, prev)
        raise
    finally:
        n = force_cancel_all(order_registry)
        if n > 0:
            log.info('[shutdown] force-canceled %d open order(s)', n)

    return results
