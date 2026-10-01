"""
trade/core/execution.py — Kalshi order execution primitives.

Strategy-agnostic: placing/canceling/monitoring orders, Kelly sizing, and EV math.
Every strategy in trade/strategies/ builds on these rather than reimplementing them
— the three-way drift between bot.py/prospect.py/nothing.py (three different
already_bet_tickers() implementations, inconsistent fee handling before this
module existed) is exactly the bug class this consolidation removes.

Split into submodules by concern (constants, sizing, orders, account, market_data, order_mgmt, monitor);
every name stays importable from trade.core.execution, so existing imports are unchanged.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from trade.core.execution.constants import (  # noqa: F401
    BASE_URL, TAKER_FEE, MAKER_FEE, MIN_CROSS_EV, KALSHI_POLL, PINNACLE_POLL, MAX_DURATION, PRE_EVENT_BUFFER, MIN_NOTIONAL, _CLOSED_STATUSES, OPEN_ORDER_STATUSES,
)
from trade.core.execution.sizing import (  # noqa: F401
    _exact_ev_ok, _ev, _kelly_fraction, kelly_contracts, resolve_contracts,
)
from trade.core.execution.orders import (  # noqa: F401
    _price_str, place_order, cancel_order, _is_resting, ensure_canceled, list_resting_orders, cancel_all_resting_orders, get_order_status, filled_count, _final_order_status, force_cancel_all,
)
from trade.core.execution.account import (  # noqa: F401
    get_balance, get_portfolio_snapshot, _position_mark_price, get_position_marks,
)
from trade.core.execution.market_data import (  # noqa: F401
    get_market_price, get_market_prices, get_orderbook_depth, rest_price_dollars, signal_rest_price,
)
from trade.core.execution.order_mgmt import (  # noqa: F401
    place_rest_with_requote, cross_and_cancel_order, extend_resting_order, resize_resting_order, cancel_and_rerest,
)
from trade.core.execution.monitor import (  # noqa: F401
    _recheck_signal, _monitor,
)
