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

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'trades.csv')

console = Console()


# ── Load ────────────────────────────────────────────────────────────────────

def load_log() -> pd.DataFrame:
    if not os.path.exists(LOG_PATH):
        console.print('[yellow]No trade log found yet.[/yellow]')
        raise SystemExit(0)
    df = pd.read_csv(LOG_PATH, parse_dates=['logged_at', 'commence'])
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

    for _, r in df.sort_values('logged_at').iterrows():
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

    fig = plt.figure(figsize=(14, 10))
    fig.suptitle('K/P Cross-Market Arbitrage — Edge Realization', fontsize=14, fontweight='bold')
    gs  = gridspec.GridSpec(2, 2, hspace=0.4, wspace=0.35)

    # ── 1. Edge distribution ─────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    if not filled.empty:
        colors = ['#2ecc71' if v > 0 else '#e74c3c' for v in filled['edge']]
        ax1.bar(range(len(filled)), filled['edge'].values, color=colors, width=0.8)
        ax1.axhline(0, color='white', linewidth=0.8, linestyle='--')
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
    fig.patch.set_facecolor('#0f0f23')
    ax1.set_facecolor('#1a1a2e')
    ax1.tick_params(colors='white')
    ax1.title.set_color('white')
    ax1.xaxis.label.set_color('white')
    ax1.yaxis.label.set_color('white')
    for spine in ax1.spines.values():
        spine.set_edgecolor('#333')

    # ── 2. Cumulative EV vs actual PnL ───────────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    if not filled.empty:
        cum_ev = filled.sort_values('logged_at')['ev_total'].cumsum().values
        ax2.plot(range(len(cum_ev)), cum_ev, color='#3498db', linewidth=2,
                 label='Projected EV', marker='o', markersize=4)
        if not settled.empty:
            cum_pnl = settled['actual_pnl'].cumsum().values
            # align settled indices to their position in filled
            settled_indices = [
                i for i, (_, r) in enumerate(filled.sort_values('logged_at').iterrows())
                if r['result'] in ('WIN', 'LOSS')
            ]
            ax2.plot(settled_indices[:len(cum_pnl)], cum_pnl,
                     color='#2ecc71', linewidth=2, label='Actual PnL',
                     marker='s', markersize=4)
        ax2.axhline(0, color='white', linewidth=0.5, linestyle='--')
        ax2.set_title('Cumulative EV vs Actual PnL')
        ax2.set_xlabel('Trade #')
        ax2.set_ylabel('$')
        ax2.legend(fontsize=8)
    else:
        ax2.text(0.5, 0.5, 'No filled orders yet', ha='center', va='center',
                 transform=ax2.transAxes, color='gray')
        ax2.set_title('Cumulative EV vs Actual PnL')
    ax2.set_facecolor('#1a1a2e')
    ax2.tick_params(colors='white')
    ax2.title.set_color('white')
    ax2.xaxis.label.set_color('white')
    ax2.yaxis.label.set_color('white')
    for spine in ax2.spines.values():
        spine.set_edgecolor('#333')

    # ── 3. EV per contract histogram ─────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    if not filled.empty:
        ax3.hist(filled['ev_per_contract'], bins=max(5, len(filled) // 3),
                 color='#9b59b6', edgecolor='#0f0f23', linewidth=0.5)
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
    ax3.set_facecolor('#1a1a2e')
    ax3.tick_params(colors='white')
    ax3.title.set_color('white')
    ax3.xaxis.label.set_color('white')
    ax3.yaxis.label.set_color('white')
    for spine in ax3.spines.values():
        spine.set_edgecolor('#333')

    # ── 4. Win rate by order type ─────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    if not settled.empty:
        types    = settled['order_type'].unique()
        wins     = [len(settled[(settled['order_type'] == t) & (settled['result'] == 'WIN')]) for t in types]
        losses   = [len(settled[(settled['order_type'] == t) & (settled['result'] == 'LOSS')]) for t in types]
        x        = np.arange(len(types))
        w        = 0.35
        bars_w   = ax4.bar(x - w/2, wins,   w, label='WIN',  color='#2ecc71')
        bars_l   = ax4.bar(x + w/2, losses, w, label='LOSS', color='#e74c3c')
        ax4.set_title('Win / Loss by Order Type')
        ax4.set_xticks(x)
        ax4.set_xticklabels(types)
        ax4.set_ylabel('Count')
        ax4.legend(fontsize=8)
        # add expected win rate annotation
        for i, t in enumerate(types):
            sub = settled[settled['order_type'] == t]
            wr  = (sub['result'] == 'WIN').mean()
            avg_fp = sub['fair_prob'].mean()
            ax4.text(i, max(wins[i], losses[i]) + 0.1,
                     f'WR={wr:.0%}\nfair={avg_fp:.2f}',
                     ha='center', fontsize=7, color='white')
    else:
        ax4.text(0.5, 0.5, 'No settled trades yet\n(update result column in CSV)',
                 ha='center', va='center', transform=ax4.transAxes, color='gray')
        ax4.set_title('Win / Loss by Order Type')
    ax4.set_facecolor('#1a1a2e')
    ax4.tick_params(colors='white')
    ax4.title.set_color('white')
    ax4.xaxis.label.set_color('white')
    ax4.yaxis.label.set_color('white')
    for spine in ax4.spines.values():
        spine.set_edgecolor('#333')

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
