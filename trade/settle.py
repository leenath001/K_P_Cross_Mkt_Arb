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
from datetime import datetime, timezone
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import pandas as pd
from rich.console import Console
from KALSHI.k_helpers import kalshi_headers
from trade.core.positions import _open_position_tickers
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


def fetch_market_result(ticker: str) -> dict:
    """
    Returns {'result': 'yes'|'no'|'void'|'scalar'|None, 'settled_at': iso_str|None}.
    result is None if not yet settled (or on error — logged).

    settled_at is Kalshi's own settlement_ts — the actual moment the market
    resolved, not whenever our own polling happened to notice it (which could
    be anywhere up to a full settle cycle later). Falls back to updated_time
    or close_time if a specific market lacks settlement_ts.
    """
    path = f'/trade-api/v2/markets/{ticker}'
    try:
        resp = requests.get(f'{BASE_URL}/markets/{ticker}',
                            headers=kalshi_headers('GET', path))
    except requests.exceptions.RequestException:
        log.exception('fetch_market_result network failure for %s', ticker)
        return {'result': None, 'settled_at': None}
    if not resp.ok:
        log.warning('fetch_market_result failed for %s: %s %s', ticker, resp.status_code, resp.reason)
        return {'result': None, 'settled_at': None}
    market = resp.json().get('market', {})
    result = market.get('result')   # 'yes' | 'no' | 'void' | 'scalar' | null
    if not result:
        return {'result': None, 'settled_at': None}
    settled_at = (market.get('settlement_ts') or market.get('updated_time')
                 or market.get('close_time'))
    return {'result': result.lower(), 'settled_at': settled_at}


def fetch_realized_pnl_from_fills(ticker: str, side: str) -> Optional[dict]:
    """
    For a position that was closed out manually (sold back on Kalshi) before
    the market settled — something fetch_market_result() alone can never see,
    since it only asks "has the MARKET resolved", never "do we still hold
    this". Computes the ACTUAL realized PnL from real fill prices (what we
    paid to buy, what we received to sell), not an assumed future settlement.

    Returns {'pnl': float, 'contracts_closed': int} if fully closed via a
    sell, or None if there's no sell fill (nothing to reconcile) or the
    position was only partially sold (still has an open remainder — leave it
    PENDING rather than guess).
    """
    path = '/trade-api/v2/portfolio/fills'
    try:
        resp = requests.get(f'{BASE_URL}/portfolio/fills',
                            headers=kalshi_headers('GET', path),
                            params={'ticker': ticker, 'limit': 200})
    except requests.exceptions.RequestException:
        log.exception('fetch_realized_pnl_from_fills network failure for %s', ticker)
        return None
    if not resp.ok:
        log.warning('fetch_realized_pnl_from_fills failed for %s: %s %s',
                    ticker, resp.status_code, resp.reason)
        return None

    fills = resp.json().get('fills', [])
    from trade.mm.ledger import mm_order_ids           # market-making fills belong to the MM ledger, not this trade
    _mm = mm_order_ids()
    fills = [f for f in fills if f.get('ticker') == ticker and str(f.get('order_id')) not in _mm
             and f.get('outcome_side', f.get('side')) == side]
    if not fills:
        return None

    buys  = [f for f in fills if f.get('action') == 'buy']
    sells = [f for f in fills if f.get('action') == 'sell']
    if not sells or not buys:
        return None   # nothing sold back — not actually closed early

    price_field = 'yes_price_dollars' if side == 'yes' else 'no_price_dollars'
    try:
        buy_cost      = sum(float(f['count_fp']) * float(f[price_field]) + float(f.get('fee_cost', 0))
                            for f in buys)
        sell_proceeds = sum(float(f['count_fp']) * float(f[price_field]) - float(f.get('fee_cost', 0))
                            for f in sells)
        buy_count     = sum(float(f['count_fp']) for f in buys)
        sell_count    = sum(float(f['count_fp']) for f in sells)
    except (KeyError, TypeError, ValueError):
        log.exception('fetch_realized_pnl_from_fills: malformed fill data for %s', ticker)
        return None

    if sell_count < buy_count - 0.01:
        return None   # only partially closed — still has an open remainder

    return {'pnl': round(sell_proceeds - buy_cost, 4), 'contracts_closed': int(round(sell_count))}


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
    Kalshi's fee is charged AT ENTRY, on every filled contract, regardless of
    how it eventually settles — fee_per_contract = fee_rate * price * (1-price)
    (same formula as trade.core.execution._ev(), which this MUST stay
    consistent with: ev_total in the logs is contracts * _ev(...), and this
    function computes what a settled trade actually realized — the two are
    meant to be directly comparable, e.g. in the Review tab's "Projected EV
    vs Realized PnL" chart).

    WIN  : receive (1 - entry_price) per contract, minus the entry fee.
    LOSS : lose entry_price per contract, ALSO minus the entry fee — it was
           already paid when the order filled, win or lose.
    VOID : stake returned, no gain or loss (fee is not charged for a void).
    SCALAR : handled separately via compute_scalar_pnl.

    For side='yes': WIN when Kalshi result='yes'.
    For side='no' : WIN when Kalshi result='no'.
    """
    winning_result   = 'no' if side == 'no' else 'yes'
    losing_result    = 'yes' if side == 'no' else 'no'
    fee_per_contract = fee_rate * entry_price * (1 - entry_price)
    if result == winning_result:
        return round(contracts * ((1 - entry_price) - fee_per_contract), 4)
    if result == losing_result:
        return round(contracts * (-entry_price - fee_per_contract), 4)
    return 0.0   # void or scalar (caller handles scalar separately)


def compute_scalar_pnl(contracts: int, entry_price: float, fee_rate: float,
                        settlement_value: float, side: str = 'yes') -> float:
    """
    Scalar / last-price settlement PnL. Same entry-fee model as compute_pnl()
    — charged once at entry based on entry_price, not conditional on the
    eventual gain/loss (a scalar settlement doesn't change how the fee was
    assessed when the order filled).
    YES holder receives `settlement_value` dollars per contract.
    NO  holder receives `1 - settlement_value` dollars per contract.
    """
    our_value        = (1 - settlement_value) if side == 'no' else settlement_value
    fee_per_contract = fee_rate * entry_price * (1 - entry_price)
    gain_per         = our_value - entry_price - fee_per_contract
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

    # One batched live fetch, not one GET per pending row — cross-referenced
    # locally below whenever a market hasn't settled yet, to catch a position
    # that was manually closed (sold back) on Kalshi in the meantime. Without
    # this, a manual close-out just sits as PENDING forever, since "has the
    # market settled" alone can never see it.
    _open_now = _open_position_tickers()

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

        _mr      = fetch_market_result(ticker)
        k_result = _mr['result']

        if k_result is None:
            if ticker in _open_now:
                console.print('[dim]not settled yet[/dim]')
                continue
            # Market hasn't settled, but we no longer hold this position —
            # closed out manually. Reconcile from actual fill prices rather
            # than leaving it PENDING indefinitely or guessing at a result.
            # No Kalshi settlement_ts exists for this (it's not a market
            # settlement event) — settled_at is when we detected it.
            closed = fetch_realized_pnl_from_fills(ticker, row_side)
            if closed is None:
                console.print('[yellow]position closed but fills unreconciled — skipping[/yellow]')
                continue
            pnl_c = 'green' if closed['pnl'] >= 0 else 'red'
            console.print(
                f'[bold cyan]CLOSED EARLY[/bold cyan]  '
                f'{closed["contracts_closed"]}ct sold back  '
                f'[{pnl_c}]pnl=${closed["pnl"]:+.4f}[/{pnl_c}]'
                + (' [dim](dry run — not written)[/dim]' if dry_run else '')
            )
            if not dry_run:
                df.at[idx, 'result']     = 'CLOSED_EARLY'
                df.at[idx, 'actual_pnl'] = closed['pnl']
                df.at[idx, 'settled_at'] = datetime.now(timezone.utc).isoformat()
            updates += 1
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
                df.at[idx, 'settled_at'] = _mr['settled_at'] or datetime.now(timezone.utc).isoformat()
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
            # Kalshi's own settlement_ts when available — the real moment the
            # market resolved, not whenever this settle pass happened to run.
            df.at[idx, 'settled_at'] = _mr['settled_at'] or datetime.now(timezone.utc).isoformat()
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
