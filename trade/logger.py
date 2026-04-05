"""
logger.py — Append-only trade log for K/P arbitrage positions.

Each filled/executed order gets one row. Skipped orders are not logged.
The `result` and `actual_pnl` columns start as PENDING and can be updated
manually (or via a settle script) after the event resolves.

Log location: trade/logs/trades.csv
"""

import csv, os
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'trades.csv')

FIELDS = [
    'logged_at',
    'order_id',
    'sport',
    'outcome',
    'k_ticker',
    'commence',
    'order_type',        # 'cross' or 'rest'
    'fair_prob',         # Pinnacle fair probability at signal time
    'yes_ask_at_signal', # Kalshi ask when signal was generated
    'entry_price',       # actual order price (dollars, e.g. 0.27)
    'entry_price_cents', # same in cents
    'fee_rate',          # 0.07 for cross, 0.03 for rest
    'ev_per_contract',   # EV in dollars per contract at entry
    'edge',              # fair_prob - entry_price (raw, pre-fee edge)
    'contracts',
    'total_cost',        # contracts * entry_price
    'ev_total',          # ev_per_contract * contracts
    'final_status',      # 'executed', 'filled', 'canceled', etc.
    'close_reason',      # why the monitor exited
    'result',            # WIN / LOSS / PENDING  (fill in after event)
    'actual_pnl',        # net dollars after fees  (fill in after event)
]


def log_trade(*, order_id: str, sport: str, outcome: str, k_ticker: str,
              commence, order_type: str, fair_prob: float,
              yes_ask_at_signal: float, entry_price: float, fee_rate: float,
              ev_per_contract: float, contracts: int,
              final_status: str, close_reason: str) -> dict:
    """
    Append one row to trade/logs/trades.csv.
    Returns the row dict that was written.
    """
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    write_header = not os.path.exists(LOG_PATH)

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
    }

    with open(LOG_PATH, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    return row
