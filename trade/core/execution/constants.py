"""
trade/core/execution/constants.py — Shared constants: fallback fee rates, polling cadence, order lifetime limits, order statuses.
"""

from KALSHI import client as kalshi
from applog import get_logger


log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split

BASE_URL          = kalshi.BASE_URL   # kept for importers; requests go through KALSHI/client.py
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
MIN_NOTIONAL      = 2.0    # floor $ risked per trade — see kelly_contracts() docstring (halved from 5.0 per user request)

# Statuses Kalshi uses to indicate an order is no longer open
_CLOSED_STATUSES = {'filled', 'executed', 'canceled', 'expired'}


OPEN_ORDER_STATUSES = {'resting', 'open', 'pending'}
