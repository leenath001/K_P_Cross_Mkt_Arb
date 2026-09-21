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

import threading
import time
import streamlit as st
import pandas as pd
from datetime import datetime
import config
from theODDS.p_helpers import pinnacle_odds, fetch_usage, get_api_usage, get_active_sports, check_sports_with_events
from KALSHI.k_helpers   import kalshi_odds, TAKER_FEE_BASE, MAKER_FEE_BASE
from trade.core.execution import (get_balance, cross_and_cancel_order, cancel_and_rerest,
                                  get_orderbook_depth, cancel_all_resting_orders, kelly_contracts,
                                  resize_resting_order, _ev)
from trade.core.positions import open_tickers, opposite_leg_blocked
from trade.mm.ui import render_trade as _mm_trade, render_review as _mm_review
from trade.clv import load_closing_lines, start_background as _start_clv_capture
from trade.strategies.kp_arb import run_all_signals, resume_monitoring
from dashboard          import StreamlitDashboard
import settle
from applog             import get_logger

log = get_logger(__name__)

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

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title='K/P Arb Dashboard', layout='wide')

# The live dashboard's countdown timer reruns every 1s, its positions table
# every 10s, and Review every 30s (see @st.fragment usage below) — Streamlit's
# built-in top-right "running" indicator would
# otherwise be flashing almost constantly during an active session. There's
# no config option for this (it's core UX plumbing), only its own testid.
# Buttons also get shorter padding here — Streamlit's default button height
# reads as noticeably tall/clunky once there are several in a row.
st.markdown("""
<style>
[data-testid="stStatusWidget"] { display: none; }
button[data-testid^="stBaseButton-"] {
    padding-top: 0.28rem;
    padding-bottom: 0.28rem;
}
/* Button-only rows keyed "_btnrow_*" (st.container(key=...)) — a wide leading
   spacer column pushes the buttons to the right edge, and this fixes the gap
   between them at 15px regardless of Streamlit's column-count gap presets. */
div[class*="st-key-_btnrow_"] [data-testid="stHorizontalBlock"] {
    gap: 15px;
}
/* ✕ panel-close buttons — no button pill (transparent, borderless), a
   smaller glyph, pushed flush to the right edge of their column. */
/* Inner tabs (Trade | Review) sit on the same row, to the right of the outer tabs (Pinnacle | Market Making) */
[data-baseweb="tab-panel"] [data-baseweb="tab-list"] {
    margin-top: -58px; margin-left: 200px; width: calc(100% - 200px);
    position: relative; z-index: 2;
}
[data-baseweb="tab-panel"] [data-baseweb="tab-list"] [data-baseweb="tab-border"] { display: none; }
/* No dead space above the tabs: less top padding, and the zero-height tab-memory script frame takes no room */
[data-testid="stMainBlockContainer"] { padding-top: 3.5rem !important; }
/* Market Making ON / Cancel all buttons: same height as the input boxes beside them */
div[class*="st-key-_mm_onoff"] button, div[class*="st-key-_mm_cancel_all"] button { min-height: 38px; height: 38px; position: relative; top: -1px; }
[data-testid="stElementContainer"]:has(> iframe[data-testid="stIFrame"]) {
    position: absolute; width: 0; height: 0; margin: 0; overflow: hidden;
}
div[class*="st-key-_close_inspect"] button,
div[class*="st-key-_close_calib_detail"] button,
div[class*="st-key-_close_bar_"] button {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 0.2rem !important;
    font-size: 0.75rem;
    float: right;
}
</style>
""", unsafe_allow_html=True)

_start_clv_capture()   # closing-line capture thread (idempotent across reruns)

# Remember the selected tab in every tab bar (per browser tab) and put it back if a rerun resets it to the first one.
# Streamlit re-mounts the tab bars on some reruns (the Market Making ON/OFF click did), which snaps the page back to
# Pinnacle → Trade. Same-origin script; no effect on anything else.
import streamlit.components.v1 as _components
_components.html("""<script>
(function(){
  const P=window.parent, D=P.document, KEY='tabmem_v1';
  if(P.__tabmem) return; P.__tabmem=true;
  const store=()=>{try{return JSON.parse(P.sessionStorage.getItem(KEY)||'{}')}catch(e){return {}}};
  const put=o=>{try{P.sessionStorage.setItem(KEY,JSON.stringify(o))}catch(e){}};
  const lists=()=>[...D.querySelectorAll('[data-baseweb="tab-list"]')];
  const sig=l=>lists().indexOf(l)+':'+[...l.querySelectorAll('[data-baseweb="tab"]')].map(t=>t.innerText.trim()).join('|');
  D.addEventListener('click',e=>{const t=e.target.closest('[data-baseweb="tab"]'); if(!t)return;
    const l=t.closest('[data-baseweb="tab-list"]'); if(!l)return; const s=store(); s[sig(l)]=t.innerText.trim(); put(s)},true);
  function restore(){
    const s=store();
    lists().forEach(l=>{const want=s[sig(l)]; if(!want)return;
      const cur=l.querySelector('[data-baseweb="tab"][aria-selected="true"]'); if(cur&&cur.innerText.trim()===want)return;
      const tgt=[...l.querySelectorAll('[data-baseweb="tab"]')].find(t=>t.innerText.trim()===want); if(tgt)tgt.click();});
  }
  new MutationObserver(()=>{clearTimeout(P.__tabmemT);P.__tabmemT=setTimeout(restore,150)})
    .observe(D.body,{subtree:true,childList:true,attributes:true,attributeFilter:['aria-selected']});
})();
</script>""", height=0)


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

# ── Sidebar ──────────────────────────────────────────────────────────────────

# Everything in the sidebar lives in ONE fragment: clicking Refresh usage / Refresh balance or moving a slider reruns
# only this block, not the whole page. Values other parts read (hours, threshold, live) are keyed session_state entries.
with st.sidebar:
    @st.fragment
    def _sidebar_panel():
        st.header('API Usage')
        if st.button('Refresh usage'):
            with st.spinner('Fetching...'):
                try:
                    used, remaining = fetch_usage()
                    st.session_state['api_used']      = used
                    st.session_state['api_remaining'] = remaining
                except Exception as e:
                    log.exception('Refresh usage failed')
                    st.error(f'Failed: {e}')

        used      = st.session_state.get('api_used',      0)
        remaining = st.session_state.get('api_remaining', 500)
        limit     = used + remaining
        pct       = min(max(used / max(limit, 1), 0.0), 1.0)
        st.progress(pct, text=f'{used} / {limit} requests used  ({remaining} remaining)')
        if pct >= 0.9:
            st.error('API quota nearly exhausted')
        elif pct >= 0.7:
            st.warning('API quota above 70%')

        st.divider()

        st.header('Kalshi Balance')
        if st.button('Refresh balance'):
            with st.spinner('Fetching...'):
                try:
                    st.session_state['balance'] = get_balance()
                except Exception as e:
                    log.exception('Refresh balance failed')
                    st.error(f'Failed: {e}')
        balance = st.session_state.get('balance', None)
        if balance is not None:
            st.metric('Balance', f'${balance:.2f}')
        else:
            st.caption('Click refresh to load')

        st.divider()

        st.header('Run Config')
        st.slider('Look-ahead (hours)', 1, 168, config.LOOKAHEAD_HRS, key='_hrs')
        st.slider('Match threshold',    0.5, 1.0, 0.85, step=0.01, key='_threshold')
        st.toggle('Fetch live games', value=config.LIVE, key='_live')

        st.caption(
            'Fees are fetched live per Kalshi series (not a fixed global rate) — verified '
            'to genuinely vary, e.g. MLB gets a 0.5x multiplier and several soccer/boxing '
            'series charge no maker fee at all. See the "fee %" columns in the signals table.'
        )

    _sidebar_panel()

# Fallback-only defaults if a series' live fee lookup fails — see
# KALSHI/k_helpers.fee_rate_for(). No longer user-adjustable: a single override
# can't be correct across series with genuinely different published fee rates.
taker_fee = TAKER_FEE_BASE
maker_fee = MAKER_FEE_BASE


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_pin, tab_mm = st.tabs(['Pinnacle', 'Market Making'])
with tab_pin:
    tab_trade, tab_review = st.tabs(['Trade', 'Review'])
with tab_mm:
    tab_mm_trade, tab_mm_review = st.tabs(['Trade', 'Review'])

# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — TRADE
# ════════════════════════════════════════════════════════════════════════════

with tab_trade:

    # The sports picker is its own fragment: ticking a sport / Select All / Re-check reruns only this block.
    @st.fragment
    def _sports_panel():
        # ── Sport selection ──────────────────────────────────────────────────────
        st.subheader('Select Sports')

        # Show OddsAPI active-sport check status
        _as_ok = st.session_state.get('active_sports_ok', False)
        _as_at = st.session_state.get('active_sports_at', '—')
        _as    = st.session_state.get('active_sports', {})
        _n_active_sports = sum(1 for k in config.SPORTS_CONFIG if _as.get(k, False))
        _all_keys      = list(config.SPORTS_CONFIG.keys())
        _paused_sports = getattr(config, 'PAUSED_SPORTS', set())
        # A [caption, button-cluster] column split, with the cluster itself a
        # horizontal=True container — each button sizes to its own content and
        # Streamlit's own gap applies uniformly between them, rather than trying
        # to guess per-button st.columns() ratios (which left uneven leftover
        # space whenever a button didn't fill its column exactly).
        _oa_cap, _oa_btns = st.columns([3, 2])
        with _oa_cap:
            if _as_ok:
                st.caption(
                    f':green[OddsAPI event check:] **{_n_active_sports}/{len(config.SPORTS_CONFIG)} sports have upcoming events** '
                    f'in the {st.session_state.get("_hrs", config.LOOKAHEAD_HRS)}h window — checked {_as_at}'
                )
            else:
                st.caption(':red[OddsAPI event check failed] — falling back to season-month defaults. Check your API key.')
        with _oa_btns:
            with st.container(key='_btnrow_oddsapi', horizontal=True,
                              horizontal_alignment='right', gap='medium'):
                if st.button('Re-check', key='recheck_odds_api',
                            help=f'Re-query events for all {len(config.SPORTS_CONFIG)} sports. Costs ~{len(config.SPORTS_CONFIG)} API credits.'):
                    _cached_event_counts.clear()
                    _load_active_sports()
                    _fragment_rerun()
                if st.button('Select All', key='_select_all_sports'):
                    for k in _all_keys:
                        st.session_state[f'sport_{k}'] = _in_season(k) and k not in _paused_sports
                if st.button('Deselect All', key='_deselect_all_sports'):
                    for k in _all_keys:
                        st.session_state[f'sport_{k}'] = False

        _CATEGORY_LABELS = {
            'americanfootball': 'American Football',
            'aussierules':      'Aussie Rules',
            'baseball':         'Baseball',
            'basketball':       'Basketball',
            'boxing':           'Boxing / MMA',
            'mma':              'Boxing / MMA',
            'icehockey':        'Ice Hockey',
            'lacrosse':         'Lacrosse',
            'rugbyleague':      'Rugby',
            'soccer':           'Soccer',
        }

        def _category(key):
            return _CATEGORY_LABELS.get(key.split('_')[0], key.split('_')[0].title())

        grouped = {}
        for key, cfg in config.SPORTS_CONFIG.items():
            grouped.setdefault(_category(key), []).append((key, cfg['label']))

        cat_order = sorted([c for c in grouped if c != 'Soccer']) + (['Soccer'] if 'Soccer' in grouped else [])

        selected_sports = []          # (kept for the count caption below; the fetch panel derives its own from session_state)

        non_soccer = [c for c in cat_order if c != 'Soccer']
        cols = st.columns(min(len(non_soccer), 3))
        for i, cat in enumerate(non_soccer):
            with cols[i % 3]:
                st.markdown(f'**{cat}**')
                for key, label in grouped[cat]:
                    if key in _paused_sports:
                        st.checkbox(f':gray[{label} (paused — see notebooks/win_loss_analysis.ipynb)]',
                                   value=False, key=f'sport_{key}', disabled=True)
                        continue
                    active = _in_season(key)
                    _color = 'green' if active else 'red'
                    if st.checkbox(f':{_color}[{label}]', value=active, key=f'sport_{key}'):
                        selected_sports.append(key)

        if 'Soccer' in grouped:
            st.markdown('**Soccer**')
            soccer_cols = st.columns(4)
            for i, (key, label) in enumerate(grouped['Soccer']):
                with soccer_cols[i % 4]:
                    if key in _paused_sports:
                        st.checkbox(f':gray[{label} (paused)]', value=False, key=f'sport_{key}', disabled=True)
                        continue
                    active = _in_season(key)
                    _color = 'green' if active else 'red'
                    if st.checkbox(f':{_color}[{label}]', value=active, key=f'sport_{key}'):
                        selected_sports.append(key)

        st.caption(f'{len(selected_sports)} sport(s) selected')
        st.divider()


    _sports_panel()

    # ── Fetch signals ────────────────────────────────────────────────────────
    @st.fragment
    def _fetch_and_trade_panel():
        # Read the sidebar / sports-picker values fresh on every run of THIS fragment — a closure over the module-level
        # names would be frozen at the last full-page run, and those widgets no longer trigger one.
        hrs       = st.session_state.get('_hrs', config.LOOKAHEAD_HRS)
        threshold = st.session_state.get('_threshold', 0.85)
        live      = st.session_state.get('_live', config.LIVE)
        balance   = st.session_state.get('balance', None)
        used      = st.session_state.get('api_used', 0)
        remaining = st.session_state.get('api_remaining', 500)
        selected_sports = [k for k in config.SPORTS_CONFIG
                           if k not in getattr(config, 'PAUSED_SPORTS', set()) and st.session_state.get(f'sport_{k}', False)]
        if st.button('Fetch Signals', type='primary', disabled=len(selected_sports) == 0):
            with st.status('Running pipeline...', expanded=True) as status:
                try:
                    st.write(f'Fetching Pinnacle odds for {len(selected_sports)} sport(s)...')
                    pinnacle_df = pinnacle_odds(selected_sports, hrs=hrs, live=live)
                    # NOTE: named _fetch_used/_fetch_remaining, not used/remaining — this
                    # function also reads the outer (sidebar) `used`/`remaining` closure
                    # variables further down (API-usage display near the Execute button).
                    # Assigning to a bare `used`/`remaining` anywhere in this function body
                    # would make Python treat them as local for the WHOLE function, shadowing
                    # the closure on every code path that doesn't go through this branch
                    # (e.g. clicking Execute on a later fragment rerun) — UnboundLocalError.
                    _fetch_used, _fetch_remaining = get_api_usage()
                    st.session_state['api_used']      = _fetch_used
                    st.session_state['api_remaining'] = _fetch_remaining
                    st.write(f'{len(pinnacle_df)} outcome rows  '
                            f'(API: {_fetch_used} used / {_fetch_remaining} remaining)')

                    st.write('Matching to Kalshi markets...')
                    matched_df = kalshi_odds(pinnacle_df, threshold=threshold)
                    st.write(f'{len(matched_df)} matches found')

                    st.session_state['matched_df'] = matched_df
                    status.update(label='Done', state='complete')
                    _fragment_rerun()
                except ValueError as e:
                    log.warning('Fetch Signals: no data — %s', e)
                    status.update(label='No data', state='error')
                    st.warning(str(e))
                    st.session_state['matched_df'] = pd.DataFrame()
                    _fragment_rerun()
                except Exception as e:
                    log.exception('Fetch Signals pipeline failed')
                    status.update(label='Error', state='error')
                    st.error(str(e))
                    st.session_state['matched_df'] = pd.DataFrame()
                    _fragment_rerun()

        # ── Results ──────────────────────────────────────────────────────────────
        matched_df = st.session_state.get('matched_df', pd.DataFrame())

        if not matched_df.empty:
            st.subheader('Results')

            # Side selector drives which signals are shown and how orders execute
            _sc1, _sc2, _sc3 = st.columns([1, 2.6, 2])
            with _sc1:
                trade_side = st.radio(
                    'Contract side',
                    ['YES', 'NO'],
                    index=0, horizontal=True, key='_side_select',
                )
            with _sc2:
                trade_mode = st.radio(
                    'Order mode',
                    ['Rest (maker)', 'Cross (taker)', 'Auto (cross if EV+, else rest)'],
                    index=2, horizontal=True, key='_mode_select',
                    help='Rest = post limit at top of book (fee varies per series, often 0-1.75%). '
                         'Cross = take at ask immediately (fee varies per series, ~3.5-7%). '
                         'Auto = cross only when taker EV is positive, else rest — the default, '
                         'since REST fills have outperformed CROSS in this bot\'s own trade '
                         'history (notebooks/win_loss_analysis.ipynb) and carry lower fees.'
                )
            side        = 'no' if trade_side == 'NO' else 'yes'
            force_cross = trade_mode.startswith('Cross')
            limit_only_mode = trade_mode.startswith('Rest')

            # Pick signal mask based on side + mode so fees are correct
            def _col(name):
                if name in matched_df.columns:
                    return matched_df[name]
                return pd.Series(False, index=matched_df.index)

            if side == 'no':
                if force_cross:
                    sig_mask = _col('signal_no_cross')
                elif limit_only_mode:
                    sig_mask = _col('signal_no')
                else:  # AUTO
                    sig_mask = _col('signal_no_cross') | _col('signal_no')
            else:  # YES
                if force_cross:
                    sig_mask = _col('signal')
                elif limit_only_mode:
                    sig_mask = _col('signal_yes_rest')
                else:  # AUTO
                    sig_mask = _col('signal') | _col('signal_yes_rest')

            signals = matched_df[sig_mask]

            # Edge÷price quartile filter — quartiles come from settled history (same
            # buckets as the Review chart), each live signal is bucketed by where its
            # own edge÷price falls. All four checked = no filtering.
            with _sc3:
                st.caption('Edge÷Price quartile',
                           help='Only trade signals whose edge÷price falls in the checked '
                                'quartile(s). Cutoffs come from your settled trade history '
                                '(same buckets as the Review chart). Counts are signals in each. '
                                'Q4 (largest) is always excluded.')
                _eop_cuts = _eop_quartile_cutoffs()
                if _eop_cuts is None:
                    st.caption('Needs 8+ settled trades to define quartiles.')
                elif not signals.empty:
                    _s_entry = _entry_price_series(signals, side, force_cross, limit_only_mode)
                    _s_fair  = (1 - signals['fair_prob']) if side == 'no' else signals['fair_prob']
                    _s_eop   = (_s_fair - _s_entry) / _s_entry.clip(lower=0.01)
                    _s_q     = pd.cut(_s_eop, [-float('inf')] + _eop_cuts + [float('inf')],
                                      labels=_Q_LABELS)
                    _q_counts = _s_q.value_counts()
                    # Q4 (largest edge÷price) is never offered — in settled history those
                    # trades realized far below the model's promise (overpaying for "edge"),
                    # so they're always excluded, not just unchecked.
                    with st.container(horizontal=True, gap='medium', vertical_alignment='bottom'):
                        _q_chosen = [q for q in _Q_LABELS[:3]
                                     if st.checkbox(f'{q} ({int(_q_counts.get(q, 0))})',
                                                    value=True, key=f'_q_filter_{q}')]
                    if int(_q_counts.get('Q4', 0)):
                        st.caption(f"Q4 excluded: {int(_q_counts.get('Q4', 0))} signal(s) "
                                   f"with edge÷price above {_eop_cuts[2]:.3f}")
                    signals = signals[_s_q.isin(_q_chosen).values]

            if not any(c in matched_df.columns for c in ('signal', 'signal_yes_rest', 'signal_no', 'signal_no_cross')):
                st.warning('Signal columns missing — refetch signals.')

            m1, m2, m3 = st.columns(3)
            m1.metric('Matches', len(matched_df))
            m2.metric(f'{side.upper()} signals', len(signals))
            m3.metric('Events',  matched_df['event_id'].nunique() if 'event_id' in matched_df.columns else '—')

            thread_key      = '_trade_thread'
            _trading_active = thread_key in st.session_state

            # ── Signals table + execute button (hidden while a trade session is active) ──
            if not _trading_active:
                if not signals.empty:
                    # Select all / Deselect all — data_editor only adopts a fresh 'Execute'
                    # column when its widget key changes, so these buttons bump a version
                    # counter to force that instead of trying to mutate editor state in place.
                    _ver_key = f'_signals_editor_version_{side}'
                    st.session_state.setdefault(_ver_key, 0)
                    _sig_hdr, _sig_btns = st.columns([2, 3])
                    _sig_hdr.markdown(f'#### {side.upper()} Signals')
                    with _sig_btns:
                        with st.container(key=f'_btnrow_signals_{side}', horizontal=True,
                                          horizontal_alignment='right', gap='medium'):
                            if st.button('Select All', key=f'_select_all_{side}'):
                                st.session_state[f'_signals_execute_override_{side}'] = True
                                st.session_state[_ver_key] += 1
                                _fragment_rerun()
                            if st.button('Deselect All', key=f'_deselect_all_{side}'):
                                st.session_state[f'_signals_execute_override_{side}'] = False
                                st.session_state[_ver_key] += 1
                                _fragment_rerun()
                            # Global — cancels EVERY resting order on Kalshi, not just this
                            # session's, matching cancel_all.py. Live-checked
                            # (list_resting_orders) rather than a locally-tracked registry.
                            if st.button('Cancel All Orders', key=f'_cancel_all_orders_{side}',
                                        help='Cancel every currently resting order on Kalshi '
                                             '(not just orders from this session).'):
                                with st.spinner('Canceling all resting orders...'):
                                    _cxl_result = cancel_all_resting_orders()
                                if _cxl_result['total'] == 0:
                                    st.info('No resting orders to cancel.')
                                elif _cxl_result['failed']:
                                    _fails = ', '.join(f'{t} ({oid})' for oid, t in _cxl_result['failed'][:5])
                                    st.warning(f"Canceled {_cxl_result['canceled']}/{_cxl_result['total']} — "
                                              f"{len(_cxl_result['failed'])} failed: {_fails}")
                                else:
                                    st.success(f"Canceled all {_cxl_result['canceled']} resting order(s).")

                    # Size × needs to be known before building the table below (it seeds
                    # the default shown in the now-editable Contracts column), so it's
                    # rendered here rather than down by the other trade options.
                    size_mult = st.number_input(
                        'Size ×', min_value=0.1, max_value=10.0, value=1.0, step=0.5,
                        key='trade_size_mult',
                        help='Multiplies Kelly contract count for any row you haven\'t hand-'
                             'edited in the Contracts column below. Order is skipped if total '
                             'cost exceeds available balance.'
                    )

                    # Pending tickers — live Kalshi state (open positions + resting
                    # orders), the same source run_all_signals() dedupes against when
                    # Execute is clicked. A UI-only filter that disagrees (e.g. trusting a
                    # stale CSV row) lets the user check/approve rows that silently get
                    # dropped server-side — the checkbox lies about what will happen, and
                    # Execute can appear to do nothing because everything approved got
                    # filtered out internally.
                    _pending_tickers = open_tickers()

                    # Compute entry_price (what the bot fills/posts at) before building display
                    # CROSS → taker fills at ask; REST → limit order at bid+1¢ (YES) or ask-1¢ (NO)
                    _sig = signals.copy()
                    _sig['entry_price'] = _entry_price_series(_sig, side, force_cross, limit_only_mode)

                    # On the NO tab, flip fair_prob to 1-fair_prob so it reads as the NO probability
                    if side == 'no':
                        _sig['fair_prob'] = (1 - _sig['fair_prob']).round(4)

                    # Edge = fair_prob - entry_price (raw, pre-fee) — same definition used
                    # for signal/ev math and in the post-execute log, just visible earlier now.
                    _sig['edge'] = (_sig['fair_prob'] - _sig['entry_price']).round(4)

                    # fee_pct = the live per-series rate that actually applies to THIS row's
                    # order type (taker if it'll cross, maker if it'll rest) — fetched by
                    # kalshi_odds() per series, not a single global assumption. Often 0 for
                    # series with no maker fee.
                    if force_cross:
                        _sig['fee_pct'] = (_sig['taker_fee_rate'] * 100).round(3)
                    elif limit_only_mode:
                        _sig['fee_pct'] = (_sig['maker_fee_rate'] * 100).round(3)
                    else:  # AUTO: per-row, matching whichever leg actually fired
                        _cross_col = 'signal_no_cross' if side == 'no' else 'signal'
                        _sig['fee_pct'] = _sig.apply(
                            lambda r: round(r['taker_fee_rate'] * 100, 3) if r.get(_cross_col, False)
                                      else round(r['maker_fee_rate'] * 100, 3),
                            axis=1,
                        )

                    # EV = edge - fee_rate*price*(1-price) — the fee-adjusted expected
                    # value per contract that actually gates whether a signal fires (see
                    # KALSHI/k_helpers._ev). 'edge' above is the raw, pre-fee mispricing —
                    # useful for spotting bad fuzzy-matches, but overstates true expected
                    # profit since it ignores the fee that gets charged on every fill.
                    _fee_frac = _sig['fee_pct'] / 100
                    _sig['ev'] = (_sig['edge'] - _fee_frac * _sig['entry_price'] * (1 - _sig['entry_price'])).round(4)

                    # contracts — auto-sized via Kelly × Size×, shown as an editable starting
                    # point (double-click a cell to type directly). Whatever ends up in this
                    # column at Execute time — edited or not — becomes contracts_override,
                    # which trade.core.execution.resolve_contracts() takes as-is ahead of
                    # recomputing Kelly sizing, so this is the actual number sent to Kalshi.
                    # kelly_contracts() already floors at MIN_NOTIONAL ($2) regardless of
                    # bankroll — including bankroll=0 (balance not yet fetched) — so there's
                    # no need to special-case that here with a bare "1"; let the real floor do
                    # its job the same way it would at Execute time.
                    _bankroll_for_sizing = balance if balance else 0
                    _sig['contracts'] = _sig.apply(
                        lambda r: max(1, round(kelly_contracts(
                            r['fair_prob'], r['entry_price'], _bankroll_for_sizing,
                            r['fee_pct'] / 100) * size_mult)),
                        axis=1,
                    )

                    # mkt_ask = Kalshi top-of-book ask (taker price, shown for reference only)
                    mkt_ask_col = 'no_ask' if side == 'no' else 'yes_ask'
                    display_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                                'fair_prob', 'entry_price', 'contracts', 'edge', 'ev',
                                                'fee_pct', mkt_ask_col, 'match_score', 'k_ticker']
                                    if c in _sig.columns]

                    editable = _sig[display_cols].copy().reset_index(drop=True)
                    editable = editable.rename(columns={mkt_ask_col: 'mkt_ask', 'fee_pct': 'fee %'})
                    already_traded = editable['k_ticker'].isin(_pending_tickers)
                    # Opposite leg of a 2-way event we already hold (YES A ≡ NO B) — same
                    # bet on another ticker; run_all_signals() skips these too.
                    _opp_leg = editable['k_ticker'].isin(
                        opposite_leg_blocked(editable['k_ticker'].tolist(), _pending_tickers)
                    ) & ~already_traded
                    editable.insert(1, 'Status', already_traded.map({True: 'pending', False: ''})
                                    .mask(_opp_leg, 'opp. leg held'))
                    already_traded = already_traded | _opp_leg

                    _override = st.session_state.pop(f'_signals_execute_override_{side}', None)
                    default_execute = (~already_traded) if _override is None else pd.Series(_override, index=editable.index)
                    editable.insert(0, 'Execute', default_execute)

                    locked_cols = [c for c in editable.columns if c not in ('Execute', 'contracts')]
                    edited = st.data_editor(
                        editable,
                        width="stretch",
                        hide_index=True,
                        disabled=locked_cols,
                        column_config={
                            'Execute':   st.column_config.CheckboxColumn('Execute', default=True),
                            'contracts': st.column_config.NumberColumn(
                                'contracts', min_value=1, step=1, format='%d',
                                help='Auto-sized via Kelly × Size× — double-click to type your own '
                                     'count. Whatever is here at Execute time is sent to Kalshi as-is.'),
                            'edge':      st.column_config.NumberColumn('edge', format='%+.4f',
                                                                        help='Raw mispricing: fair_prob - price (pre-fee)'),
                            'ev':        st.column_config.NumberColumn('ev', format='%+.4f',
                                                                        help='Fee-adjusted expected value per contract — what actually gates the signal'),
                            'fee %':     st.column_config.NumberColumn('fee %', format='%.3f%%'),
                        },
                        key=f'signals_editor_{side}_{st.session_state[_ver_key]}',
                    )

                    approved_mask    = edited['Execute'].values
                    approved_signals = signals.iloc[approved_mask].copy()
                    if 'contracts' in edited.columns:
                        approved_signals['contracts_override'] = edited['contracts'].values[approved_mask]
                    n_approved       = int(approved_mask.sum())

                    # limit_only is a YES-only option — NO always rests
                    _opt_c1, _opt_c2 = st.columns([3, 1])
                    with _opt_c1:
                        limit_only = limit_only_mode
                        if force_cross:
                            st.caption(f'{n_approved} {side.upper()} signal(s) — **CROSS** at ask '
                                       f'(taker fee — varies per series, ~{TAKER_FEE_BASE*100:.2g}% general rate)')
                        elif limit_only:
                            st.caption(f'{n_approved} {side.upper()} signal(s) — **REST** at top of book '
                                       f'(maker fee — varies per series, ~{MAKER_FEE_BASE*100:.2g}% general rate, '
                                       f'often $0 — see "fee %" column)')
                        else:
                            st.caption(f'{n_approved} {side.upper()} signal(s) — **AUTO**: cross if taker EV positive, else rest')
                    with _opt_c2:
                        trade_ttl_min = st.number_input(
                            'Order TTL (min)', min_value=1, max_value=1440, value=30,
                            key='trade_ttl_min',
                            help='Cancel unfilled orders after this many minutes'
                        )

                    # Explicit, content-independent key: this button's label includes
                    # n_approved, which changes on every checkbox edit (including Select
                    # all/Deselect all). Without a stable key, Streamlit's auto-derived
                    # widget identity (based partly on label text) changes along with it,
                    # so a click can fail to register as THIS button's click on the rerun
                    # that processes it — the click appears to do nothing while the table
                    # still redraws, looking like everything just got deselected.
                    btn_label = f'Execute {n_approved} {side.upper()} Signal(s)'
                    if st.button(btn_label, type='primary', disabled=(n_approved == 0),
                                key=f'_execute_btn_{side}'):
                        if balance is None:
                            st.error('Refresh your Kalshi balance in the sidebar before trading.')
                        else:
                            # Read fresh from session_state rather than the closure
                            # `used`/`remaining` (captured from the sidebar at the last
                            # FULL script run) — a fragment-scoped rerun (see
                            # _fragment_rerun()) never re-executes the sidebar, so that
                            # closure can be a stale pre-fetch snapshot if Fetch Signals
                            # updated the API usage earlier in this same fragment session.
                            _cur_used      = st.session_state.get('api_used', used)
                            _cur_remaining = st.session_state.get('api_remaining', remaining)
                            # Fresh dashboard + shared results holder
                            dash           = StreamlitDashboard(api_limit=_cur_used + _cur_remaining)
                            dash.set_api_usage(_cur_used, _cur_remaining)
                            results_holder: list = []
                            stop_event     = threading.Event()
                            limit_only     = limit_only_mode

                            def _worker(approved=approved_signals.copy(),
                                        bk=balance, tf=taker_fee, mf=maker_fee,
                                        lo=limit_only, fc=force_cross, s=side,
                                        ttl=int(trade_ttl_min) * 60,
                                        sm=float(size_mult),
                                        d=dash, rh=results_holder, se=stop_event):
                                try:
                                    out = run_all_signals(
                                        approved, bankroll=bk,
                                        taker_fee=tf, maker_fee=mf,
                                        limit_only=lo, force_cross=fc, side=s,
                                        max_duration=ttl,
                                        size_mult=sm,
                                        dashboard=d, stop_event=se,
                                    )
                                    rh.extend(out or [])
                                except Exception as exc:
                                    log.exception('Trade worker thread failed')
                                    rh.append({'status': 'error', 'ticker': '',
                                               'outcome': '', 'reason': str(exc),
                                               'order_id': None, 'contracts': 0})

                            t = threading.Thread(target=_worker, daemon=True)
                            t.start()
                            st.session_state[thread_key]          = t
                            st.session_state['_trade_dash']       = dash
                            st.session_state['_trade_results_h']  = results_holder
                            st.session_state['_trade_stop']       = stop_event
                            st.session_state['_trade_side']       = side
                            st.session_state['_trade_start_time'] = time.time()
                            st.session_state['_trade_ttl_sec']    = int(trade_ttl_min) * 60
                            _mlab = 'CROSS' if force_cross else ('REST' if limit_only else 'AUTO')
                            st.session_state['_trade_mode'] = _mlab
                            _fragment_rerun()
                else:
                    st.info(f'No {side.upper()} signals at current fees / threshold.')

            # ── Live dashboard — always at a fixed position in the tree ────────────
            if _trading_active:
                thread        = st.session_state[thread_key]
                dash          = st.session_state.get('_trade_dash')
                results_h     = st.session_state.get('_trade_results_h', [])
                stop_event    = st.session_state.get('_trade_stop')
                last_side     = st.session_state.get('_trade_side', 'yes')
                last_mode     = st.session_state.get('_trade_mode', 'REST')

                is_alive = thread.is_alive()

                @st.fragment(run_every='1s' if is_alive else None)
                def _live_panel():
                    if dash is None:
                        return
                    snap = dash.snapshot()

                    # ── Time progress bar ───────────────────────────────────────
                    _extra_sec = snap.get('extra_seconds', 0)
                    _start   = st.session_state.get('_trade_start_time', time.time())
                    _ttl     = st.session_state.get('_trade_ttl_sec', 1800) + _extra_sec
                    _elapsed = time.time() - _start
                    _rem     = max(_ttl - _elapsed, 0)
                    _pct     = min(_elapsed / max(_ttl, 1), 1.0)
                    _m, _s   = int(_rem // 60), int(_rem % 60)
                    _extend_note = f'  (+{_extra_sec // 60}m extended)' if _extra_sec else ''
                    st.progress(_pct, text=f'{_m}m {_s:02d}s remaining{_extend_note}')

                    # Header + cancel + cross&cancel + keep-rest + extend buttons
                    #
                    # This whole panel is inside @st.fragment(run_every='1s'). Any output
                    # written directly under a button's `if` only lives for that one script
                    # run — about a second later the timer reruns the fragment, the button
                    # is no longer "clicked", and that output disappears while the table
                    # shifts to fill the gap. To keep results from flickering away under the
                    # user, action results are stashed in session_state and rendered
                    # unconditionally below, so they survive every subsequent auto-refresh
                    # until the next action replaces them.
                    hc1, hc_btns = st.columns([4, 4])
                    hc1.markdown('#### Dashboard')
                    if thread.is_alive():
                        with hc_btns:
                            with st.container(key='_btnrow_dashboard', horizontal=True,
                                              horizontal_alignment='right', gap='medium'):
                                if st.button('Cancel all', key='_cancel_all_btn'):
                                    st.session_state['_fast_positions_until'] = time.time() + 45   # watch rows clear as each order cancels
                                    if stop_event:
                                        stop_event.set()
                                    st.session_state['_dash_last_action'] = {
                                        'type': 'cancel_all',
                                        'ts':   datetime.now().strftime('%H:%M:%S'),
                                    }
                                if st.button('↑ Cross & Cancel', key='_cross_cancel_btn',
                                              help='Re-check each resting order against Pinnacle fair value. '
                                                   'Crosses if EV still positive at current ask, cancels if not.'):
                                    st.session_state['_fast_positions_until'] = time.time() + 45
                                    _snap2    = dash.snapshot()
                                    _pos_now  = _snap2['positions']
                                    _RESTING  = {'resting', 'open', 'pending', 'unknown'}
                                    _targets  = [(oid, pos) for oid, pos in _pos_now.items()
                                                 if pos.get('status') in _RESTING]
                                    if not _targets:
                                        st.session_state['_dash_last_action'] = {
                                            'type': 'cross_cancel_empty',
                                            'ts':   datetime.now().strftime('%H:%M:%S'),
                                        }
                                    else:
                                        _xc_results = []
                                        for _oid, _pos in _targets:
                                            _r = cross_and_cancel_order(
                                                _pos['ticker'], _oid,
                                                _pos.get('contracts', 1), taker_fee, side,
                                                event_id=_pos.get('event_id', ''),
                                                sport=_pos.get('sport', ''),
                                                outcome=_pos.get('raw_outcome', ''),
                                            )
                                            _xc_results.append(_r)
                                        _n_errors = sum(1 for r in _xc_results if r['action'] == 'error')
                                        if _n_errors:
                                            log.warning('Cross & Cancel: %d error(s) — %s', _n_errors,
                                                       [r for r in _xc_results if r['action'] == 'error'])
                                        st.session_state['_dash_last_action'] = {
                                            'type':    'cross_cancel',
                                            'ts':      datetime.now().strftime('%H:%M:%S'),
                                            'results': _xc_results,
                                        }
                                if st.button('Keep Rest', key='_keep_rest_btn',
                                              help='Re-rest each resting order until 30 min before game start, '
                                                   'then stop Pinnacle polling to save API credits.'):
                                    _kr_snap    = dash.snapshot()
                                    _kr_pos_now = _kr_snap['positions']
                                    _KR_RESTING = {'resting', 'open', 'pending', 'unknown'}
                                    _kr_targets = [(oid, pos) for oid, pos in _kr_pos_now.items()
                                                   if pos.get('status') in _KR_RESTING
                                                   and pos.get('contracts', 0) - pos.get('filled', 0) > 0]
                                    if not _kr_targets:
                                        st.session_state['_dash_last_action'] = {
                                            'type': 'keep_rest_empty',
                                            'ts':   datetime.now().strftime('%H:%M:%S'),
                                        }
                                    else:
                                        _kr_results = []
                                        for _oid, _pos in _kr_targets:
                                            _r = cancel_and_rerest(
                                                _pos['ticker'], _oid,
                                                _pos.get('contracts', 1) - _pos.get('filled', 0),
                                                _pos['entry_price'],
                                                _pos.get('side', 'yes'),
                                                _pos.get('commence', ''),
                                            )
                                            _kr_results.append((_pos['ticker'], _r))
                                        if stop_event:
                                            stop_event.set()
                                        _kr_err = sum(1 for _, r in _kr_results if r['action'] == 'error')
                                        if _kr_err:
                                            log.warning('Keep Rest: %d error(s) — %s', _kr_err,
                                                       [r for _, r in _kr_results if r['action'] == 'error'])
                                        st.session_state['_dash_last_action'] = {
                                            'type':    'keep_rest',
                                            'ts':      datetime.now().strftime('%H:%M:%S'),
                                            'results': _kr_results,
                                        }
                                if st.button('+15 min', key='_extend_time_btn',
                                              help='Push back the auto-cancel deadline for every resting '
                                                   'order in this session by 15 minutes.'):
                                    dash.extend_time(15 * 60)
                                    st.session_state['_dash_last_action'] = {
                                        'type':    'extend',
                                        'ts':      datetime.now().strftime('%H:%M:%S'),
                                        'minutes': 15,
                                    }

                    # Render whatever the last action was — persists across the 1s
                    # auto-refresh instead of disappearing after one script run.
                    _last_action = st.session_state.get('_dash_last_action')
                    if _last_action:
                        _atype, _ats = _last_action['type'], _last_action['ts']
                        if _atype == 'cancel_all':
                            st.warning(f'[{_ats}] Cancellation requested — monitors will close orders.')
                        elif _atype == 'cross_cancel_empty':
                            st.info(f'[{_ats}] No resting orders to process.')
                        elif _atype == 'keep_rest_empty':
                            st.info(f'[{_ats}] No unfilled resting orders to keep.')
                        elif _atype == 'extend':
                            st.success(f"[{_ats}] Extended by {_last_action['minutes']}m — "
                                       f"total extension now +{_extra_sec // 60}m")
                        elif _atype == 'cross_cancel':
                            _xc_results = _last_action['results']
                            _n_crossed  = sum(1 for r in _xc_results if r['action'] == 'crossed')
                            _n_canceled = sum(1 for r in _xc_results if r['action'] == 'canceled')
                            _n_errors   = sum(1 for r in _xc_results if r['action'] == 'error')
                            with st.expander(
                                f'[{_ats}] ↑ {_n_crossed} crossed · {_n_canceled} canceled · {_n_errors} errors',
                                expanded=True,
                            ):
                                for _r in _xc_results:
                                    if _r['action'] == 'crossed':
                                        st.success(f"{_r['ticker']}  {_r.get('contracts')}ct  "
                                                   f"ask={_r['ask']:.2f}  ev={_r['ev']:+.4f}")
                                    elif _r['action'] == 'canceled':
                                        st.warning(f"{_r['ticker']}  {_r.get('reason', 'canceled')}")
                                    else:
                                        st.error(f"{_r['ticker']}  {_r.get('reason', 'error')}")
                        elif _atype == 'keep_rest':
                            _kr_results = _last_action['results']
                            _kr_ok  = sum(1 for _, r in _kr_results if r['action'] == 'rested')
                            _kr_err = sum(1 for _, r in _kr_results if r['action'] == 'error')
                            with st.expander(
                                f'[{_ats}] {_kr_ok} re-rested · {_kr_err} errors — Pinnacle polling stopped',
                                expanded=True,
                            ):
                                for _tkr, _r in _kr_results:
                                    if _r['action'] == 'rested':
                                        st.success(f"{_tkr}  {_r['price_cents']}¢  GTC {_r['expiry']}")
                                    elif _r['action'] == 'skipped':
                                        st.caption(f"— {_tkr}  {_r.get('reason', 'skipped')}")
                                    else:
                                        st.error(f"{_tkr}  {_r.get('reason', 'error')}")


                    # API bar
                    try:
                        au, al = snap['api_used'], snap['api_limit']
                        ar = max(al - au, 0)
                        pct = min(max(au / max(al, 1), 0.0), 1.0)
                        st.progress(pct, text=f'API  {au} / {al} used  ({ar} remaining)')
                    except Exception:
                        log.exception('Live dashboard: API bar render failed')

                    if not thread.is_alive():
                        st.success('Trade session complete.')

                # A separate, slower-cadence nested fragment — not the outer 1s one.
                # st.dataframe's widget identity is a hash of its own data (see the
                # click-to-inspect comment below); the instant ANY cell changes it's
                # treated as a brand-new grid and remounts, resetting scroll position.
                # Checking every 1s bought nothing — the background monitor thread
                # backing this table only polls Kalshi every KALSHI_POLL (10s) — so
                # 9 of 10 checks were pure remount risk for zero possible new data.
                # 10s matches the real freshness ceiling exactly: no wasted reruns,
                # and roughly a 10x cut in how often scrolling gets interrupted. The
                # countdown timer above stays on the outer 1s cadence since a plain
                # st.progress isn't a widget with state to preserve — it updates via
                # normal prop diffing, no remount risk, so it can afford to be smooth.
                @st.fragment(run_every=('2s' if time.time() < st.session_state.get('_fast_positions_until', 0) else '10s') if is_alive else None)
                def _positions_panel():
                    if dash is None:
                        return
                    snap = dash.snapshot()

                    # Positions table — wrapped: one malformed row must not repeatedly
                    # break the whole fragment.
                    try:
                        positions = snap['positions']
                        if positions:
                            # Sorted by ticker — a stable order independent of dict
                            # insertion history, so row index N reliably maps to the
                            # same ticker across the reruns this whole panel does
                            # (needed for the click-to-inspect selection below; dict
                            # iteration order could otherwise shift if a position drops
                            # out mid-session).
                            ordered = sorted(positions.values(), key=lambda p: p['ticker'])
                            rows = []
                            for pos in ordered:
                                cts    = pos['contracts']
                                filled = pos.get('filled', 0)
                                if filled == cts and cts > 0:
                                    disp_status = 'executed'
                                elif 0 < filled < cts:
                                    disp_status = 'partial'
                                else:
                                    disp_status = pos['status']
                                avg_fp = pos.get('avg_fill_price')
                                rows.append({
                                    'Ticker':    pos['ticker'],
                                    'Outcome':   pos['outcome'],
                                    'Contracts': cts,
                                    'Filled':    filled,
                                    # Must always be a str, never a bare int — a column that mixes
                                    # int and str('—') across rows is `object`-dtype with mixed
                                    # Python types, which pyarrow can't serialize (crashes the
                                    # whole live panel, not just this column). Same fix already
                                    # applied to 'Mkt Ask ¢' below.
                                    'Avg Fill ¢': f"{round(avg_fp * 100)}" if avg_fp is not None else '—',
                                    'Entry ¢':   pos['entry_price'],
                                    'Mkt Ask ¢': f"{pos['market_ask']}" if pos['market_ask'] is not None else '—',
                                    'Fair entry':f"{pos['fair_entry']:.3f}",
                                    'Fair last': f"{pos['fair_last']:.3f}",
                                    'Edge':      f"{pos['edge_last']:+.3f}",
                                    'Status':    disp_status,
                                })
                            n = len(rows)

                            # Red = unfilled and still resting, yellow = partial, green = fully filled;
                            # an order that has been cancelled / expired loses its highlight.
                            def _fill_color(row):
                                cts_, filled_ = row['Contracts'], row['Filled']
                                if row['Status'] in ('canceled', 'expired') and filled_ <= 0:
                                    return [''] * len(row)
                                if cts_ > 0 and filled_ >= cts_:
                                    bg = 'rgba(46, 204, 113, 0.25)'    # green
                                elif filled_ > 0:
                                    bg = 'rgba(241, 196, 15, 0.25)'    # yellow
                                else:
                                    bg = 'rgba(231, 76, 60, 0.25)'     # red
                                return [f'background-color: {bg}'] * len(row)

                            positions_df = pd.DataFrame(rows)
                            styled = positions_df.style.apply(_fill_color, axis=1)
                            st.dataframe(styled, width="stretch", hide_index=True,
                                        height=min(35 * n + 38, 600))

                            # ── Click-to-inspect ─────────────────────────────────────────────
                            # A row click on the st.dataframe above (on_select='rerun') was
                            # tried here first, but it's unreliable inside a fragment that
                            # auto-refreshes every 10s: st.dataframe's widget identity is a hash
                            # that includes the data bytes themselves (see
                            # compute_and_register_element_id(..., data=proto.data, ...) in
                            # streamlit/elements/arrow.py), and live price ticks (Mkt Ask ¢,
                            # Fair last) change that identity on nearly every render — the same
                            # kalshi_poll cadence (10s) that drives this fragment's own timer.
                            # A click can race the fragment's own next scheduled auto-tick: the
                            # server can already be pushing a fresh, unselected widget instance
                            # right as the click's selection event arrives, so the detail panel
                            # flashes or never appears — confirmed in production logs as the
                            # cause of "select a row, no detail shows" reports. A plain
                            # st.selectbox doesn't have this problem: its identity is its `key`
                            # alone, not a hash of its options, so it survives every remount of
                            # the table above it.
                            if st.session_state.pop('_inspect_reset', False):
                                st.session_state['_inspect_select'] = None

                            _tkr_options = [p['ticker'] for p in ordered]
                            _tkr_labels  = {p['ticker']: f"{p['ticker']} — {p['outcome']}"
                                           for p in ordered}
                            _prior_tkr    = st.session_state.get('_inspect_ticker')
                            _default_idx  = (_tkr_options.index(_prior_tkr) + 1
                                            if _prior_tkr in _tkr_options else 0)
                            _tkr = st.selectbox(
                                'Inspect a position', options=[None] + _tkr_options,
                                format_func=lambda t: '— select a position —' if t is None
                                                      else _tkr_labels[t],
                                index=_default_idx, key='_inspect_select',
                            )
                            st.session_state['_inspect_ticker'] = _tkr

                            if _tkr and _tkr in {p['ticker'] for p in ordered}:
                                _pos = next(p for p in ordered if p['ticker'] == _tkr)

                                # Depth is a fresh live API call — throttled to once per 3s
                                # per selected ticker regardless of what triggered this
                                # redraw (the panel's own 10s cadence, or an out-of-cycle
                                # rerun from clicking ✕/Resize), so a burst of interactions
                                # can't spam the endpoint.
                                _depth_key = f'_orderbook_depth_{_tkr}'
                                _cached    = st.session_state.get(_depth_key)
                                _stale     = (_cached is None or
                                             time.time() - _cached.get('_fetched_at', 0) > 3)
                                if _stale:
                                    _depth = get_orderbook_depth(_tkr, levels=2)
                                    _depth['_fetched_at'] = time.time()
                                    st.session_state[_depth_key] = _depth
                                else:
                                    _depth = _cached

                                with st.container(border=True):
                                    _hdr_col, _x_col = st.columns([10, 1])
                                    _hdr_col.markdown(f'#### {_tkr} — {_pos["outcome"]}')
                                    if _x_col.button('✕', key='_close_inspect', help='Close'):
                                        st.session_state['_inspect_ticker'] = None
                                        # Can't overwrite '_inspect_select' here directly — it
                                        # was already instantiated earlier in this same run.
                                        # Flag it and clear on the next run's first line instead.
                                        st.session_state['_inspect_reset'] = True
                                        # Header above was already drawn this pass with the old
                                        # ticker — force a fresh run now instead of letting it
                                        # linger until this fragment's next 10s tick (same fix
                                        # as the calibration-chart detail panel's close button).
                                        _fragment_rerun()

                                if _tkr:
                                    with st.container(border=True):
                                        _sum_col, _book_col = st.columns([1, 1])

                                        with _sum_col:
                                            st.markdown('**Order summary**')
                                            _avg = _pos.get('avg_fill_price')
                                            st.markdown(
                                                f"- Side: **{_pos['side'].upper()}**\n"
                                                f"- Contracts: **{_pos['contracts']}**  "
                                                f"(filled **{_pos.get('filled', 0)}**)\n"
                                                f"- Entry: **{_pos['entry_price']}¢**\n"
                                                f"- Avg fill: **{f'{_avg*100:.1f}¢' if _avg is not None else '—'}**\n"
                                                f"- Fair (entry → last): **{_pos['fair_entry']:.3f} → "
                                                f"{_pos['fair_last']:.3f}**\n"
                                                f"- Edge: **{_pos['edge_last']:+.3f}**\n"
                                                f"- Status: **{_pos['status']}**\n"
                                                f"- Sport: {_pos.get('sport', '—')}\n"
                                                f"- Last ping: {_pos['last_ping']}"
                                            )

                                            # Live resize — only while there's still an
                                            # unfilled remainder resting on Kalshi. Cancels
                                            # the current resting order and re-places for
                                            # the new total (new_total - already_filled) at
                                            # the same price; see resize_resting_order().
                                            _filled_now    = _pos.get('filled', 0)
                                            _still_resting = _pos['status'] not in (
                                                'executed', 'filled', 'canceled', 'expired')
                                            if _still_resting and _pos.get('order_id'):
                                                st.markdown('**Resize this order**')
                                                _rz_c1, _rz_c2 = st.columns([2, 1])
                                                _new_size = _rz_c1.number_input(
                                                    'New total contracts',
                                                    min_value=_filled_now + 1,
                                                    value=max(_pos['contracts'], _filled_now + 1),
                                                    step=1, key=f'_resize_input_{_tkr}',
                                                    help=f'{_filled_now} already filled — resize '
                                                         'applies to the remaining unfilled portion.',
                                                )
                                                if _rz_c2.button('Resize', key=f'_resize_btn_{_tkr}'):
                                                    with st.spinner('Resizing...'):
                                                        _rz = resize_resting_order(
                                                            _tkr, _pos['order_id'], _pos['side'],
                                                            int(_new_size), _pos['entry_price'],
                                                        )
                                                    if _rz['action'] == 'resized':
                                                        # resize_resting_order() only cancels +
                                                        # re-places — it returns immediately and
                                                        # doesn't know about the dashboard or
                                                        # monitoring. Without picking that back up
                                                        # here, the table would never reflect the
                                                        # new order (still shows the just-canceled
                                                        # old one until its own monitor thread
                                                        # notices and exits), it'd get no
                                                        # auto-cancel-before-event protection, and
                                                        # it would never get logged when it
                                                        # eventually resolves. Runs in its own
                                                        # thread since _monitor() blocks until the
                                                        # order closes — can't do that on the UI
                                                        # thread.
                                                        _rz_price     = _pos['entry_price'] / 100
                                                        _rz_fee_rate  = _pos.get('fee_rate', MAKER_FEE_BASE)
                                                        _rz_fair      = _pos.get(
                                                            'fair_last', _pos.get('fair_entry', 0.5))
                                                        # Fee-adjusted EV (same formula ev_total in the
                                                        # logs is built from), not edge_last — edge is
                                                        # the raw pre-fee mispricing and would overstate
                                                        # this trade's Projected EV in the Review chart.
                                                        _rz_ev = _ev(_rz_fair, _rz_price, _rz_fee_rate)
                                                        threading.Thread(
                                                            target=resume_monitoring,
                                                            kwargs=dict(
                                                                order_id=_rz['new_order_id'],
                                                                ticker=_tkr,
                                                                event_id=_pos.get('event_id', ''),
                                                                sport=_pos.get('sport', ''),
                                                                outcome=_pos.get('raw_outcome',
                                                                                 _pos['outcome']),
                                                                order_price=_rz_price,
                                                                fee_rate=_rz_fee_rate,
                                                                commence=_pos.get('commence', ''),
                                                                side=_pos['side'],
                                                                contracts=_rz['new_remaining'],
                                                                fair_prob=_rz_fair,
                                                                ev_per_contract=_rz_ev,
                                                                dashboard=dash,
                                                                stop_event=stop_event,
                                                            ),
                                                            daemon=True,
                                                        ).start()
                                                        st.success(
                                                            f"Resized to {_rz['new_total']} total "
                                                            f"({_rz['already_filled']} filled + "
                                                            f"{_rz['new_remaining']} now resting) — "
                                                            "monitoring resumed under the new order.")
                                                    else:
                                                        st.error(f"Resize failed: "
                                                                f"{_rz.get('reason', 'unknown error')}")
                                            elif _pos.get('order_id') is None:
                                                st.caption('No order_id on this position — placed '
                                                          'before this feature existed; can\'t resize.')
                                            else:
                                                st.caption('Order is no longer resting — nothing to resize.')

                                        with _book_col:
                                            st.markdown('**Market depth** (YES side, top 2 levels)')
                                            if not _depth or (not _depth.get('bids') and not _depth.get('asks')):
                                                st.caption('Depth unavailable right now.')
                                            else:
                                                _lp = _depth.get('last_price_cents')
                                                if _lp is not None:
                                                    st.markdown(f"<div style='text-align:right'>Last: "
                                                               f"<b>{_lp}¢</b></div>", unsafe_allow_html=True)
                                                _book_rows = []
                                                for lv in reversed(_depth.get('asks', [])):
                                                    _book_rows.append({'Side': 'Ask', 'Price ¢': lv['price_cents'],
                                                                       'Qty': lv['qty'], '$ Notional': lv['dollars']})
                                                for lv in _depth.get('bids', []):
                                                    _book_rows.append({'Side': 'Bid', 'Price ¢': lv['price_cents'],
                                                                       'Qty': lv['qty'], '$ Notional': lv['dollars']})
                                                if _book_rows:
                                                    _book_df  = pd.DataFrame(_book_rows)
                                                    _ask_mask = _book_df['Side'] == 'Ask'
                                                    _bid_mask = ~_ask_mask
                                                    _vmax     = _book_df['$ Notional'].max()

                                                    def _side_text_color(row):
                                                        c = '#e74c3c' if row['Side'] == 'Ask' else '#2ecc71'
                                                        return [f'color: {c}; font-weight: 600'] * len(row)

                                                    # .bar() draws the depth-size bar as a background
                                                    # gradient behind the $ value, red for asks / green
                                                    # for bids, scaled to the deepest level shown — the
                                                    # same visual language as Kalshi's own book.
                                                    _book_styled = (
                                                        _book_df.style
                                                        .apply(_side_text_color, axis=1)
                                                        .bar(subset=pd.IndexSlice[_book_df.index[_ask_mask], ['$ Notional']],
                                                            color='rgba(231, 76, 60, 0.35)', vmin=0, vmax=_vmax, align='left')
                                                        .bar(subset=pd.IndexSlice[_book_df.index[_bid_mask], ['$ Notional']],
                                                            color='rgba(46, 204, 113, 0.35)', vmin=0, vmax=_vmax, align='left')
                                                        .format({'Price ¢': '{:.0f}¢', 'Qty': '{:,.0f}',
                                                                '$ Notional': '${:,.2f}'})
                                                    )
                                                    st.dataframe(_book_styled, width="stretch", hide_index=True,
                                                                height=35 * len(_book_rows) + 38)
                                                else:
                                                    st.caption('No resting depth on either side.')
                        else:
                            st.caption('Waiting for orders to be placed...')
                    except Exception:
                        log.exception('Live dashboard: positions table render failed')
                        st.error('Dashboard table failed to render — see trade/logs/app.log. '
                                 'Trading continues in the background.')

                _live_panel()
                _positions_panel()

                # Cleanup once thread finishes, then rerun to restore the signals view
                if not is_alive:
                    final_results = list(results_h)
                    st.session_state['last_results'] = final_results
                    st.session_state['last_side']    = last_side
                    for k in (thread_key, '_trade_dash', '_trade_results_h',
                              '_trade_stop', '_trade_side', '_trade_mode'):
                        st.session_state.pop(k, None)
                    _fragment_rerun()

            # ── Final results (persist after dashboard clears) ──────────────────
            last_results = st.session_state.get('last_results')
            last_side    = st.session_state.get('last_side', 'yes')
            if last_results:
                st.markdown(f'#### Trade Results — {last_side.upper()}')
                results_df = pd.DataFrame([r for r in last_results if r is not None])
                if not results_df.empty:
                    price_key  = 'no_price' if last_side == 'no' else 'yes_price'
                    show_cols  = [c for c in ['ticker', 'outcome', 'order_type', 'status',
                                              'reason', 'contracts', price_key, 'ev']
                                  if c in results_df.columns]
                    st.dataframe(results_df[show_cols], width="stretch", hide_index=True)
                    if 'status' in results_df.columns:
                        errors = results_df[results_df['status'] == 'error']
                        for _, err in errors.iterrows():
                            st.error(f"{err.get('ticker')} — {err.get('reason')}")

            with st.expander('All matches'):
                all_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                        'fair_prob', 'yes_ask', 'no_ask', 'match_score',
                                        'signal', 'signal_no', 'k_ticker']
                            if c in matched_df.columns]
                st.dataframe(matched_df[all_cols], width="stretch", hide_index=True)

    _fetch_and_trade_panel()


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — REVIEW
# ════════════════════════════════════════════════════════════════════════════

with tab_review:

    @st.fragment(run_every='30s')
    def _review_panel():
        st.subheader('Trade Review')

        # ── Auto-settle ──────────────────────────────────────────────────────────
        # Checks Kalshi for newly-settled markets every time this tab loads, throttled
        # to once per 2 min via session_state so repeated reruns (e.g. switching tabs)
        # don't hammer the API. Reuses settle.py's functions as-is — refresh_statuses()
        # catches orders that filled after the bot exited and never wrote back;
        # _settle_file() then resolves any PENDING+filled row whose market has closed.
        _last_settle    = st.session_state.get('_last_settle_check', 0)
        _settle_stale   = (time.time() - _last_settle) > 120
        _rc1, _rc2      = st.columns([5, 1])
        _force_settle   = _rc2.button('Refresh now', key='_settle_refresh_now')
        if _settle_stale or _force_settle:
            try:
                n_refreshed = settle.refresh_statuses(LOG_PATH) + settle.refresh_statuses(NO_LOG_PATH)
                n_settled   = (settle._settle_file(LOG_PATH, side='yes', dry_run=False) +
                              settle._settle_file(NO_LOG_PATH, side='no', dry_run=False))
                st.session_state['_last_settle_check']  = time.time()
                st.session_state['_last_settle_result'] = (n_refreshed, n_settled)
            except Exception:
                log.exception('Auto-settle on Review load failed')
                st.session_state['_last_settle_result'] = None
        _settle_res = st.session_state.get('_last_settle_result')
        with _rc1:
            if _settle_res:
                _n_ref, _n_set = _settle_res
                st.caption(f'Checked Kalshi — refreshed {_n_ref} stale order status(es), '
                           f'settled {_n_set} newly-resolved trade(s).')
            else:
                st.caption('Auto-checks Kalshi for settlements every 2 min while this tab is open.')

        log_df2 = _load_all_logs()  # YES + NO combined, tagged with 'side'

        if log_df2.empty:
            st.info('No trade log found yet.')
        else:
            # Hide canceled / expired / resting rows AND anything closed via
            # user_canceled reason — these aren't real completed trades for stats.
            _reason = log_df2['close_reason'].fillna('') if 'close_reason' in log_df2.columns else ''
            filled  = log_df2[
                log_df2['final_status'].isin(['executed', 'filled']) &
                ~_reason.str.contains('canceled|cancelled|user_', case=False, regex=True)
            ]
            # CLOSED_EARLY = a position manually sold back on Kalshi before the market
            # settled (see settle.py's fetch_realized_pnl_from_fills) — a real, completed
            # outcome with a real PnL, so it belongs in the settled stats alongside
            # WIN/LOSS, not silently dropped as if still pending.
            settled = filled[filled['result'].isin(['WIN', 'LOSS', 'CLOSED_EARLY'])].copy()
            settled['actual_pnl'] = pd.to_numeric(settled['actual_pnl'], errors='coerce')
            # is_win is PnL-based, not label-based, so a profitable early close counts as
            # a win and a losing one counts as a loss — same rule WIN/LOSS already follow
            # by construction (compute_pnl() only labels WIN when pnl >= 0).
            settled['is_win'] = (settled['actual_pnl'] >= 0).astype(int)

            total_ev   = filled['ev_total'].sum()      if not filled.empty  else 0
            actual_pnl = settled['actual_pnl'].sum()   if not settled.empty else 0
            win_rate   = settled['is_win'].mean() if not settled.empty else None
            total_cost = filled['total_cost'].sum()    if not filled.empty  else 0

            # ── Summary metrics ──────────────────────────────────────────────────
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric('Total placed',  len(log_df2))
            c2.metric('Filled',        len(filled))
            c3.metric('Settled',       len(settled))
            c4.metric('Projected EV',  f'${total_ev:+.2f}')
            c5.metric('Actual PnL',    f'${actual_pnl:+.2f}',
                      delta_color='normal' if actual_pnl >= 0 else 'inverse')

            c6, c7 = st.columns(2)
            c6.metric('Win rate',      f'{win_rate:.0%}' if win_rate is not None else '—')
            c7.metric('Total wagered', f'${total_cost:.2f}')

            st.divider()

            # ── Full trade table ─────────────────────────────────────────────────
            st.markdown('#### All Trades')

            def _colour_result(val):
                if val == 'WIN':  return 'color: green; font-weight: bold'
                if val == 'LOSS': return 'color: red'
                return 'color: orange'

            # Filters combine (AND). Empty multiselect / "Any" / blank text = no filter on
            # that field, so e.g. Result=WIN + Settled=Today shows today's wins.
            def _opts(col):
                return sorted(log_df2[col].dropna().astype(str).unique()) if col in log_df2.columns else []

            _f1, _f2, _f3, _f4 = st.columns(4)
            _f_sport  = _f1.multiselect('Sport',      _opts('sport'),      key='_tf_sport')
            _f_result = _f2.multiselect('Result',     _opts('result'),     key='_tf_result')
            _f_side   = _f3.multiselect('Side',       _opts('side'),       key='_tf_side')
            _f_otype  = _f4.multiselect('Order type', _opts('order_type'), key='_tf_otype')
            _g1, _g2, _g3 = st.columns([1, 1.4, 1.6])
            _f_when = _g1.selectbox('Date settled',
                                    ['Any', 'Today', 'Last 7 days', 'Last 30 days', 'Custom range'],
                                    key='_tf_when',
                                    help='Uses the settle time Kalshi reported; older rows without one '
                                         'fall back to when the trade was placed.')
            _f_range = None
            if _f_when == 'Custom range':
                _f_range = _g2.date_input('Settled between', value=(), key='_tf_range')
            _f_team = _g3.text_input('Team / outcome contains', key='_tf_team',
                                     placeholder='e.g. Flamengo')

            view = log_df2.copy()
            if _f_sport:  view = view[view['sport'].astype(str).isin(_f_sport)]
            if _f_result: view = view[view['result'].astype(str).isin(_f_result)]
            if _f_side:   view = view[view['side'].astype(str).isin(_f_side)]
            if _f_otype:  view = view[view['order_type'].astype(str).isin(_f_otype)]
            if _f_team.strip() and 'outcome' in view.columns:
                view = view[view['outcome'].astype(str).str.contains(_f_team.strip(), case=False,
                                                                     regex=False, na=False)]
            if _f_when != 'Any':
                _placed  = pd.to_datetime(view['logged_at'], errors='coerce', utc=True)
                _settled = (pd.to_datetime(view['settled_at'], errors='coerce', utc=True)
                            if 'settled_at' in view.columns else pd.Series(pd.NaT, index=view.index))
                _when_dt = _settled.fillna(_placed).dt.tz_convert(None)
                _today   = pd.Timestamp.now(tz='UTC').tz_convert(None).normalize()
                view = view[view['result'].astype(str) != 'PENDING']  # unsettled rows have no settle date
                _when_dt = _when_dt.loc[view.index]
                if _f_when == 'Today':
                    view = view[_when_dt >= _today]
                elif _f_when == 'Last 7 days':
                    view = view[_when_dt >= _today - pd.Timedelta(days=6)]
                elif _f_when == 'Last 30 days':
                    view = view[_when_dt >= _today - pd.Timedelta(days=29)]
                elif _f_range and len(_f_range) == 2:
                    _lo, _hi = pd.Timestamp(_f_range[0]), pd.Timestamp(_f_range[1]) + pd.Timedelta(days=1)
                    view = view[(_when_dt >= _lo) & (_when_dt < _hi)]

            _n_pnl = pd.to_numeric(view['actual_pnl'], errors='coerce').sum() if 'actual_pnl' in view.columns else 0
            st.caption(f'{len(view)} of {len(log_df2)} trades · settled PnL in view ${_n_pnl:+.2f}')

            styled = view.style.map(_colour_result, subset=['result']) \
                               if 'result' in view.columns else view

            st.dataframe(styled, width="stretch", hide_index=True)

            # ── Live / unsettled trades ──────────────────────────────────────────
            # Orphan (never-filled) rows no longer land in these logs at all — see
            # trade/core/logging_io.py — so any real row with result=='PENDING' here
            # genuinely filled and is just waiting on its market to close.
            live_trades = filled[filled['result'] == 'PENDING'].copy() if not filled.empty else filled
            with st.expander(f'Live trades — {len(live_trades)} unsettled',
                             expanded=not live_trades.empty):
                if live_trades.empty:
                    st.caption('No trades currently awaiting settlement.')
                else:
                    _live_cols = [c for c in ['k_ticker', 'side', 'sport', 'outcome', 'commence',
                                              'order_type', 'contracts', 'entry_price', 'ev_total',
                                              'logged_at'] if c in live_trades.columns]
                    st.dataframe(live_trades[_live_cols].sort_values('logged_at', ascending=False),
                                width="stretch", hide_index=True)

            # ── Charts ───────────────────────────────────────────────────────────
            if not filled.empty:
                st.divider()
                st.markdown('#### Charts')

                try:
                    import plotly.graph_objects as go
                    import numpy as np

                    _NO_DATA = dict(showarrow=False, xref='paper', yref='paper', x=0.5, y=0.5,
                                    font=dict(color='gray'))
                    _CHART_LAYOUT = dict(height=340, margin=dict(t=40, b=30),
                                         xaxis=dict(hoverformat='.3f'), yaxis=dict(hoverformat='.3f'))

                    filled_sorted = filled.sort_values('logged_at').copy()

                    _SPORT_GROUPS = [
                        ('soccer', 'soccer'), ('basketball', 'basketball'), ('baseball', 'baseball'),
                        ('americanfootball', 'am. football'), ('rugby', 'rugby'), ('boxing', 'combat'),
                        ('mma', 'combat'), ('icehockey', 'hockey'), ('lacrosse', 'lacrosse'),
                        ('aussierules', 'aussie rules'), ('tennis', 'tennis'),
                    ]
                    def _sport_group(s):
                        s = str(s)
                        for key, label in _SPORT_GROUPS:
                            if key in s:
                                return label
                        return s

                    def _bar_click(event, name, axis):
                        """Remember a clicked bar. Plotly selections persist across reruns, so only act when the point changed."""
                        pts = event.selection.points if event and event.selection else []
                        val = pts[0].get(axis) if pts else None
                        if val is not None and val != st.session_state.get(f'_bar_last_{name}'):
                            st.session_state[f'_bar_last_{name}'] = val
                            st.session_state[f'_bar_sel_{name}'] = val

                    def _detail_panel(name, title, sub):
                        """Stats + trade history for the trades behind a clicked bar (same idea as the calibration panel)."""
                        with st.container(border=True):
                            _h, _x = st.columns([10, 1])
                            _h.markdown(f'#### {title}')
                            if _x.button('✕', key=f'_close_bar_{name}', help='Close'):
                                st.session_state[f'_bar_sel_{name}'] = None
                                _fragment_rerun()
                        _pnl = pd.to_numeric(sub['actual_pnl'], errors='coerce')
                        _done = sub[_pnl.notna() & sub['result'].isin(['WIN', 'LOSS', 'CLOSED_EARLY'])] \
                                if 'result' in sub.columns else sub.iloc[0:0]
                        _dp   = pd.to_numeric(_done['actual_pnl'], errors='coerce')
                        _ev   = pd.to_numeric(_done['ev_total'], errors='coerce').sum() if 'ev_total' in _done else 0
                        _fair = pd.to_numeric(_done['fair_prob'], errors='coerce').mean() if 'fair_prob' in _done else float('nan')
                        _wr   = (_dp >= 0).mean() if len(_done) else float('nan')
                        _wag  = pd.to_numeric(sub['total_cost'], errors='coerce').sum() if 'total_cost' in sub else 0
                        a1, a2, a3, a4 = st.columns(4)
                        a1.metric('Trades', f'{len(sub)}  ({len(_done)} settled)')
                        a2.metric('Win rate', f'{_wr:.0%}' if len(_done) else '—')
                        a3.metric('Total PnL', f'${_dp.sum():+.2f}',
                                  delta_color='normal' if _dp.sum() >= 0 else 'inverse')
                        a4.metric('Projected EV', f'${_ev:+.2f}')
                        b1, b2, b3, b4 = st.columns(4)
                        b1.metric('Luck (PnL − EV)', f'${_dp.sum() - _ev:+.2f}')
                        b2.metric('Avg edge', f"{pd.to_numeric(sub['edge'], errors='coerce').mean():+.3f}"
                                  if 'edge' in sub else '—')
                        b3.metric('Avg predicted', f'{_fair:.3f}' if _fair == _fair else '—')
                        b4.metric('Total wagered', f'${_wag:.2f}')
                        st.markdown('**Trade history**')
                        _cols = [c for c in ['logged_at', 'k_ticker', 'outcome', 'side', 'entry_price',
                                             'fair_prob', 'close_fair', 'clv', 'edge', 'contracts', 'order_type', 'result',
                                             'actual_pnl'] if c in sub.columns]
                        st.dataframe(sub[_cols].sort_values('logged_at', ascending=False),
                                     width='stretch', hide_index=True)

                    sg_src = pd.DataFrame()
                    eop_src = pd.DataFrame()
                    _c1, _c2 = st.columns(2)

                    # 1. Distribution of outcomes — SETTLED WIN/LOSS trades only. Replays the
                    # settled book many times, drawing each trade's win/lose from its own
                    # probability and summing the payoffs (same fee formula as settle.py):
                    #   • filled curve  = our model (fair_prob)     — what we expect
                    #   • dashed curve  = market price as the odds  — the zero-edge benchmark
                    # The realized total is then placed on the model curve. Assumes independent
                    # outcomes (correlated legs would fatten the real tails).
                    with _c1:
                        fig1 = go.Figure()
                        _dist = settled[settled['result'].isin(['WIN', 'LOSS'])].copy() \
                                if 'result' in settled.columns else settled.iloc[0:0]
                        _need = ['contracts', 'entry_price', 'fee_rate', 'fair_prob', 'actual_pnl']
                        if len(_dist) >= 8 and all(c in _dist.columns for c in _need):
                            _dist = _dist[_need].apply(pd.to_numeric, errors='coerce').dropna()
                            _ct, _pr = _dist['contracts'].values, _dist['entry_price'].values
                            _fp, _fee = _dist['fair_prob'].clip(0, 1).values, _dist['fee_rate'].values
                            _fpp  = _fee * _pr * (1 - _pr)
                            _winp = _ct * ((1 - _pr) - _fpp)
                            _losp = _ct * (-_pr - _fpp)
                            _rng  = np.random.default_rng(0)
                            _N    = 10000
                            def _sim(p_):
                                return np.where(_rng.random((_N, len(p_))) < p_, _winp, _losp).sum(axis=1)
                            _sim_m, _sim_k = _sim(_fp), _sim(_pr)
                            _real = float(_dist['actual_pnl'].sum())
                            _lo   = min(_sim_m.min(), _sim_k.min(), _real)
                            _hi   = max(_sim_m.max(), _sim_k.max(), _real)
                            _bins = np.linspace(_lo, _hi, 70)
                            _mid  = (_bins[:-1] + _bins[1:]) / 2
                            _w = _bins[1] - _bins[0]
                            def _smooth(d, sigma=2.0):
                                # Gaussian-kernel smoothing of the histogram (edge-normalised),
                                # then renormalised to sum to 1 so bin probabilities stay valid.
                                k  = np.arange(-int(4 * sigma), int(4 * sigma) + 1)
                                ker = np.exp(-0.5 * (k / sigma) ** 2); ker /= ker.sum()
                                sm = np.convolve(d, ker, 'same') / np.convolve(np.ones_like(d), ker, 'same')
                                return sm / sm.sum()          # probability mass per bin
                            _pm = _smooth(np.histogram(_sim_m, bins=_bins)[0].astype(float))
                            _pk = _smooth(np.histogram(_sim_k, bins=_bins)[0].astype(float))
                            _tip = ('EV $%{customdata[0]:.3f}<br>P(this outcome) %{customdata[1]:.3f}'
                                    '<extra>%{fullData.name}</extra>')
                            # Tooltip EV = PnL × probability of landing in that PnL bin.
                            fig1.add_trace(go.Scatter(x=_mid, y=_pm / _w, mode='lines', fill='tozeroy',
                                                      name='Model (fair_prob)',
                                                      line=dict(color='#3498db', width=2, shape='spline'),
                                                      fillcolor='rgba(52,152,219,0.25)',
                                                      customdata=np.stack([_mid * _pm, _pm], axis=-1),
                                                      hovertemplate=_tip))
                            fig1.add_trace(go.Scatter(x=_mid, y=_pk / _w, mode='lines',
                                                      name='Market (price = odds)',
                                                      line=dict(color='#888', width=2, dash='dash',
                                                                shape='spline'),
                                                      customdata=np.stack([_mid * _pk, _pk], axis=-1),
                                                      hovertemplate=_tip))
                            fig1.add_vline(x=_real, line_color='#f39c12', line_width=2.5)
                            fig1.add_vline(x=0, line_color='#888', line_width=1)
                            fig1.add_annotation(x=_real, y=1, yref='paper', text=f"Realized {'-' if _real < 0 else ''}${abs(_real):.3f}",
                                                showarrow=False, yanchor='bottom', font=dict(color='#f39c12'))
                            fig1.update_layout(legend=dict(orientation='h', yanchor='top', y=-0.22,
                                                           xanchor='center', x=0.5))
                        else:
                            fig1.add_annotation(text='Needs 8+ settled WIN/LOSS trades', **_NO_DATA)
                        fig1.update_layout(title='Distribution of Outcomes (settled trades)',
                                          xaxis_title='Total PnL ($)', yaxis_title='Density',
                                          yaxis_showticklabels=False,
                                          **{**_CHART_LAYOUT, 'margin': dict(t=40, b=70)})
                        st.plotly_chart(fig1, width='stretch', theme='streamlit')

                    # 2. Projected EV / Realized PnL / Luck
                    # Projected EV  = cumulative ev_total (edge at entry, from Pinnacle − Kalshi ask)
                    # Realized PnL  = cumulative actual_pnl for settled trades
                    # Luck          = Realized PnL − Projected EV (random outcome variance)
                    with _c2:
                        fig2 = go.Figure()
                        if not settled.empty:
                            # Sort by when the trade actually SETTLED, not when it was
                            # placed — settled_at (Kalshi's own settlement_ts, captured
                            # by settle.py) vs logged_at would otherwise mean a trade
                            # placed early for a far-future event gets plotted ahead of
                            # one placed later that resolved sooner, and a freshly-
                            # settled old trade can appear to insert itself into the
                            # MIDDLE of the sequence instead of at the end where you'd
                            # expect a new result to land. Falls back to logged_at only
                            # for rows settled before this column existed, or where
                            # Kalshi's markets endpoint no longer has the market (old
                            # markets eventually drop out of it).
                            _place_ts = pd.to_datetime(settled['logged_at'], errors='coerce', utc=True)
                            if 'settled_at' in settled.columns:
                                _settle_ts = pd.to_datetime(settled['settled_at'], errors='coerce', utc=True)
                            else:
                                _settle_ts = pd.Series(pd.NaT, index=settled.index)
                            settled_sorted = settled.copy()
                            settled_sorted['_sort_ts'] = _settle_ts.fillna(_place_ts)
                            settled_sorted = settled_sorted.sort_values('_sort_ts')
                            settled_sorted['luck'] = settled_sorted['actual_pnl'] - settled_sorted['ev_total']
                            _xs = list(range(len(settled_sorted)))
                            _cum_proj = settled_sorted['ev_total'].cumsum()
                            _cum_real = settled_sorted['actual_pnl'].cumsum()
                            _cum_luck = settled_sorted['luck'].cumsum()
                            # Luck vs. a ±2σ noise band. Each trade is a win/lose bet, so its
                            # payoff variance is contracts² · p · (1−p) (p = our-side fair_prob);
                            # summed over trades that's how big a gap luck ALONE would produce.
                            # Inside the band = variance; outside = the model is off.
                            _p   = pd.to_numeric(settled_sorted['fair_prob'], errors='coerce').clip(0, 1)
                            _ct  = pd.to_numeric(settled_sorted['contracts'], errors='coerce')
                            _cum_sd = np.sqrt((_ct ** 2 * _p * (1 - _p)).fillna(0).cumsum())
                            _band = 2 * _cum_sd
                            _z = (_cum_luck.iloc[-1] / _cum_sd.iloc[-1]) if _cum_sd.iloc[-1] > 0 else float('nan')
                            fig2.add_trace(go.Scatter(x=_xs, y=_band, mode='lines', line=dict(width=0),
                                                      hoverinfo='skip', showlegend=False))
                            fig2.add_trace(go.Scatter(x=_xs, y=-_band, mode='lines', line=dict(width=0),
                                                      fill='tonexty', fillcolor='rgba(136,136,136,0.18)',
                                                      name='±2σ noise band', hoverinfo='skip'))
                            fig2.add_trace(go.Scatter(x=_xs, y=_cum_luck, mode='lines',
                                                      name=f'Luck (realized − projected), now {_z:+.1f}σ',
                                                      line=dict(color='#f39c12', width=2.5)))
                            fig2.add_trace(go.Scatter(x=_xs, y=_cum_proj, mode='lines+markers',
                                                      name='Projected EV',
                                                      line=dict(color='#3498db', width=2),
                                                      marker=dict(size=5)))
                            fig2.add_trace(go.Scatter(x=_xs, y=_cum_real, mode='lines+markers',
                                                      name='Realized PnL',
                                                      line=dict(color='#9b59b6', width=2),
                                                      marker=dict(size=5)))
                            fig2.add_hline(y=0, line_color='#888', line_width=1)
                            fig2.update_layout(legend=dict(orientation='h', yanchor='top', y=-0.22,
                                                          xanchor='center', x=0.5))
                        else:
                            fig2.add_annotation(text='No settled trades yet', **_NO_DATA)
                        fig2.update_layout(title='Projected EV vs Realized PnL, Luck vs ±2σ',
                                          xaxis_title='Settled trade #', yaxis_title='$',
                                          **{**_CHART_LAYOUT, 'margin': dict(t=40, b=70)})
                        st.plotly_chart(fig2, width='stretch', theme='streamlit')

                    _c3, _c4 = st.columns(2)

                    # 3. Closing line value by order type — did we get a better price than
                    # Pinnacle's final (closing) fair probability? CLV = close_fair − entry
                    # price for OUR side, captured by trade/clv.py in the minutes before the
                    # start. Much lower-noise than win rate; available as soon as the event
                    # starts, before it settles. Only trades placed since capture began have it.
                    clv_src = pd.DataFrame()
                    with _c3:
                        fig3 = go.Figure()
                        _cl = load_closing_lines()
                        if not _cl.empty and 'order_id' in filled.columns:
                            clv_src = filled.merge(_cl[['order_id', 'close_fair']], on='order_id', how='inner')
                            clv_src['close_fair'] = pd.to_numeric(clv_src['close_fair'], errors='coerce')
                            clv_src['entry_price'] = pd.to_numeric(clv_src['entry_price'], errors='coerce')
                            clv_src['clv'] = clv_src['close_fair'] - clv_src['entry_price']
                            clv_src = clv_src.dropna(subset=['clv'])
                        if not clv_src.empty:
                            _LABEL_MAP = {'no_rest': 'rest', 'no_cross': 'cross'}
                            clv_src['order_type'] = clv_src['order_type'].map(lambda v: _LABEL_MAP.get(v, v))
                            _groups = [('all', clv_src)] + [(t, g) for t, g in clv_src.groupby('order_type')]
                            _names = [n for n, _ in _groups]
                            _means = [g['clv'].mean() * 100 for _, g in _groups]
                            _txt = [f"{m:+.3f}¢<br>{(g['clv'] > 0).mean():.0%} beat close · n={len(g)}"
                                    for m, (_, g) in zip(_means, _groups)]
                            fig3.add_trace(go.Bar(x=_names, y=_means,
                                                  marker_color=['#2ecc71' if m > 0 else '#e74c3c' for m in _means],
                                                  text=_txt, textposition='outside',
                                                  hoverinfo='text', hovertext=_txt))
                            fig3.add_hline(y=0, line_color='#888', line_width=1)
                            _pad = max(abs(m) for m in _means) * 0.6 + 0.3
                            fig3.update_layout(yaxis_range=[min(min(_means), 0) - _pad, max(max(_means), 0) + _pad])
                        else:
                            fig3.add_annotation(text='No closing lines captured yet — collected for trades<br>'
                                                     'starting in the next ~12 min, from now on', **_NO_DATA)
                        fig3.update_layout(title='Closing Line Value by Order Type', yaxis_title='avg CLV (¢)',
                                          showlegend=False, **_CHART_LAYOUT)
                        _ev3 = st.plotly_chart(fig3, width='stretch', theme='streamlit',
                                               on_select='rerun', selection_mode=['points'],
                                               key='_bar_chart_ordertype')
                        _bar_click(_ev3, 'ordertype', 'x')

                    # 4. Calibration by sport — bubbles carry no default label; hover shows
                    # detail, click opens a full stats + trade-history panel below. Color is
                    # diverging on the calibration gap (actual − predicted), not sport identity
                    # — that's what the click is for, so 10+ sports never need 10+ competing
                    # hues. Size = trade count.
                    with _c4:
                        fig4 = go.Figure()
                        calib = pd.DataFrame()
                        if not settled.empty and 'sport' in settled.columns and 'fair_prob' in settled.columns:
                            calib_src = settled.copy()
                            calib_src['sport_group'] = calib_src['sport'].apply(_sport_group)
                            calib = calib_src.groupby('sport_group', observed=True).agg(
                                n=('is_win', 'size'), win_rate=('is_win', 'mean'),
                                avg_fair=('fair_prob', 'mean')).reset_index()
                            calib['gap'] = calib['win_rate'] - calib['avg_fair']

                            fig4.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode='lines',
                                                      line=dict(dash='dash', color='#888', width=1.5),
                                                      hoverinfo='skip', showlegend=False))
                            if not calib.empty:
                                _gmax = max(calib['gap'].abs().max(), 0.01)
                                fig4.add_trace(go.Scatter(
                                    x=calib['avg_fair'], y=calib['win_rate'], mode='markers',
                                    marker=dict(
                                        size=calib['n'], sizemode='area',
                                        sizeref=2. * calib['n'].max() / (46. ** 2), sizemin=10,
                                        color=calib['gap'],
                                        colorscale=[[0, '#e74c3c'], [0.5, '#d8d8d8'], [1, '#2ecc71']],
                                        cmin=-_gmax, cmax=_gmax, cmid=0,
                                        colorbar=dict(title='actual −<br>predicted', thickness=12),
                                        line=dict(width=1.5, color='white'),
                                    ),
                                    customdata=np.stack(
                                        [calib['sport_group'], calib['n'], calib['gap']], axis=-1),
                                    hovertemplate='<b>%{customdata[0]}</b><br>predicted: %{x:.3f}'
                                                 '<br>actual: %{y:.3f}<br>n=%{customdata[1]:.0f}'
                                                 '<br>gap: %{customdata[2]:+.3f}'
                                                 '<extra></extra>',
                                    showlegend=False,
                                ))
                            fig4.update_layout(xaxis_range=[-0.03, 1.03], yaxis_range=[-0.03, 1.05])
                        else:
                            fig4.add_annotation(text='No settled trades yet', **_NO_DATA)
                        fig4.update_layout(title='Calibration by Sport',
                                          xaxis_title='predicted fair_prob',
                                          yaxis_title='actual win rate', **_CHART_LAYOUT)
                        _calib_event = st.plotly_chart(
                            fig4, width='stretch', theme='streamlit',
                            on_select='rerun', selection_mode=['points'], key='_calib_chart')
                        _calib_points = (_calib_event.selection.points
                                        if _calib_event and _calib_event.selection else [])
                        # A plotly widget's selection PERSISTS across reruns it didn't cause —
                        # e.g. the rerun triggered by clicking ✕ below still sees this chart
                        # reporting its old point as selected. Applying that unconditionally
                        # every render was the actual bug: it silently undid the ✕ button (the
                        # very next line always re-set the sport right back), and made clicking
                        # a second bubble look like nothing happened. Only act when the
                        # selected point's index actually changed since we last processed one.
                        _new_point = _calib_points[0].get('point_index') if _calib_points else None
                        if _new_point is not None and _new_point != st.session_state.get('_calib_last_point'):
                            st.session_state['_calib_last_point'] = _new_point
                            _cd = _calib_points[0].get('customdata')
                            if _cd:
                                st.session_state['_calib_selected_sport'] = _cd[0]

                    # ── Calibration detail panel — ticker-keyed-style session_state (see
                    # the live-positions inspect panel for the same pattern/rationale): stays
                    # open across this fragment's 30s auto-refresh until explicitly closed.
                    _sel_sport = st.session_state.get('_calib_selected_sport')
                    if _sel_sport and not calib.empty:
                        _match_mask = calib_src['sport_group'] == _sel_sport if not settled.empty else pd.Series(dtype=bool)
                        if _match_mask.any():
                            with st.container(border=True):
                                _h, _x = st.columns([10, 1])
                                _h.markdown(f'#### {_sel_sport}')
                                if _x.button('✕', key='_close_calib_detail', help='Close'):
                                    st.session_state['_calib_selected_sport'] = None
                                    # The header line above was already rendered this pass
                                    # (before this click could be seen) with the old sport
                                    # name — without forcing a fresh run right now it would
                                    # visibly linger until this fragment's next 30s tick.
                                    _fragment_rerun()
                            if _sel_sport:
                                _sub = calib_src[_match_mask]
                                _n_t   = len(_sub)
                                _wr_t  = _sub['is_win'].mean()
                                _fair_t = _sub['fair_prob'].mean()
                                _gap_t  = _wr_t - _fair_t
                                _pnl_t  = pd.to_numeric(_sub['actual_pnl'], errors='coerce').sum()
                                m1, m2, m3, m4, m5 = st.columns(5)
                                m1.metric('Trades', _n_t)
                                m2.metric('Win rate', f'{_wr_t:.0%}')
                                m3.metric('Avg predicted', f'{_fair_t:.3f}')
                                m4.metric('Calib. gap', f'{_gap_t:+.3f}',
                                         delta_color='normal' if _gap_t >= 0 else 'inverse')
                                m5.metric('Total PnL', f'${_pnl_t:+.2f}',
                                         delta_color='normal' if _pnl_t >= 0 else 'inverse')
                                st.markdown('**Trade history**')
                                _hist_cols = [c for c in ['logged_at', 'k_ticker', 'outcome', 'side',
                                                          'entry_price', 'fair_prob', 'edge',
                                                          'order_type', 'result', 'actual_pnl']
                                             if c in _sub.columns]
                                st.dataframe(_sub[_hist_cols].sort_values('logged_at', ascending=False),
                                           width='stretch', hide_index=True)
                        else:
                            # Sport no longer present in current settled data — drop the stale
                            # selection rather than show an empty panel with no way to tell why.
                            st.session_state['_calib_selected_sport'] = None

                    _sel = st.session_state.get('_bar_sel_ordertype')
                    if _sel and not clv_src.empty:
                        _sub = clv_src if _sel == 'all' else clv_src[clv_src['order_type'] == _sel]
                        if not _sub.empty:
                            _detail_panel('ordertype', f'CLV — {_sel}', _sub)

                    _c5, _c6 = st.columns(2)

                    # 5. Win rate by sport — plain ranked view (identity IS the point here,
                    # unlike chart 4, so a categorical-position encoding is fine — no color
                    # scale needed since it's one bar per sport already labeled on the axis).
                    with _c5:
                        fig5 = go.Figure()
                        if not settled.empty and 'sport' in settled.columns:
                            sg_src = settled.copy()
                            sg_src['sport_group'] = sg_src['sport'].apply(_sport_group)
                            sg = sg_src.groupby('sport_group', observed=True).agg(
                                n=('is_win', 'size'), win_rate=('is_win', 'mean')).sort_values('win_rate')
                            _bcolors = ['#2ecc71' if wr >= 0.5 else '#e74c3c' for wr in sg['win_rate']]
                            fig5.add_trace(go.Bar(
                                y=sg.index, x=sg['win_rate'], orientation='h', marker_color=_bcolors,
                                customdata=sg['n'],
                                hovertemplate='<b>%{y}</b><br>win rate: %{x:.0%}'
                                             '<br>n=%{customdata}<extra></extra>',
                            ))
                            fig5.add_vline(x=0.5, line_dash='dash', line_color='#888')
                            fig5.update_layout(xaxis_tickformat='.0%', xaxis_range=[0, 1])
                        else:
                            fig5.add_annotation(text='No settled trades yet', **_NO_DATA)
                        fig5.update_layout(title='Win Rate by Sport', showlegend=False, **_CHART_LAYOUT)
                        _ev5 = st.plotly_chart(fig5, width='stretch', theme='streamlit',
                                               on_select='rerun', selection_mode=['points'],
                                               key='_bar_chart_sport')
                        _bar_click(_ev5, 'sport', 'y')

                    # 6. Win rate by edge÷price quartile — live check that MAX_EDGE_OVER_PRICE
                    # (KALSHI/k_helpers.py) is doing its job: if the top quartile still shows a
                    # materially worse win rate than the rest, the cap needs to be tightened.
                    with _c6:
                        fig6 = go.Figure()
                        if (not settled.empty and 'edge' in settled.columns
                                and 'entry_price' in settled.columns and len(settled) >= 8):
                            eop_src = settled.copy()
                            eop_src['edge_over_price'] = eop_src['edge'] / eop_src['entry_price'].clip(lower=0.01)
                            try:
                                eop_src['eop_quartile'] = pd.qcut(
                                    eop_src['edge_over_price'], 4,
                                    labels=['Q1 (smallest)', 'Q2', 'Q3', 'Q4 (largest)'], duplicates='drop')
                                q = eop_src.groupby('eop_quartile', observed=True).agg(
                                    n=('is_win', 'size'), win_rate=('is_win', 'mean'))
                                _bcolors = ['#2ecc71' if wr >= 0.5 else '#e74c3c' for wr in q['win_rate']]
                                _txt = [f'{wr:.0%}<br>n={n}' for wr, n in zip(q['win_rate'], q['n'])]
                                fig6.add_trace(go.Bar(x=q.index.astype(str), y=q['win_rate'],
                                                      marker_color=_bcolors, text=_txt,
                                                      textposition='outside', hoverinfo='text',
                                                      hovertext=_txt))
                                fig6.add_hline(y=0.5, line_dash='dash', line_color='#888')
                                fig6.update_layout(yaxis_tickformat='.0%', yaxis_range=[0, 1.25])
                            except ValueError:
                                fig6.add_annotation(text='Not enough spread to quartile', **_NO_DATA)
                        else:
                            fig6.add_annotation(text='Not enough settled trades yet', **_NO_DATA)
                        fig6.update_layout(title='Win Rate by Edge÷Price Quartile',
                                          showlegend=False, **_CHART_LAYOUT)
                        _ev6 = st.plotly_chart(fig6, width='stretch', theme='streamlit',
                                               on_select='rerun', selection_mode=['points'],
                                               key='_bar_chart_quartile')
                        _bar_click(_ev6, 'quartile', 'x')

                    _sel = st.session_state.get('_bar_sel_sport')
                    if _sel and not sg_src.empty:
                        _sub = sg_src[sg_src['sport_group'] == _sel]
                        if not _sub.empty:
                            _detail_panel('sport', f'Sport: {_sel}', _sub)
                    _sel = st.session_state.get('_bar_sel_quartile')
                    if _sel and 'eop_quartile' in eop_src.columns:
                        _sub = eop_src[eop_src['eop_quartile'].astype(str) == _sel]
                        if not _sub.empty:
                            _detail_panel('quartile', f'Edge÷Price {_sel}', _sub)

                except ImportError:
                    st.warning('Install plotly to see charts: `pip install plotly`')


    _review_panel()

with tab_mm_trade:
    _mm_trade()
with tab_mm_review:
    _mm_review()
