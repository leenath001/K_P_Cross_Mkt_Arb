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
import streamlit as st
import pandas as pd
from datetime import datetime
import config
from theODDS.p_helpers import pinnacle_odds, fetch_usage, get_api_usage
from KALSHI.k_helpers   import kalshi_odds
from bot                import get_balance, run_all_signals
from dashboard          import StreamlitDashboard
from settle             import fetch_market_result, compute_pnl

def _in_season(key: str) -> bool:
    """True if the sport is currently in season per config.SEASON_MONTHS."""
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

tab_trade, tab_settle, tab_review = st.tabs(['Trade', 'Settle', 'Review'])

# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — TRADE
# ════════════════════════════════════════════════════════════════════════════

with tab_trade:

    # ── Sport selection ──────────────────────────────────────────────────────
    st.subheader('Select Sports')

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
        trade_side = st.radio(
            'Contract side',
            ['YES — buy YES (cross or rest)', 'NO — rest NO at top of book (maker, 3%)'],
            index=0, horizontal=True, key='_side_select',
        )
        side    = 'no' if trade_side.startswith('NO') else 'yes'
        sig_col = 'signal_no' if side == 'no' else 'signal'

        if sig_col in matched_df.columns:
            signals = matched_df[matched_df[sig_col]]
        else:
            signals = matched_df.iloc[0:0]
            st.warning(f'`{sig_col}` not in results — refetch signals.')

        m1, m2, m3 = st.columns(3)
        m1.metric('Matches', len(matched_df))
        m2.metric(f'{side.upper()} signals', len(signals))
        m3.metric('Events',  matched_df['event_id'].nunique() if 'event_id' in matched_df.columns else '—')

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

            # Columns adapt per side: show the price that drives the signal
            price_col = 'no_ask' if side == 'no' else 'yes_ask'
            display_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                        'fair_prob', price_col, 'match_score', 'k_ticker']
                            if c in signals.columns]

            editable = signals[display_cols].copy().reset_index(drop=True)
            if side == 'no' and 'no_ask' in editable.columns:
                # Show the resting price the bot will actually post at
                editable['rest_price'] = (editable['no_ask'] - 0.01).round(2)
            already_traded = editable['k_ticker'].isin(_pending_tickers)
            editable.insert(0, 'Execute', (~already_traded))
            editable.insert(1, 'Status', already_traded.map({True: '⚠️ pending', False: ''}))
            locked_cols = [c for c in editable.columns if c != 'Execute']
            edited = st.data_editor(
                editable,
                use_container_width=True,
                hide_index=True,
                disabled=locked_cols,
                column_config={'Execute': st.column_config.CheckboxColumn('Execute', default=True)},
                key=f'signals_editor_{side}',
            )

            approved_mask    = edited['Execute'].values
            approved_signals = signals.iloc[approved_mask].copy()
            n_approved       = int(approved_mask.sum())

            # limit_only is a YES-only option — NO always rests
            if side == 'yes':
                limit_only = st.checkbox('Limit orders only (never cross book)', value=False)
                st.caption(f'{n_approved} YES signal(s) selected — will cross at `yes_ask` or rest at `yes_bid+1¢`')
            else:
                limit_only = False
                st.caption(f'{n_approved} NO signal(s) selected — will rest at `no_ask − 1¢` (maker fee {maker_fee*100:.0f}%)')

            btn_label  = f'Execute {n_approved} {side.upper()} Signal(s)'
            thread_key = '_trade_thread'
            running    = (thread_key in st.session_state and
                          st.session_state[thread_key].is_alive())

            if st.button(btn_label, type='primary',
                         disabled=(n_approved == 0 or running)):
                if balance is None:
                    st.error('Refresh your Kalshi balance in the sidebar before trading.')
                else:
                    # Fresh dashboard + shared results holder
                    dash           = StreamlitDashboard(api_limit=used + remaining)
                    dash.set_api_usage(used, remaining)
                    results_holder: list = []
                    stop_event     = threading.Event()

                    def _worker(approved=approved_signals.copy(),
                                bk=balance, tf=taker_fee, mf=maker_fee,
                                lo=limit_only, s=side,
                                d=dash, rh=results_holder, se=stop_event):
                        try:
                            out = run_all_signals(
                                approved, bankroll=bk,
                                taker_fee=tf, maker_fee=mf,
                                limit_only=lo, side=s,
                                dashboard=d, stop_event=se,
                            )
                            rh.extend(out or [])
                        except Exception as exc:
                            rh.append({'status': 'error', 'ticker': '',
                                       'outcome': '', 'reason': str(exc),
                                       'order_id': None, 'contracts': 0})

                    t = threading.Thread(target=_worker, daemon=True)
                    t.start()
                    st.session_state[thread_key]   = t
                    st.session_state['_trade_dash']       = dash
                    st.session_state['_trade_results_h']  = results_holder
                    st.session_state['_trade_stop']       = stop_event
                    st.session_state['_trade_side']       = side
                    st.rerun()

            # ── Live dashboard (while trades are running) ───────────────────
            if thread_key in st.session_state:
                thread        = st.session_state[thread_key]
                dash          = st.session_state.get('_trade_dash')
                results_h     = st.session_state.get('_trade_results_h', [])
                stop_event    = st.session_state.get('_trade_stop')
                last_side     = st.session_state.get('_trade_side', 'yes')

                is_alive = thread.is_alive()

                @st.fragment(run_every='1s' if is_alive else None)
                def _live_panel():
                    if dash is None:
                        return
                    snap = dash.snapshot()

                    # Header + cancel button
                    hc1, hc2 = st.columns([4, 1])
                    hc1.markdown(f'#### Live {last_side.upper()} Dashboard')
                    if thread.is_alive():
                        if hc2.button('🛑 Cancel all', key='_cancel_all_btn'):
                            if stop_event:
                                stop_event.set()
                            st.warning('Cancellation requested — monitors will close orders.')

                    # Positions table
                    positions = snap['positions']
                    if positions:
                        rows = []
                        for pos in positions.values():
                            rows.append({
                                'Ticker':     pos['ticker'],
                                'Outcome':    pos['outcome'],
                                'Contracts':  pos['contracts'],
                                'Entry ¢':    pos['entry_price'],
                                'Mkt Ask ¢':  pos['market_ask'] if pos['market_ask'] is not None else '—',
                                'Fair entry': f"{pos['fair_entry']:.3f}",
                                'Fair last':  f"{pos['fair_last']:.3f}",
                                'Edge':       f"{pos['edge_last']:+.3f}",
                                'Status':     pos['status'],
                                'Last ping':  pos['last_ping'],
                            })
                        st.dataframe(pd.DataFrame(rows),
                                     use_container_width=True, hide_index=True)
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

                # Cleanup: once thread is done, surface results and clear
                if not is_alive:
                    final_results = list(results_h)
                    st.session_state['last_results'] = final_results
                    st.session_state['last_side']    = last_side
                    for k in (thread_key, '_trade_dash', '_trade_results_h',
                              '_trade_stop', '_trade_side'):
                        st.session_state.pop(k, None)

            # ── Final results (persist after dashboard clears) ──────────────
            last_results = st.session_state.get('last_results')
            last_side    = st.session_state.get('last_side', 'yes')
            if last_results:
                st.markdown(f'#### {last_side.upper()} Trade Results')
                results_df = pd.DataFrame([r for r in last_results if r is not None])
                if not results_df.empty:
                    price_key  = 'no_price' if last_side == 'no' else 'yes_price'
                    show_cols  = [c for c in ['ticker', 'outcome', 'order_type', 'status',
                                              'reason', 'contracts', price_key, 'ev']
                                  if c in results_df.columns]
                    st.dataframe(results_df[show_cols], use_container_width=True, hide_index=True)
                    if 'status' in results_df.columns:
                        errors = results_df[results_df['status'] == 'error']
                        for _, err in errors.iterrows():
                            st.error(f"{err.get('ticker')} — {err.get('reason')}")
        else:
            st.info(f'No {side.upper()} signals at current fees / threshold.')

        with st.expander('All matches'):
            all_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                    'fair_prob', 'yes_ask', 'no_ask', 'match_score',
                                    'signal', 'signal_no', 'k_ticker']
                        if c in matched_df.columns]
            st.dataframe(matched_df[all_cols], use_container_width=True, hide_index=True)


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
                             use_container_width=True, hide_index=True)
            if not no_pending.empty:
                st.markdown('#### Pending NO Trades')
                st.dataframe(no_pending[[c for c in show_cols if c in no_pending.columns]],
                             use_container_width=True, hide_index=True)

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
                                     use_container_width=True, hide_index=True)
                    if not no_orph.empty:
                        st.caption('NO log orphans')
                        st.dataframe(no_orph[[c for c in orphan_cols if c in no_orph.columns]],
                                     use_container_width=True, hide_index=True)
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
                st.dataframe(combined, use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — REVIEW
# ════════════════════════════════════════════════════════════════════════════

with tab_review:
    st.subheader('Trade Review')

    log_df2 = _load_all_logs()  # YES + NO combined, tagged with 'side'

    if log_df2.empty:
        st.info('No trade log found yet.')
    else:
        filled  = log_df2[log_df2['final_status'].isin(['executed', 'filled'])]
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

        styled = log_df2.style.applymap(_colour_result, subset=['result']) \
                              if 'result' in log_df2.columns else log_df2

        st.dataframe(styled, use_container_width=True, hide_index=True)

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

                # 3. Win / Loss by order type
                ax = axes[2]
                if not settled.empty:
                    types  = settled['order_type'].unique()
                    wins   = [len(settled[(settled['order_type'] == t) & (settled['result'] == 'WIN')]) for t in types]
                    losses = [len(settled[(settled['order_type'] == t) & (settled['result'] == 'LOSS')]) for t in types]
                    x = np.arange(len(types))
                    ax.bar(x - 0.175, wins,   0.35, label='WIN',  color='#2ecc71')
                    ax.bar(x + 0.175, losses, 0.35, label='LOSS', color='#e74c3c')
                    ax.set_xticks(x)
                    ax.set_xticklabels(types)
                    for i, t in enumerate(types):
                        sub = settled[settled['order_type'] == t]
                        wr  = (sub['result'] == 'WIN').mean()
                        ax.text(i, max(wins[i], losses[i]) + 0.1,
                                f'WR={wr:.0%}', ha='center', fontsize=8, color='black')
                    ax.legend(fontsize=8)
                else:
                    ax.text(0.5, 0.5, 'No settled trades yet', ha='center', va='center',
                            transform=ax.transAxes, color='gray')
                ax.set_title('Win / Loss by Order Type', color='black')
                ax.set_ylabel('Count', color='black')
                ax.set_facecolor('white')
                ax.tick_params(colors='black')
                for sp in ax.spines.values(): sp.set_edgecolor('#ccc')

                plt.tight_layout()
                st.pyplot(fig)
                plt.close(fig)

            except ImportError:
                st.warning('Install matplotlib to see charts: `pip install matplotlib`')
