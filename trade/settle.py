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
from applog import get_logger

log = get_logger(__name__)

LOG_DIR              = os.path.join(os.path.dirname(__file__), 'logs')
LOG_PATH             = os.path.join(LOG_DIR, 'trades.csv')
NO_LOG_PATH          = os.path.join(LOG_DIR, 'no_trades.csv')
NOTHING_LOG_PATH     = os.path.join(LOG_DIR, 'nothing_trades.csv')
PROSPECT_LOG_PATH    = os.path.join(LOG_DIR, 'prospect_trades.csv')
BASE_URL    = 'https://api.elections.kalshi.com/trade-api/v2'

console = Console()


TERMINAL_ORDER_STATUSES = {'executed', 'filled', 'canceled', 'expired'}


def fetch_order_status(order_id: str) -> Optional[dict]:
    """Fetch one order directly. Returns the order dict or None on error (logged)."""
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    try:
        resp = requests.get(f'{BASE_URL}/portfolio/orders/{order_id}',
                            headers=kalshi_headers('GET', path))
    except requests.exceptions.RequestException:
        log.exception('fetch_order_status network failure for %s', order_id)
        return None
    if resp.ok:
        return resp.json().get('order', {})
    log.warning('fetch_order_status failed for %s: %s %s', order_id, resp.status_code, resp.reason)
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
                log.debug('refresh_statuses: could not parse remaining_count for order %s',
                         row.get('order_id'))
        updated += 1

    if updated:
        df.to_csv(path, index=False)
        console.print(f'  [green]{label}: updated {updated} row(s)[/green]')
    return updated


def fetch_market_result(ticker: str) -> Optional[str]:
    """
    Returns 'yes', 'no', 'void', 'scalar', or None (not yet settled, or on error — logged).
    """
    path = f'/trade-api/v2/markets/{ticker}'
    try:
        resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                            headers=kalshi_headers('GET', path))
    except requests.exceptions.RequestException:
        log.exception('fetch_market_result network failure for %s', ticker)
        return None
    if not resp.ok:
        log.warning('fetch_market_result failed for %s: %s %s', ticker, resp.status_code, resp.reason)
        return None
    market = resp.json().get('market', {})
    result = market.get('result')          # 'yes' | 'no' | 'void' | 'scalar' | null
    status = market.get('status', '')      # 'settled' or 'finalized' when done
    if result and status in ('settled', 'finalized'):
        return result.lower()
    # Fallback: a non-null result field is itself proof of resolution
    if result:
        return result.lower()
    return None


def fetch_scalar_settlement_value(ticker: str) -> Optional[float]:
    """
    For a scalar market (result='scalar'), returns the per-contract payout in
    dollars (0–1). Kalshi settles these at the last traded price.
    Returns None if the value cannot be determined.
    """
    path = f'/trade-api/v2/markets/{ticker}'
    try:
        resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                            headers=kalshi_headers('GET', path))
    except requests.exceptions.RequestException:
        log.exception('fetch_scalar_settlement_value network failure for %s', ticker)
        return None
    if not resp.ok:
        log.warning('fetch_scalar_settlement_value failed for %s: %s %s',
                    ticker, resp.status_code, resp.reason)
        return None
    market = resp.json().get('market', {})
    # Primary field Kalshi uses for scalar settlement value (0–1 dollar range)
    for field in ('result_value', 'settlement_value', 'final_settlement_price'):
        v = market.get(field)
        if v is not None:
            try:
                val = float(v)
                if 0.0 <= val <= 1.0:
                    return round(val, 4)
                if 0.0 < val <= 100.0:   # value expressed in cents
                    return round(val / 100.0, 4)
            except (ValueError, TypeError):
                pass
    # Fallback: last traded mid-price (bid+ask)/2, which is the "last price" Kalshi uses
    ya = market.get('yes_ask_dollars')
    yb = market.get('yes_bid_dollars')
    if ya and yb:
        try:
            return round((float(ya) + float(yb)) / 2.0, 4)
        except (ValueError, TypeError):
            pass
    if yb:
        try:
            return round(float(yb), 4)
        except (ValueError, TypeError):
            pass
    return None


def compute_pnl(result: str, contracts: int,
                entry_price: float, fee_rate: float,
                side: str = 'yes') -> float:
    """
    WIN    : receive (1 - entry_price) per contract, minus fee on winnings.
    LOSS   : lose entry_price per contract.
    VOID   : stake returned, no gain or loss.
    SCALAR : handled separately via compute_scalar_pnl.

    For side='yes': WIN when Kalshi result='yes'.
    For side='no' : WIN when Kalshi result='no'.
    """
    winning_result = 'no' if side == 'no' else 'yes'
    losing_result  = 'yes' if side == 'no' else 'no'
    if result == winning_result:
        return round(contracts * (1 - entry_price) * (1 - fee_rate), 4)
    if result == losing_result:
        return round(-contracts * entry_price, 4)
    return 0.0   # void or scalar (caller handles scalar separately)


def compute_scalar_pnl(contracts: int, entry_price: float, fee_rate: float,
                        settlement_value: float, side: str = 'yes') -> float:
    """
    Scalar / last-price settlement PnL.
    YES holder receives `settlement_value` dollars per contract.
    NO  holder receives `1 - settlement_value` dollars per contract.
    Fee is charged on positive gains only.
    """
    our_value = (1 - settlement_value) if side == 'no' else settlement_value
    gain_per  = our_value - entry_price
    if gain_per > 0:
        return round(contracts * gain_per * (1 - fee_rate), 4)
    return round(contracts * gain_per, 4)


def _settle_file(path: str, side: str, dry_run: bool,
                 side_col: Optional[str] = None) -> int:
    """
    Settle one log file. Returns number of rows updated.
    side_col: if set, read the trade side from that column per row
              (used for prospect_trades.csv where pt_side varies per row).
    """
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

    side_label = 'mixed' if side_col else side.upper()
    console.print(f'\n[bold]{label}[/bold] ({side_label}) — checking [cyan]{len(pending)}[/cyan] pending\n')

    updates = 0
    for idx, row in pending.iterrows():
        ticker   = row['k_ticker']
        desc     = row.get('outcome') if 'outcome' in row.index else row.get('title', '')
        row_side = row[side_col] if (side_col and side_col in df.columns and
                                     pd.notna(row.get(side_col))) else side

        win_result  = 'no' if row_side == 'no' else 'yes'
        loss_result = 'yes' if row_side == 'no' else 'no'
        label_map   = {win_result: 'WIN', loss_result: 'LOSS', 'void': 'VOID'}

        console.print(f'  {ticker}  {desc}', end='  ')

        k_result = fetch_market_result(ticker)

        if k_result is None:
            console.print('[dim]not settled yet[/dim]')
            continue

        if k_result == 'scalar':
            sv = fetch_scalar_settlement_value(ticker)
            if sv is None:
                console.print('[yellow]SCALAR — settlement value unavailable, skipping[/yellow]')
                continue
            pnl = compute_scalar_pnl(int(row['contracts']),
                                     float(row['entry_price']),
                                     float(row['fee_rate']),
                                     sv, side=row_side)
            result_label = 'SCALAR'
            pnl_c = 'green' if pnl >= 0 else ('red' if pnl < 0 else 'dim')
            console.print(
                f'[bold cyan]SCALAR[/bold cyan]  settle={sv:.3f}  '
                f'[{pnl_c}]pnl=${pnl:+.4f}[/{pnl_c}]'
                + (' [dim](dry run — not written)[/dim]' if dry_run else '')
            )
            if not dry_run:
                df.at[idx, 'result']     = result_label
                df.at[idx, 'actual_pnl'] = pnl
            updates += 1
            continue

        if k_result not in label_map:
            console.print(f'[yellow]unknown result={k_result!r} — skipping[/yellow]')
            continue

        result_label = label_map[k_result]
        pnl = compute_pnl(k_result,
                          int(row['contracts']),
                          float(row['entry_price']),
                          float(row['fee_rate']),
                          side=row_side)
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
        refresh_statuses(PROSPECT_LOG_PATH)

    total = 0
    total += _settle_file(LOG_PATH,          side='yes', dry_run=dry_run)
    total += _settle_file(NO_LOG_PATH,       side='no',  dry_run=dry_run)
    total += _settle_file(NOTHING_LOG_PATH,  side='no',  dry_run=dry_run)
    total += _settle_file(PROSPECT_LOG_PATH, side='yes', dry_run=dry_run,
                          side_col='pt_side')
    if total == 0:
        console.print('\n[yellow]No markets have settled yet.[/yellow]')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing to CSV')
    args = parser.parse_args()
    run(dry_run=args.dry_run)
