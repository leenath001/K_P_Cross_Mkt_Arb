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
from KALSHI.k_helpers   import kalshi_odds
from bot                import get_balance, run_all_signals, cross_and_cancel_order
from dashboard          import StreamlitDashboard
from settle             import fetch_market_result, compute_pnl

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
    taker_fee = st.slider('Taker fee %',        0,   15,  7) / 100
    maker_fee = st.slider('Maker fee %',        0,   15,  3) / 100
    live      = st.toggle('Fetch live games', value=config.LIVE)

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_trade, tab_settle, tab_review, tab_nothing, tab_prospect = st.tabs(
    ['Trade', 'Settle', 'Review', 'Nothing', 'Prospect'])

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
    _b1, _b2, _ = st.columns([1, 1, 6])
    if _b1.button('Select all active'):
        for k in _all_keys:
            st.session_state[f'sport_{k}'] = _in_season(k)
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
                active = _in_season(key)
                dot    = '🟢' if active else '🔴'
                if st.checkbox(f'{dot} {label}', value=active, key=f'sport_{key}'):
                    selected_sports.append(key)

    if 'Soccer' in grouped:
        st.markdown('**Soccer**')
        soccer_cols = st.columns(4)
        for i, (key, label) in enumerate(grouped['Soccer']):
            with soccer_cols[i % 4]:
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
                matched_df = kalshi_odds(pinnacle_df, threshold=threshold, fees=taker_fee)
                st.write(f'✅ {len(matched_df)} matches found')

                st.session_state['matched_df'] = matched_df
                status.update(label='Done', state='complete')
                st.rerun()
            except ValueError as e:
                status.update(label='No data', state='error')
                st.warning(str(e))
                st.session_state['matched_df'] = pd.DataFrame()
                st.rerun()
            except Exception as e:
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
                ['Rest (maker, 3%)', 'Cross (taker, 7%)', 'Auto (cross if EV+, else rest)'],
                index=0, horizontal=True, key='_mode_select',
                help='Rest = post limit at top of book. Cross = take at ask immediately. Auto = cross only when taker EV is positive.'
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

                # Pending tickers — check BOTH logs so we don't double-bet across sides.
                # Only count rows that were ACTUALLY filled (executed/filled). Orphans
                # (canceled/expired that stayed PENDING) shouldn't block new bets.
                _log = _load_all_logs()
                _pending_tickers = set()
                if not _log.empty and 'result' in _log.columns and 'k_ticker' in _log.columns:
                    _really_open = _log[
                        (_log['result'] == 'PENDING') &
                        (_log['final_status'].isin(['executed', 'filled']))
                    ]
                    _pending_tickers = set(_really_open['k_ticker'].tolist())

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

                # mkt_ask = Kalshi top-of-book ask (taker price, shown for reference only)
                mkt_ask_col = 'no_ask' if side == 'no' else 'yes_ask'
                display_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                            'fair_prob', 'entry_price', mkt_ask_col,
                                            'match_score', 'k_ticker']
                                if c in _sig.columns]

                editable = _sig[display_cols].copy().reset_index(drop=True)
                editable = editable.rename(columns={mkt_ask_col: 'mkt_ask'})
                already_traded = editable['k_ticker'].isin(_pending_tickers)
                editable.insert(0, 'Execute', (~already_traded))
                editable.insert(1, 'Status', already_traded.map({True: '⚠️ pending', False: ''}))
                locked_cols = [c for c in editable.columns if c != 'Execute']
                edited = st.data_editor(
                    editable,
                    width="stretch",
                    hide_index=True,
                    disabled=locked_cols,
                    column_config={'Execute': st.column_config.CheckboxColumn('Execute', default=True)},
                    key=f'signals_editor_{side}',
                )

                approved_mask    = edited['Execute'].values
                approved_signals = signals.iloc[approved_mask].copy()
                n_approved       = int(approved_mask.sum())

                # limit_only is a YES-only option — NO always rests
                _opt_c1, _opt_c2, _opt_c3 = st.columns([2, 1, 1])
                with _opt_c1:
                    limit_only = limit_only_mode
                    if force_cross:
                        st.caption(f'{n_approved} {side.upper()} signal(s) — **CROSS** at ask (taker, {taker_fee*100:.0f}% fee)')
                    elif limit_only:
                        st.caption(f'{n_approved} {side.upper()} signal(s) — **REST** at top of book (maker, {maker_fee*100:.0f}% fee)')
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

                btn_label = f'Execute {n_approved} {side.upper()} Signal(s)'
                if st.button(btn_label, type='primary', disabled=(n_approved == 0)):
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
                _start   = st.session_state.get('_trade_start_time', time.time())
                _ttl     = st.session_state.get('_trade_ttl_sec', 1800)
                _elapsed = time.time() - _start
                _rem     = max(_ttl - _elapsed, 0)
                _pct     = min(_elapsed / max(_ttl, 1), 1.0)
                _m, _s   = int(_rem // 60), int(_rem % 60)
                st.progress(_pct, text=f'⏱ {_m}m {_s:02d}s remaining')

                # Header + cancel + cross&cancel buttons
                hc1, hc2, hc3 = st.columns([3, 1, 1])
                hc1.markdown('#### Dashboard')
                if thread.is_alive():
                    if hc2.button('🛑 Cancel all', key='_cancel_all_btn'):
                        if stop_event:
                            stop_event.set()
                        st.warning('Cancellation requested — monitors will close orders.')
                    if hc3.button('↑ Cross & Cancel', key='_cross_cancel_btn',
                                  help='Re-check each resting order against Pinnacle fair value. '
                                       'Crosses if EV still positive at current ask, cancels if not.'):
                        _snap2    = dash.snapshot()
                        _pos_now  = _snap2['positions']
                        _RESTING  = {'resting', 'open', 'pending', 'unknown'}
                        _targets  = [(oid, pos) for oid, pos in _pos_now.items()
                                     if pos.get('status') in _RESTING]
                        if not _targets:
                            st.info('No resting orders to process.')
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
                            _n_crossed  = sum(1 for r in _xc_results if r['action'] == 'crossed')
                            _n_canceled = sum(1 for r in _xc_results if r['action'] == 'canceled')
                            _n_errors   = sum(1 for r in _xc_results if r['action'] == 'error')
                            st.info(f'↑ {_n_crossed} crossed · {_n_canceled} canceled · {_n_errors} errors')
                            for _r in _xc_results:
                                if _r['action'] == 'crossed':
                                    st.success(f"✓ {_r['ticker']}  {_r.get('contracts')}ct  "
                                               f"ask={_r['ask']:.2f}  ev={_r['ev']:+.4f}")
                                elif _r['action'] == 'canceled':
                                    st.warning(f"✗ {_r['ticker']}  {_r.get('reason', 'canceled')}")
                                else:
                                    st.error(f"⚠ {_r['ticker']}  {_r.get('reason', 'error')}")

                # Positions table
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
                        rows.append({
                            'Ticker':    pos['ticker'],
                            'Outcome':   pos['outcome'],
                            'Contracts': cts,
                            'Filled':    filled,
                            'Entry ¢':   pos['entry_price'],
                            'Mkt Ask ¢': pos['market_ask'] if pos['market_ask'] is not None else '—',
                            'Fair entry':f"{pos['fair_entry']:.3f}",
                            'Fair last': f"{pos['fair_last']:.3f}",
                            'Edge':      f"{pos['edge_last']:+.3f}",
                            'Status':    disp_status,
                            'Last ping': pos['last_ping'],
                        })
                    n = len(rows)
                    st.dataframe(pd.DataFrame(rows), width="stretch",
                                 hide_index=True,
                                 height=min(35 * n + 38, 600))
                else:
                    st.caption('Waiting for orders to be placed...')

                # API bar
                au, al = snap['api_used'], snap['api_limit']
                ar = max(al - au, 0)
                pct = min(max(au / max(al, 1), 0.0), 1.0)
                st.progress(pct, text=f'API  {au} / {al} used  ({ar} remaining)')

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

        col1, col2, col3 = st.columns(3)
        col1.metric('Total trades', total_count)
        col2.metric('Pending',      pending_count)
        col3.metric('Settled',      total_count - pending_count)

        if pending_count == 0:
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
            yes_orph = yes_log[(yes_log['result'] == 'PENDING') &
                               (~yes_log['final_status'].isin(['executed', 'filled']))] \
                       if not yes_log.empty else pd.DataFrame()
            no_orph  = no_log[(no_log['result'] == 'PENDING') &
                              (~no_log['final_status'].isin(['executed', 'filled']))] \
                       if not no_log.empty else pd.DataFrame()
            orphan_count = len(yes_orph) + len(no_orph)

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
                        st.write(f'[{side.upper()}] Checking `{ticker}`...')
                        k_result = fetch_market_result(ticker)
                        if k_result is None:
                            st.write('  ⏳ Not settled yet')
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


# ════════════════════════════════════════════════════════════════════════════
# TAB 4 — NOTHING EVER HAPPENS
# ════════════════════════════════════════════════════════════════════════════

with tab_nothing:
    import nothing as _nothing
    from nothing_config import NOTHING_SERIES as _NOTHING_SERIES
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    st.subheader('"Nothing Ever Happens" — NO-side event bot')
    st.caption('Buys 1 NO contract per qualifying market. Caps total spend at '
               'a fraction of Kalshi cash so the K/P bot keeps capital.')

    # ── Series selection ─────────────────────────────────────────────────────
    if 'nothing_open_counts' not in st.session_state:
        with st.spinner(f'Checking Kalshi for open markets across {len(_NOTHING_SERIES)} series...'):
            _counts = {}
            for _t, _l, _c in _NOTHING_SERIES:
                try:
                    _counts[_t] = len(_nothing.fetch_series_markets(_t))
                except Exception:
                    _counts[_t] = 0
            st.session_state['nothing_open_counts'] = _counts
            st.session_state['nothing_counts_checked_at'] = _dt.now(_tz.utc).strftime('%H:%M UTC')

    _ncounts    = st.session_state['nothing_open_counts']
    _checked_at = st.session_state.get('nothing_counts_checked_at', '—')
    _n_active   = sum(1 for v in _ncounts.values() if v > 0)
    st.caption(f'Kalshi market check: **{_n_active}/{len(_NOTHING_SERIES)} series have open markets** — last checked {_checked_at}')

    _nb1, _nb2, _ = st.columns([1, 1, 6])
    if _nb1.button('Select all active', key='nothing_sel_all'):
        for _t, _l, _c in _NOTHING_SERIES:
            st.session_state[f'nseries_{_t}'] = _ncounts.get(_t, 0) > 0
    if _nb2.button('Deselect all', key='nothing_desel_all'):
        for _t, _l, _c in _NOTHING_SERIES:
            st.session_state[f'nseries_{_t}'] = False
    if st.button('🔄 Refresh market counts', key='nothing_refresh_counts'):
        st.session_state.pop('nothing_open_counts', None)
        st.rerun()

    # Group by category and render as checkboxes (like Trade tab sports)
    _n_grouped: dict = {}
    for _t, _l, _c in _NOTHING_SERIES:
        _n_grouped.setdefault(_c, []).append((_t, _l))

    selected_series: list = []
    for _cat, _items in _n_grouped.items():
        st.markdown(f'**{_cat.title()}**')
        _ncols = st.columns(min(len(_items), 3))
        for _i, (_t, _l) in enumerate(_items):
            with _ncols[_i % 3]:
                _cnt   = _ncounts.get(_t, 0)
                _dot   = '🟢' if _cnt > 0 else '🔴'
                _dflt  = _cnt > 0
                _label = f'{_dot} {_l}  ({_cnt} open)' if _cnt > 0 else f'{_dot} {_l}  (no open markets)'
                if st.checkbox(_label, value=_dflt, key=f'nseries_{_t}'):
                    selected_series.append(_t)

    st.caption(f'{len(selected_series)} series selected')
    st.divider()

    c1, c2, c3 = st.columns(3)
    with c1:
        extra_series_raw = st.text_input('Extra series (optional)',
                                         key='nothing_extra_series',
                                         placeholder='KXEXTRASERIES')
    with c2:
        tickers_raw = st.text_input('Explicit market tickers (optional)',
                                    key='nothing_tickers',
                                    placeholder='MKT-ABC MKT-DEF')
    with c3:
        budget_pct = st.slider('Budget % of Kalshi cash', 1, 50, 10,
                               key='nothing_budget_pct') / 100

    c4, c5, c6, c7, c8 = st.columns(5)
    with c4:
        max_no_price = st.slider('Max NO price', 0.05, 0.95, 0.50, step=0.01,
                                 key='nothing_max_no_price')
    with c5:
        ttl_min = st.number_input('TTL (min)', 5, 1440, 30,
                                  key='nothing_ttl_min',
                                  help='Cancel unfilled orders after this many minutes (or when market closes)')
    with c6:
        rest_mode = st.toggle('Rest (maker)', value=False,
                              key='nothing_rest',
                              help='Off = cross at no_ask (taker). On = rest at no_ask−1¢ post_only (maker)')
    with c7:
        dynamic_size = st.toggle('Dynamic sizing', value=False,
                                 key='nothing_dynamic',
                                 help='Off = 1 contract per market. On = partial Kelly (1–5 contracts) — more when NO is cheap.')
    with c8:
        allow_multi = st.toggle('Allow mutex events', value=False,
                                key='nothing_multi',
                                help='Off = drop A-vs-B events where yes-asks sum to ~1.0')

    series  = selected_series + [s.strip() for s in extra_series_raw.split() if s.strip()]
    tickers = [t.strip() for t in tickers_raw.split() if t.strip()]

    preview_col, execute_col, cancel_col = st.columns(3)
    preview = preview_col.button('Preview', key='nothing_preview',
                                 width="stretch")
    execute = execute_col.button('Execute (LIVE)', key='nothing_execute',
                                 type='primary', width="stretch")
    cancel  = cancel_col.button('Cancel all tracked', key='nothing_cancel',
                                width="stretch")

    if cancel:
        with st.spinner('Canceling tracked orders...'):
            try:
                _nothing.cancel_all_tracked()
                st.success('Cancel pass complete — check stdout.')
            except Exception as exc:
                st.error(f'Cancel failed: {exc}')

    if (preview or execute) and not (series or tickers):
        st.warning('Enter at least one series or ticker.')

    if (preview or execute) and (series or tickers):
        with st.spinner('Fetching markets...'):
            markets = []
            for s in series:
                if s in _nothing.SPORTS_SERIES:
                    st.warning(f'Skipping {s} — sports series blocklisted')
                    continue
                try:
                    fetched = _nothing.fetch_series_markets(s)
                    for m in fetched:
                        m['_series'] = s
                    markets.extend(fetched)
                except Exception as exc:
                    st.error(f'{s}: {exc}')
            for t in tickers:
                m = _nothing.fetch_ticker(t)
                if m is None:
                    st.warning(f'{t} — not found')
                    continue
                m['_series'] = (m.get('event_ticker') or '').split('-')[0]
                markets.append(m)

        kept = _nothing.filter_markets(markets, max_no_price=max_no_price,
                                       mutually_exclusive_only_filter=not allow_multi)

        # Dedup: drop tickers already sitting in nothing_trades.csv as open/pending
        existing = _nothing.already_bet_tickers()
        dropped_existing = 0
        if existing:
            before = len(kept)
            kept = [m for m in kept if m['ticker'] not in existing]
            dropped_existing = before - len(kept)

        cap = (f'fetched={len(markets)} · kept={len(kept)} · '
               f'single_event={not allow_multi}')
        if dropped_existing:
            cap += f' · deduped={dropped_existing} (already open)'
        st.caption(cap)

        # Resolve budget
        try:
            cash = _nothing.get_balance()
        except Exception as exc:
            st.error(f'Balance fetch failed: {exc}')
            cash = 0.0
        budget = round(cash * budget_pct, 2)
        st.metric('Budget',
                  f'${budget:.2f}',
                  delta=f'{budget_pct*100:.0f}% of ${cash:.2f} cash')

        plans, skipped = _nothing.plan_contracts(kept, budget, rest=rest_mode,
                                                 dynamic=dynamic_size)
        if not plans:
            st.info('No plans — budget cannot afford a single contract.')
        else:
            total_cost = sum(n * (c / 100) for _, n, c, _ in plans)
            total_cts  = sum(n for _, n, _, _ in plans)
            fee_rate   = _nothing.MAKER_FEE if rest_mode else _nothing.TAKER_FEE

            plan_rows = []
            for m, n, cents, mode in plans:
                entry_p = cents / 100
                plan_rows.append({
                    'ticker':      m['ticker'],
                    'title':       (m.get('title') or '')[:60],
                    'mode':        mode,
                    'no_price':    f'{cents}¢',
                    'contracts':   n,
                    'cost':        round(n * entry_p, 2),
                    'max_payout':  round(n * (1 - entry_p) * (1 - fee_rate), 2),
                    'event':       m.get('event_ticker', ''),
                })
            st.dataframe(pd.DataFrame(plan_rows), width="stretch",
                         hide_index=True)
            st.caption(f'{len(plans)} market(s) · {total_cts} contracts · '
                       f'est cost ${total_cost:.2f} · skipped {len(skipped)} over budget · '
                       f'mode = {"REST" if rest_mode else "CROSS"} · '
                       f'sizing = {"DYNAMIC" if dynamic_size else "EQUAL"}')

            if execute:
                st.warning('Placing live orders...')
                expiry_ts = int((_dt.now(_tz.utc) + _td(minutes=int(ttl_min))).timestamp())
                placed_tracked = []  # feeds the monitor thread
                errored = []
                progress = st.progress(0.0)
                status_placeholder = st.empty()

                for i, (m, n, cents, mode) in enumerate(plans, start=1):
                    try:
                        order = _nothing.place_no_order(
                            m['ticker'], cents, n, mode, expiry_ts)
                        oid    = order.get('order_id')
                        status = order.get('status', 'unknown')
                        entry  = cents / 100
                        _nothing.log_trade({
                            'logged_at':         _dt.now(_tz.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
                            'order_id':          oid,
                            'series_ticker':     m.get('_series', ''),
                            'event_ticker':      m.get('event_ticker', ''),
                            'k_ticker':          m['ticker'],
                            'title':             (m.get('title') or '')[:120],
                            'mode':              mode,
                            'entry_price':       entry,
                            'entry_price_cents': cents,
                            'fee_rate':          fee_rate,
                            'contracts':         n,
                            'total_cost':        round(n * entry, 4),
                            'max_payout':        round(n * (1 - entry) * (1 - fee_rate), 4),
                            'expires_at':        _dt.fromtimestamp(expiry_ts, tz=_tz.utc).isoformat(),
                            'final_status':      status,
                            'close_reason':      '',
                            'result':            'PENDING',
                            'actual_pnl':        '',
                        })

                        close_time = None
                        ct_raw = m.get('close_time')
                        if ct_raw:
                            try:
                                close_time = _dt.fromisoformat(
                                    str(ct_raw).replace('Z', '+00:00'))
                            except Exception:
                                close_time = None

                        if oid:
                            placed_tracked.append({
                                'order_id':    oid,
                                'ticker':      m['ticker'],
                                'title':       (m.get('title') or '')[:60],
                                'contracts':   n,
                                'entry_cents': cents,
                                'close_time':  close_time,
                            })
                    except Exception as exc:
                        errored.append({'ticker': m['ticker'], 'error': str(exc)})
                    progress.progress(i / len(plans))
                    status_placeholder.write(
                        f'Placed {len(placed_tracked)} · errors {len(errored)} · '
                        f'{i}/{len(plans)}')

                st.success(f'Placed {len(placed_tracked)}, errors {len(errored)}')
                if errored:
                    st.error('Errors:')
                    st.dataframe(pd.DataFrame(errored), width="stretch",
                                 hide_index=True)

                # ── Kick off live monitor ───────────────────────────────────
                if placed_tracked:
                    state      = {}
                    stop_event = threading.Event()
                    ttl_sec    = int(ttl_min) * 60

                    def _monitor_worker(tracked=placed_tracked, state=state,
                                        stop=stop_event, ttl=ttl_sec):
                        try:
                            _nothing.monitor_orders(tracked, stop, state,
                                                    ttl_seconds=ttl,
                                                    poll_seconds=10,
                                                    close_buffer_seconds=300)
                        except Exception as exc:
                            state['_error'] = str(exc)

                    t = threading.Thread(target=_monitor_worker, daemon=True)
                    t.start()
                    st.session_state['_nothing_thread'] = t
                    st.session_state['_nothing_state']  = state
                    st.session_state['_nothing_stop']   = stop_event
                    st.session_state['_nothing_ttl_min'] = ttl_min
                    st.rerun()

    # ── Live monitor dashboard ──────────────────────────────────────────────
    if '_nothing_thread' in st.session_state:
        _n_thread = st.session_state['_nothing_thread']
        _n_state  = st.session_state['_nothing_state']
        _n_stop   = st.session_state['_nothing_stop']
        _alive    = _n_thread.is_alive()

        @st.fragment(run_every='2s' if _alive else None)
        def _nothing_live():
            hc1, hc2, hc3 = st.columns([3, 1, 1])
            hc1.markdown('#### Live Nothing Dashboard')
            if _n_thread.is_alive():
                if hc2.button('🛑 Cancel ALL orders', key='_nothing_live_cancel',
                              type='primary', width="stretch"):
                    _n_stop.set()
                    st.warning('Cancel requested — monitor will kill all open orders.')
                if hc3.button('↑ Cross & Cancel', key='_nothing_cross_cancel',
                              help='For each resting order, cross at current ask if price '
                                   'is unchanged or better, otherwise cancel.'):
                    _nc_lock = _n_state.get('_lock')
                    _nc_snap = {}
                    if _nc_lock:
                        with _nc_lock:
                            _nc_snap = {k: dict(v) for k, v in _n_state.items()
                                        if not k.startswith('_')}
                    _NC_RESTING = {'pending', 'resting', 'open', 'unknown'}
                    _nc_ttl     = st.session_state.get('_nothing_ttl_min', 30)
                    _nc_exp     = int((_dt.now(_tz.utc) + _td(minutes=int(_nc_ttl))).timestamp())
                    _nc_results = []
                    for _oid, _rec in _nc_snap.items():
                        if _rec.get('status') in _NC_RESTING:
                            _r = _nothing.cross_no_order(
                                _rec['ticker'], _oid,
                                _rec.get('entry_cents', 0),
                                _rec.get('contracts', 1),
                                _nc_exp,
                            )
                            _nc_results.append(_r)
                    if not _nc_results:
                        st.info('No resting orders to process.')
                    else:
                        _nc_crossed  = sum(1 for r in _nc_results if r['action'] == 'crossed')
                        _nc_canceled = sum(1 for r in _nc_results if r['action'] == 'canceled')
                        _nc_errors   = sum(1 for r in _nc_results if r['action'] == 'error')
                        st.info(f'↑ {_nc_crossed} crossed · {_nc_canceled} canceled · {_nc_errors} errors')
                        for _r in _nc_results:
                            if _r['action'] == 'crossed':
                                st.success(f"✓ {_r['ticker']}  {_r.get('contracts')}ct  @ {_r['ask_cents']}¢")
                            elif _r['action'] == 'canceled':
                                st.warning(f"✗ {_r['ticker']}  {_r.get('reason', 'canceled')}")
                            else:
                                st.error(f"⚠ {_r['ticker']}  {_r.get('reason', 'error')}")

            if '_error' in _n_state:
                st.error(f'Monitor error: {_n_state["_error"]}')

            lock = _n_state.get('_lock')
            snapshot = {}
            if lock:
                with lock:
                    snapshot = {k: dict(v) for k, v in _n_state.items()
                                if not k.startswith('_')}

            if snapshot:
                rows = []
                for oid, rec in snapshot.items():
                    rows.append({
                        'Ticker':    rec.get('ticker', ''),
                        'Title':     rec.get('title', ''),
                        'Contracts': rec.get('contracts', 0),
                        'Filled':    rec.get('filled', 0),
                        'Remaining': rec.get('remaining', 0),
                        'Price ¢':   rec.get('entry_cents', 0),
                        'Status':    rec.get('status', 'unknown'),
                        'Reason':    rec.get('reason', ''),
                        'Elapsed':   f"{rec.get('elapsed', 0)}s",
                        'Last ping': rec.get('last_ping', ''),
                    })
                st.dataframe(pd.DataFrame(rows),
                             width="stretch", hide_index=True)
                filled_ct  = sum(1 for r in snapshot.values()
                                 if r.get('status') in ('executed', 'filled'))
                resting_ct = sum(1 for r in snapshot.values()
                                 if r.get('status') == 'resting')
                st.caption(f'{filled_ct} filled · {resting_ct} resting · '
                           f'{len(snapshot)} tracked · poll=10s · '
                           f'TTL={int(ttl_min)}min')
            else:
                st.caption('Monitor starting — waiting for first poll...')

            if not _n_thread.is_alive():
                st.success('Monitor complete — all orders resolved or canceled.')

        _nothing_live()

        if not _alive:
            for k in ('_nothing_thread', '_nothing_state', '_nothing_stop'):
                st.session_state.pop(k, None)

    # ── Performance metrics ─────────────────────────────────────────────────
    st.divider()
    st.markdown('#### Nothing Bot Performance')
    try:
        import trade.nothing_review as _nr
        import matplotlib.pyplot as _plt
        _nr_df = _nr.load_log()
        if not _nr_df.empty:
            _nr_fig = _nr.build_charts(_nr_df)
            if _nr_fig:
                st.pyplot(_nr_fig)
                _plt.close(_nr_fig)
            else:
                st.caption('No settled trades yet — nothing to chart.')
        else:
            st.caption('No trades logged yet.')
    except Exception as exc:
        st.error(f'Metrics error: {exc}')

    # ── Log viewer ──────────────────────────────────────────────────────────
    st.divider()
    st.caption(f'Log: `{_nothing.LOG_PATH}`')
    try:
        if os.path.exists(_nothing.LOG_PATH) and os.path.getsize(_nothing.LOG_PATH) > 0:
            log_df = pd.read_csv(_nothing.LOG_PATH)
            st.dataframe(log_df.tail(50), width="stretch", hide_index=True)
            st.caption(f'{len(log_df)} total rows · showing last 50')
        else:
            st.caption('No trades logged yet.')
    except Exception as exc:
        st.error(f'Failed to read log: {exc}')

# ════════════════════════════════════════════════════════════════════════════
# TAB 5 — PROSPECT THEORY
# ════════════════════════════════════════════════════════════════════════════

with tab_prospect:
    from KALSHI.k_helpers import prospect_signals as _pt_prospect_signals
    from prospect import run_prospect_signals as _run_prospect_signals
    PROSPECT_LOG_PATH = os.path.join(_TRADE, 'logs', 'prospect_trades.csv')

    st.subheader('Prospect Theory Strategy')
    st.caption(
        'Exploits cognitive-bias price distortions in Kalshi prediction markets. '
        '**Favorite zone** (yes_ask $0.75–$0.92): retail underprices high-prob events → buy YES. '
        '**Longshot zone** (yes_ask $0.05–$0.15): retail overprices low-prob events → buy NO.'
    )

    # ── Sidebar-shared params ─────────────────────────────────────────────────
    _pt_hrs        = st.session_state.get('hrs', config.LOOKAHEAD_HRS)
    _pt_taker_fee  = st.session_state.get('taker_fee', 0.07)
    _pt_maker_fee  = st.session_state.get('maker_fee', 0.03)
    _pt_threshold  = st.session_state.get('threshold', 0.85)

    # ── Zone bounds ───────────────────────────────────────────────────────────
    with st.expander('Zone bounds', expanded=False):
        _ptc1, _ptc2 = st.columns(2)
        with _ptc1:
            st.markdown('**Longshot zone** (buy NO)')
            _pt_long_lo = st.number_input('Lower bound', min_value=0.01, max_value=0.50,
                                           value=0.05, step=0.01, format='%.2f',
                                           key='_pt_long_lo')
            _pt_long_hi = st.number_input('Upper bound', min_value=0.01, max_value=0.50,
                                           value=0.15, step=0.01, format='%.2f',
                                           key='_pt_long_hi')
        with _ptc2:
            st.markdown('**Favorite zone** (buy YES)')
            _pt_fav_lo = st.number_input('Lower bound', min_value=0.50, max_value=0.99,
                                          value=0.75, step=0.01, format='%.2f',
                                          key='_pt_fav_lo')
            _pt_fav_hi = st.number_input('Upper bound', min_value=0.50, max_value=0.99,
                                          value=0.92, step=0.01, format='%.2f',
                                          key='_pt_fav_hi')

    _pt_fetch = st.button('Fetch Prospect Signals', key='_pt_fetch',
                          type='primary',
                          disabled='_pt_thread' in st.session_state)

    if _pt_fetch:
        with st.spinner('Fetching Pinnacle odds and matching to Kalshi...'):
            try:
                _pt_pinn = pinnacle_odds(config.SPORTS, hrs=_pt_hrs, live=False)
                _pt_matched = kalshi_odds(
                    _pt_pinn, threshold=_pt_threshold,
                    fees=_pt_taker_fee, maker_fees=_pt_maker_fee,
                )
                _pt_signals_df = _pt_prospect_signals(
                    _pt_matched,
                    longshot_lo=_pt_long_lo, longshot_hi=_pt_long_hi,
                    favorite_lo=_pt_fav_lo,  favorite_hi=_pt_fav_hi,
                )
                # Exclude tickers with open PENDING bets in prospect log
                if os.path.exists(PROSPECT_LOG_PATH) and os.path.getsize(PROSPECT_LOG_PATH) > 0:
                    _pt_log = pd.read_csv(PROSPECT_LOG_PATH)
                    _pt_open = set(_pt_log.loc[_pt_log['result'] == 'PENDING', 'k_ticker'])
                    if _pt_open:
                        _pt_signals_df = _pt_signals_df[
                            ~_pt_signals_df['k_ticker'].isin(_pt_open)]
                st.session_state['_pt_signals']    = _pt_signals_df
                st.session_state['_pt_show_table'] = True
            except Exception as exc:
                st.error(f'Fetch failed: {exc}')

    # ── Show signals ──────────────────────────────────────────────────────────
    _pt_showing = (st.session_state.get('_pt_show_table')
                   and '_pt_thread' not in st.session_state)
    if _pt_showing and '_pt_signals' in st.session_state:
        _pt_df  = st.session_state['_pt_signals']
        _pt_sig = _pt_df[_pt_df['pt_signal']]
        _pt_fav = _pt_sig[_pt_sig['pt_zone'] == 'favorite']
        _pt_lng = _pt_sig[_pt_sig['pt_zone'] == 'longshot']

        if _pt_sig.empty:
            st.info('No prospect theory signals at current zone bounds / fees.')
        else:
            if not _pt_fav.empty:
                st.markdown(f'**Favorite zone — {len(_pt_fav)} signal(s)  (buy YES)**')
                _pt_fav_disp = _pt_fav[['sport', 'home', 'away', 'outcome',
                                         'fair_prob', 'yes_ask', 'no_ask',
                                         'match_score', 'k_ticker']].copy()
                _pt_fav_disp['ev'] = _pt_fav_disp.apply(
                    lambda r: round(r['fair_prob'] - r['yes_ask'] -
                                    _pt_taker_fee * r['yes_ask'] * (1 - r['yes_ask']), 4), axis=1)
                st.dataframe(_pt_fav_disp, hide_index=True, use_container_width=True)

            if not _pt_lng.empty:
                st.markdown(f'**Longshot zone — {len(_pt_lng)} signal(s)  (buy NO)**')
                _pt_lng_disp = _pt_lng[['sport', 'home', 'away', 'outcome',
                                         'fair_prob', 'yes_ask', 'no_ask',
                                         'match_score', 'k_ticker']].copy()
                _pt_lng_disp['no_fair'] = (1 - _pt_lng_disp['fair_prob']).round(3)
                _pt_lng_disp['ev'] = _pt_lng_disp.apply(
                    lambda r: round((1 - r['fair_prob']) - r['no_ask'] -
                                    _pt_taker_fee * r['no_ask'] * (1 - r['no_ask']), 4)
                    if r['no_ask'] is not None else None, axis=1)
                st.dataframe(_pt_lng_disp, hide_index=True, use_container_width=True)

            # ── Execute controls ──────────────────────────────────────────────
            st.divider()
            _pt_oc1, _pt_oc2, _pt_oc3, _pt_oc4 = st.columns([2, 1, 1, 1])
            with _pt_oc1:
                _pt_mode = st.selectbox('Order mode', ['rest', 'cross', 'auto'],
                                         key='_pt_mode')
            with _pt_oc2:
                _pt_ttl = st.number_input('TTL (min)', min_value=5, max_value=1440,
                                           value=30, key='_pt_ttl')
            with _pt_oc3:
                _pt_size = st.number_input('Size ×', min_value=0.1, max_value=10.0,
                                            value=1.0, step=0.5, key='_pt_size_mult')
            with _pt_oc4:
                balance = st.session_state.get('balance')

            _pt_n = len(_pt_sig)
            if st.button(f'Execute {_pt_n} Prospect Signal(s)',
                          type='primary', key='_pt_execute',
                          disabled=(balance is None)):
                if balance is None:
                    st.error('Refresh Kalshi balance in the sidebar before trading.')
                else:
                    _pt_dash       = StreamlitDashboard(api_limit=1000)
                    _pt_results_h: list = []
                    _pt_stop       = threading.Event()
                    _pt_limit_only = (_pt_mode == 'rest')
                    _pt_force_cross= (_pt_mode == 'cross')

                    def _pt_worker(sdf=_pt_sig.copy(), bk=balance,
                                   tf=_pt_taker_fee, mf=_pt_maker_fee,
                                   lo=_pt_limit_only, fc=_pt_force_cross,
                                   ttl=int(_pt_ttl) * 60, sm=float(_pt_size),
                                   d=_pt_dash, rh=_pt_results_h, se=_pt_stop):
                        try:
                            out = _run_prospect_signals(
                                sdf, bankroll=bk,
                                taker_fee=tf, maker_fee=mf,
                                limit_only=lo, force_cross=fc,
                                max_duration=ttl, size_mult=sm,
                                dashboard=d, stop_event=se,
                            )
                            rh.extend(out or [])
                        except Exception as exc:
                            rh.append({'status': 'error', 'ticker': '',
                                       'outcome': '', 'reason': str(exc),
                                       'order_id': None, 'contracts': 0})

                    _pt_t = threading.Thread(target=_pt_worker, daemon=True)
                    _pt_t.start()
                    st.session_state['_pt_thread']      = _pt_t
                    st.session_state['_pt_dash']        = _pt_dash
                    st.session_state['_pt_results_h']   = _pt_results_h
                    st.session_state['_pt_stop']        = _pt_stop
                    st.session_state['_pt_start_time']  = time.time()
                    st.session_state['_pt_ttl_sec']     = int(_pt_ttl) * 60
                    st.session_state['_pt_show_table']  = False
                    st.rerun()

    # ── Live dashboard ────────────────────────────────────────────────────────
    if '_pt_thread' in st.session_state:
        _pt_thread   = st.session_state['_pt_thread']
        _pt_dash_obj = st.session_state.get('_pt_dash')
        _pt_results_h= st.session_state.get('_pt_results_h', [])
        _pt_stop_ev  = st.session_state.get('_pt_stop')
        _pt_alive    = _pt_thread.is_alive()

        @st.fragment(run_every='1s' if _pt_alive else None)
        def _pt_live_panel():
            if _pt_dash_obj is None:
                return
            _pt_snap = _pt_dash_obj.snapshot()

            # Progress bar
            _pt_s  = st.session_state.get('_pt_start_time', time.time())
            _pt_tl = st.session_state.get('_pt_ttl_sec', 1800)
            _pt_el = time.time() - _pt_s
            _pt_rm = max(_pt_tl - _pt_el, 0)
            _pt_pc = min(_pt_el / max(_pt_tl, 1), 1.0)
            _pt_mm, _pt_ss = int(_pt_rm // 60), int(_pt_rm % 60)
            st.progress(_pt_pc, text=f'⏱ {_pt_mm}m {_pt_ss:02d}s remaining')

            hc1, hc2, hc3 = st.columns([3, 1, 1])
            hc1.markdown('#### Prospect Dashboard')
            if _pt_thread.is_alive():
                if hc2.button('🛑 Cancel all', key='_pt_cancel_btn'):
                    if _pt_stop_ev:
                        _pt_stop_ev.set()
                    st.warning('Cancellation requested.')
                if hc3.button('↑ Cross & Cancel', key='_pt_cc_btn',
                              help='Re-ping Pinnacle, cross if EV > 0.005 else cancel.'):
                    _pt_snap2   = _pt_dash_obj.snapshot()
                    _pt_pos_now = _pt_snap2['positions']
                    _PT_REST    = {'resting', 'open', 'pending', 'unknown'}
                    _pt_targets = [(oid, pos) for oid, pos in _pt_pos_now.items()
                                   if pos.get('status') in _PT_REST]
                    if not _pt_targets:
                        st.info('No resting orders to process.')
                    else:
                        _pt_xc = []
                        for _oid, _pos in _pt_targets:
                            _pt_side = _pos.get('side', 'yes')
                            _r = cross_and_cancel_order(
                                _pos['ticker'], _oid,
                                _pos.get('contracts', 1),
                                _pt_taker_fee, _pt_side,
                                event_id=_pos.get('event_id', ''),
                                sport=_pos.get('sport', ''),
                                outcome=_pos.get('raw_outcome', ''),
                            )
                            _pt_xc.append(_r)
                        _nc = sum(1 for r in _pt_xc if r['action'] == 'crossed')
                        _nx = sum(1 for r in _pt_xc if r['action'] == 'canceled')
                        _ne = sum(1 for r in _pt_xc if r['action'] == 'error')
                        st.info(f'↑ {_nc} crossed · {_nx} canceled · {_ne} errors')
                        for _r in _pt_xc:
                            if _r['action'] == 'crossed':
                                st.success(f"✓ {_r['ticker']}  {_r.get('contracts')}ct  "
                                           f"ev={_r['ev']:+.4f}")
                            elif _r['action'] == 'canceled':
                                st.warning(f"✗ {_r['ticker']}  {_r.get('reason', 'canceled')}")
                            else:
                                st.error(f"⚠ {_r['ticker']}  {_r.get('reason', 'error')}")

            positions = _pt_snap['positions']
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
                    rows.append({
                        'Ticker':    pos['ticker'],
                        'Zone':      pos.get('pt_zone', '—'),
                        'Side':      pos.get('side', '—'),
                        'Outcome':   pos['outcome'],
                        'Contracts': cts,
                        'Filled':    filled,
                        'Entry ¢':   pos['entry_price'],
                        'Fair':      f"{pos['fair_entry']:.3f}",
                        'Edge':      f"{pos['edge_last']:+.3f}",
                        'Status':    disp_status,
                        'Last ping': pos['last_ping'],
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.caption('Waiting for orders to be placed...')

            if not _pt_thread.is_alive():
                st.success('Prospect session complete.')

        _pt_live_panel()

        if not _pt_alive:
            st.session_state.pop('_pt_thread', None)
            st.session_state.pop('_pt_dash', None)
            st.session_state.pop('_pt_results_h', None)
            st.session_state.pop('_pt_stop', None)

    # ── Review section ────────────────────────────────────────────────────────
    st.divider()
    st.markdown('#### Prospect Theory Performance')
    try:
        import trade.prospect_review as _ptrv
        import matplotlib.pyplot as _pt_plt
        _pt_rv_df = _ptrv.load_log()
        if not _pt_rv_df.empty:
            _pt_m = _ptrv.compute_metrics(_pt_rv_df)
            if _pt_m:
                _mc1, _mc2, _mc3, _mc4 = st.columns(4)
                _mc1.metric('Settled', _pt_m['settled'])
                _mc2.metric('Win Rate',
                            f'{_pt_m["win_rate"]:.0%}' if _pt_m['win_rate'] is not None else '—')
                _mc3.metric('PnL',
                            f'${_pt_m["pnl"]:+.2f}')
                _mc4.metric('ROI',
                            f'{_pt_m["emp_ev_dol"]:.1%}' if _pt_m['emp_ev_dol'] is not None else '—')
            _pt_rv_fig = _ptrv.build_charts(_pt_rv_df)
            if _pt_rv_fig:
                st.pyplot(_pt_rv_fig)
                _pt_plt.close(_pt_rv_fig)
            else:
                st.caption('No settled trades yet — nothing to chart.')
        else:
            st.caption('No prospect trades logged yet.')
    except Exception as exc:
        st.error(f'Prospect metrics error: {exc}')

    # ── Log viewer ────────────────────────────────────────────────────────────
    st.divider()
    st.caption(f'Log: `{PROSPECT_LOG_PATH}`')
    try:
        if os.path.exists(PROSPECT_LOG_PATH) and os.path.getsize(PROSPECT_LOG_PATH) > 0:
            _pt_log_df = pd.read_csv(PROSPECT_LOG_PATH)
            st.dataframe(_pt_log_df.tail(50), use_container_width=True, hide_index=True)
            st.caption(f'{len(_pt_log_df)} total rows · showing last 50')
        else:
            st.caption('No trades logged yet.')
    except Exception as exc:
        st.error(f'Failed to read log: {exc}')
