"""
trade/mm/ledger.py — market-making fills and PnL, kept SEPARATE from the K/P bot.

Every MM fill is appended to trade/logs/mm_fills.csv. The K/P bot's PnL (trades.csv /
no_trades.csv, the Review tab, settle.py) never sees these rows, and settle.py's
fills-based reconciliation skips any fill whose order_id is in this ledger.

PnL per market (zero maker fee, so no fee terms):
  locked spread  = matched pairs × (avg ask fill − avg bid fill)   — realized the moment both sides fill
  inventory      = net unmatched contracts: settled at 100/0 once the market resolves, else marked to fair
"""
import os, csv
from datetime import datetime, timezone
import pandas as pd

LOG_DIR    = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
FILLS_PATH = os.path.join(LOG_DIR, 'mm_fills.csv')
IGNORED_PATH = os.path.join(LOG_DIR, 'mm_ignored_orders.txt')   # orders closed by hand: kept out of inventory / PnL
FIELDS     = ['ts', 'ticker', 'side', 'price_c', 'qty', 'fair_c', 'order_id']


def log_fill(ticker, side, price_c, qty, fair_c, order_id):
    os.makedirs(LOG_DIR, exist_ok=True)
    new = not os.path.exists(FILLS_PATH) or os.path.getsize(FILLS_PATH) == 0
    with open(FILLS_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({'ts': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'ticker': ticker,
                    'side': side, 'price_c': price_c, 'qty': qty,
                    'fair_c': '' if fair_c is None else fair_c, 'order_id': order_id})


def ignored_order_ids() -> set:
    try:
        with open(IGNORED_PATH) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def load_fills() -> pd.DataFrame:
    if not os.path.exists(FILLS_PATH) or os.path.getsize(FILLS_PATH) == 0:
        return pd.DataFrame(columns=FIELDS)
    f = pd.read_csv(FILLS_PATH)
    ign = ignored_order_ids()
    return f[~f['order_id'].astype(str).isin(ign)] if ign else f


def fills_for_ticker(ticker: str) -> pd.DataFrame:
    f = load_fills()
    return f[f['ticker'] == ticker] if not f.empty else f


def per_order_totals() -> dict:
    """{order_id: total qty already in the ledger} — lets a Kalshi reconcile add only what's missing."""
    f = load_fills()
    have = {} if f.empty else f.groupby(f['order_id'].astype(str))['qty'].sum().to_dict()
    return {**have, **{i: 1e9 for i in ignored_order_ids()}}      # ignored orders never get re-added


def mm_order_ids() -> set:
    return set(load_fills()['order_id'].astype(str)) | ignored_order_ids()


def pnl_table(results: dict, live_fair: dict) -> pd.DataFrame:
    """
    results:   {ticker: 'yes'|'no'|'void'|None} market outcome (None = unresolved)
    live_fair: {ticker: fair_c} for markets still tracked; others fall back to the last fair seen at a fill
    """
    f = load_fills()
    if f.empty:
        return pd.DataFrame()
    rows = []
    for t, g in f.groupby('ticker'):
        b, a = g[g['side'] == 'bid'], g[g['side'] == 'ask']
        bq, aq = b['qty'].sum(), a['qty'].sum()
        avg_b = (b['price_c'] * b['qty']).sum() / bq if bq else None
        avg_a = (a['price_c'] * a['qty']).sum() / aq if aq else None
        matched = min(bq, aq)
        locked = matched * (avg_a - avg_b) / 100 if matched else 0.0
        net = round(bq - aq, 4)                       # + = net long YES, − = net short YES (long NO)
        res = results.get(t)
        fair = live_fair.get(t)
        if fair is None:
            fv = pd.to_numeric(g['fair_c'], errors='coerce').dropna()
            fair = float(fv.iloc[-1]) if len(fv) else None
        inv = 0.0; status = 'open (marked to fair)'
        if abs(net) > 1e-9:
            if res in ('yes', 'no'):
                payoff = 100.0 if res == 'yes' else 0.0
                inv = net * (payoff - avg_b) / 100 if net > 0 else -net * (avg_a - payoff) / 100
                status = f'settled {res.upper()}'
            elif res == 'void':
                status = 'void'
            elif fair is not None:
                inv = net * (fair - avg_b) / 100 if net > 0 else -net * (avg_a - fair) / 100
        elif res in ('yes', 'no', 'void'):
            status = f'settled {str(res).upper()}'
        else:
            status = 'flat'
        rows.append({'ticker': t, 'bid_qty': round(bq, 2), 'avg_bid': None if avg_b is None else round(avg_b, 2),
                     'ask_qty': round(aq, 2), 'avg_ask': None if avg_a is None else round(avg_a, 2),
                     'matched': round(matched, 2), 'locked_$': round(locked, 3), 'net_inv': net,
                     'inventory_$': round(inv, 3), 'total_$': round(locked + inv, 3), 'status': status})
    return pd.DataFrame(rows)
