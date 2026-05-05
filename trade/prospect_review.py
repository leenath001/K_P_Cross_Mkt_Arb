"""
prospect_review.py — Analytics for the Prospect Theory strategy.

Called by:
    python trade/review.py           (appended after K/P and Nothing reviews)
    trade/web_app.py Prospect tab    (via build_charts / compute_metrics)
    python trade/prospect_review.py  (standalone)
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from rich.console import Console
from rich.table   import Table
from rich         import box

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'prospect_trades.csv')

console = Console()


# ── Load ──────────────────────────────────────────────────────────────────────

def load_log() -> pd.DataFrame:
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return pd.DataFrame()
    df = pd.read_csv(LOG_PATH)
    for col in ('actual_pnl', 'entry_price', 'total_cost', 'ev_per_contract',
                'ev_total', 'fair_prob', 'edge'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


# ── Metrics dict (used by web_app) ────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    filled  = df[df['final_status'].isin(['executed', 'filled'])]
    settled = filled[filled['result'].isin(['WIN', 'LOSS', 'VOID', 'SCALAR'])]
    wins    = settled[settled['result'] == 'WIN']
    losses  = settled[settled['result'] == 'LOSS']
    pending = filled[filled['result'] == 'PENDING']

    wagered    = filled['total_cost'].sum()
    pnl        = settled['actual_pnl'].sum()
    win_rate   = len(wins) / len(settled) if len(settled) > 0 else None
    emp_ev_bet = settled['actual_pnl'].mean() if not settled.empty else None
    emp_ev_dol = pnl / wagered if wagered > 0 else None
    avg_price  = filled['entry_price'].mean() if not filled.empty else None

    by_zone = None
    if not settled.empty and 'pt_zone' in settled.columns:
        by_zone = (
            settled.groupby('pt_zone')
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

    by_side = None
    if not settled.empty and 'pt_side' in settled.columns:
        by_side = (
            settled.groupby('pt_side')
            .agg(
                bets   =('result', 'count'),
                wins   =('result', lambda x: (x == 'WIN').sum()),
                losses =('result', lambda x: (x == 'LOSS').sum()),
                pnl    =('actual_pnl', 'sum'),
            )
            .assign(win_rate=lambda d: d['wins'] / d['bets'])
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
        'by_zone':     by_zone,
        'by_side':     by_side,
        'filled_df':   filled,
        'settled_df':  settled,
    }


# ── Terminal summary ──────────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame):
    m = compute_metrics(df)
    if not m:
        console.print('[yellow]Prospect trades log is empty.[/yellow]')
        return

    console.rule('[bold cyan]Prospect Theory Strategy Review[/bold cyan]')
    console.print(
        f'  Bets placed: [cyan]{m["total"]}[/cyan]  |  '
        f'Filled: [cyan]{m["filled"]}[/cyan]  |  '
        f'Settled: [cyan]{m["settled"]}[/cyan]  |  '
        f'Pending: [cyan]{m["pending"]}[/cyan]'
    )

    wr_s  = f'{m["win_rate"]:.0%}' if m['win_rate'] is not None else '—'
    ev_s  = f'${m["emp_ev_bet"]:+.4f}/bet' if m['emp_ev_bet'] is not None else '—'
    roi_s = f'{m["emp_ev_dol"]:.1%}' if m['emp_ev_dol'] is not None else '—'
    pnl_c = 'green' if m['pnl'] >= 0 else 'red'
    console.print(
        f'  PnL: [{pnl_c}]${m["pnl"]:+.2f}[/{pnl_c}]  |  '
        f'Wagered: [cyan]${m["wagered"]:.2f}[/cyan]  |  '
        f'Win rate: [cyan]{wr_s}[/cyan]  |  '
        f'Emp. EV: [cyan]{ev_s}[/cyan]  |  '
        f'ROI: [cyan]{roi_s}[/cyan]'
    )
    console.print()

    # Per-zone breakdown
    if m['by_zone'] is not None and not m['by_zone'].empty:
        tbl = Table(box=box.SIMPLE_HEAD, expand=False, padding=(0, 1))
        tbl.add_column('Zone',     width=12)
        tbl.add_column('Bets',     justify='right', width=5)
        tbl.add_column('W',        justify='right', width=4)
        tbl.add_column('L',        justify='right', width=4)
        tbl.add_column('Win Rate', justify='right', width=9)
        tbl.add_column('PnL',      justify='right', width=9)
        tbl.add_column('ROI',      justify='right', width=7)
        for _, row in m['by_zone'].iterrows():
            wr  = row['win_rate']
            wrc = 'green' if wr >= 0.5 else 'red'
            p   = row['pnl']
            pc  = 'green' if p >= 0 else 'red'
            roi = row['roi'] if not pd.isna(row['roi']) else float('nan')
            tbl.add_row(
                str(row['pt_zone']),
                str(int(row['bets'])),
                str(int(row['wins'])),
                str(int(row['losses'])),
                f'[{wrc}]{wr:.0%}[/{wrc}]',
                f'[{pc}]${p:+.2f}[/{pc}]',
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
    tbl2.add_column('Date',    width=12)
    tbl2.add_column('Ticker',  width=30)
    tbl2.add_column('Zone',    width=10)
    tbl2.add_column('Side',    width=5)
    tbl2.add_column('Price',   justify='right', width=6)
    tbl2.add_column('Cts',     justify='right', width=4)
    tbl2.add_column('Cost',    justify='right', width=7)
    tbl2.add_column('Result',  width=6)
    tbl2.add_column('PnL',     justify='right', width=8)

    RESULT_C = {'WIN': 'bold green', 'LOSS': 'red', 'VOID': 'dim', 'SCALAR': 'cyan'}
    for _, r in settled_df.sort_values('logged_at').iterrows():
        date = str(r.get('logged_at', ''))[:10]
        rc   = RESULT_C.get(str(r['result']), 'white')
        pc   = 'green' if r['actual_pnl'] >= 0 else 'red'
        zone = str(r.get('pt_zone', ''))
        side = str(r.get('pt_side', ''))
        tbl2.add_row(
            date,
            str(r.get('k_ticker', ''))[-30:],
            zone,
            side.upper(),
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

    ZONE_COLOR = {'favorite': '#2ecc71', 'longshot': '#3498db'}
    SIDE_COLOR = {'yes': '#2ecc71', 'no': '#e67e22'}

    fig = plt.figure(figsize=(14, 9), facecolor='white', layout='constrained')
    fig.suptitle('Prospect Theory Strategy', fontsize=13,
                 fontweight='bold', color='black')
    gs = gridspec.GridSpec(2, 2, figure=fig)

    def _style(ax, title):
        ax.set_title(title, color='black', fontsize=10)
        ax.set_facecolor('white')
        ax.tick_params(colors='black')
        ax.xaxis.label.set_color('black')
        ax.yaxis.label.set_color('black')
        for sp in ax.spines.values():
            sp.set_edgecolor('#ccc')

    # ── 1. Edge per trade (colored by zone) ──────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    if 'edge' in settled.columns:
        edges    = settled['edge'].fillna(0).values
        zones    = settled['pt_zone'].fillna('').values
        bar_cols = [ZONE_COLOR.get(z, '#aaa') for z in zones]
        ax1.bar(range(len(edges)), edges, color=bar_cols, width=0.8)
        ax1.axhline(0, color='black', linewidth=0.6, linestyle='--')
        mean_edge = np.mean(edges)
        ax1.axhline(mean_edge, color='gray', linewidth=1, linestyle=':',
                    label=f'mean={mean_edge:+.4f}')
        ax1.legend(fontsize=8,
                   handles=[
                       plt.Rectangle((0, 0), 1, 1, color='#2ecc71', label='favorite'),
                       plt.Rectangle((0, 0), 1, 1, color='#3498db', label='longshot'),
                   ])
    ax1.set_xlabel('Trade #')
    ax1.set_ylabel('Edge')
    _style(ax1, 'Edge per Trade by Zone')

    # ── 2. Cumulative EV vs actual PnL by zone ────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    for zone, zc in ZONE_COLOR.items():
        z_df = settled[settled['pt_zone'] == zone].reset_index(drop=True)
        if z_df.empty:
            continue
        cum_pnl = z_df['actual_pnl'].cumsum().values
        cum_ev  = z_df['ev_total'].cumsum().values if 'ev_total' in z_df.columns else None
        xs = range(len(cum_pnl))
        ax2.plot(xs, cum_pnl, color=zc, linewidth=2, marker='o', markersize=3,
                 label=f'{zone} PnL')
        if cum_ev is not None:
            ax2.plot(xs, cum_ev, color=zc, linewidth=1.2, linestyle='--',
                     label=f'{zone} EV')
    ax2.axhline(0, color='black', linewidth=0.6, linestyle='--')
    ax2.set_xlabel('Settled trade #')
    ax2.set_ylabel('Cumulative ($)')
    ax2.legend(fontsize=8)
    _style(ax2, 'Cumulative EV vs PnL by Zone')

    # ── 3. Win rate by zone ───────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    if m['by_zone'] is not None and not m['by_zone'].empty:
        bz     = m['by_zone']
        labels = bz['pt_zone'].tolist()
        wr_v   = bz['win_rate'].values
        bar_c  = [ZONE_COLOR.get(z, '#aaa') for z in labels]
        ax3.bar(labels, wr_v, color=bar_c, width=0.5)
        ax3.axhline(0.5, color='gray', linewidth=1, linestyle='--')
        ax3.set_ylim(0, 1.2)
        ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
        for i, (wr, row) in enumerate(zip(wr_v, bz.itertuples())):
            pnl_s = f'${row.pnl:+.2f}'
            ax3.text(i, wr + 0.05,
                     f'{wr:.0%}  n={int(row.bets)}\n{pnl_s}',
                     ha='center', fontsize=8, color='black')
    _style(ax3, 'Win Rate by Zone')

    # ── 4. Win rate by side ───────────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    if m['by_side'] is not None and not m['by_side'].empty:
        bs     = m['by_side']
        labels = bs['pt_side'].tolist()
        wr_v   = bs['win_rate'].values
        bar_c  = [SIDE_COLOR.get(s, '#aaa') for s in labels]
        ax4.bar(labels, wr_v, color=bar_c, width=0.5)
        ax4.axhline(0.5, color='gray', linewidth=1, linestyle='--')
        ax4.set_ylim(0, 1.2)
        ax4.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
        for i, (wr, row) in enumerate(zip(wr_v, bs.itertuples())):
            pnl_s = f'${row.pnl:+.2f}'
            ax4.text(i, wr + 0.05,
                     f'{wr:.0%}  n={int(row.bets)}\n{pnl_s}',
                     ha='center', fontsize=8, color='black')
    _style(ax4, 'Win Rate by Side (YES vs NO)')

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
