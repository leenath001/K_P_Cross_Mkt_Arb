"""
quickstart.py — Run the full arbitrage pipeline end-to-end.

Usage:
    python quickstart.py               # dry run (no orders placed)
    python quickstart.py --live        # live trading with dashboard
"""

import argparse
import pandas as pd
import config
from theODDS.p_helpers import pinnacle_odds, get_api_usage
from KALSHI.k_helpers import kalshi_odds
from bot import get_balance, kelly_contracts

# ── Args ────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument('--live',       action='store_true',                          help='Place real orders (default: dry run)')
parser.add_argument('--bankroll',   type=float, default=None,                     help='Override balance in dollars (default: fetch from Kalshi)')
parser.add_argument('--hrs',        type=int,   default=config.LOOKAHEAD_HRS,     help=f'Look-ahead window in hours (default: {config.LOOKAHEAD_HRS} from config)')
parser.add_argument('--fetch-live', action='store_true', default=config.LIVE,     help=f'Fetch live games instead of upcoming (default: {config.LIVE} from config)')
parser.add_argument('--taker-fee',  type=float, default=0.07,                     help='Taker fee rate as fraction of winnings (default: 0.07)')
parser.add_argument('--maker-fee',  type=float, default=0.03,                     help='Maker fee rate as fraction of winnings (default: 0.03)')
parser.add_argument('--limit-only', action='store_true',                          help='Never cross the book — always rest at bid')
parser.add_argument('--threshold',  type=float, default=0.85,                     help='Min fuzzy-match score (default: 0.85)')
args = parser.parse_args()

# ── Fetch bankroll ───────────────────────────────────────────────────────────

if args.bankroll is not None:
    bankroll = args.bankroll
    print(f'  Bankroll (manual): ${bankroll:.2f}')
else:
    bankroll = get_balance()
    print(f'  Kalshi balance: ${bankroll:.2f}')

# ── Step 1: Fetch Pinnacle odds ──────────────────────────────────────────────

print('=' * 60)
print('STEP 1 — Fetching Pinnacle odds')
print('=' * 60)
print(f'  Sports : {config.SPORTS}')
print(f'  Window : next {args.hrs}h\n')

pinnacle_df = pinnacle_odds(config.SPORTS, hrs=args.hrs, live=args.fetch_live)
used, remaining = get_api_usage()
print(f'  {len(pinnacle_df)} outcome rows fetched  '
      f'(API: {used} used / {remaining} remaining)')
print(pinnacle_df[['sport', 'home', 'away', 'outcome', 'fair_prob', 'vig_pct']].to_string(index=False))

# ── Step 2: Match to Kalshi markets ─────────────────────────────────────────

print('\n' + '=' * 60)
print('STEP 2 — Matching to Kalshi markets')
print('=' * 60)
print(f'  Match threshold : {args.threshold}')
print(f'  Taker fee       : {args.taker_fee * 100:.0f}% of winnings')
print(f'  Maker fee       : {args.maker_fee * 100:.0f}% of winnings\n')

matched_df = kalshi_odds(pinnacle_df, threshold=args.threshold)
signals    = matched_df[matched_df['signal']]
print(f'  {len(matched_df)} rows matched  |  {len(signals)} signal(s) found\n')

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 120)
pd.set_option('display.float_format', '{:.3f}'.format)
print(matched_df[['sport', 'outcome', 'fair_prob', 'yes_ask', 'match_score', 'signal']].to_string(index=False))

if matched_df.empty:
    print('\n  No matches. Try increasing --hrs or lowering --threshold.')
    raise SystemExit(0)

# ── Step 3: Show signals ─────────────────────────────────────────────────────

print('\n' + '=' * 60)
print('STEP 3 — Active signals')
print('=' * 60)

if signals.empty:
    print('  No signals. All Kalshi asks are fairly priced vs Pinnacle.')
    raise SystemExit(0)

from bot import _ev, TAKER_FEE, MAKER_FEE
for _, row in signals.iterrows():
    ev = _ev(row['fair_prob'], row['yes_ask'], args.taker_fee)
    print(f"  {row['outcome']:30s}  fair={row['fair_prob']:.3f}  "
          f"ask={row['yes_ask']:.2f}  ev(taker)={ev:+.3f}  ticker={row['k_ticker']}")

# ── Step 4: Trade (live) or preview (dry run) ────────────────────────────────

print('\n' + '=' * 60)
if not args.live:
    print('STEP 4 — DRY RUN (pass --live to place real orders)')
    print('=' * 60)
    from bot import kelly_contracts
    print(f'\n  Bankroll: ${bankroll:.2f}\n')
    for _, row in signals.iterrows():
        tev      = _ev(row['fair_prob'], row['yes_ask'], args.taker_fee)
        bid      = row['yes_bid'] if row['yes_bid'] else None
        price    = row['yes_ask'] if tev > 0 else bid
        fee_rate = args.taker_fee if tev > 0 else args.maker_fee
        mode     = 'cross' if tev > 0 else 'rest'
        if price is None:
            continue
        n    = kelly_contracts(row['fair_prob'], price, bankroll, fee_rate)
        cost = n * price
        ev   = _ev(row['fair_prob'], price, fee_rate)
        print(f"  {row['outcome']:30s}  [{mode}]  {n:3d}x @ {round(price*100)}¢  "
              f"cost=${cost:.2f}  ev/contract=${ev:.3f}")

else:
    print('STEP 4 — LIVE TRADING')
    print('=' * 60)
    print(f'\n  Bankroll : ${bankroll:.2f}')
    print(f'  Signals  : {len(signals)}')
    print(f'  Orders   : resting YES limit @ ask, max 30 min, cancel 5 min before start')
    print(f'  Monitor  : re-check Pinnacle every 2 min\n')

    print('  Approve each signal:  y = trade  |  n = skip  |  q = abort all\n')
    approved_rows = []
    for _, row in signals.iterrows():
        tev   = _ev(row['fair_prob'], row['yes_ask'], args.taker_fee)
        bid   = row['yes_bid'] if row['yes_bid'] else None
        price = row['yes_ask'] if tev > 0 else bid
        mode  = 'cross' if tev > 0 else 'rest'
        if price is None:
            print(f"  [skip — no bid]  {row['outcome']}")
            continue
        n    = kelly_contracts(row['fair_prob'], price, bankroll, args.taker_fee if tev > 0 else args.maker_fee)
        cost = n * price
        ans  = input(f"  [{mode}] {row['outcome']:30s}  {n}x @ {round(price*100)}¢  cost=${cost:.2f}  ev={tev:+.3f}  [y/n/q] ").strip().lower()
        if ans == 'q':
            print('  Aborted.')
            raise SystemExit(0)
        if ans == 'y':
            approved_rows.append(row)

    if not approved_rows:
        print('  No signals approved.')
        raise SystemExit(0)

    approved_df = pd.DataFrame(approved_rows)
    print(f'\n  {len(approved_df)} signal(s) approved — launching...\n')

    import threading
    from bot import run_all_signals
    from dashboard import Dashboard

    stop_event = threading.Event()
    results    = []

    try:
        with Dashboard(api_limit=used + remaining) as dash:
            dash.set_api_usage(used, remaining)
            results = run_all_signals(approved_df, bankroll=bankroll,
                                      taker_fee=args.taker_fee, maker_fee=args.maker_fee,
                                      limit_only=args.limit_only, dashboard=dash,
                                      stop_event=stop_event)
    except KeyboardInterrupt:
        print('\n  All orders canceled.')

    print('  Done.\n')

    # ── Trade summary ────────────────────────────────────────────────────────
    filled = [r for r in (results or []) if r and r.get('status') in ('executed', 'filled')]
    if filled:
        from bot import _ev
        print('=' * 60)
        print('TRADE SUMMARY')
        print('=' * 60)
        for r in filled:
            price     = r['yes_price'] / 100
            fee_rate  = args.taker_fee if r.get('order_type') == 'cross' else args.maker_fee
            ev_total  = _ev(r['fair_prob'], price, fee_rate) * r['contracts']
            mode      = 'TAKER (cross)' if r.get('order_type') == 'cross' else 'MAKER (rest)'
            print(f"  {r['ticker']}")
            print(f"    Outcome   : {r['outcome']}")
            print(f"    Mode      : {mode}")
            print(f"    Pinnacle  : fair_prob = {r['fair_prob']:.3f}")
            print(f"    Kalshi    : executed @ {r['yes_price']}¢  x{r['contracts']} contracts")
            print(f"    Proj. EV  : ${ev_total:.3f}  (${_ev(r['fair_prob'], price, fee_rate):.3f}/contract)")
            print()
