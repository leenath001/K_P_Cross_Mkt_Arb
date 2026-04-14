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

import streamlit as st
import pandas as pd
from datetime import datetime
import config
from theODDS.p_helpers import pinnacle_odds, fetch_usage, get_api_usage, get_active_sports
from KALSHI.k_helpers   import kalshi_odds
from bot                import get_balance, run_all_signals
from settle             import fetch_market_result, compute_pnl

def _in_season(key: str) -> bool:
    """
    True if the sport is currently in season.
    Uses live active flags from theOdds API if a usage refresh has been done,
    otherwise falls back to config.SEASON_MONTHS.
    """
    live = get_active_sports()
    if key in live:
        return live[key]
    months = config.SEASON_MONTHS.get(key)
    if months is not None:
        return datetime.now().month in months
    return True  # unknown — assume in season

LOG_PATH = os.path.join(_TRADE, 'logs', 'trades.csv')

def _load_log():
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return pd.DataFrame()
    return pd.read_csv(LOG_PATH)

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
    pct       = used / max(limit, 1)
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
        signals = matched_df[matched_df['signal']]

        st.subheader('Results')
        m1, m2, m3 = st.columns(3)
        m1.metric('Matches', len(matched_df))
        m2.metric('Signals', len(signals))
        m3.metric('Events',  matched_df['event_id'].nunique() if 'event_id' in matched_df.columns else '—')

        if not signals.empty:
            st.markdown('#### Signals')

            # Tickers already traded and still pending — block re-execution
            _log = _load_log()
            _pending_tickers = set()
            if not _log.empty and 'result' in _log.columns and 'k_ticker' in _log.columns:
                _pending_tickers = set(
                    _log.loc[_log['result'] == 'PENDING', 'k_ticker'].tolist()
                )

            display_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                        'fair_prob', 'yes_ask', 'match_score', 'k_ticker']
                            if c in signals.columns]

            editable = signals[display_cols].copy().reset_index(drop=True)
            already_traded = editable['k_ticker'].isin(_pending_tickers)
            editable.insert(0, 'Execute', (~already_traded))
            editable.insert(1, 'Status', already_traded.map({True: '⚠️ pending', False: ''}))
            edited = st.data_editor(
                editable,
                use_container_width=True,
                hide_index=True,
                disabled=display_cols + ['Status'],
                column_config={'Execute': st.column_config.CheckboxColumn('Execute', default=True)},
            )

            approved_mask    = edited['Execute'].values
            approved_signals = signals.iloc[approved_mask].copy()
            n_approved       = int(approved_mask.sum())
            st.caption(f'{n_approved} signal(s) selected for execution')

            limit_only = st.checkbox('Limit orders only (never cross book)', value=False)

            if st.button(f'Execute {n_approved} Signal(s)', type='primary', disabled=n_approved == 0):
                if balance is None:
                    st.error('Refresh your Kalshi balance in the sidebar before trading.')
                else:
                    with st.status(f'Executing {n_approved} trade(s)...', expanded=True) as exec_status:
                        try:
                            results = run_all_signals(
                                approved_signals,
                                bankroll=balance,
                                taker_fee=taker_fee,
                                maker_fee=maker_fee,
                                limit_only=limit_only,
                            )
                            exec_status.update(label='Done', state='complete')
                            st.session_state['last_results'] = results
                        except Exception as e:
                            exec_status.update(label='Error', state='error')
                            st.error(str(e))

            last_results = st.session_state.get('last_results')
            if last_results:
                st.markdown('#### Trade Results')
                results_df = pd.DataFrame([r for r in last_results if r is not None])
                if not results_df.empty:
                    show_cols = [c for c in ['ticker', 'outcome', 'order_type', 'status',
                                             'reason', 'contracts', 'yes_price', 'ev']
                                 if c in results_df.columns]
                    st.dataframe(results_df[show_cols], use_container_width=True, hide_index=True)
                    errors = results_df[results_df['status'] == 'error']
                    if not errors.empty:
                        for _, err in errors.iterrows():
                            st.error(f"{err.get('ticker')} — {err.get('reason')}")
        else:
            st.info('No signals — all Kalshi asks are fairly priced vs Pinnacle at current fees.')

        with st.expander('All matches'):
            all_cols = [c for c in ['sport', 'home', 'away', 'commence', 'outcome',
                                    'fair_prob', 'yes_ask', 'match_score', 'signal', 'k_ticker']
                        if c in matched_df.columns]
            st.dataframe(matched_df[all_cols], use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — SETTLE
# ════════════════════════════════════════════════════════════════════════════

with tab_settle:
    st.subheader('Settle Pending Trades')

    log_df = _load_log()

    if log_df.empty:
        st.info('No trade log found yet.')
    else:
        pending = log_df[
            (log_df['result'] == 'PENDING') &
            (log_df['final_status'].isin(['executed', 'filled']))
        ]

        col1, col2, col3 = st.columns(3)
        col1.metric('Total trades',   len(log_df))
        col2.metric('Pending',        len(pending))
        col3.metric('Settled',        len(log_df) - len(pending))

        if pending.empty:
            st.success('No pending trades — all settled.')
        else:
            st.markdown('#### Pending Trades')
            show_cols = [c for c in ['logged_at', 'k_ticker', 'outcome', 'sport',
                                     'contracts', 'entry_price', 'ev_total', 'final_status']
                         if c in pending.columns]
            st.dataframe(pending[show_cols], use_container_width=True, hide_index=True)

            dry_run = st.checkbox('Dry run (preview only, do not write)', value=False)

            if st.button('Settle All Pending', type='primary'):
                updates = 0
                rows_out = []
                with st.status('Checking markets...', expanded=True) as settle_status:
                    for idx, row in pending.iterrows():
                        ticker = row['k_ticker']
                        st.write(f'Checking `{ticker}`...')
                        k_result = fetch_market_result(ticker)

                        if k_result is None:
                            st.write(f'  ⏳ Not settled yet')
                            continue

                        result_label = {'yes': 'WIN', 'no': 'LOSS', 'void': 'VOID'}[k_result]
                        pnl = compute_pnl(k_result,
                                          int(row['contracts']),
                                          float(row['entry_price']),
                                          float(row['fee_rate']))

                        colour = 'green' if pnl >= 0 else 'red'
                        st.markdown(f'  :{colour}[**{result_label}**]  pnl = ${pnl:+.4f}'
                                    + ('  *(dry run)*' if dry_run else ''))

                        if not dry_run:
                            log_df.at[idx, 'result']     = result_label
                            log_df.at[idx, 'actual_pnl'] = pnl
                        updates += 1

                    if updates == 0:
                        settle_status.update(label='No markets settled yet', state='complete')
                    elif dry_run:
                        settle_status.update(label=f'{updates} row(s) would be updated (dry run)', state='complete')
                    else:
                        log_df.to_csv(LOG_PATH, index=False)
                        settle_status.update(label=f'Updated {updates} row(s)', state='complete')

        # Show full log
        with st.expander('Full trade log'):
            st.dataframe(log_df, use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — REVIEW
# ════════════════════════════════════════════════════════════════════════════

with tab_review:
    st.subheader('Trade Review')

    log_df2 = _load_log()  # reuses the same helper defined in the Settle tab

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
