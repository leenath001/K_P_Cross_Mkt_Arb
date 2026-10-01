"""
trade/ui/startup.py — once-per-session setup (which sports have Pinnacle events, the starting Kalshi balance) and the
live-cash reader trading uses for sizing.
"""
from datetime import datetime

import streamlit as st

import config
from theODDS.p_helpers import fetch_usage, check_sports_with_events
from trade.core.execution import get_balance
from trade.core.balance_feed import latest as _balance_latest
from applog import get_logger

log = get_logger(__name__)


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_event_counts(keys: tuple, hrs: int):
    """Process-wide, so opening/reloading the page doesn't re-spend a credit per sport (or wait on them)."""
    return check_sports_with_events(list(keys), hrs)

# ── Startup: check which sports have real Pinnacle events in the window ───────
def _load_active_sports():
    with st.spinner(f'Checking OddsAPI for upcoming events across {len(config.SPORTS_CONFIG)} sports...'):
        try:
            used, remaining = fetch_usage()
            st.session_state['api_used']       = used
            st.session_state['api_remaining']  = remaining
            _event_counts = _cached_event_counts(tuple(config.SPORTS_CONFIG.keys()), config.LOOKAHEAD_HRS)
            # True = at least 1 event in the look-ahead window
            st.session_state['active_sports']     = {k: v > 0 for k, v in _event_counts.items()}
            st.session_state['active_sports_at']  = datetime.utcnow().strftime('%H:%M UTC')
            st.session_state['active_sports_ok']  = True
        except Exception:
            log.exception('Active-sports check failed')
            st.session_state['active_sports']     = {}
            st.session_state['active_sports_at']  = '—'
            st.session_state['active_sports_ok']  = False


def run():
    """First run of a browser session: active-sports check + a starting balance, so trading never waits on either."""
    if 'active_sports' not in st.session_state:
        _load_active_sports()

    # ── Startup: pull the Kalshi balance once so trading never blocks on a manual
    #    "Refresh balance" click first — same pattern as the active-sports check above.
    if 'balance' not in st.session_state:
        try:
            st.session_state['balance'] = get_balance()
        except Exception:
            log.exception('Startup balance fetch failed')
            st.session_state['balance'] = None


def _live_cash():
    """Available Kalshi cash for sizing: the balance feed's latest read (refreshed every ~10s, same number as the
    Cash readout), else the one-off read taken at startup."""
    try:
        cash = _balance_latest().get('cash')
    except Exception:
        cash = None
    if cash is not None:
        st.session_state['balance'] = cash
    return st.session_state.get('balance')
