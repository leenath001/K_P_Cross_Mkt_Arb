"""
review.py — Visual review of trade log for edge realization analysis.

Usage:
    python trade/review.py             # summary table + charts
    python trade/review.py --table     # Rich table only (no charts)

Filling in results:
    After an event resolves, update the CSV manually:
      result     → WIN or LOSS
      actual_pnl → contracts * (1 - entry_price) * (1 - fee_rate)   if WIN
                   contracts * (-entry_price)                         if LOSS
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich import box

LOG_PATH    = os.path.join(os.path.dirname(__file__), 'logs', 'trades.csv')
NO_LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'no_trades.csv')

console = Console()


# ── Load ────────────────────────────────────────────────────────────────────

def load_log() -> pd.DataFrame:
    frames = []
    for path in (LOG_PATH, NO_LOG_PATH):
        if os.path.exists(path):
            _df = pd.read_csv(path, parse_dates=['logged_at', 'commence'])
            if not _df.empty:
                frames.append(_df)
    if not frames:
        console.print('[yellow]No trade log found yet.[/yellow]')
        raise SystemExit(0)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        console.print('[yellow]Trade log is empty.[/yellow]')
        raise SystemExit(0)
    return df


# ── Rich summary table ───────────────────────────────────────────────────────

def print_summary(df: pd.DataFrame):
    filled  = df[df['final_status'].isin(['executed', 'filled'])]
    settled = filled[filled['result'].isin(['WIN', 'LOSS'])]

    # Header stats
    total_ev     = filled['ev_total'].sum()
    actual_pnl   = pd.to_numeric(settled['actual_pnl'], errors='coerce').sum()
    win_rate     = (settled['result'] == 'WIN').mean() if len(settled) else float('nan')
    total_cost   = filled['total_cost'].sum()

    console.rule('[bold blue]K/P Arbitrage — Trade Review[/bold blue]')
    console.print(
        f'  Total placed: [cyan]{len(df)}[/cyan]  |  '
        f'Filled: [cyan]{len(filled)}[/cyan]  |  '
        f'Settled: [cyan]{len(settled)}[/cyan]  |  '
        f'Pending: [cyan]{len(filled) - len(settled)}[/cyan]'
    )
    console.print(
        f'  Projected EV : [green]${total_ev:+.2f}[/green]  |  '
        f'Actual PnL : [{"green" if actual_pnl >= 0 else "red"}]${actual_pnl:+.2f}[/{"green" if actual_pnl >= 0 else "red"}]  |  '
        f'Win rate : [cyan]{win_rate:.0%}[/cyan]  |  '
        f'Total wagered : [cyan]${total_cost:.2f}[/cyan]'
    )

    # Win rate by order type
    if not settled.empty and 'order_type' in settled.columns:
        _LABEL_MAP = {'no_rest': 'rest', 'no_cross': 'cross'}
        settled = settled.copy()
        settled['order_type'] = settled['order_type'].map(lambda v: _LABEL_MAP.get(v, v))
        console.print()
        console.print('  [bold]Win rate by order type:[/bold]')
        for otype in sorted(settled['order_type'].unique()):
            sub  = settled[settled['order_type'] == otype]
            w    = (sub['result'] == 'WIN').sum()
            l    = (sub['result'] == 'LOSS').sum()
            wr   = w / len(sub) if len(sub) else float('nan')
            pnl  = pd.to_numeric(sub['actual_pnl'], errors='coerce').sum()
            avg_ev = sub['ev_total'].mean() if 'ev_total' in sub.columns else float('nan')
            wr_c = 'green' if wr >= 0.5 else 'red'
            pnl_c = 'green' if pnl >= 0 else 'red'
            console.print(
                f'    [cyan]{otype:8s}[/cyan]  '
                f'{w}W / {l}L  '
                f'win rate=[{wr_c}]{wr:.0%}[/{wr_c}]  '
                f'pnl=[{pnl_c}]${pnl:+.2f}[/{pnl_c}]  '
                + (f'avg_ev=${avg_ev:+.3f}' if not pd.isna(avg_ev) else '')
            )
    console.print()

    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
    table.add_column('Date',       width=11)
    table.add_column('Outcome',    width=26)
    table.add_column('Type',       width=6)
    table.add_column('Edge',       justify='right', width=6)
    table.add_column('Entry',      justify='right', width=7)
    table.add_column('Fair',       justify='right', width=6)
    table.add_column('Fee',        justify='right', width=5)
    table.add_column('EV/ct',      justify='right', width=7)
    table.add_column('Cts',        justify='right', width=4)
    table.add_column('EV Total',   justify='right', width=9)
    table.add_column('Status',     width=14)
    table.add_column('Result',     width=8)
    table.add_column('PnL',        justify='right', width=7)

    STATUS_STYLE = {
        'executed': 'bold green', 'filled': 'bold green',
        'canceled': 'red', 'signal_flipped': 'red',
        'max_duration_exceeded': 'dim', 'event_imminent': 'dim',
    }
    RESULT_STYLE = {'WIN': 'bold green', 'LOSS': 'red', 'PENDING': 'yellow'}

    _CANCELLED = {'canceled', 'signal_flipped', 'max_duration_exceeded', 'event_imminent', 'user_canceled'}
    for _, r in df[~df['final_status'].isin(_CANCELLED)].sort_values('logged_at').iterrows():
        date_str   = str(r['commence'])[:10] if pd.notna(r['commence']) else '—'
        status_c   = STATUS_STYLE.get(r['final_status'], 'white')
        result_c   = RESULT_STYLE.get(str(r['result']), 'dim')
        pnl_str    = f"${float(r['actual_pnl']):+.2f}" if str(r['actual_pnl']) not in ('', 'nan') else '—'
        pnl_c      = 'green' if str(r['actual_pnl']) not in ('', 'nan') and float(r['actual_pnl']) >= 0 else 'red'
        edge_c     = 'green' if r['edge'] > 0 else 'red'
        type_c     = 'cyan' if r['order_type'] == 'cross' else 'magenta'

        table.add_row(
            date_str,
            str(r['outcome'])[:26],
            f'[{type_c}]{r["order_type"]}[/{type_c}]',
            f'[{edge_c}]{r["edge"]:+.3f}[/{edge_c}]',
            f'{r["entry_price_cents"]}¢',
            f'{r["fair_prob"]:.3f}',
            f'{int(r["fee_rate"]*100)}%',
            f'{r["ev_per_contract"]:+.3f}',
            str(r['contracts']),
            f'[{"green" if r["ev_total"] > 0 else "red"}]{r["ev_total"]:+.3f}[/{"green" if r["ev_total"] > 0 else "red"}]',
            f'[{status_c}]{r["final_status"]}[/{status_c}]',
            f'[{result_c}]{r["result"]}[/{result_c}]',
            f'[{pnl_c}]{pnl_str}[/{pnl_c}]' if pnl_str != '—' else '—',
        )

    console.print(table)


# ── Charts ───────────────────────────────────────────────────────────────────

def show_charts(df: pd.DataFrame):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import numpy as np
    except ImportError:
        console.print('[yellow]matplotlib not installed — skipping charts. pip install matplotlib[/yellow]')
        return

    filled   = df[df['final_status'].isin(['executed', 'filled'])].copy()
    settled  = filled[filled['result'].isin(['WIN', 'LOSS'])].copy()
    settled['actual_pnl'] = pd.to_numeric(settled['actual_pnl'], errors='coerce')
    settled  = settled.sort_values('logged_at')

    fig = plt.figure(figsize=(14, 10), facecolor='white')
    fig.suptitle('K/P Cross-Market Arbitrage — Edge Realization', fontsize=14, fontweight='bold', color='black')
    gs  = gridspec.GridSpec(2, 2, hspace=0.4, wspace=0.35)

    # ── 1. Edge distribution ─────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    if not filled.empty:
        colors = ['#2ecc71' if v > 0 else '#e74c3c' for v in filled['edge']]
        ax1.bar(range(len(filled)), filled['edge'].values, color=colors, width=0.8)
        ax1.axhline(0, color='black', linewidth=0.8, linestyle='--')
        ax1.set_title('Edge per Trade (fair_prob − entry_price)')
        ax1.set_xlabel('Trade #')
        ax1.set_ylabel('Edge')
        ax1.set_facecolor('#1a1a2e')
        # annotate mean
        mean_edge = filled['edge'].mean()
        ax1.axhline(mean_edge, color='yellow', linewidth=1, linestyle=':',
                    label=f'mean={mean_edge:+.3f}')
        ax1.legend(fontsize=8)
    else:
        ax1.text(0.5, 0.5, 'No filled orders yet', ha='center', va='center',
                 transform=ax1.transAxes, color='gray')
        ax1.set_title('Edge per Trade')
    fig.patch.set_facecolor('white')
    ax1.set_facecolor('white')
    ax1.tick_params(colors='black')
    ax1.title.set_color('black')
    ax1.xaxis.label.set_color('black')
    ax1.yaxis.label.set_color('black')
    for spine in ax1.spines.values():
        spine.set_edgecolor('#ccc')

    # ── 2. Cumulative EV vs actual PnL (settled bets only) ───────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    if not settled.empty:
        xs      = range(len(settled))
        cum_ev  = settled['ev_total'].cumsum().values
        cum_pnl = settled['actual_pnl'].cumsum().values
        ax2.plot(xs, cum_ev,  color='#3498db', linewidth=2,
                 label='Projected EV', marker='o', markersize=4)
        ax2.plot(xs, cum_pnl, color='#2ecc71', linewidth=2,
                 label='Actual PnL',   marker='s', markersize=4)
        ax2.axhline(0, color='black', linewidth=0.5, linestyle='--')
        ax2.set_title('Cumulative EV vs Actual PnL (settled)')
        ax2.set_xlabel('Settled bet #')
        ax2.set_ylabel('$')
        ax2.legend(fontsize=8)
    else:
        ax2.text(0.5, 0.5, 'No settled bets yet', ha='center', va='center',
                 transform=ax2.transAxes, color='gray')
        ax2.set_title('Cumulative EV vs Actual PnL (settled)')
    ax2.set_facecolor('white')
    ax2.tick_params(colors='black')
    ax2.title.set_color('black')
    ax2.xaxis.label.set_color('black')
    ax2.yaxis.label.set_color('black')
    for spine in ax2.spines.values():
        spine.set_edgecolor('#ccc')

    # ── 3. EV per contract histogram ─────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    if not filled.empty:
        ax3.hist(filled['ev_per_contract'], bins=max(5, len(filled) // 3),
                 color='#9b59b6', edgecolor='white', linewidth=0.5)
        ax3.axvline(0, color='red', linewidth=1, linestyle='--')
        ax3.axvline(filled['ev_per_contract'].mean(), color='yellow',
                    linewidth=1, linestyle=':', label=f"mean={filled['ev_per_contract'].mean():+.3f}")
        ax3.set_title('EV per Contract Distribution')
        ax3.set_xlabel('EV ($)')
        ax3.set_ylabel('Count')
        ax3.legend(fontsize=8)
    else:
        ax3.text(0.5, 0.5, 'No filled orders yet', ha='center', va='center',
                 transform=ax3.transAxes, color='gray')
        ax3.set_title('EV per Contract Distribution')
    ax3.set_facecolor('white')
    ax3.tick_params(colors='black')
    ax3.title.set_color('black')
    ax3.xaxis.label.set_color('black')
    ax3.yaxis.label.set_color('black')
    for spine in ax3.spines.values():
        spine.set_edgecolor('#ccc')

    # ── 4. Win rate by order type ─────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    if not settled.empty and 'order_type' in settled.columns:
        _LABEL_MAP = {'no_rest': 'rest', 'no_cross': 'cross'}
        settled = settled.copy()
        settled['order_type'] = settled['order_type'].map(lambda v: _LABEL_MAP.get(v, v))
        types   = sorted(settled['order_type'].unique())
        wr_vals = []
        colors  = []
        for t in types:
            sub = settled[settled['order_type'] == t]
            wr  = (sub['result'] == 'WIN').mean()
            wr_vals.append(wr)
            colors.append('#2ecc71' if wr >= 0.5 else '#e74c3c')
        x = np.arange(len(types))
        ax4.bar(x, wr_vals, color=colors, width=0.5)
        ax4.axhline(0.5, color='gray', linewidth=1, linestyle='--', label='50%')
        ax4.set_ylim(0, 1.05)
        ax4.set_title('Win Rate by Order Type')
        ax4.set_xticks(x)
        ax4.set_xticklabels(types)
        ax4.set_ylabel('Win Rate')
        ax4.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
        ax4.legend(fontsize=8)
        for i, t in enumerate(types):
            sub = settled[settled['order_type'] == t]
            w   = (sub['result'] == 'WIN').sum()
            l   = (sub['result'] == 'LOSS').sum()
            pnl = pd.to_numeric(sub['actual_pnl'], errors='coerce').sum()
            ax4.text(i, wr_vals[i] + 0.03,
                     f'{wr_vals[i]:.0%}\n{w}W/{l}L  ${pnl:+.2f}',
                     ha='center', fontsize=8, color='black')
    else:
        ax4.text(0.5, 0.5, 'No settled trades yet', ha='center', va='center',
                 transform=ax4.transAxes, color='gray')
        ax4.set_title('Win Rate by Order Type')
    ax4.set_facecolor('white')
    ax4.tick_params(colors='black')
    ax4.title.set_color('black')
    ax4.xaxis.label.set_color('black')
    ax4.yaxis.label.set_color('black')
    for spine in ax4.spines.values():
        spine.set_edgecolor('#ccc')

    plt.tight_layout()
    plt.show()


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--table', action='store_true', help='Print table only, skip charts')
    args = parser.parse_args()

    df = load_log()
    print_summary(df)
    if not args.table:
        show_charts(df)

    # Nothing Ever Happens bot review
    import nothing_review as _nr
    _nr_df = _nr.load_log()
    if not _nr_df.empty:
        _nr.print_summary(_nr_df)
        if not args.table:
            import matplotlib.pyplot as plt
            _nr_fig = _nr.build_charts(_nr_df)
            if _nr_fig:
                plt.show()
            else:
                console.print('[dim]No settled Nothing trades yet — nothing to chart.[/dim]')

    # Prospect Theory strategy review
    import prospect_review as _pr
    _pr_df = _pr.load_log()
    if not _pr_df.empty:
        _pr.print_summary(_pr_df)
        if not args.table:
            import matplotlib.pyplot as plt
            _pr_fig = _pr.build_charts(_pr_df)
            if _pr_fig:
                plt.show()
            else:
                console.print('[dim]No settled Prospect trades yet — nothing to chart.[/dim]')
