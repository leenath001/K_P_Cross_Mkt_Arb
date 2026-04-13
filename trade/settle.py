"""
settle.py — Auto-settle pending trades by querying Kalshi for market results.

Usage:
    python trade/settle.py             # update all PENDING rows
    python trade/settle.py --dry-run   # preview changes without writing

How it works:
    For each PENDING row in trades.csv, fetches the Kalshi market and checks
    whether it has a result ('yes' / 'no' / 'void'). If settled, writes:
        result     → WIN, LOSS, or VOID
        actual_pnl → computed from contracts, entry_price, fee_rate
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import pandas as pd
from rich.console import Console
from KALSHI.k_helpers import kalshi_headers

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'trades.csv')
BASE_URL  = 'https://api.elections.kalshi.com/trade-api/v2'

console = Console()


def fetch_market_result(ticker: str) -> str | None:
    """
    Returns 'yes', 'no', 'void', or None (not yet settled).
    """
    path = f'/trade-api/v2/markets/{ticker}'
    resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                        headers=kalshi_headers('GET', path))
    if not resp.ok:
        return None
    market = resp.json().get('market', {})
    result = market.get('result')          # 'yes' | 'no' | 'void' | null
    status = market.get('status', '')      # 'settled' or 'finalized' when done
    if result and status in ('settled', 'finalized'):
        return result.lower()
    # Fallback: a non-null result field is itself proof of resolution
    if result:
        return result.lower()
    return None


def compute_pnl(result: str, contracts: int,
                entry_price: float, fee_rate: float) -> float:
    """
    WIN  (result='yes'): receive (1 - entry_price) per contract, minus fee on winnings.
    LOSS (result='no') : lose entry_price per contract.
    VOID               : stake returned, no gain or loss.
    """
    if result == 'yes':
        return round(contracts * (1 - entry_price) * (1 - fee_rate), 4)
    if result == 'no':
        return round(-contracts * entry_price, 4)
    return 0.0   # void


def run(dry_run: bool = False):
    if not os.path.exists(LOG_PATH):
        console.print('[yellow]No trade log found.[/yellow]')
        return

    if os.path.getsize(LOG_PATH) == 0:
        console.print('[green]No pending trades to settle.[/green]')
        return

    df = pd.read_csv(LOG_PATH)
    pending = df[
        (df['result'] == 'PENDING') &
        (df['final_status'].isin(['executed', 'filled']))
    ]

    if pending.empty:
        console.print('[green]No pending trades to settle.[/green]')
        return

    console.print(f'Checking [cyan]{len(pending)}[/cyan] pending trade(s)...\n')

    updates = 0
    for idx, row in pending.iterrows():
        ticker  = row['k_ticker']
        console.print(f'  {ticker}  {row["outcome"]}', end='  ')

        k_result = fetch_market_result(ticker)

        if k_result is None:
            console.print('[dim]not settled yet[/dim]')
            continue

        result_label = {'yes': 'WIN', 'no': 'LOSS', 'void': 'VOID'}[k_result]
        pnl = compute_pnl(k_result,
                          int(row['contracts']),
                          float(row['entry_price']),
                          float(row['fee_rate']))
        pnl_c = 'green' if pnl >= 0 else 'red'

        console.print(
            f'[bold]{result_label}[/bold]  '
            f'[{pnl_c}]pnl=${pnl:+.4f}[/{pnl_c}]'
            + (' [dim](dry run — not written)[/dim]' if dry_run else '')
        )

        if not dry_run:
            df.at[idx, 'result']     = result_label
            df.at[idx, 'actual_pnl'] = pnl
        updates += 1

    if updates == 0:
        console.print('\n[yellow]No markets have settled yet.[/yellow]')
        return

    if not dry_run:
        df.to_csv(LOG_PATH, index=False)
        console.print(f'\n[green]Updated {updates} row(s) in trades.csv[/green]')
    else:
        console.print(f'\n[dim]{updates} row(s) would be updated (dry run)[/dim]')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing to CSV')
    args = parser.parse_args()
    run(dry_run=args.dry_run)
