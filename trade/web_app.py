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
import config
from theODDS.p_helpers import pinnacle_odds, fetch_usage, get_api_usage
from KALSHI.k_helpers   import kalshi_odds
from bot                import get_balance, run_all_signals, TAKER_FEE, MAKER_FEE

# ── Page config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title='K/P Arb Dashboard',
    page_icon='📊',
    layout='wide',
)

st.title('K/P Cross-Market Arbitrage')

# ── Sidebar — API usage ──────────────────────────────────────────────────────

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
        st.error('⚠️ API quota nearly exhausted')
    elif pct >= 0.7:
        st.warning('API quota above 70%')

    st.divider()

    # ── Kalshi balance ───────────────────────────────────────────────────────
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

    # ── Run config ───────────────────────────────────────────────────────────
    st.header('Run Config')
    hrs       = st.slider('Look-ahead (hours)', 1, 168, config.LOOKAHEAD_HRS)
    threshold = st.slider('Match threshold',    0.5, 1.0, 0.85, step=0.01)
    taker_fee = st.slider('Taker fee %',        0,   15,  7) / 100
    maker_fee = st.slider('Maker fee %',        0,   15,  3) / 100
    live      = st.toggle('Fetch live games', value=config.LIVE)

# ── Sport selection ──────────────────────────────────────────────────────────

st.subheader('Select Sports')

# Group by sport category (prefix before first underscore)
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

def _category(key: str) -> str:
    prefix = key.split('_')[0]
    return _CATEGORY_LABELS.get(prefix, prefix.title())

grouped: dict[str, list] = {}
for key, cfg in config.SPORTS_CONFIG.items():
    cat = _category(key)
    grouped.setdefault(cat, []).append((key, cfg['label']))

# Sort categories, Soccer last (it's long)
cat_order = sorted([c for c in grouped if c != 'Soccer']) + (['Soccer'] if 'Soccer' in grouped else [])

selected_sports: list[str] = []

# Render in columns — 3 per row for non-Soccer, full width for Soccer
non_soccer = [c for c in cat_order if c != 'Soccer']
cols = st.columns(min(len(non_soccer), 3))

for i, cat in enumerate(non_soccer):
    with cols[i % 3]:
        st.markdown(f'**{cat}**')
        for key, label in grouped[cat]:
            default = key in config.SPORTS
            if st.checkbox(label, value=default, key=f'sport_{key}'):
                selected_sports.append(key)

if 'Soccer' in grouped:
    st.markdown('**Soccer**')
    soccer_cols = st.columns(4)
    for i, (key, label) in enumerate(grouped['Soccer']):
        default = key in config.SPORTS
        with soccer_cols[i % 4]:
            if st.checkbox(label, value=default, key=f'sport_{key}'):
                selected_sports.append(key)

st.caption(f'{len(selected_sports)} sport(s) selected')

# ── Run ──────────────────────────────────────────────────────────────────────

st.divider()

run_col, status_col = st.columns([1, 4])
with run_col:
    run = st.button('🔍 Fetch Signals', type='primary', disabled=len(selected_sports) == 0)

if run:
    if not selected_sports:
        st.warning('Select at least one sport.')
    else:
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

            except ValueError as e:
                status.update(label='No data', state='error')
                st.warning(str(e))
                st.session_state['matched_df'] = pd.DataFrame()
            except Exception as e:
                status.update(label='Error', state='error')
                st.error(str(e))
                st.session_state['matched_df'] = pd.DataFrame()

# ── Results ──────────────────────────────────────────────────────────────────

matched_df: pd.DataFrame = st.session_state.get('matched_df', pd.DataFrame())

if not matched_df.empty:
    signals = matched_df[matched_df['signal']]

    st.subheader('Results')
    m1, m2, m3 = st.columns(3)
    m1.metric('Matches',  len(matched_df))
    m2.metric('Signals',  len(signals))
    m3.metric('Events',   matched_df['event_id'].nunique() if 'event_id' in matched_df.columns else '—')

    # ── Signals table with per-row trade checkboxes ──────────────────────────
    if not signals.empty:
        st.markdown('#### Signals')

        display_cols = ['sport', 'home', 'away', 'commence', 'outcome',
                        'fair_prob', 'yes_ask', 'match_score', 'k_ticker']
        display_cols = [c for c in display_cols if c in signals.columns]

        editable = signals[display_cols].copy().reset_index(drop=True)
        editable.insert(0, 'Execute', True)

        edited = st.data_editor(
            editable,
            use_container_width=True,
            hide_index=True,
            disabled=display_cols,
            column_config={'Execute': st.column_config.CheckboxColumn('Execute', default=True)},
        )

        approved_mask   = edited['Execute'].values
        approved_signals = signals.iloc[approved_mask].copy()
        n_approved = int(approved_mask.sum())

        st.caption(f'{n_approved} signal(s) selected for execution')

        limit_only = st.checkbox('Limit orders only (never cross book)', value=False)

        exec_col, _ = st.columns([1, 4])
        with exec_col:
            execute = st.button(
                f'Execute {n_approved} Signal(s)',
                type='primary',
                disabled=n_approved == 0,
            )

        if execute:
            bankroll = st.session_state.get('balance')
            if bankroll is None:
                st.error('Refresh your Kalshi balance in the sidebar before trading.')
            else:
                with st.status(f'Executing {n_approved} trade(s)...', expanded=True) as exec_status:
                    try:
                        results = run_all_signals(
                            approved_signals,
                            bankroll=bankroll,
                            taker_fee=taker_fee,
                            maker_fee=maker_fee,
                            limit_only=limit_only,
                        )
                        exec_status.update(label='Done', state='complete')
                        st.session_state['last_results'] = results
                    except Exception as e:
                        exec_status.update(label='Error', state='error')
                        st.error(str(e))

        # Show last trade results if available
        last_results = st.session_state.get('last_results')
        if last_results:
            st.markdown('#### Trade Results')
            results_df = pd.DataFrame(last_results)
            if not results_df.empty:
                show_cols = [c for c in ['k_ticker', 'outcome', 'final_status',
                                         'close_reason', 'contracts', 'entry_price',
                                         'ev_total'] if c in results_df.columns]
                st.dataframe(results_df[show_cols], use_container_width=True, hide_index=True)

    else:
        st.info('No signals — all Kalshi asks are fairly priced vs Pinnacle at current fees.')

    # Full match table (expandable)
    with st.expander('All matches'):
        display_cols_all = ['sport', 'home', 'away', 'commence', 'outcome',
                            'fair_prob', 'yes_ask', 'match_score', 'signal', 'k_ticker']
        display_cols_all = [c for c in display_cols_all if c in matched_df.columns]
        st.dataframe(matched_df[display_cols_all], use_container_width=True, hide_index=True)
