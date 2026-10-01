"""
trade/ui/common.py — helpers shared by the dashboard's tabs: scoped reruns, season checks, trade-log loading, and
the entry-price / edge-quartile helpers the signals table and Review charts both use.
"""
import os
from datetime import datetime

import pandas as pd
import streamlit as st

import config
from KALSHI.k_helpers import TAKER_FEE_BASE, MAKER_FEE_BASE

_TRADE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # trade/


def _fragment_rerun():
    """
    Rerun scoped to the enclosing @st.fragment, so only that subtree redraws
    instead of the whole page (sidebar, other tabs, unrelated widgets all
    dimming/flashing along with it). scope="fragment" is only valid when the
    current execution genuinely IS a fragment-triggered rerun — if this ever
    fires during a full-script run instead (an edge case, not the common
    click path), Streamlit raises StreamlitAPIException; fall back to a plain
    full-app rerun rather than crash.
    """
    try:
        st.rerun(scope='fragment')
    except st.errors.StreamlitAPIException:
        st.rerun()

def _in_season(key: str) -> bool:
    if st.session_state.get('active_sports_ok'):
        # API call succeeded — missing key means no events, don't fall back
        return st.session_state.get('active_sports', {}).get(key, False)
    # API call failed or hasn't run yet — fall back to season months
    months = config.SEASON_MONTHS.get(key)
    if months is not None:
        return datetime.now().month in months
    return True

LOG_PATH    = os.path.join(_TRADE, 'logs', 'trades.csv')
NO_LOG_PATH = os.path.join(_TRADE, 'logs', 'no_trades.csv')

def _read_csv(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame()
    return pd.read_csv(path)

def _load_log():
    """YES-side log only (legacy callers)."""
    return _read_csv(LOG_PATH)

def _load_all_logs():
    """Return both logs combined, tagged with a 'side' column."""
    yes = _read_csv(LOG_PATH)
    no  = _read_csv(NO_LOG_PATH)
    if not yes.empty: yes['side'] = 'yes'
    if not no.empty:  no['side']  = 'no'
    if yes.empty and no.empty:
        return pd.DataFrame()
    return pd.concat([yes, no], ignore_index=True)

_Q_LABELS = ['Q1', 'Q2', 'Q3', 'Q4']

def _entry_price_series(df, side, force_cross, limit_only_mode):
    """Price the bot will actually fill/post at, per signal row (shared by the table and the quartile filter)."""
    # Rest rows use the price kalshi_odds() computed (one tick below the ask on the market's own
    # price grid); falls back to ask − 1¢ for a matched_df fetched before that column existed.
    def _rest(col, ask_col):
        return df[col] if col in df.columns else (df[ask_col] - 0.01).round(2)
    if side == 'no':
        return df['no_ask'] if force_cross else _rest('rest_price_no', 'no_ask')
    if force_cross:
        return df['yes_ask']
    if limit_only_mode:
        return _rest('rest_price_yes', 'yes_ask')
    rp = _rest('rest_price_yes', 'yes_ask')
    return df['yes_ask'].where(df.get('signal', pd.Series(False, index=df.index)).astype(bool), rp)

def _eop_quartile_cutoffs():
    """Q1/Q2/Q3 edge÷price cutoffs from settled history — same definition as the Review quartile chart."""
    h = _load_all_logs()
    if h.empty or not {'result', 'edge', 'entry_price'} <= set(h.columns):
        return None
    h = h[h['result'].isin(['WIN', 'LOSS', 'CLOSED_EARLY'])]
    eop = (pd.to_numeric(h['edge'], errors='coerce')
           / pd.to_numeric(h['entry_price'], errors='coerce').clip(lower=0.01)).dropna()
    if len(eop) < 8:
        return None
    cuts = eop.quantile([0.25, 0.5, 0.75]).tolist()
    return cuts if cuts == sorted(set(cuts)) else None


# Fallback-only defaults if a series' live fee lookup fails — see
# KALSHI/k_helpers.fee_rate_for(). No longer user-adjustable: a single override
# can't be correct across series with genuinely different published fee rates.
taker_fee = TAKER_FEE_BASE
maker_fee = MAKER_FEE_BASE
