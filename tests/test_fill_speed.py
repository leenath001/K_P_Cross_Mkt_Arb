"""
test_fill_speed.py — Does improving the bid by 1¢ speed up NO fills?

How Kalshi NO-side pricing works (e.g. market quoted 49/51 in YES terms):
  NO bid = 49¢   — best price buyers of NO are offering  ← resting here joins queue
  NO ask = 51¢   — best price sellers of NO will accept  ← crossing here = taker fill

Placing AT the NO ask (or above) crosses the spread and fills immediately as a taker.
Resting orders must be BELOW the NO ask.

Hypothesis: placing at (NO bid + 1¢) makes you the new best bid, so when a seller
arrives you fill first — potentially much faster than sitting at the back of the bid
queue. The cost is 1 extra cent per contract.

This script places TWO small resting orders on the same market simultaneously:
  A) at current NO bid price  (joins back of queue)
  B) at NO bid + 1¢           (improves bid by 1¢, still resting, better priority)

Then polls both and reports which filled first and the time difference.

Usage:
    python test_fill_speed.py --ticker KXSOME-MKTCODE --contracts 1

NOTE: Places REAL orders. Use a liquid mention-style market with 1 contract.
      Run --cancel-only <id_a> <id_b> afterward if orders don't fill.
"""

import os, sys, time, argparse
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from KALSHI.k_helpers import kalshi_headers

BASE_URL = 'https://api.elections.kalshi.com/trade-api/v2'


# ── Kalshi helpers ────────────────────────────────────────────────────────────

def get_market(ticker: str) -> dict:
    path = f'/trade-api/v2/markets/{ticker}'
    r = requests.get(f'{BASE_URL}/markets/{ticker}',
                     headers=kalshi_headers('GET', path))
    r.raise_for_status()
    return r.json()['market']


def get_orderbook(ticker: str) -> dict:
    """Returns {'yes': [...], 'no': [...]} price ladders."""
    path = f'/trade-api/v2/markets/{ticker}/orderbook'
    r = requests.get(f'{BASE_URL}/markets/{ticker}/orderbook',
                     headers=kalshi_headers('GET', path))
    r.raise_for_status()
    return r.json().get('orderbook', {})


def place_order(ticker: str, no_price_cents: int, contracts: int,
                expiry_ts: int, label: str) -> dict:
    """
    Place a resting NO order (post_only=True).
    yes_price = 100 - no_price_cents per Kalshi convention.
    """
    body = {
        'ticker':       ticker,
        'action':       'buy',
        'side':         'no',
        'type':         'limit',
        'count':        contracts,
        'no_price':     no_price_cents,
        'yes_price':    100 - no_price_cents,
        'post_only':    True,
        'client_order_id': f'filltest_{label}_{int(time.time())}',
        'expiration_ts':   expiry_ts,
    }
    path = '/trade-api/v2/portfolio/orders'
    r = requests.post(f'{BASE_URL}/portfolio/orders',
                      json={'order': body},
                      headers=kalshi_headers('POST', path))
    r.raise_for_status()
    return r.json().get('order', {})


def get_order(order_id: str) -> dict:
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    r = requests.get(f'{BASE_URL}/portfolio/orders/{order_id}',
                     headers=kalshi_headers('GET', path))
    r.raise_for_status()
    return r.json().get('order', {})


def cancel_order(order_id: str) -> bool:
    path = f'/trade-api/v2/portfolio/orders/{order_id}'
    r = requests.delete(f'{BASE_URL}/portfolio/orders/{order_id}',
                        headers=kalshi_headers('DELETE', path))
    return r.ok


# ── Core test ─────────────────────────────────────────────────────────────────

def run_test(ticker: str, contracts: int, ttl_minutes: int,
             poll_seconds: int, cancel_only: list[str]):

    # Cancel-only mode: cancel provided order IDs and exit
    if cancel_only:
        for oid in cancel_only:
            ok = cancel_order(oid)
            print(f'Cancel {oid}: {"OK" if ok else "FAILED"}')
        return

    # ── Fetch current order book ──────────────────────────────────────────
    print(f'\nFetching market: {ticker}')
    market = get_market(ticker)
    print(f'  Title:  {market.get("title", "?")}')
    print(f'  Status: {market.get("status", "?")}')
    print(f'  Close:  {market.get("close_time", "?")}')

    book  = get_orderbook(ticker)
    # NO side: Kalshi returns asks sorted best-to-worst.
    # no_ask = best (lowest) price a NO seller will accept.
    # Kalshi orderbook: 'yes' and 'no' keys each contain [[price_cents, qty], ...]
    # sorted from best to worst (highest bid first for buyers, lowest ask first for sellers).
    # For NO buyers (us), the relevant side is the NO BID — what existing buyers offer.
    # We want to rest at the bid or improve it by 1¢.
    yes_levels = book.get('yes', [])
    no_levels  = book.get('no',  [])

    print(f'\n  YES book levels (bids): {yes_levels[:5]}')
    print(f'  NO  book levels (bids): {no_levels[:5]}')

    # Try to derive NO bid / ask from the book first, then fall back to market fields.
    no_bid_cents = no_ask_cents = None

    if yes_levels and no_levels:
        yes_bid_cents = yes_levels[0][0]
        no_ask_cents  = 100 - yes_bid_cents
        no_bid_cents  = no_levels[0][0]
        print(f'  Book → YES bid={yes_bid_cents}¢  NO bid={no_bid_cents}¢  NO ask={no_ask_cents}¢')
    elif no_levels:
        no_bid_cents = no_levels[0][0]
        no_ask_cents = no_bid_cents + 2
        print(f'  NO bid (book): {no_bid_cents}¢  (assumed spread +2¢)')
    elif yes_levels:
        yes_bid_cents = yes_levels[0][0]
        no_ask_cents  = 100 - yes_bid_cents
        no_bid_cents  = no_ask_cents - 2
        print(f'  YES bid (book): {yes_bid_cents}¢  →  NO ask={no_ask_cents}¢  (assumed spread -2¢)')

    # Kalshi returns prices as *_dollars fields (float 0.00–1.00). Convert to cents.
    def _to_cents(val) -> int | None:
        if val is None:
            return None
        try:
            return round(float(val) * 100)
        except (TypeError, ValueError):
            return None

    m_no_bid  = _to_cents(market.get('no_bid_dollars')  or market.get('no_bid'))
    m_no_ask  = _to_cents(market.get('no_ask_dollars')  or market.get('no_ask'))
    m_yes_bid = _to_cents(market.get('yes_bid_dollars') or market.get('yes_bid'))
    m_yes_ask = _to_cents(market.get('yes_ask_dollars') or market.get('yes_ask'))
    frac_ok   = market.get('fractional_trading_enabled', False)
    print(f'  Market prices → yes_bid={m_yes_bid}¢ yes_ask={m_yes_ask}¢ '
          f'no_bid={m_no_bid}¢ no_ask={m_no_ask}¢  '
          f'(fractional_trading_enabled={frac_ok})')

    if no_bid_cents is None:
        if m_no_bid is not None:
            no_bid_cents = m_no_bid
        elif m_yes_ask is not None:
            no_bid_cents = 100 - m_yes_ask
        else:
            manual = input('No price data available. '
                           'Enter a NO price in cents to test with (or blank to abort): ').strip()
            if not manual:
                print('Aborted.')
                return
            no_bid_cents = int(manual)
        print(f'  Using NO bid = {no_bid_cents}¢ (from market fields)')

    if no_ask_cents is None:
        no_ask_cents = m_no_ask if m_no_ask is not None else no_bid_cents + 2

    price_A = no_bid_cents          # join current best NO bid queue
    price_B = no_bid_cents + 1      # improve bid by 1¢ — still resting, better priority

    if price_B >= no_ask_cents:
        print(f'\nWARNING: price_B={price_B}¢ would cross to NO ask={no_ask_cents}¢ — this is a TAKER order.')
        print('Consider using a wider-spread market for a cleaner resting test.')

    if price_A <= 0:
        print(f'ERROR: price_A={price_A}¢ — can\'t place order at 0¢.')
        return

    print(f'\nWill place:')
    print(f'  Order A: NO {price_A}¢  (at current NO bid — joins back of queue)')
    print(f'  Order B: NO {price_B}¢  (bid + 1¢ — new best bid, fills first when seller arrives)')
    if price_B >= no_ask_cents:
        print(f'  ⚠️  Order B crosses spread — will fill as TAKER immediately')
    print(f'  Contracts: {contracts}  |  TTL: {ttl_minutes}min  |  Poll: {poll_seconds}s')
    confirm = input('\nProceed? [y/N] ')
    if confirm.strip().lower() != 'y':
        print('Aborted.')
        return

    expiry_ts = int(time.time()) + ttl_minutes * 60

    print('\nPlacing Order A...')
    order_a = place_order(ticker, price_A, contracts, expiry_ts, 'A')
    oid_a   = order_a.get('order_id')
    t_place_a = time.time()
    print(f'  Order A id={oid_a}  status={order_a.get("status")}')

    time.sleep(0.5)  # small gap so they're distinct

    print('Placing Order B...')
    order_b = place_order(ticker, price_B, contracts, expiry_ts, 'B')
    oid_b   = order_b.get('order_id')
    t_place_b = time.time()
    print(f'  Order B id={oid_b}  status={order_b.get("status")}')

    print(f'\nMonitoring for up to {ttl_minutes}min (poll every {poll_seconds}s)...')
    print('  Press Ctrl-C to stop and cancel both orders.\n')

    terminal = {'executed', 'filled', 'canceled', 'expired'}
    results  = {'A': None, 'B': None}
    t_fill_a = t_fill_b = None

    try:
        while True:
            now = time.time()

            if results['A'] is None and oid_a:
                oa = get_order(oid_a)
                st = oa.get('status', '')
                remaining = oa.get('remaining_count_fp') or oa.get('remaining_count') or contracts
                if st in terminal:
                    results['A'] = st
                    t_fill_a     = now - t_place_a
                    print(f'[{_ts()}] Order A → {st}  (elapsed {t_fill_a:.1f}s)')

            if results['B'] is None and oid_b:
                ob = get_order(oid_b)
                st = ob.get('status', '')
                if st in terminal:
                    results['B'] = st
                    t_fill_b     = now - t_place_b
                    print(f'[{_ts()}] Order B → {st}  (elapsed {t_fill_b:.1f}s)')

            if results['A'] is not None and results['B'] is not None:
                break

            # Show heartbeat
            open_orders = [k for k, v in results.items() if v is None]
            print(f'[{_ts()}] open: {open_orders}', end='\r')

            if now - t_place_a > ttl_minutes * 60:
                print('\nTTL reached — canceling open orders.')
                break

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        print('\nInterrupted — canceling open orders.')

    # Cancel anything still open
    for label, oid in [('A', oid_a), ('B', oid_b)]:
        if results.get(label) is None and oid:
            ok = cancel_order(oid)
            print(f'  Canceled order {label} ({oid}): {"OK" if ok else "FAILED"}')
            results[label] = 'canceled_by_test'

    # ── Report ────────────────────────────────────────────────────────────
    print('\n' + '='*60)
    print('RESULTS')
    print('='*60)
    print(f'  Ticker:  {ticker}')
    print(f'  Order A  (NO @ {price_A}¢ — at ask):      '
          f'status={results["A"]}  fill_time={f"{t_fill_a:.1f}s" if t_fill_a else "—"}')
    print(f'  Order B  (NO @ {price_B}¢ — 1¢ inside):   '
          f'status={results["B"]}  fill_time={f"{t_fill_b:.1f}s" if t_fill_b else "—"}')

    if t_fill_a is not None and t_fill_b is not None:
        faster = 'B (inside)' if t_fill_b < t_fill_a else 'A (at ask)'
        delta  = abs(t_fill_b - t_fill_a)
        print(f'\n  → {faster} filled faster by {delta:.1f}s')
        print(f'  → Price delta: {price_B - price_A}¢ per contract')
    elif t_fill_a is not None:
        print('\n  → A filled; B did not fill in time.')
    elif t_fill_b is not None:
        print('\n  → B filled; A did not fill in time.')
    else:
        print('\n  → Neither filled within TTL.')
    print('='*60)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime('%H:%M:%S')


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Test fill-speed difference between at-ask vs. 1¢-inside NO orders')
    parser.add_argument('--ticker',       required=False, default='',
                        help='Kalshi market ticker, e.g. KXTRUMPMENTION-26APR25-FAKE')
    parser.add_argument('--contracts',    type=int, default=1,
                        help='Contracts per order (default 1)')
    parser.add_argument('--ttl',          type=int, default=10,
                        help='Minutes to wait for fills before canceling (default 10)')
    parser.add_argument('--poll',         type=int, default=5,
                        help='Poll interval in seconds (default 5)')
    parser.add_argument('--cancel-only',  nargs='+', default=[],
                        metavar='ORDER_ID',
                        help='Cancel these order IDs and exit (cleanup mode)')
    args = parser.parse_args()

    if not args.ticker and not args.cancel_only:
        parser.error('--ticker is required unless using --cancel-only')

    run_test(
        ticker       = args.ticker,
        contracts    = args.contracts,
        ttl_minutes  = args.ttl,
        poll_seconds = args.poll,
        cancel_only  = args.cancel_only,
    )
