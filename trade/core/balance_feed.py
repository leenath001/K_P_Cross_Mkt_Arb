"""
trade/core/balance_feed.py — background Cash / Portfolio polling for the top-bar readout.

The browser can't call Kalshi directly (it would need the RSA private key), so a background thread refreshes both
numbers every INTERVAL_SEC and exposes them over the same loopback-only, token-gated HTTP channel the live boards use
(trade/core/actionserver.py). The page's own JS polls that local endpoint instead.
"""
import threading, time

from trade.core.execution import get_portfolio_snapshot, get_position_marks
from trade.core.actionserver import ActionServer
from applog import get_logger

log = get_logger(__name__)

INTERVAL_SEC = 10

_lock = threading.Lock()
_state = {'cash': None, 'portfolio': None, 'positions_value': None, 'positions': [],
          'updated_at': 0, 'error': None}


def _snapshot():
    with _lock:
        return dict(_state)


def _refresh():
    """
    One refresh. Balance and positions are fetched independently so a failed positions read (the flakier call) never
    freezes the Cash number. Portfolio = cash + open positions marked to market; while the marks are unavailable it
    falls back to Kalshi's own positions value (last-trade priced) rather than going blank.
    """
    errors, update = [], {}
    try:
        snap = get_portfolio_snapshot()
        update['cash'] = snap['cash']
        kalshi_pos_value = snap['positions_value']
    except Exception as exc:
        errors.append(f'balance: {exc}')
        kalshi_pos_value = None
    try:
        marks = get_position_marks()
        update['positions'] = marks
        update['positions_value'] = round(sum(m['mark_dollars'] for m in marks), 2)
    except Exception as exc:
        errors.append(f'positions: {exc}')
        if kalshi_pos_value is not None:
            update['positions_value'] = kalshi_pos_value

    with _lock:
        _state.update(update)
        if 'cash' in update and _state['positions_value'] is not None:
            _state['portfolio'] = round(_state['cash'] + _state['positions_value'], 2)
        if not errors:
            _state['updated_at'] = time.time()
        _state['error'] = '; '.join(errors) or None
    if errors:
        log.warning('balance_feed: refresh failed: %s', _state['error'])


def _loop():
    while True:
        try:
            _refresh()
        except Exception:
            log.exception('balance_feed: unexpected refresh error')
        time.sleep(INTERVAL_SEC)


def start() -> dict:
    """
    Start the poller once per process and return {'port', 'token'} for the page's JS to reach it.

    Idempotent across ordinary Streamlit reruns AND across a runOnSave hot-reload: a hot-reload re-executes this
    module's top-level code, which would reset a module-level "already started" flag to None even though the OLD
    background thread is still alive and still perfectly good — reading a stale None there is exactly what crashed
    here. Instead, the {'port', 'token'} info is stashed directly on the Thread OBJECT, which survives the reload
    (only this module's own globals get reset, not already-running threads) — so an old, still-running thread is
    found and reused via its own attribute, never through this module's globals.
    """
    for t in threading.enumerate():
        if t.name == 'balance-feed' and t.is_alive() and hasattr(t, 'info'):
            return t.info
    server = ActionServer(_snapshot, lambda act: None)   # read-only: no actions accepted
    th = threading.Thread(target=_loop, name='balance-feed', daemon=True)
    th.info = server.info()
    th.start()
    return th.info
