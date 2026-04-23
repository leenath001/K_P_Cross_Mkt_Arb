"""
nothing_review.py — Analytics for the "Nothing Ever Happens" NO-side bot.

Called by:
    python trade/review.py          (appended after the main K/P review)
    trade/web_app.py Nothing tab    (via render_nothing_charts)
    python trade/nothing_review.py  (standalone)
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from rich.console import Console
from rich.table   import Table
from rich         import box

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'nothing_trades.csv')

console = Console()


# ── Load ──────────────────────────────────────────────────────────────────────

def load_log() -> pd.DataFrame:
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return pd.DataFrame()
    df = pd.read_csv(LOG_PATH)
    df['actual_pnl']   = pd.to_numeric(df['actual_pnl'],   errors='coerce')
    df['entry_price']  = pd.to_numeric(df['entry_price'],  errors='coerce')
    df['total_cost']   = pd.to_numeric(df['total_cost'],   errors='coerce')
    return df


# ── Metrics dict (used by web_app) ────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    filled  = df[df['final_status'].isin(['executed', 'filled'])]
    settled = filled[filled['result'].isin(['WIN', 'LOSS', 'VOID'])]
    wins    = settled[settled['result'] == 'WIN']
    losses  = settled[settled['result'] == 'LOSS']
    pending = filled[filled['result'] == 'PENDING']

    wagered     = filled['total_cost'].sum()
    pnl         = settled['actual_pnl'].sum()
    win_rate    = len(wins) / len(settled) if len(settled) > 0 else None
    emp_ev_bet  = settled['actual_pnl'].mean() if not settled.empty else None  # avg $/bet
    emp_ev_dol  = pnl / wagered if wagered > 0 else None                       # ROI
    avg_price   = filled['entry_price'].mean() if not filled.empty else None

    by_series = None
    if not settled.empty and 'series_ticker' in settled.columns:
        by_series = (
            settled.groupby('series_ticker')
            .agg(
                bets   =('result', 'count'),
                wins   =('result', lambda x: (x == 'WIN').sum()),
                losses =('result', lambda x: (x == 'LOSS').sum()),
                pnl    =('actual_pnl', 'sum'),
                wagered=('total_cost', lambda x: x.sum()),
            )
            .assign(
                win_rate=lambda d: d['wins'] / d['bets'],
                roi     =lambda d: d['pnl'] / d['wagered'].replace(0, float('nan')),
            )
            .reset_index()
        )

    return {
        'total':       len(df),
        'filled':      len(filled),
        'settled':     len(settled),
        'pending':     len(pending),
        'wins':        len(wins),
        'losses':      len(losses),
        'win_rate':    win_rate,
        'pnl':         pnl,
        'wagered':     wagered,
        'emp_ev_bet':  emp_ev_bet,
        'emp_ev_dol':  emp_ev_dol,
        'avg_price':   avg_price,
        'by_series':   by_series,
        'filled_df':   filled,
        'settled_df':  settled,
    }


# ── Terminal summary ──────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame):
    m = compute_metrics(df)
    if not m:
        console.print('[yellow]Nothing trades log is empty.[/yellow]')
        return

    console.rule('[bold magenta]Nothing Ever Happens — NO-Side Bot Review[/bold magenta]')
    console.print(
        f'  Bets placed: [cyan]{m["total"]}[/cyan]  |  '
        f'Filled: [cyan]{m["filled"]}[/cyan]  |  '
        f'Settled: [cyan]{m["settled"]}[/cyan]  |  '
        f'Pending: [cyan]{m["pending"]}[/cyan]'
    )

    wr_s   = f'{m["win_rate"]:.0%}' if m['win_rate'] is not None else '—'
    ev_s   = f'${m["emp_ev_bet"]:+.4f}/bet' if m['emp_ev_bet'] is not None else '—'
    roi_s  = f'{m["emp_ev_dol"]:.1%}' if m['emp_ev_dol'] is not None else '—'
    pnl_c  = 'green' if m['pnl'] >= 0 else 'red'
    console.print(
        f'  PnL: [{pnl_c}]${m["pnl"]:+.2f}[/{pnl_c}]  |  '
        f'Wagered: [cyan]${m["wagered"]:.2f}[/cyan]  |  '
        f'Win rate: [cyan]{wr_s}[/cyan]  |  '
        f'Empirical EV: [cyan]{ev_s}[/cyan]  |  '
        f'ROI: [cyan]{roi_s}[/cyan]'
    )
    console.print(
        f'  Avg entry price: [cyan]{m["avg_price"]:.0%}[/cyan]' if m['avg_price'] else ''
    )
    console.print()

    # Per-series breakdown
    if m['by_series'] is not None and not m['by_series'].empty:
        tbl = Table(box=box.SIMPLE_HEAD, expand=False, padding=(0, 1))
        tbl.add_column('Series',    width=28)
        tbl.add_column('Bets',      justify='right', width=5)
        tbl.add_column('W',         justify='right', width=4)
        tbl.add_column('L',         justify='right', width=4)
        tbl.add_column('Win Rate',  justify='right', width=9)
        tbl.add_column('PnL',       justify='right', width=9)
        tbl.add_column('ROI',       justify='right', width=7)
        for _, row in m['by_series'].iterrows():
            wr   = row['win_rate']
            wr_c = 'green' if wr >= 0.5 else 'red'
            p    = row['pnl']
            p_c  = 'green' if p >= 0 else 'red'
            roi  = row['roi'] if not pd.isna(row['roi']) else float('nan')
            tbl.add_row(
                row['series_ticker'],
                str(int(row['bets'])),
                str(int(row['wins'])),
                str(int(row['losses'])),
                f'[{wr_c}]{wr:.0%}[/{wr_c}]',
                f'[{p_c}]${p:+.2f}[/{p_c}]',
                f'{roi:.1%}' if not pd.isna(roi) else '—',
            )
        console.print(tbl)
        console.print()

    # Trade log table
    settled_df = m['settled_df']
    if settled_df.empty:
        console.print('  [dim]No settled trades yet.[/dim]')
        return

    tbl2 = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
    tbl2.add_column('Date',      width=12)
    tbl2.add_column('Ticker',    width=30)
    tbl2.add_column('Series',    width=18)
    tbl2.add_column('Mode',      width=6)
    tbl2.add_column('Price',     justify='right', width=6)
    tbl2.add_column('Cts',       justify='right', width=4)
    tbl2.add_column('Cost',      justify='right', width=7)
    tbl2.add_column('Result',    width=6)
    tbl2.add_column('PnL',       justify='right', width=8)

    RESULT_C = {'WIN': 'bold green', 'LOSS': 'red', 'VOID': 'dim'}
    for _, r in settled_df.sort_values('logged_at').iterrows():
        date = str(r.get('logged_at', ''))[:10]
        rc   = RESULT_C.get(str(r['result']), 'white')
        pc   = 'green' if r['actual_pnl'] >= 0 else 'red'
        tbl2.add_row(
            date,
            str(r.get('k_ticker', ''))[-30:],
            str(r.get('series_ticker', '')),
            str(r.get('mode', '')),
            f'{r["entry_price_cents"]}¢',
            str(int(r.get('contracts', 1))),
            f'${r["total_cost"]:.2f}',
            f'[{rc}]{r["result"]}[/{rc}]',
            f'[{pc}]${r["actual_pnl"]:+.4f}[/{pc}]',
        )
    console.print(tbl2)


# ── Charts (shared by terminal and web_app via fig return) ────────────────────

def build_charts(df: pd.DataFrame):
    """
    Returns a matplotlib Figure, or None if no data.
    Caller is responsible for showing (plt.show) or rendering (st.pyplot).
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import numpy as np
    except ImportError:
        return None

    m = compute_metrics(df)
    if not m or m['settled'] == 0:
        return None

    settled = m['settled_df'].copy().sort_values('logged_at').reset_index(drop=True)
    filled  = m['filled_df'].copy()

    fig = plt.figure(figsize=(14, 9), facecolor='white', layout='constrained')
    fig.suptitle('Nothing Ever Happens — NO-Side Bot', fontsize=13,
                 fontweight='bold', color='black')
    gs = gridspec.GridSpec(2, 3, figure=fig)

    def _style(ax, title):
        ax.set_title(title, color='black', fontsize=10)
        ax.set_facecolor('white')
        ax.tick_params(colors='black')
        ax.xaxis.label.set_color('black')
        ax.yaxis.label.set_color('black')
        for sp in ax.spines.values():
            sp.set_edgecolor('#ccc')

    # ── 1. Cumulative PnL ────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :2])
    cum = settled['actual_pnl'].cumsum().values
    xs  = range(len(cum))
    color_line = '#2ecc71' if cum[-1] >= 0 else '#e74c3c'
    ax1.plot(xs, cum, color=color_line, linewidth=2, marker='o', markersize=4)
    ax1.axhline(0, color='black', linewidth=0.6, linestyle='--')
    ax1.fill_between(xs, cum, 0,
                     where=(np.array(cum) >= 0), alpha=0.15, color='#2ecc71')
    ax1.fill_between(xs, cum, 0,
                     where=(np.array(cum) < 0),  alpha=0.15, color='#e74c3c')
    ax1.set_xlabel('Settled trade #')
    ax1.set_ylabel('Cumulative PnL ($)')
    _style(ax1, f'Cumulative PnL  (final: ${cum[-1]:+.2f})')

    # ── 2. Win rate by series ────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 2])
    if m['by_series'] is not None and not m['by_series'].empty:
        bs      = m['by_series'].sort_values('win_rate', ascending=False)
        labels  = [s.replace('KXEARNINGSMEN', 'ERN_').replace('KXTRUMPMENTION', 'TRUMP') for s in bs['series_ticker']]
        wr_vals = bs['win_rate'].values
        bar_c   = ['#2ecc71' if w >= 0.5 else '#e74c3c' for w in wr_vals]
        y = np.arange(len(labels))
        ax2.barh(y, wr_vals, color=bar_c, height=0.5)
        ax2.axvline(0.5, color='gray', linewidth=1, linestyle='--')
        ax2.set_xlim(0, 1.1)
        ax2.set_yticks(y)
        ax2.set_yticklabels(labels, fontsize=8)
        ax2.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
        for i, (wr, n) in enumerate(zip(wr_vals, bs['bets'].values)):
            ax2.text(wr + 0.02, i, f'{wr:.0%} (n={int(n)})', va='center', fontsize=7)
    _style(ax2, 'Win Rate by Series')

    # ── 3. PnL per bet bar ───────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    pnl_vals = settled['actual_pnl'].values
    bar_c    = ['#2ecc71' if v >= 0 else '#e74c3c' for v in pnl_vals]
    ax3.bar(range(len(pnl_vals)), pnl_vals, color=bar_c, width=0.8)
    ax3.axhline(0, color='black', linewidth=0.6, linestyle='--')
    emp_ev = np.mean(pnl_vals)
    ax3.axhline(emp_ev, color='steelblue', linewidth=1.2, linestyle=':',
                label=f'mean={emp_ev:+.4f}')
    ax3.set_xlabel('Trade #')
    ax3.set_ylabel('PnL ($)')
    ax3.legend(fontsize=8)
    _style(ax3, 'PnL per Bet')

    # ── 4. Win rate by entry price bucket ────────────────────────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    bins   = [0, 0.15, 0.25, 0.35, 0.50, 1.0]
    labels = ['≤15¢', '16-25¢', '26-35¢', '36-50¢', '>50¢']
    settled['price_bucket'] = pd.cut(settled['entry_price'], bins=bins, labels=labels)
    bucket_stats = (
        settled.groupby('price_bucket', observed=True)
        .agg(win_rate=('result', lambda x: (x == 'WIN').mean()),
             n=('result', 'count'))
        .reset_index()
    )
    if not bucket_stats.empty:
        bc = ['#2ecc71' if w >= 0.5 else '#e74c3c' for w in bucket_stats['win_rate']]
        ax4.bar(range(len(bucket_stats)), bucket_stats['win_rate'], color=bc, width=0.6)
        ax4.axhline(0.5, color='gray', linewidth=1, linestyle='--')
        ax4.set_ylim(0, 1.15)
        ax4.set_xticks(range(len(bucket_stats)))
        ax4.set_xticklabels(bucket_stats['price_bucket'], fontsize=8)
        ax4.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
        for i, row in bucket_stats.iterrows():
            if row['n'] > 0:
                ax4.text(i, row['win_rate'] + 0.04,
                         f'{row["win_rate"]:.0%}\nn={int(row["n"])}',
                         ha='center', fontsize=7, color='black')
    _style(ax4, 'Win Rate by Entry Price')

    # ── 5. Empirical EV summary card ─────────────────────────────────────────
    ax5 = fig.add_subplot(gs[1, 2])
    ax5.axis('off')
    lines = [
        ('Bets settled',     f'{m["settled"]}'),
        ('Wins / Losses',    f'{m["wins"]} / {m["losses"]}'),
        ('Win rate',         f'{m["win_rate"]:.1%}' if m['win_rate'] is not None else '—'),
        ('Total PnL',        f'${m["pnl"]:+.2f}'),
        ('Total wagered',    f'${m["wagered"]:.2f}'),
        ('Emp. EV / bet',    f'${m["emp_ev_bet"]:+.4f}' if m['emp_ev_bet'] is not None else '—'),
        ('ROI',              f'{m["emp_ev_dol"]:.1%}' if m['emp_ev_dol'] is not None else '—'),
        ('Avg entry price',  f'{m["avg_price"]:.0%}' if m['avg_price'] is not None else '—'),
    ]
    y0 = 0.95
    ax5.text(0.5, 1.02, 'Summary', ha='center', va='top', fontsize=10,
             fontweight='bold', color='black', transform=ax5.transAxes)
    for label, val in lines:
        ax5.text(0.05, y0, label,  ha='left',  va='top', fontsize=9, color='#555')
        ax5.text(0.95, y0, val,    ha='right', va='top', fontsize=9,
                 color='black', fontweight='bold')
        y0 -= 0.11
    _style(ax5, '')

    return fig


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--table', action='store_true', help='Table only, no charts')
    args = parser.parse_args()

    df = load_log()
    print_summary(df)
    if not args.table:
        fig = build_charts(df)
        if fig:
            import matplotlib.pyplot as plt
            plt.show()
        else:
            console.print('[dim]No settled trades yet — nothing to chart.[/dim]')
