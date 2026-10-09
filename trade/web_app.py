"""
web_app.py — Streamlit dashboard for K/P Cross-Market Arbitrage.

Usage:
    streamlit run trade/web_app.py
"""

import os, sys
_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRADE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, _TRADE)

import importlib

import streamlit as st

from trade.ui import common, analytics, startup, chrome, trade_tab, review_tab
import trade.mm.ui as mm_ui

# Re-import the UI modules on every full run (dependencies first). When this page was one file, each run re-executed
# its imports, so an edit to e.g. trade/core/execution.py was live on the next rerun; separately imported modules
# would otherwise keep the old functions until the server restarts (Streamlit reloads only the file that changed).
# (Not trade.mm.ui: it registers the board component at import and handles engine reloads itself.)
for _m in (common, analytics, startup, chrome, trade_tab, review_tab):
    importlib.reload(_m)

st.set_page_config(page_title='K/P Arb Dashboard', layout='wide')

from trade.core.shutdown import install_exit_watchdog
install_exit_watchdog()          # Ctrl+C always quits, even if a network call is stuck (see trade/core/shutdown.py)

chrome.render_head()
if 'active_sports' not in st.session_state or 'balance' not in st.session_state:
    startup.run()
chrome.render_top_config()

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_pin, tab_mm = st.tabs(['Pinnacle', 'Market Making'])
with tab_pin:
    tab_trade, tab_review = st.tabs(['Trade', 'Review'])
with tab_mm:
    tab_mm_trade, tab_mm_review = st.tabs(['Trade', 'Review'])

with tab_trade:
    trade_tab.render()
with tab_review:
    review_tab.render()
with tab_mm_trade:
    mm_ui.render_trade()
with tab_mm_review:
    mm_ui.render_review()
