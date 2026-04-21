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
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import pandas as pd
from rich.console import Console
from KALSHI.k_helpers import kalshi_headers

LOG_DIR          = os.path.join(os.path.dirname(__file__), 'logs')
LOG_PATH         = os.path.join(LOG_DIR, 'trades.csv')
NO_LOG_PATH      = os.path.join(LOG_DIR, 'no_trades.csv')
NOTHING_LOG_PATH = os.path.join(LOG_DIR, 'nothing_trades.csv')
BASE_URL    = 'https://api.elections.kalshi.com/trade-api/v2'

console = Console()


TERMINAL_ORDER_STATUSES = {'executed', 'filled', 'canceled', 'expired'}


def fetch_order_status(order_id: str) -> Optional[dict]:
    """Fetch one order directly. Returns the order dict or None on error."""
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    resp = requests.get(f'{BASE_URL}/portfolio/orders/{order_id}',
                        headers=kalshi_headers('GET', path))
    if resp.ok:
        return resp.json().get('order', {})
    return None


def refresh_statuses(path: str) -> int:
    """
    For every row with a non-terminal `final_status`, re-query Kalshi by
    order_id and update `final_status` + `contracts` (filled count) in place.
    Returns the number of rows updated.
    """
    label = os.path.basename(path)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    df = pd.read_csv(path)
    if df.empty or 'order_id' not in df.columns or 'final_status' not in df.columns:
        return 0

    mask = (
        df['order_id'].fillna('').ne('') &
        ~df['final_status'].fillna('').str.lower().isin(TERMINAL_ORDER_STATUSES)
    )
    stale = df[mask]
    if stale.empty:
        return 0

    console.print(f'  [dim]{label}[/dim]: refreshing {len(stale)} non-terminal row(s)')
    updated = 0
    for idx, row in stale.iterrows():
        order = fetch_order_status(row['order_id'])
        if not order:
            continue
        new_status = order.get('status', '')
        if not new_status or new_status == row['final_status']:
            continue
        df.at[idx, 'final_status'] = new_status
        # Update filled count if Kalshi reports a remaining_count
        remaining = order.get('remaining_count_fp') or order.get('remaining_count')
        if remaining is not None:
            try:
                original = int(row.get('contracts', 0))
                filled   = max(original - int(float(remaining)), 0)
                df.at[idx, 'contracts'] = filled
            except Exception:
                pass
        updated += 1

    if updated:
        df.to_csv(path, index=False)
        console.print(f'  [green]{label}: updated {updated} row(s)[/green]')
    return updated


def fetch_market_result(ticker: str) -> Optional[str]:
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
                entry_price: float, fee_rate: float,
                side: str = 'yes') -> float:
    """
    WIN  : receive (1 - entry_price) per contract, minus fee on winnings.
    LOSS : lose entry_price per contract.
    VOID : stake returned, no gain or loss.

    For side='yes': WIN when Kalshi result='yes'.
    For side='no' : WIN when Kalshi result='no'.
    """
    winning_result = 'no' if side == 'no' else 'yes'
    losing_result  = 'yes' if side == 'no' else 'no'
    if result == winning_result:
        return round(contracts * (1 - entry_price) * (1 - fee_rate), 4)
    if result == losing_result:
        return round(-contracts * entry_price, 4)
    return 0.0   # void


def _settle_file(path: str, side: str, dry_run: bool) -> int:
    """Settle one log file. Returns number of rows updated."""
    label = os.path.basename(path)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        console.print(f'[dim]{label}: no log[/dim]')
        return 0

    df = pd.read_csv(path)
    pending = df[
        (df['result'] == 'PENDING') &
        (df['final_status'].isin(['executed', 'filled']))
    ]

    if pending.empty:
        console.print(f'[dim]{label}: no pending trades[/dim]')
        return 0

    console.print(f'\n[bold]{label}[/bold] ({side.upper()} side) — checking [cyan]{len(pending)}[/cyan] pending\n')

    # For NO trades the winning Kalshi result is 'no', not 'yes'
    win_result  = 'no' if side == 'no' else 'yes'
    loss_result = 'yes' if side == 'no' else 'no'
    label_map   = {win_result: 'WIN', loss_result: 'LOSS', 'void': 'VOID'}

    updates = 0
    for idx, row in pending.iterrows():
        ticker = row['k_ticker']
        desc   = row.get('outcome') if 'outcome' in row.index else row.get('title', '')
        console.print(f'  {ticker}  {desc}', end='  ')

        k_result = fetch_market_result(ticker)

        if k_result is None:
            console.print('[dim]not settled yet[/dim]')
            continue

        if k_result not in label_map:
            console.print(f'[yellow]unknown result={k_result!r} — skipping[/yellow]')
            continue

        result_label = label_map[k_result]
        pnl = compute_pnl(k_result,
                          int(row['contracts']),
                          float(row['entry_price']),
                          float(row['fee_rate']),
                          side=side)
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

    if updates and not dry_run:
        df.to_csv(path, index=False)
        console.print(f'[green]Updated {updates} row(s) in {label}[/green]')
    elif updates:
        console.print(f'[dim]{updates} row(s) would be updated (dry run)[/dim]')
    return updates


def run(dry_run: bool = False):
    # First, refresh any stale final_status/contracts by re-querying Kalshi.
    # This catches orders that filled after the bot exited and never wrote back.
    if not dry_run:
        console.print('[bold]Refreshing order statuses from Kalshi...[/bold]')
        refresh_statuses(LOG_PATH)
        refresh_statuses(NO_LOG_PATH)
        refresh_statuses(NOTHING_LOG_PATH)

    total = 0
    total += _settle_file(LOG_PATH,         side='yes', dry_run=dry_run)
    total += _settle_file(NO_LOG_PATH,      side='no',  dry_run=dry_run)
    total += _settle_file(NOTHING_LOG_PATH, side='no',  dry_run=dry_run)
    if total == 0:
        console.print('\n[yellow]No markets have settled yet.[/yellow]')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing to CSV')
    args = parser.parse_args()
    run(dry_run=args.dry_run)
