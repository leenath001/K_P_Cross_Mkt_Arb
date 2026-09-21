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
MARKOUTS_PATH = os.path.join(LOG_DIR, 'mm_markouts.csv')
FIELDS     = ['ts', 'ticker', 'side', 'price_c', 'qty', 'fair_c', 'order_id', 'liq', 'fee_usd']
MARKOUT_FIELDS = ['ts', 'ticker', 'side', 'price_c', 'qty', 'fair0_c', 'fair_1m_c', 'fair_5m_c', 'order_id']


def _ensure_schema():
    """Older ledgers have no liquidity / fee columns: add them (maker, $0) before appending a new-format row."""
    if not os.path.exists(FILLS_PATH) or os.path.getsize(FILLS_PATH) == 0:
        return
    with open(FILLS_PATH) as f:
        header = f.readline().strip().split(',')
    if header != FIELDS:
        df = pd.read_csv(FILLS_PATH)
        if 'liq' not in df:
            df['liq'] = 'maker'
        if 'fee_usd' not in df:
            df['fee_usd'] = 0.0
        df[FIELDS].to_csv(FILLS_PATH, index=False)


def log_markout(ticker, side, price_c, qty, fair0, fair_1m, fair_5m, order_id):
    os.makedirs(LOG_DIR, exist_ok=True)
    new = not os.path.exists(MARKOUTS_PATH) or os.path.getsize(MARKOUTS_PATH) == 0
    with open(MARKOUTS_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=MARKOUT_FIELDS)
        if new:
            w.writeheader()
        w.writerow({'ts': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'ticker': ticker, 'side': side,
                    'price_c': price_c, 'qty': qty, 'fair0_c': fair0, 'fair_1m_c': fair_1m, 'fair_5m_c': fair_5m,
                    'order_id': order_id})


def load_markouts() -> pd.DataFrame:
    if not os.path.exists(MARKOUTS_PATH) or os.path.getsize(MARKOUTS_PATH) == 0:
        return pd.DataFrame(columns=MARKOUT_FIELDS)
    return pd.read_csv(MARKOUTS_PATH)


def log_fill(ticker, side, price_c, qty, fair_c, order_id, liq='maker', fee_usd=0.0):
    os.makedirs(LOG_DIR, exist_ok=True)
    _ensure_schema()
    new = not os.path.exists(FILLS_PATH) or os.path.getsize(FILLS_PATH) == 0
    with open(FILLS_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({'ts': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'ticker': ticker,
                    'side': side, 'price_c': price_c, 'qty': qty,
                    'fair_c': '' if fair_c is None else fair_c, 'order_id': order_id,
                    'liq': liq, 'fee_usd': round(float(fee_usd), 4)})


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
    if 'liq' not in f:
        f['liq'] = 'maker'
    if 'fee_usd' not in f:
        f['fee_usd'] = 0.0
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
        fees = float(pd.to_numeric(g['fee_usd'], errors='coerce').fillna(0).sum())
        rows.append({'ticker': t, 'bid_qty': round(bq, 2), 'avg_bid': None if avg_b is None else round(avg_b, 2),
                     'ask_qty': round(aq, 2), 'avg_ask': None if avg_a is None else round(avg_a, 2),
                     'matched': round(matched, 2), 'locked_$': round(locked, 3), 'net_inv': net,
                     'inventory_$': round(inv, 3), 'fees_$': round(fees, 3),
                     'total_$': round(locked + inv - fees, 3), 'status': status})
    return pd.DataFrame(rows)


# ── market-maker analytics ────────────────────────────────────────────────────

def walk_fills(f: pd.DataFrame) -> pd.DataFrame:
    """
    Replay every fill in order with average-cost accounting per market. Adds, per fill:
      realized_$   profit booked by that fill when it closes existing inventory (net of its own taker fee)
      pos          net position in that market after the fill (+ long YES, − long NO)
      edge_c       price improvement vs Pinnacle fair at the moment of the fill (+ = we got the better side of fair)
    """
    if f.empty:
        return f.assign(realized_usd=[], pos=[], edge_c=[], cum_realized=[], net_pos_all=[])
    f = f.sort_values('ts', kind='stable').reset_index(drop=True).copy()
    state, real, poss = {}, [], []
    for r in f.itertuples():
        pos, avg = state.get(r.ticker, (0.0, 0.0))
        q, p = float(r.qty), float(r.price_c)
        signed = q if r.side == 'bid' else -q
        gain = 0.0
        if pos == 0 or (pos > 0) == (signed > 0):
            avg = (avg * abs(pos) + p * q) / (abs(pos) + q)
            pos += signed
        else:
            closing = min(q, abs(pos))
            gain = closing * ((p - avg) if pos > 0 else (avg - p)) / 100
            pos += signed
            if abs(pos) < 1e-9:
                pos, avg = 0.0, 0.0
            elif (pos > 0) == (signed > 0):
                avg = p
        fee = float(r.fee_usd) if pd.notna(r.fee_usd) else 0.0
        real.append(gain - fee)
        state[r.ticker] = (pos, avg)
        poss.append(pos)
    f['realized_usd'] = real
    f['pos'] = poss
    fair = pd.to_numeric(f['fair_c'], errors='coerce')
    f['edge_c'] = (fair - f['price_c']).where(f['side'] == 'bid', f['price_c'] - fair)
    f['cum_realized'] = f['realized_usd'].cumsum()
    last = {}
    net = []
    for r in f.itertuples():
        last[r.ticker] = r.pos
        net.append(sum(last.values()))
    f['net_pos_all'] = net
    return f


def mm_summary(f: pd.DataFrame, marks: pd.DataFrame, results: dict) -> dict:
    """Numbers a market maker actually watches. Returns {'kpi': [(label, value, help)], 'by_market': DataFrame}."""
    w = walk_fills(f)
    q = w['qty'].astype(float)
    maker = w['liq'] == 'maker'
    fee_total = float(pd.to_numeric(w['fee_usd'], errors='coerce').fillna(0).sum())
    contracts = float(q.sum())
    notional = float((q * w['price_c'].where(w['side'] == 'bid', 100 - w['price_c']) / 100).sum())
    bought, sold = float(q[w['side'] == 'bid'].sum()), float(q[w['side'] == 'ask'].sum())
    e = w.dropna(subset=['edge_c'])
    e = e[e['liq'] == 'maker']
    edge = float((e['edge_c'] * e['qty']).sum() / e['qty'].sum()) if len(e) and e['qty'].sum() else None

    def markout(col):
        m = marks.dropna(subset=['fair0_c', col]) if len(marks) else marks
        if m is None or not len(m):
            return None
        d = (m[col] - m['price_c']).where(m['side'] == 'bid', m['price_c'] - m[col])
        return float((d * m['qty']).sum() / m['qty'].sum())
    m1, m5 = (markout('fair_1m_c'), markout('fair_5m_c')) if len(marks) else (None, None)

    # round trips and per-market table
    rows = []
    for t, g in w.groupby('ticker'):
        b, a = g[g['side'] == 'bid'], g[g['side'] == 'ask']
        bq, aq = b['qty'].sum(), a['qty'].sum()
        avg_b = (b['price_c'] * b['qty']).sum() / bq if bq else None
        avg_a = (a['price_c'] * a['qty']).sum() / aq if aq else None
        matched = min(bq, aq)
        rows.append({'market': t, 'series': t.split('-')[0], 'fills': len(g), 'contracts': round(g['qty'].sum(), 1),
                     'bought': round(bq, 1), 'sold': round(aq, 1), 'net_pos': round(bq - aq, 1),
                     'peak_|pos|': round(g['pos'].abs().max(), 1), 'avg_|pos|': round(g['pos'].abs().mean(), 1),
                     'round_trips': round(matched, 1),
                     'spread_captured_c': None if not matched else round(avg_a - avg_b, 2),
                     'avg_edge_c': None if g['edge_c'].dropna().empty else round(g['edge_c'].dropna().mean(), 2),
                     'realized_$': round(g['realized_usd'].sum(), 3), 'fees_$': round(float(pd.to_numeric(g['fee_usd'], errors='coerce').fillna(0).sum()), 3),
                     'settled': results.get(t) if results.get(t) in ('yes', 'no', 'void') else ''})
    bm = pd.DataFrame(rows)
    rt = float((bm['round_trips']).sum()) if len(bm) else 0.0
    cap = float((bm['round_trips'] * bm['spread_captured_c'].fillna(0)).sum() / rt) if rt else None

    # fills on settled markets: how often did the side we were filled on turn out right?
    ok = tot = 0.0
    for r in w.itertuples():
        res = results.get(r.ticker)
        if res in ('yes', 'no'):
            tot += r.qty
            ok += r.qty if ((r.side == 'bid') == (res == 'yes')) else 0.0
    kpi = [
        ('Fills / contracts', f'{len(w)} / {contracts:.0f}', 'Fill events and contracts traded (both sides).'),
        ('Notional traded', f'${notional:,.2f}', 'Cash committed across all fills (price for buys of YES, 1−price for sells).'),
        ('Realized PnL (net of fees)', f'${w["realized_usd"].sum():+.3f}', 'Profit booked when a fill closed inventory, average-cost basis, after taker fees.'),
        ('Fees paid', f'${fee_total:.2f}', 'Taker fees on offload trades. Resting fills on zero-fee series cost nothing.'),
        ('Avg edge at fill', 'n/a' if edge is None else f'{edge:+.2f}¢', 'Price improvement versus Pinnacle fair at the instant of the fill, qty-weighted (maker fills). This is the spread you earn per contract before adverse moves.'),
        ('Markout 1 min', 'n/a' if m1 is None else f'{m1:+.2f}¢', 'Fair value one minute after our fill versus our fill price. Negative = we were picked off (adverse selection).'),
        ('Markout 5 min', 'n/a' if m5 is None else f'{m5:+.2f}¢', 'Same at five minutes.'),
        ('Round trips / captured', 'n/a' if not rt else f'{rt:.0f} @ {cap:.2f}¢', 'Contracts we bought AND sold in the same market, and the average ask−bid gap they locked in.'),
        ('Buy / sell balance', f'{bought:.0f} / {sold:.0f}', 'Lopsided fills mean we are accumulating inventory rather than turning it over.'),
        ('Maker share', f'{100 * q[maker].sum() / contracts:.0f}%' if contracts else 'n/a', 'Share of contracts filled while resting (the rest were offloaded through the book).'),
        ('Peak |position| / now (net)', f'{bm["peak_|pos|"].max():.0f} / {abs(bm["net_pos"]).sum():.0f}' if len(bm) else 'n/a', 'Largest single-market position held, and total net contracts held now.'),
        ('Settled fill hit-rate', 'n/a' if not tot else f'{100 * ok / tot:.0f}%', 'Of contracts filled on settled markets, the share whose side won. ~50% is neutral; well below means we are consistently picked off.'),
    ]
    return {'kpi': kpi, 'by_market': bm, 'walk': w, 'marks': marks}
