"""trade/mm/ui.py — Market Making: the Trade tab (live board) and the Review tab (PnL)."""
import os, time
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import threading
from trade.mm.engine import (MMEngine, ENGINE_VERSION, MAX_SIZE, DEFAULT_SIZE, MAX_INV_LIMIT, DEFAULT_MAX_INV, zero_fee_sports, screen_candidates, spec_from_row)
from trade.mm.ledger import pnl_table, load_fills, load_markouts, mm_summary
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


def _engine_module():
    """The CURRENT trade.mm.engine module. Streamlit re-imports an edited module, but this file (unedited) keeps the names it
    imported at the top, i.e. the OLD class and version: an engine fix then never took effect until this file was touched."""
    import importlib
    return importlib.import_module('trade.mm.engine')


@st.cache_resource
def _make_engine(version: int):
    _stop_stale_engines()          # Streamlit hot-reloads changed modules but keeps cached objects built from the old class
    e = _engine_module().MMEngine()
    e.start()
    return e


def get_engine():
    return _make_engine(_engine_module().ENGINE_VERSION)


def _toggle_quoting(eng):
    """ON button: fire orders. While quoting the same button reads OFF, and clicking it cancels everything."""
    if eng.params['live']:
        eng.kill()
    else:
        eng.set_params(live=True)


def _cancel_all(eng):
    eng.kill()


def _refresh_markets(eng):
    """Force a market re-screen now (the next controls refresh, ≤2s, starts it in the background)."""
    if eng.screen_state != 'loading':
        eng.screen_key = None


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
        c1, c1b, c1m, c1c, c2, c2b, c3, c4 = st.columns([1.15, 1, 1, 1, 0.75, 1.1, 0.9, 1.3], vertical_alignment='bottom')
        hrs = c1.number_input('Games starting within', 2, 48, 18, key='_mm_hrs')
        c1b.number_input('Size, all markets', 1, MAX_SIZE, DEFAULT_SIZE, key='_mm_size_all',
                         on_change=lambda: (eng.set_all_sizes(st.session_state['_mm_size_all']), st.session_state.setdefault('_mm_size_seen', set()).update(eng.snapshot_tickers())),
                         help='Contracts offered on each side of every market. Each card\'s own box can still be edited after.')
        c1m.number_input('Max inventory, all', 1, MAX_INV_LIMIT, DEFAULT_MAX_INV, key='_mm_maxinv_all',
                         on_change=lambda: (eng.set_all_max_inv(st.session_state['_mm_maxinv_all']), st.session_state.setdefault('_mm_size_seen', set()).update(eng.snapshot_tickers())),
                         help='Most net contracts held per market, either direction. At the cap that side stops quoting until we can reduce; '
                              'the other side stays up. Each card\'s own box can still be edited after.')
        c1c.number_input('Pinnacle refresh', 10, 300, 30, step=5, key='_mm_fair_sec',
                         on_change=lambda: eng.set_params(fair_refresh_sec=st.session_state['_mm_fair_sec']),
                         help='How often Pinnacle fair values are re-fetched for sports with live quotes. Each refresh costs OddsAPI credits.')
        seen = st.session_state.setdefault('_mm_size_seen', set())
        eng.apply_size_to_new(st.session_state['_mm_size_all'], seen, st.session_state['_mm_maxinv_all'])
        if eng.params['fair_refresh_sec'] != st.session_state['_mm_fair_sec']:
            eng.set_params(fair_refresh_sec=st.session_state['_mm_fair_sec'])
        quoting = eng.params['live']
        # The label is the ACTION: ON (click to fire quotes) while stopped; OFF (click to cancel them) while quoting.
        c2.button('OFF' if quoting else 'ON', key='_mm_onoff', type='primary' if quoting else 'secondary',
                  on_click=_toggle_quoting, args=(eng,), width='stretch',
                  help='ON → fire quotes on every funded market. While quoting this button reads OFF: click it to cancel them all.')
        c2b.button('Refresh', key='_mm_refresh', on_click=_refresh_markets, args=(eng,), width='stretch',
                   help='Re-screen now for new zero-fee games instead of waiting for the 45-minute refresh (about 1 OddsAPI credit per zero-fee sport).')
        offload = c4.toggle('Auto-offload', value=True, key='_mm_offload',
                            help='While a market is quoting, sell (or buy back) held inventory at the touch whenever it '
                                 'nets a profit after the taker fee.')
        if eng.params['auto_offload'] != offload:
            eng.set_params(auto_offload=offload)
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
                       ' · markets are funded in ranking order until cash runs out' +
                       (f" · {len(eng.screen_failed)} sport(s) failed to load (OddsAPI busy) — press Refresh markets" if getattr(eng, 'screen_failed', []) else ''))

    _controls()

    # ── live board ────────────────────────────────────────────────────────
    @st.fragment(run_every='1s')
    def _live_board():
        act = _board(state=eng.snapshot(), key='mm_board', default=None)
        # Actions from the board (queued client-side); the nonce de-dupes re-delivery, `ack` releases the queue.
        if act and act.get('nonce') != eng.ack:
            eng.handle_action(act)
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

        # ── market-maker metrics ──────────────────────────────────────────
        st.markdown('#### Market-maker metrics')
        summ = mm_summary(fills, load_markouts(), results)
        for row in (summ['kpi'][:6], summ['kpi'][6:]):
            cols = st.columns(len(row))
            for col, (label, val, hint) in zip(cols, row):
                col.metric(label, val, help=hint)
        w = summ['walk']
        import plotly.graph_objects as go
        g1, g2 = st.columns(2)
        fig = go.Figure(go.Scatter(x=pd.to_datetime(w['ts']), y=w['cum_realized'], mode='lines+markers', line_shape='hv',
                                   line=dict(color='#2ecc71'), hovertemplate='%{x}<br>cumulative realized $%{y:.3f}<extra></extra>'))
        fig.update_layout(title='Cumulative realized PnL (net of fees)', height=280, margin=dict(l=10, r=10, t=40, b=10), yaxis_title='$')
        g1.plotly_chart(fig, width='stretch')
        fig = go.Figure(go.Scatter(x=pd.to_datetime(w['ts']), y=w['net_pos_all'], mode='lines', line_shape='hv',
                                   line=dict(color='#4c9be8'), hovertemplate='%{x}<br>net contracts %{y:.0f}<extra></extra>'))
        fig.update_layout(title='Net inventory over time (all markets)', height=280, margin=dict(l=10, r=10, t=40, b=10), yaxis_title='contracts')
        g2.plotly_chart(fig, width='stretch')
        e = w.dropna(subset=['edge_c'])
        if len(e):
            g3, g4 = st.columns(2)
            fig = go.Figure(go.Histogram(x=e['edge_c'], nbinsx=25, marker_color='#f5b301'))
            fig.update_layout(title='Edge at fill vs Pinnacle fair (¢) — right of 0 = we got the better side', height=260,
                              margin=dict(l=10, r=10, t=40, b=10), bargap=0.05)
            g3.plotly_chart(fig, width='stretch')
            snap_m = pd.DataFrame(eng.snapshot()['markets'])
            if len(snap_m):
                q = pd.DataFrame({'market': snap_m['ticker'].str[-24:],
                                  'two-sided %': [m['stats']['two_sided_pct'] for m in eng.snapshot()['markets']],
                                  'requotes': [m['stats']['requotes'] for m in eng.snapshot()['markets']],
                                  'API errors': [m['stats']['api_err'] for m in eng.snapshot()['markets']]})
                g4.markdown('**Quoting quality (this session)**')
                g4.caption('two-sided % = share of cycles both a bid and an ask were up; high re-quotes with few fills means paying API churn for nothing.')
                g4.dataframe(q[q['requotes'] > 0].sort_values('requotes', ascending=False), hide_index=True, width='stretch', height=210)
        st.markdown('**By market**')
        st.dataframe(summ['by_market'], hide_index=True, width='stretch')
        with st.expander('Fill log'):
            st.dataframe(fills.sort_values('ts', ascending=False), hide_index=True, width='stretch')

    _pnl_panel()
