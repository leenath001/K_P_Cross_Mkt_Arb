"""
trade/core/logging_io.py — shared CSV trade-log writer and the filled/unfilled split.

Previously log_trade() fired once at the end of a strategy's run_trade(), using
WHATEVER final_status resulted — an order that got placed and then canceled/expired
without ever filling landed in the same trades.csv/no_trades.csv as a real fill,
even though the file's own docstring said "each filled/executed order gets one
row." That's fixed here: `is_filled()` is the one place that decides whether an
order counts as a real trade, and log_unfilled_attempt() gives unfilled attempts
their own home (trade/logs/unfilled_attempts.csv) — pure diagnostic history, never
read by Review, the notebook, or dedup (dedup is live-state now, see positions.py).

Each strategy keeps its own schema-specific log_trade() wrapper (kp_arb and
prospect's are close to each other; nothing's is genuinely different — no
fair_prob/edge/ev columns since it doesn't price against Pinnacle) but all of them
call write_row() here instead of duplicating the "make dir, check header, DictWriter"
boilerplate three times over.
"""

import csv, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from applog import get_logger

log = get_logger(__name__)

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
UNFILLED_LOG_PATH = os.path.join(LOG_DIR, 'unfilled_attempts.csv')

FILLED_STATUSES = {'executed', 'filled'}

UNFILLED_FIELDS = [
    'logged_at', 'strategy', 'side', 'order_id', 'k_ticker', 'sport', 'outcome',
    'entry_price', 'contracts', 'final_status', 'close_reason',
]


def is_filled(final_status: str) -> bool:
    """True if an order actually resulted in a real position (executed/filled)."""
    return (final_status or '').lower() in FILLED_STATUSES


def write_row(path: str, fields: list, row: dict) -> dict:
    """Append one row to a CSV log, writing the header if the file is new/empty."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return row


def log_unfilled_attempt(*, strategy: str, side: str, order_id: str, k_ticker: str,
                         sport: str = '', outcome: str = '',
                         entry_price: float = 0.0, contracts: int = 0,
                         final_status: str, close_reason: str) -> dict:
    """
    Record an order that was placed but never filled (canceled/expired before any
    contracts traded). Diagnostic only — never read by Review, the notebook, or
    dedup, so a pile of these can never block or skew a real trading decision.
    """
    row = {
        'logged_at':     datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'strategy':      strategy,
        'side':          side,
        'order_id':      order_id,
        'k_ticker':      k_ticker,
        'sport':         sport,
        'outcome':       outcome,
        'entry_price':   round(entry_price, 4) if entry_price else '',
        'contracts':     contracts,
        'final_status':  final_status,
        'close_reason':  close_reason,
    }
    return write_row(UNFILLED_LOG_PATH, UNFILLED_FIELDS, row)
