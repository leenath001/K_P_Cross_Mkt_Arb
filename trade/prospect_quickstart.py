"""
prospect_quickstart.py — Run the Prospect Theory strategy end-to-end.

Usage:
    python trade/prospect_quickstart.py           # dry run (no orders placed)
    python trade/prospect_quickstart.py --live    # live trading with dashboard
"""

import os, sys
_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRADE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, _TRADE)

import argparse
import pandas as pd
import config
from theODDS.p_helpers import pinnacle_odds, get_api_usage, fetch_usage
from KALSHI.k_helpers import kalshi_odds, prospect_signals
from trade.core.execution import get_balance, kelly_contracts, _ev, TAKER_FEE, MAKER_FEE

# ── Args ────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    description='Prospect Theory strategy — buy NO in longshot zone, YES in favorite zone.'
)
parser.add_argument('--live',          action='store_true',                      help='Place real orders (default: dry run)')
parser.add_argument('--bankroll',      type=float, default=None,                 help='Override balance in dollars')
parser.add_argument('--hrs',           type=int,   default=config.LOOKAHEAD_HRS, help=f'Look-ahead window in hours (default: {config.LOOKAHEAD_HRS})')
parser.add_argument('--taker-fee',     type=float, default=0.07,                 help='Taker fee rate (default: 0.07, Kalshi general table)')
parser.add_argument('--maker-fee',     type=float, default=0.0175,               help='Maker fee rate (default: 0.0175, Kalshi general table)')
parser.add_argument('--threshold',     type=float, default=0.85,                 help='Min fuzzy-match score (default: 0.85)')
parser.add_argument('--mode',          choices=['rest', 'cross', 'auto'], default='rest',
                                                                          help='Order mode (default: rest)')
parser.add_argument('--size',          type=float, default=1.0,                  help='Kelly size multiplier (default: 1.0)')
parser.add_argument('--longshot-lo',   type=float, default=0.05,                 help='Longshot zone lower bound on yes_ask (default: 0.05)')
parser.add_argument('--longshot-hi',   type=float, default=0.15,                 help='Longshot zone upper bound on yes_ask (default: 0.15)')
parser.add_argument('--favorite-lo',   type=float, default=0.75,                 help='Favorite zone lower bound on yes_ask (default: 0.75)')
parser.add_argument('--favorite-hi',   type=float, default=0.92,                 help='Favorite zone upper bound on yes_ask (default: 0.92)')
parser.add_argument('--usage',         action='store_true',                      help='Print The Odds API usage and exit')
args = parser.parse_args()

# ── Usage check ──────────────────────────────────────────────────────────────

if args.usage:
    used, remaining = fetch_usage()
    limit   = used + remaining
    pct     = used / max(limit, 1)
    filled  = round(pct * 40)
    bar     = '█' * filled + '░' * (40 - filled)
    color   = '\033[92m' if pct < 0.7 else ('\033[93m' if pct < 0.9 else '\033[91m')
    reset   = '\033[0m'
    print(f'\n  The Odds API usage')
    print(f'  {color}{bar}{reset}  {used} / {limit}  ({pct:.0%} used,  {remaining} remaining)\n')
    raise SystemExit(0)

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

pinnacle_df = pinnacle_odds(config.SPORTS, hrs=args.hrs, live=False)
used, remaining = get_api_usage()
print(f'  {len(pinnacle_df)} outcome rows fetched  '
      f'(API: {used} used / {remaining} remaining)')

# ── Step 2: Match to Kalshi + apply prospect theory filter ───────────────────

print('\n' + '=' * 60)
print('STEP 2 — Matching to Kalshi + Prospect Theory filter')
print('=' * 60)
print(f'  Match threshold : {args.threshold}')
print(f'  Taker fee       : {args.taker_fee * 100:.0f}% of winnings')
print(f'  Maker fee       : {args.maker_fee * 100:.0f}% of winnings')
print(f'  Longshot zone   : yes_ask ${args.longshot_lo:.2f}–${args.longshot_hi:.2f}  →  buy NO')
print(f'  Favorite zone   : yes_ask ${args.favorite_lo:.2f}–${args.favorite_hi:.2f}  →  buy YES\n')

matched_df = kalshi_odds(pinnacle_df, threshold=args.threshold)
pt_df = prospect_signals(
    matched_df,
    longshot_lo=args.longshot_lo, longshot_hi=args.longshot_hi,
    favorite_lo=args.favorite_lo, favorite_hi=args.favorite_hi,
)

# Exclude tickers with open PENDING bets
PROSPECT_LOG = os.path.join(_TRADE, 'logs', 'prospect_trades.csv')
already_bet = set()
if os.path.exists(PROSPECT_LOG) and os.path.getsize(PROSPECT_LOG) > 0:
    log_df      = pd.read_csv(PROSPECT_LOG)
    already_bet = set(log_df.loc[log_df['result'] == 'PENDING', 'k_ticker'])
    if already_bet:
        pt_df = pt_df[~pt_df['k_ticker'].isin(already_bet)]
        print(f'  Excluded {len(already_bet)} ticker(s) with open bets')

signals = pt_df[pt_df['pt_signal']]

if signals.empty:
    print('  No prospect theory signals. Try widening zone bounds or lowering --threshold.')
    raise SystemExit(0)

# ── Step 3: Show signals by zone ─────────────────────────────────────────────

print('\n' + '=' * 60)
print('STEP 3 — Prospect Signals')
print('=' * 60)

fav_sigs  = signals[signals['pt_zone'] == 'favorite']
long_sigs = signals[signals['pt_zone'] == 'longshot']

def _order_params_pt(row, taker_fee, maker_fee, mode, side):
    """
    Returns order params for a prospect signal row given explicit side.
    For YES (favorite): buy yes_ask (cross) or bid+1¢ (rest).
    For NO  (longshot): buy no_ask  (cross) or no_bid+1¢ (rest).
    """
    if side == 'yes':
        ask = row.get('yes_ask')
        bid = row.get('yes_bid')
        fp  = row['fair_prob']
    else:
        ask = row.get('no_ask')
        bid = row.get('no_bid')
        fp  = 1 - row['fair_prob']

    if ask is None:
        return None

    if mode == 'cross':
        price, fee_rate, order_mode = ask, taker_fee, 'cross'
    elif mode == 'rest':
        if bid is None:
            return None
        price, fee_rate, order_mode = round(bid + 0.01, 2), maker_fee, 'rest'
    else:  # auto
        tev = _ev(fp, ask, taker_fee)
        if tev > 0:
            price, fee_rate, order_mode = ask, taker_fee, 'cross'
        elif bid is not None:
            price, fee_rate, order_mode = round(bid + 0.01, 2), maker_fee, 'rest'
        else:
            return None

    ev = _ev(fp, price, fee_rate)
    n  = max(1, round(kelly_contracts(fp, price, bankroll, fee_rate) * args.size))
    return {'price': price, 'fee_rate': fee_rate, 'mode': order_mode,
            'ev': ev, 'n': n, 'cost': round(n * price, 2),
            'fp': fp, 'side': side}


if not fav_sigs.empty:
    print(f'\n  FAVORITE ZONE  (buy YES,  yes_ask ${args.favorite_lo:.2f}–${args.favorite_hi:.2f})')
    for _, row in fav_sigs.iterrows():
        p = _order_params_pt(row, args.taker_fee, args.maker_fee, args.mode, 'yes')
        if p is None:
            print(f"    {row['outcome']:32s}  [skip — no bid]")
            continue
        print(f"    {row['outcome']:32s}  fair={row['fair_prob']:.3f}  "
              f"yes_ask={row['yes_ask']:.2f}  ev={p['ev']:+.3f}  "
              f"{p['n']}x @ {round(p['price']*100)}¢  cost=${p['cost']:.2f}  "
              f"{row['k_ticker']}")

if not long_sigs.empty:
    print(f'\n  LONGSHOT ZONE  (buy NO,   yes_ask ${args.longshot_lo:.2f}–${args.longshot_hi:.2f})')
    for _, row in long_sigs.iterrows():
        p = _order_params_pt(row, args.taker_fee, args.maker_fee, args.mode, 'no')
        if p is None:
            print(f"    {row['outcome']:32s}  [skip — no bid]")
            continue
        no_fair = 1 - row['fair_prob']
        print(f"    {row['outcome']:32s}  fair(NO)={no_fair:.3f}  "
              f"no_ask={row['no_ask']:.2f}  ev={p['ev']:+.3f}  "
              f"{p['n']}x @ {round(p['price']*100)}¢  cost=${p['cost']:.2f}  "
              f"{row['k_ticker']}")

# ── Step 4: Trade (live) or stop (dry run) ────────────────────────────────────

print('\n' + '=' * 60)
if not args.live:
    print('STEP 4 — DRY RUN (pass --live to place real orders)')
    print('=' * 60)
    size_tag = f'  (size ×{args.size})' if args.size != 1.0 else ''
    print(f'\n  {len(fav_sigs)} favorite signal(s)  |  {len(long_sigs)} longshot signal(s){size_tag}')
    print('  Pass --live to execute.')

else:
    print('STEP 4 — LIVE TRADING')
    print('=' * 60)
    print(f'\n  Bankroll  : ${bankroll:.2f}')
    print(f'  Signals   : {len(signals)}  ({len(fav_sigs)} favorite  +  {len(long_sigs)} longshot)')
    print(f'  Mode      : {args.mode}  |  size ×{args.size}')
    print(f'  Monitor   : re-check Pinnacle every 2 min\n')
    print('  Approve each signal:  y = trade  |  n = skip  |  q = abort all\n')

    approved_rows = []
    for zone_label, zone_df, side in [
        (f'FAVORITE ZONE  (buy YES)', fav_sigs, 'yes'),
        (f'LONGSHOT ZONE  (buy NO)',  long_sigs, 'no'),
    ]:
        if zone_df.empty:
            continue
        print(f'\n  {zone_label}')
        for _, row in zone_df.iterrows():
            p = _order_params_pt(row, args.taker_fee, args.maker_fee, args.mode, side)
            if p is None:
                print(f"    [skip — no bid]  {row['outcome']}")
                continue
            ans = input(
                f"    [{p['mode']}] [{side.upper()}] {row['outcome']:28s}  "
                f"{p['n']}x @ {round(p['price']*100)}¢  "
                f"cost=${p['cost']:.2f}  ev={p['ev']:+.3f}  [y/n/q] "
            ).strip().lower()
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
    from trade.strategies.prospect import run_all_signals as run_prospect_signals
    from dashboard import Dashboard

    stop_event = threading.Event()
    results    = []

    try:
        with Dashboard(api_limit=used + remaining) as dash:
            dash.set_api_usage(used, remaining)
            results = run_prospect_signals(
                approved_df, bankroll=bankroll,
                taker_fee=args.taker_fee, maker_fee=args.maker_fee,
                limit_only=(args.mode == 'rest'),
                force_cross=(args.mode == 'cross'),
                size_mult=args.size,
                dashboard=dash, stop_event=stop_event,
            )
    except KeyboardInterrupt:
        print('\n  All orders canceled.')

    print('  Done.\n')

    filled = [r for r in (results or []) if r and r.get('status') in ('executed', 'filled')]
    if filled:
        print('=' * 60)
        print('TRADE SUMMARY')
        print('=' * 60)
        for r in filled:
            price    = r['yes_price'] / 100
            fee_rate = args.taker_fee if r.get('order_type') == 'cross' else args.maker_fee
            side_r   = r.get('side', 'yes')
            fp       = r['fair_prob'] if side_r == 'yes' else 1 - r['fair_prob']
            ev_total = _ev(fp, price, fee_rate) * r['contracts']
            zone     = r.get('pt_zone', '—')
            print(f"  {r['ticker']}  [{zone.upper()}  {side_r.upper()}]")
            print(f"    Outcome : {r.get('outcome', '')}")
            print(f"    Kalshi  : executed @ {r['yes_price']}¢  ×{r['contracts']} contracts")
            print(f"    Proj.EV : ${ev_total:.3f}")
            print()
