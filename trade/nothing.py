"""
nothing.py — "Nothing Ever Happens" bot.

Fully standalone from the K/P arbitrage pipeline. Places resting or taker
NO buy orders on single-outcome Kalshi event markets under the assumption
that most speculative "will X happen?" questions resolve NO. Buys ONE NO
contract per qualifying market, capped at a % of Kalshi cash (default 10%)
so the main K/P bot retains capital.

What it will NOT touch:
  - Sports series (any ticker that appears in config.SPORTS_CONFIG)
  - Multi-market events (A-vs-B style) — only events with ONE open market

Usage:
    python trade/nothing.py --series KXSERIES                                 # dry run, 10% of cash
    python trade/nothing.py --series KXSERIES --live                          # place orders
    python trade/nothing.py --series KXSERIES --budget-pct 0.15 --live        # 15% of cash
    python trade/nothing.py --series KXSERIES --budget 20 --live              # override to fixed $
    python trade/nothing.py --tickers MKT-A MKT-B --live
    python trade/nothing.py --series KXSERIES --rest --live                   # maker mode
    python trade/nothing.py --cancel-all                                      # cancel every open NO order this bot placed

Logs to trade/logs/nothing_trades.csv (separate from trades.csv / no_trades.csv).
"""

import os, sys, csv, uuid, base64, signal, argparse, threading, time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

# ── Paths & self-contained Kalshi auth ───────────────────────────────────────

_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
import config  # read-only: SPORTS_CONFIG for sports-series blocklist
from nothing_config import NOTHING_SERIES, tickers as _nothing_tickers

LOG_DIR  = os.path.join(_HERE, 'logs')
LOG_PATH = os.path.join(LOG_DIR, 'nothing_trades.csv')

load_dotenv(os.path.join(_ROOT, '.env'), override=True)
API_KEY     = os.getenv('API_KEY')
API_PRIVATE = os.getenv('API_PRIVATE')
BASE_URL    = 'https://api.elections.kalshi.com/trade-api/v2'

assert API_PRIVATE, 'API_PRIVATE not found — set it in .env'
_pem = (b'-----BEGIN RSA PRIVATE KEY-----\n' +
        API_PRIVATE.strip().encode() +
        b'\n-----END RSA PRIVATE KEY-----\n')
_PRIVATE_KEY = serialization.load_pem_private_key(_pem, password=None)

TAKER_FEE = 0.07
MAKER_FEE = 0.03

SPORTS_SERIES = {cfg['ticker'] for cfg in config.SPORTS_CONFIG.values()}

FIELDS = [
    'logged_at', 'order_id', 'series_ticker', 'event_ticker', 'k_ticker',
    'title', 'mode', 'entry_price', 'entry_price_cents', 'fee_rate',
    'contracts', 'total_cost', 'max_payout', 'expires_at',
    'final_status', 'close_reason', 'result', 'actual_pnl',
]


def _headers(method: str, path: str) -> dict:
    ts  = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = _PRIVATE_KEY.sign(
        msg,
        asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()),
                         salt_length=asym_padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        'KALSHI-ACCESS-KEY':       API_KEY,
        'KALSHI-ACCESS-TIMESTAMP': ts,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
    }


# ── Market fetch ─────────────────────────────────────────────────────────────

def fetch_series_markets(series: str) -> list:
    """All open markets under a series_ticker."""
    path = '/trade-api/v2/markets'
    resp = requests.get(f'{BASE_URL}/markets',
                        headers=_headers('GET', path),
                        params={'series_ticker': series, 'status': 'open', 'limit': 500})
    resp.raise_for_status()
    return resp.json().get('markets', [])


def fetch_ticker(ticker: str) -> Optional[dict]:
    path = f'/trade-api/v2/markets/{ticker}'
    resp = requests.get(f'{BASE_URL}/markets/{ticker}', headers=_headers('GET', path))
    if not resp.ok:
        return None
    return resp.json().get('market')


def _event_market_count(markets: list) -> dict:
    """Count open markets per event_ticker — used to drop multi-outcome events."""
    counts = {}
    for m in markets:
        ev = m.get('event_ticker')
        counts[ev] = counts.get(ev, 0) + 1
    return counts


def filter_markets(markets: list, max_no_price: float,
                   mutually_exclusive_only_filter: bool = True) -> list:
    """
    Keep only markets that:
      - Have no_ask set and 0 < no_ask <= max_no_price
      - Are NOT part of a sports series (series_ticker not in SPORTS_SERIES)
      - If mutually_exclusive_only_filter is True: drop markets whose event's
        yes_ask prices sum to ≤ 1.10 (A-vs-B style mutually-exclusive groups —
        buying NO on all of them is a guaranteed loss on the one that wins).
        Independent multi-market events (e.g. "will Trump say X / Y / Z" under
        one earnings call) have yes sums well above 1.0 and pass through.
    """
    # Sum yes_ask per event to distinguish mutually-exclusive from independent
    yes_sum = {}
    for m in markets:
        ev = m.get('event_ticker') or ''
        y  = m.get('yes_ask_dollars')
        if y is None:
            continue
        yes_sum[ev] = yes_sum.get(ev, 0.0) + float(y)

    kept = []
    for m in markets:
        series = (m.get('event_ticker') or '').split('-')[0]
        if series in SPORTS_SERIES:
            continue
        no_ask = m.get('no_ask_dollars')
        if no_ask is None:
            continue
        no_ask = float(no_ask)
        if no_ask <= 0 or no_ask > max_no_price:
            continue
        if mutually_exclusive_only_filter:
            ev = m.get('event_ticker') or ''
            # A sum near 1.0 (with slack for vig) means the legs are mutually
            # exclusive — skip. Independent-word events have sums >> 1.0.
            if ev in yes_sum and yes_sum[ev] <= 1.10:
                continue
        kept.append(m)
    return kept


# ── Sizing ───────────────────────────────────────────────────────────────────

def _dynamic_contracts(no_price: float) -> int:
    """
    Partial-Kelly-style sizing clamped to [1, 5]. Cheaper NO = higher implied
    P(YES) = larger "nothing ever happens" edge under the thesis → more size.

        no_price ≤ 0.10  →  5 contracts   (implied YES ≥ 90%)
        no_price ≤ 0.20  →  4
        no_price ≤ 0.30  →  3
        no_price ≤ 0.40  →  2
        else            →  1
    """
    if no_price <= 0.10: return 5
    if no_price <= 0.20: return 4
    if no_price <= 0.30: return 3
    if no_price <= 0.40: return 2
    return 1


def plan_contracts(markets: list, budget: float, rest: bool,
                   dynamic: bool = False) -> tuple:
    """
    Sort markets cheapest NO first; take until running cost would exceed budget.

    dynamic=False  →  1 contract per market (equal sizing, default)
    dynamic=True   →  partial-Kelly tiered sizing in [1, 5]

    Returns (plans, skipped) where:
      plans   = list of (market, contracts, price_cents, mode)
      skipped = list of markets dropped for budget
    """
    priced = []
    for m in markets:
        no_ask = float(m['no_ask_dollars'])
        price  = round(no_ask - 0.01, 2) if rest else no_ask
        mode   = 'rest' if rest else 'cross'
        if price < 0.01:
            continue
        priced.append((m, price, mode))
    priced.sort(key=lambda t: t[1])

    plans, skipped, running = [], [], 0.0
    for m, price, mode in priced:
        n = _dynamic_contracts(price) if dynamic else 1
        # If dynamic size doesn't fit, try a smaller size down to 1
        while n >= 1 and running + n * price > budget:
            n -= 1
        if n < 1:
            skipped.append(m)
            continue
        plans.append((m, n, round(price * 100), mode))
        running += n * price
    return plans, skipped


# Backwards-compatible alias so older callers keep working
def size_one_each(markets: list, budget: float, rest: bool) -> tuple:
    return plan_contracts(markets, budget, rest, dynamic=False)


# ── Orders ───────────────────────────────────────────────────────────────────

def place_no_order(ticker: str, no_price_cents: int, count: int,
                   mode: str, expiration_ts: int) -> dict:
    """Buy NO. Kalshi always signs yes_price, so we send (100 - no_price)."""
    path = '/trade-api/v2/portfolio/orders'
    body = {
        'ticker':          ticker,
        'client_order_id': str(uuid.uuid4()),
        'type':            'limit',
        'action':          'buy',
        'side':            'no',
        'count':           count,
        'yes_price':       100 - no_price_cents,
        'expiration_ts':   expiration_ts,
    }
    if mode == 'rest':
        body['post_only'] = True
    resp = requests.post(f'{BASE_URL}/portfolio/orders',
                         headers={**_headers('POST', path), 'Content-Type': 'application/json'},
                         json=body)
    if not resp.ok:
        raise requests.HTTPError(f'{resp.status_code} {resp.reason} — {resp.text}', response=resp)
    return resp.json().get('order', {})


def cancel_order(order_id: str) -> bool:
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    resp = requests.delete(f'{BASE_URL}/portfolio/orders/{order_id}',
                           headers=_headers('DELETE', path))
    return resp.status_code in (200, 204)


TERMINAL_STATUSES = {'executed', 'filled', 'canceled', 'expired'}


def get_order_status(order_id: str) -> dict:
    """Single-order lookup. Returns {} or the order dict."""
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    resp = requests.get(f'{BASE_URL}/portfolio/orders/{order_id}',
                        headers=_headers('GET', path))
    if resp.ok:
        return resp.json().get('order', {})
    return {'status': 'unknown', 'order_id': order_id}


def monitor_orders(tracked: list, stop_event: threading.Event, state: dict,
                   ttl_seconds: int = 1800, poll_seconds: int = 10,
                   close_buffer_seconds: int = 300) -> None:
    """
    Background polling loop for a set of placed NO orders.

    tracked: list of dicts — {order_id, ticker, title, contracts, entry_cents,
                              close_time (datetime|None)}
    state:   shared dict — {order_id: {status, filled, remaining, last_ping,
                                       reason, elapsed, ...}, '_lock': Lock}
             ALL reads/writes must go through state['_lock'].

    Cancels any non-terminal order when:
      - stop_event is set (user cancel)
      - elapsed ≥ ttl_seconds (default 30 min)
      - market close_time is within close_buffer_seconds (default 5 min)
    """
    lock = state.setdefault('_lock', threading.Lock())
    start = time.time()
    open_ids = {o['order_id']: o for o in tracked if o.get('order_id')}

    # Seed state with initial rows so UI has something to show on first tick
    with lock:
        for oid, o in open_ids.items():
            state.setdefault(oid, {
                'ticker':      o['ticker'],
                'title':       o.get('title', ''),
                'contracts':   o.get('contracts', 0),
                'entry_cents': o.get('entry_cents', 0),
                'status':      'pending',
                'filled':      0,
                'remaining':   o.get('contracts', 0),
                'last_ping':   '',
                'reason':      '',
                'elapsed':     0,
            })

    while open_ids:
        if stop_event.is_set():
            for oid, o in list(open_ids.items()):
                try: cancel_order(oid)
                except Exception: pass
                with lock:
                    rec = state.setdefault(oid, {'ticker': o['ticker']})
                    rec.update(status='canceled', reason='user_canceled',
                               last_ping=datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))
            break

        elapsed = time.time() - start
        now_utc = datetime.now(timezone.utc)

        for oid, o in list(open_ids.items()):
            od = get_order_status(oid)
            status    = od.get('status', 'unknown')
            remaining = od.get('remaining_count_fp') or od.get('remaining_count') or 0
            try: remaining = int(float(remaining))
            except Exception: remaining = 0
            filled = max(o.get('contracts', 0) - remaining, 0)

            reason = ''
            if status in TERMINAL_STATUSES:
                reason = f'order_{status}'
            elif elapsed >= ttl_seconds:
                if cancel_order(oid):
                    status, reason = 'canceled', 'max_duration'
            elif o.get('close_time'):
                to_close = (o['close_time'] - now_utc).total_seconds()
                if to_close <= close_buffer_seconds:
                    if cancel_order(oid):
                        status, reason = 'canceled', 'market_closing'

            with lock:
                rec = state.setdefault(oid, {})
                rec.update(
                    ticker=o['ticker'],
                    title=o.get('title', ''),
                    contracts=o.get('contracts', 0),
                    entry_cents=o.get('entry_cents', 0),
                    status=status,
                    filled=filled,
                    remaining=remaining,
                    last_ping=datetime.now(timezone.utc).strftime('%H:%M:%S UTC'),
                    reason=reason,
                    elapsed=int(elapsed),
                )

            if status in TERMINAL_STATUSES or reason:
                open_ids.pop(oid, None)

        if not open_ids:
            break
        if stop_event.wait(poll_seconds):
            continue


def list_my_open_orders() -> list:
    path = '/trade-api/v2/portfolio/orders'
    resp = requests.get(f'{BASE_URL}/portfolio/orders',
                        headers=_headers('GET', path),
                        params={'status': 'resting', 'limit': 200})
    resp.raise_for_status()
    return resp.json().get('orders', [])


def get_balance() -> float:
    path = '/trade-api/v2/portfolio/balance'
    resp = requests.get(f'{BASE_URL}/portfolio/balance', headers=_headers('GET', path))
    resp.raise_for_status()
    return resp.json().get('balance', 0) / 100


# ── Logging ──────────────────────────────────────────────────────────────────

def log_trade(row: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    write_header = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
    with open(LOG_PATH, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)


_LOG_WRITE_LOCK = threading.Lock()


def update_log_row(order_id: str, *, final_status: Optional[str] = None,
                   close_reason: Optional[str] = None,
                   filled_contracts: Optional[int] = None) -> bool:
    """
    Mutate the nothing_trades.csv row matching order_id. Returns True if
    updated. Serialized via a module-level lock since the monitor thread
    calls this concurrently with other readers.
    """
    if not os.path.exists(LOG_PATH):
        return False
    with _LOG_WRITE_LOCK:
        with open(LOG_PATH, newline='') as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or FIELDS
            rows   = list(reader)

        found = False
        for r in rows:
            if r.get('order_id') == order_id:
                if final_status is not None:
                    r['final_status'] = final_status
                if close_reason is not None:
                    r['close_reason'] = close_reason
                if filled_contracts is not None:
                    r['contracts'] = filled_contracts
                    try:
                        entry = float(r.get('entry_price') or 0)
                        fee   = float(r.get('fee_rate') or 0)
                        r['total_cost'] = round(filled_contracts * entry, 4)
                        r['max_payout'] = round(
                            filled_contracts * (1 - entry) * (1 - fee), 4)
                    except Exception:
                        pass
                found = True

        if found:
            with open(LOG_PATH, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
    return found


def already_bet_tickers() -> set:
    """
    Tickers we shouldn't re-bet on. Includes rows that are still open
    (final_status in resting/executed/filled) with result=PENDING. Canceled or
    expired rows are allowed to be re-bet since those positions never filled
    or already closed out.
    """
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return set()
    tickers = set()
    with open(LOG_PATH, newline='') as f:
        for row in csv.DictReader(f):
            if (row.get('result') == 'PENDING' and
                row.get('final_status', '') in ('resting', 'executed', 'filled')):
                if row.get('k_ticker'):
                    tickers.add(row['k_ticker'])
    return tickers


def _order_ids_from_log() -> set:
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return set()
    ids = set()
    with open(LOG_PATH, newline='') as f:
        for row in csv.DictReader(f):
            if row.get('order_id'):
                ids.add(row['order_id'])
    return ids


# ── Main ─────────────────────────────────────────────────────────────────────

def run(args):
    # Collect markets
    markets = []
    if args.series:
        for s in args.series:
            if s in SPORTS_SERIES:
                print(f'  refusing {s} — sports series is blocklisted')
                continue
            fetched = fetch_series_markets(s)
            for m in fetched:
                m['_series'] = s
            markets.extend(fetched)
    if args.tickers:
        for t in args.tickers:
            m = fetch_ticker(t)
            if m is None:
                print(f'  {t} — not found')
                continue
            m['_series'] = (m.get('event_ticker') or '').split('-')[0]
            markets.append(m)

    if not markets:
        print('No markets found.')
        return

    kept = filter_markets(markets, max_no_price=args.max_no_price,
                          mutually_exclusive_only_filter=not args.allow_mutually_exclusive)

    # Drop tickers we already have open positions on (dedup safety)
    existing = already_bet_tickers()
    if existing:
        before = len(kept)
        kept = [m for m in kept if m['ticker'] not in existing]
        print(f'  Skipped {before - len(kept)} ticker(s) with existing open/pending positions')

    print(f'\n  fetched={len(markets)}  kept={len(kept)}  '
          f'(max_no_price={args.max_no_price}, drop_mutex={not args.allow_mutually_exclusive})')

    # Resolve budget: explicit --budget wins; else budget_pct of Kalshi cash
    if args.budget is not None:
        budget = args.budget
        print(f'  Budget    : ${budget:.2f}  (manual override)')
    else:
        try:
            cash = get_balance()
        except Exception as exc:
            print(f'  failed to fetch balance: {exc}')
            return
        budget = round(cash * args.budget_pct, 2)
        print(f'  Cash      : ${cash:.2f}  →  budget ${budget:.2f} ({args.budget_pct*100:.0f}% reserved for nothing-bot)')

    plans, skipped = plan_contracts(kept, budget, rest=args.rest,
                                    dynamic=args.dynamic)
    if not plans:
        print('No plans — budget cannot afford a single contract at current NO prices.')
        return

    total_cost   = sum(n * (c / 100) for _, n, c, _ in plans)
    total_cts    = sum(n for _, n, _, _ in plans)
    mode_label   = 'REST (maker, post_only)' if args.rest else 'CROSS (taker)'
    size_label   = 'DYNAMIC (partial Kelly, 1–5)' if args.dynamic else 'EQUAL (1 per market)'
    fee_rate     = MAKER_FEE if args.rest else TAKER_FEE

    print('=' * 72)
    print(f'  Mode      : {mode_label}')
    print(f'  Sizing    : {size_label}')
    print(f'  Positions : {len(plans)} market(s), {total_cts} contracts '
          f'(skipped {len(skipped)} for budget)')
    print(f'  Est cost  : ${total_cost:.2f}  / ${budget:.2f} budget')
    print(f'  TTL       : {args.ttl_min} min')
    print('=' * 72)
    for m, n, c, _mode in plans:
        title = (m.get('title') or m.get('yes_sub_title') or '')[:60]
        print(f"  {m['ticker']:30s}  {n}x NO @ {c}¢  ${n*c/100:5.2f}   {title}")
    if skipped:
        print(f'\n  Skipped (over budget): {[m["ticker"] for m in skipped[:5]]}'
              f'{" …" if len(skipped) > 5 else ""}')

    if not args.live:
        print('\n  DRY RUN — pass --live to place orders.')
        return

    # Confirmation gate
    ans = input(f'\n  Place {len(plans)} NO orders for ~${total_cost:.2f}? [y/N] ').strip().lower()
    if ans != 'y':
        print('  Aborted.')
        return

    expiry_ts = int((datetime.now(timezone.utc) + timedelta(minutes=args.ttl_min)).timestamp())
    placed    = []  # order_ids for Ctrl+C safety
    summary   = []

    def _on_sigint(signum, frame):
        print('\n[shutdown] Ctrl+C — canceling orders...')
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for oid in placed:
            try:
                if cancel_order(oid):
                    print(f'  canceled {oid}')
            except Exception as exc:
                print(f'  cancel failed {oid}: {exc}')
        sys.exit(130)

    signal.signal(signal.SIGINT, _on_sigint)

    for m, n, price_cents, mode in plans:
        try:
            order = place_no_order(m['ticker'], price_cents, n, mode, expiry_ts)
        except requests.HTTPError as exc:
            print(f'  {m["ticker"]}  FAILED  {exc}')
            summary.append({'ticker': m['ticker'], 'status': 'error', 'reason': str(exc)})
            continue

        oid     = order.get('order_id')
        status  = order.get('status', 'unknown')
        if oid:
            placed.append(oid)
        entry_p = price_cents / 100
        row = {
            'logged_at':         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
            'order_id':          oid,
            'series_ticker':     m.get('_series', ''),
            'event_ticker':      m.get('event_ticker', ''),
            'k_ticker':          m['ticker'],
            'title':             (m.get('title') or '')[:120],
            'mode':              mode,
            'entry_price':       entry_p,
            'entry_price_cents': price_cents,
            'fee_rate':          fee_rate,
            'contracts':         n,
            'total_cost':        round(n * entry_p, 4),
            'max_payout':        round(n * (1 - entry_p) * (1 - fee_rate), 4),
            'expires_at':        datetime.fromtimestamp(expiry_ts, tz=timezone.utc).isoformat(),
            'final_status':      status,
            'close_reason':      '',
            'result':            'PENDING',
            'actual_pnl':        '',
        }
        log_trade(row)
        summary.append({'ticker': m['ticker'], 'status': status, 'order_id': oid,
                        'contracts': n, 'price': price_cents})
        print(f'  {m["ticker"]:30s}  {status:10s}  {n}x @ {price_cents}¢  id={oid}')

    filled   = [s for s in summary if s.get('status') in ('executed', 'filled')]
    resting  = [s for s in summary if s.get('status') == 'resting']
    errored  = [s for s in summary if s.get('status') == 'error']
    print(f'\n  placed={len(placed)}  filled={len(filled)}  resting={len(resting)}  errors={len(errored)}')


def cancel_all_tracked():
    """Cancel any order in nothing_trades.csv that is still open on Kalshi."""
    tracked = _order_ids_from_log()
    if not tracked:
        print('No tracked orders in nothing_trades.csv.')
        return
    try:
        open_orders = list_my_open_orders()
    except Exception as exc:
        print(f'  failed to list open orders: {exc}')
        return
    canceled = 0
    for o in open_orders:
        oid = o.get('order_id')
        if oid in tracked:
            if cancel_order(oid):
                canceled += 1
                print(f'  canceled {oid}  {o.get("ticker")}')
    print(f'\n  canceled {canceled} tracked order(s)')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='"Nothing Ever Happens" NO-side bot')
    p.add_argument('--series',       nargs='+', help='One or more Kalshi series tickers')
    p.add_argument('--tickers',      nargs='+', help='Explicit market tickers to trade NO on')
    p.add_argument('--budget',       type=float, default=None,
                   help='Hard $ cap (overrides --budget-pct). Default: use budget-pct of Kalshi cash')
    p.add_argument('--budget-pct',   type=float, default=0.10,
                   help='Fraction of Kalshi cash to reserve for this bot (default 0.10)')
    p.add_argument('--max-no-price', type=float, default=0.50,
                   help='Skip markets with no_ask above this (default 0.50)')
    p.add_argument('--rest',         action='store_true',
                   help='Rest at no_ask-1¢ post_only (maker). Default: cross at no_ask (taker)')
    p.add_argument('--dynamic',      action='store_true',
                   help='Partial-Kelly sizing clamped to 1-5 contracts based on NO price. '
                        'Default: 1 contract per market (equal sizing).')
    p.add_argument('--ttl-min',      type=int, default=60,
                   help='Order expiration in minutes (default 60)')
    p.add_argument('--allow-mutually-exclusive', action='store_true',
                   help='Do not drop events whose yes-ask prices sum to ~1.0 (A-vs-B style)')
    p.add_argument('--live',         action='store_true', help='Place real orders')
    p.add_argument('--cancel-all',   action='store_true',
                   help='Cancel every open order this bot placed (per nothing_trades.csv)')
    args = p.parse_args()

    if args.cancel_all:
        cancel_all_tracked()
        sys.exit(0)

    if not args.series and not args.tickers:
        default = _nothing_tickers()
        if not default:
            p.error('need --series or --tickers (or --cancel-all). '
                    'Curated list in trade/nothing_config.py is empty.')
        args.series = default
        print(f'  Using curated list from nothing_config.py ({len(default)} series)')
    if args.budget is not None and args.budget <= 0:
        p.error('--budget must be > 0')
    if args.budget_pct <= 0 or args.budget_pct > 1:
        p.error('--budget-pct must be between 0 and 1')

    run(args)
