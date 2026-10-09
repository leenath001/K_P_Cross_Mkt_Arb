"""
trade/ui/trade_tab.py — Pinnacle → Trade: sport picker, Fetch Signals, the signals tables, Execute, and the live order dashboard.
"""
import threading
import time
import streamlit as st
import pandas as pd
from datetime import datetime
import config
from theODDS.p_helpers import pinnacle_odds, get_api_usage
from KALSHI.k_helpers   import kalshi_odds, TAKER_FEE_BASE, MAKER_FEE_BASE
from trade.core.execution import (cross_and_cancel_order, cancel_and_rerest, cancel_all_resting_orders, kelly_contracts, extend_resting_order, _ev)
from trade.core.positions import open_tickers, opposite_leg_blocked
from trade.strategies.kp_arb import run_all_signals, resume_monitoring
from trade.pinboard.board import PinBoard
from trade.pinboard.ui import render_board as _render_pin_board
from dashboard          import StreamlitDashboard
from applog             import get_logger
from trade.ui.common import (_fragment_rerun, _in_season, _entry_price_series, _eop_quartile_cutoffs, _Q_LABELS, taker_fee, maker_fee)
from trade.ui.startup import _live_cash, _cached_event_counts, _load_active_sports

log = get_logger(__name__)


def render():

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
        # Read the top-bar / sports-picker values fresh on every run of THIS fragment — a closure over the module-level
        # names would be frozen at the last full-page run, and those widgets no longer trigger one.
        hrs       = st.session_state.get('_hrs', config.LOOKAHEAD_HRS)
        threshold = st.session_state.get('_threshold', 0.85)
        live      = st.session_state.get('_live', config.LIVE)
        balance   = _live_cash()
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
                                # Stop a batch that's still firing first, so its remaining waves are never sent
                                # (and its monitors cancel what they placed), then sweep everything resting.
                                _running_stop = st.session_state.get('_trade_stop')
                                if _running_stop is not None:
                                    _running_stop.set()
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
                    _exposure_unknown = _pending_tickers is None
                    if _exposure_unknown:
                        # Couldn't verify current Kalshi exposure (network/API issue) — fail CLOSED: block every
                        # row from auto-execute rather than defaulting to "not pending", which would let Execute
                        # fire on markets we might already hold. The checkbox itself still works if the user
                        # wants to override with their own judgement.
                        st.warning('Could not verify current Kalshi exposure (network/API issue) — nothing below '
                                  'is pre-approved to execute until this is confirmed. Tick a row yourself only '
                                  'if you are sure it is not already open.')
                        _pending_tickers = set()
                    already_traded = editable['k_ticker'].isin(_pending_tickers)
                    # Opposite leg of a 2-way event we already hold (YES A ≡ NO B) — same
                    # bet on another ticker; run_all_signals() skips these too.
                    _opp_leg = editable['k_ticker'].isin(
                        opposite_leg_blocked(editable['k_ticker'].tolist(), _pending_tickers)
                    ) & ~already_traded
                    editable.insert(1, 'Status', already_traded.map({True: 'pending', False: ''})
                                    .mask(_opp_leg, 'opp. leg held'))
                    already_traded = already_traded | _opp_leg
                    if _exposure_unknown:
                        already_traded[:] = True

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
                            st.error('Kalshi balance unavailable (the Cash readout can\'t reach Kalshi) — try again shortly.')
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
                            _old_board = st.session_state.pop('_trade_board', None)
                            if _old_board is not None:
                                _old_board.stop()
                            st.session_state['_trade_board'] = PinBoard(dash, stop_event)

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
                pin_board     = st.session_state.get('_trade_board')
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
                                              help='Push back the REAL Kalshi Good-till time on every resting '
                                                   'order by 15 minutes (cancels and re-rests each one at its '
                                                   'current price for whatever is still unfilled).'):
                                    _ext_snap = dash.snapshot()
                                    _EXT_RESTING = {'resting', 'open', 'pending', 'unknown'}
                                    _ext_targets = [(oid, pos) for oid, pos in _ext_snap['positions'].items()
                                                   if pos.get('status') in _EXT_RESTING
                                                   and pos.get('contracts', 0) - pos.get('filled', 0) > 0]
                                    if not _ext_targets:
                                        st.session_state['_dash_last_action'] = {
                                            'type': 'extend_empty',
                                            'ts':   datetime.now().strftime('%H:%M:%S'),
                                        }
                                    else:
                                        dash.extend_time(15 * 60)   # keep the monitor loop's own clock in sync too
                                        _ext_results = []
                                        for _oid, _pos in _ext_targets:
                                            _r = extend_resting_order(_pos['ticker'], _oid, _pos.get('commence', ''))
                                            _ext_results.append((_pos['ticker'], _r))
                                            if _r['action'] == 'resized':
                                                _r_price = _pos['entry_price'] / 100
                                                _r_fee_rate = _pos.get('fee_rate', MAKER_FEE_BASE)
                                                _r_fair = _pos.get('fair_last', _pos.get('fair_entry', 0.5))
                                                threading.Thread(
                                                    target=resume_monitoring,
                                                    kwargs=dict(
                                                        order_id=_r['new_order_id'], ticker=_pos['ticker'],
                                                        event_id=_pos.get('event_id', ''), sport=_pos.get('sport', ''),
                                                        outcome=_pos.get('raw_outcome', _pos['outcome']),
                                                        order_price=_r_price, fee_rate=_r_fee_rate,
                                                        commence=_pos.get('commence', ''), side=_pos['side'],
                                                        contracts=_r['new_remaining'], fair_prob=_r_fair,
                                                        ev_per_contract=_ev(_r_fair, _r_price, _r_fee_rate),
                                                        dashboard=dash, stop_event=stop_event,
                                                    ),
                                                    daemon=True,
                                                ).start()
                                        _ext_err = sum(1 for _, r in _ext_results if r['action'] == 'error')
                                        if _ext_err:
                                            log.warning('+15 min: %d error(s) — %s', _ext_err,
                                                       [r for _, r in _ext_results if r['action'] == 'error'])
                                        st.session_state['_dash_last_action'] = {
                                            'type':    'extend',
                                            'ts':      datetime.now().strftime('%H:%M:%S'),
                                            'minutes': 15,
                                            'results': _ext_results,
                                        }

                    # Render whatever the last action was — persists across the 1s
                    # auto-refresh instead of disappearing after one script run.
                    _last_action = st.session_state.get('_dash_last_action')
                    if _last_action:
                        _atype, _ats = _last_action['type'], _last_action['ts']
                        if _atype == 'cancel_all':
                            st.warning(f'[{_ats}] Cancelled — live orders are being closed by their monitors; '
                                       f'any orders still queued in this batch will not be sent.')
                        elif _atype == 'cross_cancel_empty':
                            st.info(f'[{_ats}] No resting orders to process.')
                        elif _atype == 'keep_rest_empty':
                            st.info(f'[{_ats}] No unfilled resting orders to keep.')
                        elif _atype == 'extend_empty':
                            st.info(f'[{_ats}] No resting orders to extend.')
                        elif _atype == 'extend':
                            _ext_results = _last_action.get('results', [])
                            _ext_ok = sum(1 for _, r in _ext_results if r['action'] == 'resized')
                            _ext_err = sum(1 for _, r in _ext_results if r['action'] == 'error')
                            with st.expander(
                                f"[{_ats}] +{_last_action['minutes']}m — {_ext_ok} extended on Kalshi · "
                                f"{_ext_err} errors — total extension now +{_extra_sec // 60}m",
                                expanded=bool(_ext_err),
                            ):
                                for _tkr, _r in _ext_results:
                                    if _r['action'] == 'resized':
                                        st.success(f"{_tkr}  Good-till → {_r.get('new_expiry', '?')}")
                                    elif _r['action'] == 'skipped':
                                        st.caption(f"— {_tkr}  {_r.get('reason', 'skipped')}")
                                    else:
                                        st.error(f"{_tkr}  {_r.get('reason', 'error')}")
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
                @st.fragment(run_every='5s' if is_alive else None)
                def _positions_panel():
                    if dash is None:
                        return
                    snap = dash.snapshot()

                    # Positions table — wrapped: one malformed row must not repeatedly
                    # break the whole fragment.
                    try:
                        positions = snap['positions']
                        if positions:
                            # The live board: one card per market (5-level book, drag to re-price, filled / avg boxes,
                            # Cancel, fill notifications). It updates itself over its own channel between reruns.
                            if pin_board is not None:
                                _render_pin_board(pin_board)
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

                            with st.expander('Table view', expanded=False):
                                positions_df = pd.DataFrame(rows)
                                styled = positions_df.style.apply(_fill_color, axis=1)
                                st.dataframe(styled, width="stretch", hide_index=True,
                                            height=min(35 * n + 38, 600))
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
                    _done_board = st.session_state.pop('_trade_board', None)
                    if _done_board is not None:
                        _done_board.stop()
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
