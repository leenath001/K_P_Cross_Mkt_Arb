"""
trade/poly_quickstart.py — Polymarket signal scanner using Pinnacle fair probs.

Usage:
    python trade/poly_quickstart.py             # scan all configured sports
    python trade/poly_quickstart.py --sport basketball_nba icehockey_nhl
    python trade/poly_quickstart.py --hrs 48    # look-ahead window
    python trade/poly_quickstart.py --all       # show all matches, not just signals
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from rich.console import Console
from rich.table   import Table
from rich         import box

from theODDS.p_helpers   import pinnacle_odds
from POLYMARKET.p_helpers import polymarket_signals, POLY_SPORT_MAP
import config

console = Console()


def main():
    parser = argparse.ArgumentParser(description='Polymarket signal scanner')
    parser.add_argument('--sport', nargs='+', default=None,
                        help='theODDS sport key(s) to scan (default: all configured)')
    parser.add_argument('--hrs',   type=int, default=72,
                        help='Look-ahead window in hours (default: 72)')
    parser.add_argument('--all',   action='store_true',
                        help='Show all matched markets, not just positive-EV signals')
    parser.add_argument('--clob',  action='store_true',
                        help='Re-fetch live bid/ask from CLOB (slower, more accurate)')
    args = parser.parse_args()

    sports = args.sport or [s for s in config.SPORTS if s in POLY_SPORT_MAP]

    # ── Step 1: Pinnacle odds ─────────────────────────────────────────────────
    console.rule('[bold cyan]STEP 1 — Pinnacle odds[/bold cyan]')
    console.print(f'  Sports: {sports}')
    console.print(f'  Window: next {args.hrs}h')

    try:
        pin_df = pinnacle_odds(sports=sports, hrs=args.hrs)
    except Exception as exc:
        console.print(f'[red]Pinnacle fetch failed: {exc}[/red]')
        sys.exit(1)

    console.print(f'  {len(pin_df)} outcomes across '
                  f'{pin_df["sport"].nunique()} sport(s)')

    # ── Step 2: Polymarket signals ────────────────────────────────────────────
    console.rule('[bold cyan]STEP 2 — Polymarket signal matching[/bold cyan]')

    sigs = polymarket_signals(
        pin_df,
        sports=sports,
        hrs=args.hrs,
        fetch_clob=args.clob,
    )

    if sigs.empty:
        console.print('[yellow]No Polymarket game-winner markets matched '
                      'to Pinnacle outcomes.[/yellow]')
        console.print('\nPossible reasons:')
        console.print('  • No active Polymarket game markets in the look-ahead window')
        console.print('  • Fuzzy-match threshold not met (team name mismatch)')
        console.print('  • Games start too far in the future')
        return

    display = sigs if args.all else sigs[sigs['taker_signal'] | sigs['maker_signal']]

    n_taker  = int(sigs['taker_signal'].sum())
    n_maker  = int(sigs['maker_signal'].sum())
    n_all    = len(sigs)

    console.print(f'  Matched: [cyan]{n_all}[/cyan] outcomes  |  '
                  f'Taker signals: [bold green]{n_taker}[/bold green]  |  '
                  f'Maker signals: [bold blue]{n_maker}[/bold blue]')
    if args.clob:
        console.print('  [dim]Prices from live CLOB orderbook[/dim]')
    else:
        console.print('  [dim]Prices from Gamma API (use --clob for live CLOB prices)[/dim]')

    if display.empty:
        console.print('\n[yellow]No positive-EV signals found. '
                      'Use --all to show all matches.[/yellow]')
        return

    # ── Render table ──────────────────────────────────────────────────────────
    console.print()

    # Group by sport for cleaner display
    for sport, group in display.groupby('sport'):
        label = sport.replace('basketball_', '').replace('icehockey_', '').replace(
                'soccer_', '').replace('baseball_', '').replace('mma_', '').upper()
        console.rule(f'[bold white]{label}[/bold white]', style='dim')

        tbl = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
        tbl.add_column('Market',     style='cyan',  no_wrap=True, max_width=32)
        tbl.add_column('Outcome',    style='white', max_width=24)
        tbl.add_column('Side',       style='white', width=5)
        tbl.add_column('Fair',       justify='right', width=6)
        tbl.add_column('Ask',        justify='right', width=6)
        tbl.add_column('Bid',        justify='right', width=6)
        tbl.add_column('Rest',       justify='right', width=6)
        tbl.add_column('Edge',       justify='right', width=7)
        tbl.add_column('TakerEV',    justify='right', width=9)
        tbl.add_column('MakerEV',    justify='right', width=9)
        tbl.add_column('Signal',     width=6)

        for _, row in group.iterrows():
            edge_c   = 'green'  if row['edge']     > 0 else 'red'
            tev_c    = 'green'  if row['taker_ev'] > 0 else 'red'
            mev_c    = 'blue'   if row['maker_ev'] > 0 else 'dim'
            sig_str  = ''
            if row['taker_signal']:
                sig_str += '[bold green]TAK[/bold green] '
            if row['maker_signal']:
                sig_str += '[bold blue]MKR[/bold blue]'
            if not sig_str:
                sig_str = '[dim]—[/dim]'
            bid_str  = f"{row['poly_bid']:.3f}" if row['poly_bid'] is not None else '—'
            tbl.add_row(
                row['poly_title'][-32:],
                row['outcome'][-24:],
                row['token_side'].upper(),
                f"{row['fair_prob']:.3f}",
                f"{row['poly_ask']:.3f}",
                bid_str,
                f"{row['rest_price']:.3f}",
                f"[{edge_c}]{row['edge']:+.3f}[/{edge_c}]",
                f"[{tev_c}]{row['taker_ev']:+.4f}[/{tev_c}]",
                f"[{mev_c}]{row['maker_ev']:+.4f}[/{mev_c}]",
                sig_str,
            )
        console.print(tbl)

    # ── Token IDs for positive signals ───────────────────────────────────────
    signals = display[display['taker_signal'] | display['maker_signal']]
    if not signals.empty:
        console.rule('[dim]Token IDs (for order placement)[/dim]')
        for _, row in signals.iterrows():
            console.print(
                f"  [cyan]{row['poly_title'][:40]}[/cyan]  "
                f"[white]{row['outcome'][:22]}[/white]  "
                f"{'YES' if row['token_side']=='yes' else 'NO '} "
                f"token=[dim]{row['token_id'][:20]}...[/dim]"
            )


if __name__ == '__main__':
    main()
