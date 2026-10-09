"""
trade/ui/review_tab.py — Pinnacle → Review: auto-settling, live trades, performance metrics and the analysis charts.
"""
import time
import streamlit as st
import pandas as pd
from trade.clv import load_closing_lines
import settle
from applog             import get_logger
from trade.ui.common import _fragment_rerun, _load_all_logs, LOG_PATH, NO_LOG_PATH
from trade.ui.analytics import open_trade_outcomes as _open_trade_outcomes

log = get_logger(__name__)


def render():

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
            # SCALAR settles at a fractional value with a real PnL too (settle.compute_scalar_pnl), so it counts.
            settled = filled[filled['result'].isin(['WIN', 'LOSS', 'CLOSED_EARLY', 'SCALAR'])].copy()
            settled['actual_pnl'] = pd.to_numeric(settled['actual_pnl'], errors='coerce')
            # is_win is PnL-based, not label-based, so a profitable early close counts as
            # a win and a losing one counts as a loss — same rule WIN/LOSS already follow
            # by construction (compute_pnl() only labels WIN when pnl >= 0).
            settled['is_win'] = (settled['actual_pnl'] >= 0).astype(int)

            # Same trades as Actual PnL (settled ones) so the two are comparable — open trades' EV isn't realized yet.
            total_ev   = pd.to_numeric(settled['ev_total'], errors='coerce').sum() if not settled.empty else 0
            actual_pnl = settled['actual_pnl'].sum()   if not settled.empty else 0
            win_rate   = settled['is_win'].mean() if not settled.empty else None
            total_cost = filled['total_cost'].sum()    if not filled.empty  else 0

            # ── Summary metrics ──────────────────────────────────────────────────
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric('Total placed',  len(log_df2))
            c2.metric('Filled',        len(filled))
            c3.metric('Settled',       len(settled))
            c4.metric('Projected EV',  f'${total_ev:+.2f}',
                      help='Expected PnL at entry for the settled trades — the same trades as Actual PnL.')
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
                        _done = sub[_pnl.notna() & sub['result'].isin(['WIN', 'LOSS', 'CLOSED_EARLY', 'SCALAR'])] \
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
                            # Where PnL could end once the live (unsettled) trades resolve: an exact distribution,
                            # drawn sideways just right of the last realized point.
                            _dist = _open_trade_outcomes(live_trades)
                            if _dist is not None:
                                _vals, _probs, _exp = _dist
                                _x0, _y0 = len(_xs), float(_cum_real.iloc[-1])
                                _w = max(4.0, 0.12 * len(_xs))           # widest bar, in trade-# units
                                _keep = _probs >= 1e-4                   # drop vanishingly unlikely tails
                                _v, _pr = _vals[_keep], _probs[_keep]
                                _cdf = np.cumsum(_probs)
                                _lo = _vals[np.searchsorted(_cdf, 0.05)]
                                _hi = _vals[min(np.searchsorted(_cdf, 0.95), len(_vals) - 1)]
                                _step = float(np.min(np.diff(_v))) if len(_v) > 1 else 1.0
                                fig2.add_trace(go.Bar(
                                    y=_y0 + _v, x=_pr / _pr.max() * _w, base=_x0, orientation='h',
                                    width=0.8 * _step, marker_color='rgba(155,89,182,0.55)',
                                    name=f'If the {len(live_trades)} live trades settle (90%: {_lo:+.2f} to {_hi:+.2f})',
                                    customdata=np.stack([_pr * 100, _v], axis=1),
                                    hovertemplate='%{customdata[0]:.1f}% chance → PnL ends at $%{y:.2f} '
                                                  '(%{customdata[1]:+.2f})<extra></extra>'))
                                fig2.add_trace(go.Scatter(
                                    x=[_x0 - 1, _x0], y=[_y0, _y0 + _exp], mode='lines+markers',
                                    line=dict(color='#9b59b6', width=1.5, dash='dot'),
                                    marker=dict(size=[0, 9], symbol='diamond'), name='Expected after live trades',
                                    hovertemplate='Expected: $%{y:.2f}<extra></extra>'))
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
                            # predicted probability vs how often it happened: only real resolutions count (an early
                            # close or a scalar settlement isn't a win/lose outcome of the event)
                            calib_src = settled[settled['result'].isin(['WIN', 'LOSS'])].copy()
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
                                # Quartiles split the trades into four EQUAL-size groups, so n is the same in each bar
                                # by construction; what differs is the edge÷price range each one covers (shown).
                                q = eop_src.groupby('eop_quartile', observed=True).agg(
                                    n=('is_win', 'size'), win_rate=('is_win', 'mean'),
                                    predicted=('fair_prob', 'mean'),
                                    lo=('edge_over_price', 'min'), hi=('edge_over_price', 'max'))
                                # Bars: actual win rate. Diamonds: what the model predicted for the same trades. High
                                # edge÷price usually means a cheap longshot, which SHOULD lose most of the time — the
                                # gap between bar and diamond, not the bar alone, says whether a quartile underperforms.
                                _txt = [f'{wr:.0%} won vs {pr:.0%} predicted<br>edge÷price {lo:.3f}–{hi:.3f}<br>n={n}'
                                        for wr, pr, n, lo, hi in zip(q['win_rate'], q['predicted'], q['n'], q['lo'], q['hi'])]
                                _bcolors = ['#2ecc71' if wr >= pr else '#e74c3c' for wr, pr in zip(q['win_rate'], q['predicted'])]
                                fig6.add_trace(go.Bar(x=q.index.astype(str), y=q['win_rate'],
                                                      marker_color=_bcolors, text=_txt,
                                                      textposition='outside', hoverinfo='text',
                                                      hovertext=_txt, name='actual win rate'))
                                fig6.add_trace(go.Scatter(x=q.index.astype(str), y=q['predicted'], mode='markers',
                                                          marker=dict(symbol='diamond', size=12, color='#f5b301',
                                                                      line=dict(width=1, color='white')),
                                                          name='predicted (avg fair_prob)',
                                                          hovertemplate='predicted %{y:.1%}<extra></extra>'))
                                fig6.update_layout(yaxis_tickformat='.0%', yaxis_range=[0, 1.4])
                            except ValueError:
                                fig6.add_annotation(text='Not enough spread to quartile', **_NO_DATA)
                        else:
                            fig6.add_annotation(text='Not enough settled trades yet', **_NO_DATA)
                        fig6.update_layout(title='Win Rate vs Predicted by Edge÷Price Quartile',
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
