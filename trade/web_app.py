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
from bot                import (get_balance, run_all_signals, cross_and_cancel_order,
                                cancel_and_rerest, already_bet_tickers)
from dashboard          import StreamlitDashboard
from settle             import (fetch_market_result, compute_pnl,
                                fetch_scalar_settlement_value, compute_scalar_pnl)
from applog             import get_logger

log = get_logger(__name__)

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

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title='K/P Arb Dashboard', page_icon='📊', layout='wide')
st.title('K/P Cross-Market Arbitrage')

# ── Startup: check which sports have real Pinnacle events in the window ───────
if 'active_sports' not in st.session_state:
    with st.spinner(f'Checking OddsAPI for upcoming events across {len(config.SPORTS_CONFIG)} sports...'):
        try:
            used, remaining = fetch_usage()
            st.session_state['api_used']       = used
            st.session_state['api_remaining']  = remaining
            _event_counts = check_sports_with_events(
                list(config.SPORTS_CONFIG.keys()), config.LOOKAHEAD_HRS)
            # True = at least 1 event in the look-ahead window
            st.session_state['active_sports']     = {k: v > 0 for k, v in _event_counts.items()}
            st.session_state['active_sports_at']  = datetime.utcnow().strftime('%H:%M UTC')
            st.session_state['active_sports_ok']  = True
        except Exception:
            log.exception('Startup active-sports check failed')
            st.session_state['active_sports']     = {}
            st.session_state['active_sports_at']  = '—'
            st.session_state['active_sports_ok']  = False

# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
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
    hrs       = st.slider('Look-ahead (hours)', 1, 168, config.LOOKAHEAD_HRS)
    threshold = st.slider('Match threshold',    0.5, 1.0, 0.85, step=0.01)
    live      = st.toggle('Fetch live games', value=config.LIVE)

    st.caption(
        'Fees are fetched live per Kalshi series (not a fixed global rate) — verified '
        'to genuinely vary, e.g. MLB gets a 0.5x multiplier and several soccer/boxing '
        'series charge no maker fee at all. See the "fee %" columns in the signals table.'
    )
    # Fallback-only defaults if a series' live fee lookup fails — see
    # KALSHI/k_helpers.fee_rate_for(). No longer user-adjustable: a single override
    # can't be correct across series with genuinely different published fee rates.
    taker_fee = TAKER_FEE_BASE
    maker_fee = MAKER_FEE_BASE

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_trade, tab_settle, tab_review = st.tabs(['Trade', 'Settle', 'Review'])

# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — TRADE
# ════════════════════════════════════════════════════════════════════════════

with tab_trade:

    # ── Sport selection ──────────────────────────────────────────────────────
    st.subheader('Select Sports')

    # Show OddsAPI active-sport check status
    _as_ok = st.session_state.get('active_sports_ok', False)
    _as_at = st.session_state.get('active_sports_at', '—')
    _as    = st.session_state.get('active_sports', {})
    _n_active_sports = sum(1 for k in config.SPORTS_CONFIG if _as.get(k, False))
    if _as_ok:
        st.caption(
            f'🟢 OddsAPI event check: **{_n_active_sports}/{len(config.SPORTS_CONFIG)} sports have upcoming events** '
            f'in the {hrs}h window — checked {_as_at}'
        )
    else:
        st.caption('🔴 OddsAPI event check failed — falling back to season-month defaults. Check your API key.')
    if st.button('🔄 Re-check OddsAPI', key='recheck_odds_api',
                 help=f'Re-query events for all {len(config.SPORTS_CONFIG)} sports using current {hrs}h window. Costs ~{len(config.SPORTS_CONFIG)} API credits.'):
        for _k in ('active_sports', 'active_sports_at', 'active_sports_ok'):
            st.session_state.pop(_k, None)
        st.rerun()

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

    # Select / Deselect All Active buttons
    _all_keys = list(config.SPORTS_CONFIG.keys())
    _paused_sports = getattr(config, 'PAUSED_SPORTS', set())
    _b1, _b2, _ = st.columns([1, 1, 6])
    if _b1.button('Select all active'):
        for k in _all_keys:
            st.session_state[f'sport_{k}'] = _in_season(k) and k not in _paused_sports
    if _b2.button('Deselect all'):
        for k in _all_keys:
            st.session_state[f'sport_{k}'] = False

    selected_sports = []

    non_soccer = [c for c in cat_order if c != 'Soccer']
    cols = st.columns(min(len(non_soccer), 3))
    for i, cat in enumerate(non_soccer):
        with cols[i % 3]:
            st.markdown(f'**{cat}**')
            for key, label in grouped[cat]:
                if key in _paused_sports:
                    st.checkbox(f'⏸️ {label} (paused — see notebooks/win_loss_analysis.ipynb)',
                               value=False, key=f'sport_{key}', disabled=True)
                    continue
                active = _in_season(key)
                dot    = '🟢' if active else '🔴'
                if st.checkbox(f'{dot} {label}', value=active, key=f'sport_{key}'):
                    selected_sports.append(key)

    if 'Soccer' in grouped:
        st.markdown('**Soccer**')
        soccer_cols = st.columns(4)
        for i, (key, label) in enumerate(grouped['Soccer']):
            with soccer_cols[i % 4]:
                if key in _paused_sports:
                    st.checkbox(f'⏸️ {label} (paused)', value=False, key=f'sport_{key}', disabled=True)
                    continue
                active = _in_season(key)
                dot    = '🟢' if active else '🔴'
                if st.checkbox(f'{dot} {label}', value=active, key=f'sport_{key}'):
                    selected_sports.append(key)

    st.caption(f'{len(selected_sports)} sport(s) selected')
    st.divider()

    # ── Fetch signals ────────────────────────────────────────────────────────
    if st.button('🔍 Fetch Signals', type='primary', disabled=len(selected_sports) == 0):
        with st.status('Running pipeline...', expanded=True) as status:
            try:
                st.write(f'Fetching Pinnacle odds for {len(selected_sports)} sport(s)...')
                pinnacle_df = pinnacle_odds(selected_sports, hrs=hrs, live=live)
                used, remaining = get_api_usage()
                st.session_state['api_used']      = used
                st.session_state['api_remaining'] = remaining
                st.write(f'✅ {len(pinnacle_df)} outcome rows  (API: {used} used / {remaining} remaining)')

                st.write('Matching to Kalshi markets...')
                matched_df = kalshi_odds(pinnacle_df, threshold=threshold)
                st.write(f'✅ {len(matched_df)} matches found')

                st.session_state['matched_df'] = matched_df
                status.update(label='Done', state='complete')
                st.rerun()
            except ValueError as e:
                log.warning('Fetch Signals: no data — %s', e)
                status.update(label='No data', state='error')
                st.warning(str(e))
                st.session_state['matched_df'] = pd.DataFrame()
                st.rerun()
            except Exception as e:
                log.exception('Fetch Signals pipeline failed')
                status.update(label='Error', state='error')
                st.error(str(e))
                st.session_state['matched_df'] = pd.DataFrame()
                st.rerun()

    # ── Results ──────────────────────────────────────────────────────────────
    matched_df = st.session_state.get('matched_df', pd.DataFrame())

    if not matched_df.empty:
        st.subheader('Results')

        # Side selector drives which signals are shown and how orders execute
        _sc1, _sc2 = st.columns(2)
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
                st.markdown(f'#### {side.upper()} Signals')

                # Pending tickers — must match bot.already_bet_tickers() EXACTLY (same
                # function, not a re-derived copy), since that's what run_all_signals()
                # actually dedupes against when Execute is clicked. A UI-only filter that
                # disagrees (e.g. treating 'resting' as still tradeable) lets the user
                # check/approve rows that silently get dropped server-side — the
                # checkbox lies about what will happen, and Execute can appear to do
                # nothing because everything approved got filtered out internally.
                _pending_tickers = already_bet_tickers()

                # Compute entry_price (what the bot fills/posts at) before building display
                # CROSS → taker fills at ask; REST → limit order at bid+1¢ (YES) or ask-1¢ (NO)
                _sig = signals.copy()
                if side == 'no':
                    _sig['entry_price'] = (_sig['no_ask'] if force_cross
                                           else (_sig['no_ask'] - 0.01).round(2))
                else:
                    if force_cross:
                        _sig['entry_price'] = _sig['yes_ask']
                    elif limit_only_mode:
                        _sig['entry_price'] = (_sig['yes_ask'] - 0.01).round(2)
                    else:  # AUTO: cross rows use yes_ask, rest rows use yes_ask-1¢
                        _sig['entry_price'] = _sig.apply(
                            lambda r: r['yes_ask'] if r.get('signal', False)
                                      else round(r.get('yes_ask', 0) - 0.01, 2),
                            axis=1,
                        )

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

                # mkt_ask = Kalshi top-of-book ask (taker price, shown for reference only)
                mkt_ask_col = 'no_ask' if side == 'no' else 'yes_ask'
                display_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                            'fair_prob', 'entry_price', 'edge', 'ev', 'fee_pct', mkt_ask_col,
                                            'match_score', 'k_ticker']
                                if c in _sig.columns]

                editable = _sig[display_cols].copy().reset_index(drop=True)
                editable = editable.rename(columns={mkt_ask_col: 'mkt_ask', 'fee_pct': 'fee %'})
                already_traded = editable['k_ticker'].isin(_pending_tickers)
                editable.insert(1, 'Status', already_traded.map({True: '⚠️ pending', False: ''}))

                # Select all / Deselect all — data_editor only adopts a fresh 'Execute'
                # column when its widget key changes, so these buttons bump a version
                # counter to force that instead of trying to mutate editor state in place.
                _ver_key = f'_signals_editor_version_{side}'
                st.session_state.setdefault(_ver_key, 0)
                _sa1, _sa2, _sa3 = st.columns([1, 1, 6])
                if _sa1.button('Select all', key=f'_select_all_{side}'):
                    st.session_state[f'_signals_execute_override_{side}'] = True
                    st.session_state[_ver_key] += 1
                    st.rerun()
                if _sa2.button('Deselect all', key=f'_deselect_all_{side}'):
                    st.session_state[f'_signals_execute_override_{side}'] = False
                    st.session_state[_ver_key] += 1
                    st.rerun()

                _override = st.session_state.pop(f'_signals_execute_override_{side}', None)
                default_execute = (~already_traded) if _override is None else pd.Series(_override, index=editable.index)
                editable.insert(0, 'Execute', default_execute)

                locked_cols = [c for c in editable.columns if c != 'Execute']
                edited = st.data_editor(
                    editable,
                    width="stretch",
                    hide_index=True,
                    disabled=locked_cols,
                    column_config={
                        'Execute': st.column_config.CheckboxColumn('Execute', default=True),
                        'edge':    st.column_config.NumberColumn('edge', format='%+.4f',
                                                                  help='Raw mispricing: fair_prob - price (pre-fee)'),
                        'ev':      st.column_config.NumberColumn('ev', format='%+.4f',
                                                                  help='Fee-adjusted expected value per contract — what actually gates the signal'),
                        'fee %':   st.column_config.NumberColumn('fee %', format='%.3f%%'),
                    },
                    key=f'signals_editor_{side}_{st.session_state[_ver_key]}',
                )

                approved_mask    = edited['Execute'].values
                approved_signals = signals.iloc[approved_mask].copy()
                n_approved       = int(approved_mask.sum())

                # limit_only is a YES-only option — NO always rests
                _opt_c1, _opt_c2, _opt_c3 = st.columns([2, 1, 1])
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
                with _opt_c3:
                    size_mult = st.number_input(
                        'Size ×', min_value=0.1, max_value=10.0, value=1.0, step=0.5,
                        key='trade_size_mult',
                        help='Multiply Kelly contract count by this factor (e.g. 2.0 = double Kelly). Order is skipped if total cost exceeds available balance.'
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
                        # Fresh dashboard + shared results holder
                        dash           = StreamlitDashboard(api_limit=used + remaining)
                        dash.set_api_usage(used, remaining)
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
                        st.rerun()
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
                st.progress(_pct, text=f'⏱ {_m}m {_s:02d}s remaining{_extend_note}')

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
                hc1, hc2, hc3, hc4, hc5 = st.columns([2, 1, 1, 1, 1])
                hc1.markdown('#### Dashboard')
                if thread.is_alive():
                    if hc2.button('🛑 Cancel all', key='_cancel_all_btn'):
                        if stop_event:
                            stop_event.set()
                        st.session_state['_dash_last_action'] = {
                            'type': 'cancel_all',
                            'ts':   datetime.now().strftime('%H:%M:%S'),
                        }
                    if hc3.button('↑ Cross & Cancel', key='_cross_cancel_btn',
                                  help='Re-check each resting order against Pinnacle fair value. '
                                       'Crosses if EV still positive at current ask, cancels if not.'):
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
                    if hc4.button('⏸ Keep Rest', key='_keep_rest_btn',
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
                    if hc5.button('⏱ +15 min', key='_extend_time_btn',
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
                        st.success(f"[{_ats}] ⏱ Extended by {_last_action['minutes']}m — "
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
                                    st.success(f"✓ {_r['ticker']}  {_r.get('contracts')}ct  "
                                               f"ask={_r['ask']:.2f}  ev={_r['ev']:+.4f}")
                                elif _r['action'] == 'canceled':
                                    st.warning(f"✗ {_r['ticker']}  {_r.get('reason', 'canceled')}")
                                else:
                                    st.error(f"⚠ {_r['ticker']}  {_r.get('reason', 'error')}")
                    elif _atype == 'keep_rest':
                        _kr_results = _last_action['results']
                        _kr_ok  = sum(1 for _, r in _kr_results if r['action'] == 'rested')
                        _kr_err = sum(1 for _, r in _kr_results if r['action'] == 'error')
                        with st.expander(
                            f'[{_ats}] ⏸ {_kr_ok} re-rested · {_kr_err} errors — Pinnacle polling stopped',
                            expanded=True,
                        ):
                            for _tkr, _r in _kr_results:
                                if _r['action'] == 'rested':
                                    st.success(f"✓ {_tkr}  {_r['price_cents']}¢  GTC {_r['expiry']}")
                                elif _r['action'] == 'skipped':
                                    st.caption(f"— {_tkr}  {_r.get('reason', 'skipped')}")
                                else:
                                    st.error(f"⚠ {_tkr}  {_r.get('reason', 'error')}")

                # Positions table — wrapped: this renders every 1s off live worker-thread
                # state, so one malformed row must not repeatedly break the whole fragment.
                try:
                    positions = snap['positions']
                    if positions:
                        rows = []
                        for pos in positions.values():
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
                                'Avg Fill ¢': round(avg_fp * 100) if avg_fp is not None else '—',
                                'Entry ¢':   pos['entry_price'],
                                'Mkt Ask ¢': f"{pos['market_ask']}" if pos['market_ask'] is not None else '—',
                                'Fair entry':f"{pos['fair_entry']:.3f}",
                                'Fair last': f"{pos['fair_last']:.3f}",
                                'Edge':      f"{pos['edge_last']:+.3f}",
                                'Status':    disp_status,
                                'Last ping': pos['last_ping'],
                            })
                        n = len(rows)

                        # Red = unfilled, yellow = partial, green = fully filled.
                        def _fill_color(row):
                            cts_, filled_ = row['Contracts'], row['Filled']
                            if cts_ > 0 and filled_ >= cts_:
                                bg = 'rgba(46, 204, 113, 0.25)'    # green
                            elif filled_ > 0:
                                bg = 'rgba(241, 196, 15, 0.25)'    # yellow
                            else:
                                bg = 'rgba(231, 76, 60, 0.25)'     # red
                            return [f'background-color: {bg}'] * len(row)

                        positions_df = pd.DataFrame(rows)
                        styled = positions_df.style.apply(_fill_color, axis=1)
                        st.dataframe(styled, width="stretch",
                                     hide_index=True,
                                     height=min(35 * n + 38, 600))
                    else:
                        st.caption('Waiting for orders to be placed...')
                except Exception:
                    log.exception('Live dashboard: positions table render failed')
                    st.error('Dashboard table failed to render — see trade/logs/app.log. '
                             'Trading continues in the background.')

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

            _live_panel()

            # Cleanup once thread finishes, then rerun to restore the signals view
            if not is_alive:
                final_results = list(results_h)
                st.session_state['last_results'] = final_results
                st.session_state['last_side']    = last_side
                for k in (thread_key, '_trade_dash', '_trade_results_h',
                          '_trade_stop', '_trade_side', '_trade_mode'):
                    st.session_state.pop(k, None)
                st.rerun()

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


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — SETTLE
# ════════════════════════════════════════════════════════════════════════════

with tab_settle:
    st.subheader('Settle Pending Trades')

    yes_log = _read_csv(LOG_PATH)
    no_log  = _read_csv(NO_LOG_PATH)

    if yes_log.empty and no_log.empty:
        st.info('No trade log found yet.')
    else:
        total_count = len(yes_log) + len(no_log)
        yes_pending = yes_log[(yes_log['result'] == 'PENDING') &
                              (yes_log['final_status'].isin(['executed', 'filled']))] \
                      if not yes_log.empty else pd.DataFrame()
        no_pending  = no_log[(no_log['result'] == 'PENDING') &
                             (no_log['final_status'].isin(['executed', 'filled']))] \
                      if not no_log.empty else pd.DataFrame()
        pending_count = len(yes_pending) + len(no_pending)

        # Orphans: PENDING rows that never actually filled (resting/canceled/unknown).
        # Computed here (not just inside the "else" below) because the "no pending
        # trades" success message must NOT hide these — a ticker with a stale orphan
        # row is silently blocked from re-trading by already_bet_tickers() even
        # though pending_count (genuinely open/filled) is 0. Without this, the
        # cleanup tool that clears them is unreachable whenever ALL pending rows
        # happen to be orphans rather than truly-open trades.
        yes_orph = yes_log[(yes_log['result'] == 'PENDING') &
                           (~yes_log['final_status'].isin(['executed', 'filled']))] \
                   if not yes_log.empty else pd.DataFrame()
        no_orph  = no_log[(no_log['result'] == 'PENDING') &
                          (~no_log['final_status'].isin(['executed', 'filled']))] \
                   if not no_log.empty else pd.DataFrame()
        orphan_count = len(yes_orph) + len(no_orph)

        col1, col2, col3 = st.columns(3)
        col1.metric('Total trades', total_count)
        col2.metric('Pending',      pending_count)
        col3.metric('Settled',      total_count - pending_count)

        if pending_count == 0 and orphan_count == 0:
            st.success('No pending trades — all settled.')
        else:
            show_cols = ['logged_at', 'k_ticker', 'outcome', 'sport',
                         'contracts', 'entry_price', 'ev_total', 'final_status']

            if not yes_pending.empty:
                st.markdown('#### Pending YES Trades')
                st.dataframe(yes_pending[[c for c in show_cols if c in yes_pending.columns]],
                             width="stretch", hide_index=True)
            if not no_pending.empty:
                st.markdown('#### Pending NO Trades')
                st.dataframe(no_pending[[c for c in show_cols if c in no_pending.columns]],
                             width="stretch", hide_index=True)

            dry_run = st.checkbox('Dry run (preview only, do not write)', value=False)

            # ── Orphan cleanup ──────────────────────────────────────────────
            orphan_cols = ['k_ticker', 'final_status', 'result']

            if orphan_count > 0:
                with st.expander(f'⚠️ {orphan_count} orphan row(s) — never filled but still PENDING'):
                    if not yes_orph.empty:
                        st.caption('YES log orphans')
                        st.dataframe(yes_orph[[c for c in orphan_cols if c in yes_orph.columns]],
                                     width="stretch", hide_index=True)
                    if not no_orph.empty:
                        st.caption('NO log orphans')
                        st.dataframe(no_orph[[c for c in orphan_cols if c in no_orph.columns]],
                                     width="stretch", hide_index=True)
                    if st.button('Clear orphans (mark as CANCELED)'):
                        cleaned = 0
                        if not yes_orph.empty:
                            mask = (yes_log['result'] == 'PENDING') & \
                                   (~yes_log['final_status'].isin(['executed', 'filled']))
                            yes_log.loc[mask, 'result']     = 'CANCELED'
                            yes_log.loc[mask, 'actual_pnl'] = 0
                            yes_log.to_csv(LOG_PATH, index=False)
                            cleaned += int(mask.sum())
                        if not no_orph.empty:
                            mask = (no_log['result'] == 'PENDING') & \
                                   (~no_log['final_status'].isin(['executed', 'filled']))
                            no_log.loc[mask, 'result']     = 'CANCELED'
                            no_log.loc[mask, 'actual_pnl'] = 0
                            no_log.to_csv(NO_LOG_PATH, index=False)
                            cleaned += int(mask.sum())
                        st.success(f'Cleared {cleaned} orphan row(s)')
                        st.rerun()

            if st.button('Settle All Pending', type='primary'):

                def _settle_group(df, side, path):
                    """Settle pending rows in `df`, write to `path`. Returns updates."""
                    if df.empty:
                        return 0
                    pending = df[(df['result'] == 'PENDING') &
                                 (df['final_status'].isin(['executed', 'filled']))]
                    if pending.empty:
                        return 0
                    win_result  = 'no' if side == 'no' else 'yes'
                    loss_result = 'yes' if side == 'no' else 'no'
                    label_map   = {win_result: 'WIN', loss_result: 'LOSS', 'void': 'VOID'}
                    updated = 0
                    for idx, row in pending.iterrows():
                        ticker = row['k_ticker']
                        try:
                            st.write(f'[{side.upper()}] Checking `{ticker}`...')
                            k_result = fetch_market_result(ticker)
                            if k_result is None:
                                st.write('  ⏳ Not settled yet')
                                continue
                            if k_result == 'scalar':
                                sv = fetch_scalar_settlement_value(ticker)
                                if sv is None:
                                    st.write(f'  ⚠️ SCALAR — settlement value unavailable, skipping')
                                    continue
                                pnl = compute_scalar_pnl(int(row['contracts']),
                                                         float(row['entry_price']),
                                                         float(row['fee_rate']),
                                                         sv, side=side)
                                colour = 'green' if pnl >= 0 else ('red' if pnl < 0 else 'grey')
                                st.markdown(f'  :blue[**SCALAR**]  settle={sv:.3f}  '
                                            f'  :{colour}[pnl=${pnl:+.4f}]'
                                            + ('  *(dry run)*' if dry_run else ''))
                                if not dry_run:
                                    df.at[idx, 'result']     = 'SCALAR'
                                    df.at[idx, 'actual_pnl'] = pnl
                                updated += 1
                                continue
                            if k_result not in label_map:
                                st.write(f'  ⚠️ Unknown result `{k_result}` — skipping')
                                continue
                            result_label = label_map[k_result]
                            pnl = compute_pnl(k_result,
                                              int(row['contracts']),
                                              float(row['entry_price']),
                                              float(row['fee_rate']),
                                              side=side)
                            colour = 'green' if pnl >= 0 else 'red'
                            st.markdown(f'  :{colour}[**{result_label}**]  pnl = ${pnl:+.4f}'
                                        + ('  *(dry run)*' if dry_run else ''))
                            if not dry_run:
                                df.at[idx, 'result']     = result_label
                                df.at[idx, 'actual_pnl'] = pnl
                            updated += 1
                        except Exception:
                            # One malformed row shouldn't abort settlement for the rest —
                            # log it and keep going so already-processed rows still get saved.
                            log.exception('Settle: failed to settle %s row for %s', side, ticker)
                            st.error(f'  ⚠️ {ticker} — settle failed, see trade/logs/app.log')
                    if updated and not dry_run:
                        df.to_csv(path, index=False)
                    return updated

                with st.status('Checking markets...', expanded=True) as settle_status:
                    updates  = _settle_group(yes_log, 'yes', LOG_PATH)
                    updates += _settle_group(no_log,  'no',  NO_LOG_PATH)

                    if updates == 0:
                        settle_status.update(label='No markets settled yet', state='complete')
                    elif dry_run:
                        settle_status.update(label=f'{updates} row(s) would be updated (dry run)', state='complete')
                    else:
                        settle_status.update(label=f'Updated {updates} row(s)', state='complete')

        with st.expander('Full trade log (YES + NO)'):
            combined = _load_all_logs()
            if not combined.empty:
                st.dataframe(combined, width="stretch", hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — REVIEW
# ════════════════════════════════════════════════════════════════════════════

with tab_review:
    st.subheader('Trade Review')

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
        settled = filled[filled['result'].isin(['WIN', 'LOSS'])].copy()
        settled['actual_pnl'] = pd.to_numeric(settled['actual_pnl'], errors='coerce')

        total_ev   = filled['ev_total'].sum()      if not filled.empty  else 0
        actual_pnl = settled['actual_pnl'].sum()   if not settled.empty else 0
        win_rate   = (settled['result'] == 'WIN').mean() if not settled.empty else None
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

        styled = log_df2.style.map(_colour_result, subset=['result']) \
                              if 'result' in log_df2.columns else log_df2

        st.dataframe(styled, width="stretch", hide_index=True)

        # ── Charts ───────────────────────────────────────────────────────────
        if not filled.empty:
            st.divider()
            st.markdown('#### Charts')

            try:
                import matplotlib.pyplot as plt
                import matplotlib.gridspec as gridspec
                import numpy as np

                filled_sorted = filled.sort_values('logged_at').copy()

                fig, axes = plt.subplots(1, 3, figsize=(16, 4), facecolor='white')
                fig.suptitle('Edge Realization', fontsize=13, fontweight='bold', color='black')

                # 1. Edge per trade
                ax = axes[0]
                colors = ['#2ecc71' if v > 0 else '#e74c3c' for v in filled_sorted['edge']]
                ax.bar(range(len(filled_sorted)), filled_sorted['edge'].values, color=colors)
                ax.axhline(filled_sorted['edge'].mean(), color='steelblue', linewidth=1,
                           linestyle=':', label=f"mean={filled_sorted['edge'].mean():+.3f}")
                ax.axhline(0, color='black', linewidth=0.6, linestyle='--')
                ax.set_title('Edge per Trade', color='black')
                ax.set_xlabel('Trade #', color='black')
                ax.set_ylabel('Edge', color='black')
                ax.legend(fontsize=8)
                ax.set_facecolor('white')
                ax.tick_params(colors='black')
                for sp in ax.spines.values(): sp.set_edgecolor('#ccc')

                # 2. Projected EV / Realized PnL / Luck
                # Projected EV  = cumulative ev_total (edge at entry, from Pinnacle − Kalshi ask)
                # Realized PnL  = cumulative actual_pnl for settled trades
                # Luck          = Realized PnL − Projected EV (random outcome variance)
                ax = axes[1]
                if not settled.empty:
                    settled_sorted = settled.sort_values('logged_at').copy()
                    settled_sorted['luck'] = settled_sorted['actual_pnl'] - settled_sorted['ev_total']
                    n  = len(settled_sorted)
                    xs = range(n)
                    cum_proj_ev  = settled_sorted['ev_total'].cumsum().values
                    cum_real_ev  = settled_sorted['actual_pnl'].cumsum().values
                    cum_luck     = settled_sorted['luck'].cumsum().values
                    luck_colors  = ['#2ecc71' if v >= 0 else '#e74c3c' for v in cum_luck]
                    ax.plot(xs, cum_proj_ev, color='steelblue', linewidth=2,
                            label='Projected EV', marker='o', markersize=3, zorder=3)
                    ax.plot(xs, cum_real_ev, color='#9b59b6', linewidth=2,
                            label='Realized PnL', marker='s', markersize=3, zorder=3)
                    ax.bar(xs, cum_luck, color=luck_colors, alpha=0.4,
                           label='Luck (realized − projected)', zorder=2)
                    ax.axhline(0, color='black', linewidth=0.5, linestyle='--')
                else:
                    ax.text(0.5, 0.5, 'No settled trades yet', ha='center', va='center',
                            transform=ax.transAxes, color='gray')
                ax.set_title('Projected EV vs Realized PnL vs Luck', color='black')
                ax.set_xlabel('Settled trade #', color='black')
                ax.set_ylabel('$', color='black')
                ax.legend(fontsize=8)
                ax.set_facecolor('white')
                ax.tick_params(colors='black')
                for sp in ax.spines.values(): sp.set_edgecolor('#ccc')

                # 3. Win rate by order type
                ax = axes[2]
                if not settled.empty and 'order_type' in settled.columns:
                    _LABEL_MAP = {'no_rest': 'rest', 'no_cross': 'cross'}
                    settled = settled.copy()
                    settled['order_type'] = settled['order_type'].map(
                        lambda v: _LABEL_MAP.get(v, v))
                    types   = sorted(settled['order_type'].unique())
                    wr_vals = []
                    colors  = []
                    for t in types:
                        sub = settled[settled['order_type'] == t]
                        wr  = (sub['result'] == 'WIN').mean()
                        wr_vals.append(wr)
                        colors.append('#2ecc71' if wr >= 0.5 else '#e74c3c')
                    x = np.arange(len(types))
                    ax.bar(x, wr_vals, color=colors, width=0.5)
                    ax.axhline(0.5, color='gray', linewidth=1, linestyle='--')
                    ax.set_ylim(0, 1.15)
                    ax.set_xticks(x)
                    ax.set_xticklabels(types)
                    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
                    for i, t in enumerate(types):
                        sub = settled[settled['order_type'] == t]
                        w   = (sub['result'] == 'WIN').sum()
                        l   = (sub['result'] == 'LOSS').sum()
                        pnl = pd.to_numeric(sub['actual_pnl'], errors='coerce').sum()
                        ax.text(i, wr_vals[i] + 0.04,
                                f'{wr_vals[i]:.0%}\n{w}W/{l}L  ${pnl:+.2f}',
                                ha='center', fontsize=8, color='black')
                else:
                    ax.text(0.5, 0.5, 'No settled trades yet', ha='center', va='center',
                            transform=ax.transAxes, color='gray')
                ax.set_title('Win Rate by Order Type', color='black')
                ax.set_ylabel('Win Rate', color='black')
                ax.set_facecolor('white')
                ax.tick_params(colors='black')
                for sp in ax.spines.values(): sp.set_edgecolor('#ccc')

                plt.tight_layout()
                st.pyplot(fig)
                plt.close(fig)

            except ImportError:
                st.warning('Install matplotlib to see charts: `pip install matplotlib`')

