"""trade/mm/ui.py — Market Making: the Trade tab (live board) and the Review tab (PnL)."""
import os, time
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import threading
from trade.mm.engine import (MMEngine, ENGINE_VERSION, MAX_SIZE, DEFAULT_SIZE, zero_fee_sports, screen_candidates, spec_from_row)
from trade.mm.ledger import pnl_table, load_fills
from trade.settle import fetch_market_result

_board = components.declare_component(
    'mm_board', path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend'))

RESCREEN_SEC = 45 * 60      # games start and drop off; re-screen (≈1 OddsAPI credit per zero-fee sport) at most this often


@st.cache_data(ttl=60, show_spinner=False)
def _market_result(ticker: str):
    return (fetch_market_result(ticker) or {}).get('result')


def _stop_stale_engines():
    """An engine object from before a code change keeps running in its own thread; stop it (cancels its quotes)."""
    for t in threading.enumerate():
        if t.name == 'mm-engine':
            old = getattr(getattr(t, '_target', None), '__self__', None)
            if old is not None and hasattr(old, 'stop'):
                try:
                    old.stop()
                except Exception:
                    pass


@st.cache_resource
def _make_engine(version: int) -> MMEngine:
    _stop_stale_engines()          # Streamlit hot-reloads changed modules but keeps cached objects built from the old class
    e = MMEngine()
    e.start()
    return e


def get_engine() -> MMEngine:
    return _make_engine(ENGINE_VERSION)


def _toggle_quoting(eng):
    """ON button: fire orders. While quoting the same button reads OFF, and clicking it cancels everything."""
    if eng.params['live']:
        eng.kill()
    else:
        eng.set_params(live=True)


def _cancel_all(eng):
    eng.kill()


def _screen_if_needed(eng, hrs: int):
    """Preload every zero-fee, Pinnacle-matched market in the window (best market per game) — in the background."""
    if eng.screen_state == 'loading':
        return
    if eng.screen_key == hrs and time.time() - eng.screen_at < RESCREEN_SEC and not eng.screen_state.startswith('error'):
        return
    active = st.session_state.get('active_sports') or {}
    sports = zero_fee_sports([k for k, v in active.items() if v] if active else None)
    eng.screen_async(sports, int(hrs))


def render_trade():
    eng = get_engine()

    # Controls are a fragment too (refreshing every 2s so the ON/OFF label, market count and cash stay current):
    # clicking ON/OFF, Cancel all or changing the hours reruns only this block, never the whole page.
    @st.fragment(run_every='2s')
    def _controls():
        # ── controls, one row: window · ON/OFF · cancel all ────────────────────
        # Equal-width cells, bottom-aligned so the buttons sit on the same line as the input boxes.
        c1, c1b, c1c, c2, c3, _spare = st.columns([1, 1, 1, 1, 1, 1.4], vertical_alignment='bottom')
        hrs = c1.number_input('Games starting within (h)', 2, 48, 18, key='_mm_hrs')
        c1b.number_input('Size, all markets', 1, MAX_SIZE, DEFAULT_SIZE, key='_mm_size_all',
                         on_change=lambda: (eng.set_all_sizes(st.session_state['_mm_size_all']), st.session_state.setdefault('_mm_size_seen', set()).update(eng.snapshot_tickers())),
                         help='Contracts offered on each side of every market. Each card\'s own box can still be edited after.')
        c1c.number_input('Pinnacle refresh (s)', 10, 300, 30, step=5, key='_mm_fair_sec',
                         on_change=lambda: eng.set_params(fair_refresh_sec=st.session_state['_mm_fair_sec']),
                         help='How often Pinnacle fair values are re-fetched for sports with live quotes. Each refresh costs OddsAPI credits.')
        seen = st.session_state.setdefault('_mm_size_seen', set())
        eng.apply_size_to_new(st.session_state['_mm_size_all'], seen)
        if eng.params['fair_refresh_sec'] != st.session_state['_mm_fair_sec']:
            eng.set_params(fair_refresh_sec=st.session_state['_mm_fair_sec'])
        quoting = eng.params['live']
        # The label is the ACTION: ON (click to fire quotes) while stopped; OFF (click to cancel them) while quoting.
        c2.button('OFF' if quoting else 'ON', key='_mm_onoff', type='primary' if quoting else 'secondary',
                  on_click=_toggle_quoting, args=(eng,), width='stretch',
                  help='ON → fire quotes on every funded market. While quoting this button reads OFF: click it to cancel them all.')
        c3.button('Cancel all', key='_mm_cancel_all', on_click=_cancel_all, args=(eng,), width='stretch',
                  help='Stops quoting and cancels every market-making order (including strays from an earlier run).')
        _screen_if_needed(eng, int(hrs))

        snap = eng.snapshot()
        n_q = sum(1 for m in snap['markets'] if m['status'] == 'quoting')
        cash = snap.get('cash')
        if eng.screen_state == 'loading':
            st.caption(f'Loading zero-fee markets… {int(time.time() - eng.screen_at)}s (showing the last saved list meanwhile)')
        elif eng.screen_state.startswith('error'):
            st.caption(f'Market load failed — {eng.screen_state}')
        else:
            st.caption(f"{len(snap['markets'])} markets loaded · {n_q} quoting" +
                       (f" · ${cash:,.2f} available" if cash is not None else '') +
                       ' · markets are funded in ranking order until cash runs out')

    _controls()

    # ── live board ────────────────────────────────────────────────────────
    @st.fragment(run_every='1s')
    def _live_board():
        act = _board(state=eng.snapshot(), key='mm_board', default=None)
        # Actions from the board (queued client-side); the nonce de-dupes re-delivery, `ack` releases the queue.
        if act and act.get('nonce') != eng.ack:
            a, t = act.get('action'), act.get('ticker')
            if a == 'set_quote':
                eng.set_manual_quote(t, act['side'], float(act['price_c']))
            elif a == 'clear_quote':
                eng.clear_manual_quote(t, act['side'])
            elif a == 'set_size':
                eng.set_market_size(t, int(act['size']))
            elif a == 'cancel_market':
                eng.pause_market(t)
            elif a == 'resume_market':
                eng.resume_market(t)
            elif a == 'focus':
                eng.set_focus(t)
            eng.ack = act.get('nonce')

    _live_board()


def render_review():
    eng = get_engine()

    @st.fragment(run_every='30s')
    def _pnl_panel():
        st.markdown('#### Market-making PnL  (separate from the K/P bot)')
        fills = load_fills()
        if fills.empty:
            st.caption('No market-making fills yet. Fills are logged to trade/logs/mm_fills.csv.')
            return
        results = {t: _market_result(t) for t in fills['ticker'].unique()}
        live_fair = {m['ticker']: m['fair_c'] for m in eng.snapshot()['markets'] if m['fair_c'] is not None}
        tbl = pnl_table(results, live_fair)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Locked spread', f"${tbl['locked_$'].sum():+.3f}")
        c2.metric('Inventory (settled / marked)', f"${tbl['inventory_$'].sum():+.3f}")
        c3.metric('Total MM PnL', f"${tbl['total_$'].sum():+.3f}")
        c4.metric('Fills logged', len(fills))
        st.dataframe(tbl, hide_index=True, width='stretch')
        with st.expander('Fill log'):
            st.dataframe(fills.sort_values('ts', ascending=False), hide_index=True, width='stretch')

    _pnl_panel()
