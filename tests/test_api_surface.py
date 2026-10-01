"""
Every name that was importable from trade.core.execution before the refactor must stay importable (strategies, the MM
engine, the pin board, scripts and the web app all import from it), and every module in the app must still import.
"""
import importlib

import pytest

EXECUTION_NAMES = [
    'BASE_URL', 'KALSHI_POLL', 'MAKER_FEE', 'MAX_DURATION', 'MIN_CROSS_EV', 'MIN_NOTIONAL', 'OPEN_ORDER_STATUSES',
    'PINNACLE_POLL', 'PRE_EVENT_BUFFER', 'TAKER_FEE', '_CLOSED_STATUSES', '_ev', '_exact_ev_ok', '_final_order_status',
    '_is_resting', '_kelly_fraction', '_monitor', '_position_mark_price', '_price_str', '_recheck_signal',
    'cancel_all_resting_orders', 'cancel_and_rerest', 'cancel_order', 'cross_and_cancel_order', 'ensure_canceled',
    'extend_resting_order', 'filled_count', 'force_cancel_all', 'get_balance', 'get_market_price', 'get_market_prices',
    'get_order_status', 'get_orderbook_depth', 'get_portfolio_snapshot', 'get_position_marks', 'kelly_contracts',
    'list_resting_orders', 'place_order', 'place_rest_with_requote', 'resize_resting_order', 'resolve_contracts',
    'rest_price_dollars', 'signal_rest_price',
]

MODULES = [
    'applog', 'config', 'KALSHI.k_helpers', 'theODDS.p_helpers',
    'trade.core.execution', 'trade.core.positions', 'trade.core.pricing', 'trade.core.balance_feed',
    'trade.core.actionserver', 'trade.core.logging_io', 'trade.settle', 'trade.clv',
    'trade.strategies.kp_arb', 'trade.strategies.nothing', 'trade.strategies.prospect',
    'trade.mm.engine', 'trade.mm.ledger', 'trade.mm.ui', 'trade.pinboard.board', 'trade.pinboard.ui',
]


@pytest.mark.parametrize('name', EXECUTION_NAMES)
def test_execution_name_still_exported(name):
    ex = importlib.import_module('trade.core.execution')
    assert hasattr(ex, name), f'trade.core.execution.{name} is gone'


@pytest.mark.parametrize('mod', MODULES)
def test_module_imports(mod):
    importlib.import_module(mod)
