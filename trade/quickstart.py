"""
quickstart.py — Run the full arbitrage pipeline end-to-end.

Usage:
    python trade/quickstart.py               # dry run (no orders placed)
    python trade/quickstart.py --live        # live trading with dashboard
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
parser.add_argument('--mode',       choices=['rest', 'cross', 'auto'], default='rest',
                                                                          help='Order mode: rest=maker limit (default), cross=taker at ask, auto=cross if EV positive else rest')
parser.add_argument('--side',       choices=['yes', 'no'], default='yes',         help='Contract side: yes or no')
parser.add_argument('--threshold',  type=float, default=0.85,                     help='Min fuzzy-match score (default: 0.85)')
parser.add_argument('--size',       type=float, default=1.0,                      help='Size multiplier on Kelly contracts (default: 1.0; e.g. 2.0 = double Kelly)')
parser.add_argument('--usage',      action='store_true',                          help='Print The Odds API usage and exit')
args = parser.parse_args()

# ── Usage check (early exit) ─────────────────────────────────────────────────

if args.usage:
    used, remaining = fetch_usage()
    limit  = used + remaining
    pct    = used / max(limit, 1)
    filled = round(pct * 40)
    bar    = '█' * filled + '░' * (40 - filled)
    color  = '\033[92m' if pct < 0.7 else ('\033[93m' if pct < 0.9 else '\033[91m')
    reset  = '\033[0m'
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

matched_df = kalshi_odds(pinnacle_df, threshold=args.threshold,
                         fees=args.taker_fee, maker_fees=args.maker_fee)
signal_col = 'signal_no' if args.side == 'no' else 'signal'

# ── Filter out tickers with open (PENDING) bets ──────────────────────────────
LOG_PATH = os.path.join(_TRADE, 'logs', 'trades.csv')
already_bet = set()
if os.path.exists(LOG_PATH):
    log_df      = pd.read_csv(LOG_PATH) if os.path.getsize(LOG_PATH) > 0 else pd.DataFrame()
    already_bet = set(log_df.loc[log_df['result'] == 'PENDING', 'k_ticker']) if not log_df.empty else set()
    if already_bet:
        matched_df = matched_df[~matched_df['k_ticker'].isin(already_bet)]
        print(f'  Excluded {len(already_bet)} ticker(s) with open bets: {already_bet}')

if matched_df.empty or signal_col not in matched_df.columns:
    print('\n  No matches. Try increasing --hrs or lowering --threshold.')
    raise SystemExit(0)

signals    = matched_df[matched_df[signal_col]]
print(f'  Side: {args.side.upper()}  |  {len(matched_df)} rows matched  |  {len(signals)} signal(s) found\n')

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 120)
pd.set_option('display.float_format', '{:.3f}'.format)
print(matched_df[['sport', 'outcome', 'fair_prob', 'yes_ask', 'match_score', 'signal']].to_string(index=False))

# ── Helpers ──────────────────────────────────────────────────────────────────

from bot import _ev, TAKER_FEE, MAKER_FEE

def _order_params(row, taker_fee, maker_fee, mode='rest'):
    """
    Determine order mode, price, fee_rate for a signal row.
    mode: 'rest' = always maker limit, 'cross' = always taker at ask,
          'auto' = cross if taker EV positive else rest.
    Returns dict or None if no tradeable price exists.
    """
    tev = _ev(row['fair_prob'], row['yes_ask'], taker_fee)
    bid = row['yes_bid'] if row['yes_bid'] else None

    if mode == 'cross':
        price, fee_rate, order_mode = row['yes_ask'], taker_fee, 'cross'
    elif mode == 'rest':
        if bid is None:
            return None
        price, fee_rate, order_mode = round(bid + 0.01, 2), maker_fee, 'rest'
    else:  # auto
        if tev > 0:
            price, fee_rate, order_mode = row['yes_ask'], taker_fee, 'cross'
        elif bid is not None:
            price, fee_rate, order_mode = round(bid + 0.01, 2), maker_fee, 'rest'
        else:
            return None

    ev = _ev(row['fair_prob'], price, fee_rate)
    n  = max(1, round(kelly_contracts(row['fair_prob'], price, bankroll, fee_rate) * args.size))
    return {'price': price, 'fee_rate': fee_rate, 'mode': order_mode,
            'ev': ev, 'n': n, 'cost': round(n * price, 2), 'tev': tev}


def _event_header(group):
    """Print a grouped event header, flagging multi-leg opportunities."""
    first  = group.iloc[0]
    multi  = len(group) > 1
    time_s = pd.Timestamp(first['commence']).strftime('%b %d  %H:%M UTC')
    label  = '[MULTI-LEG] ' if multi else ''
    print(f"\n  {label}{first['home']} vs {first['away']}  —  {first['sport']}  ({time_s})")
    if multi:
        legs = ', '.join(group['outcome'].tolist())
        print(f"  {'':>2}Multiple underpriced legs: {legs}")
        print(f"  {'':>2}Buying both reduces variance without sacrificing edge.")


# ── Step 3: Show signals ─────────────────────────────────────────────────────

print('\n' + '=' * 60)
print('STEP 3 — Active signals')
print('=' * 60)

if signals.empty:
    print('  No signals. All Kalshi asks are fairly priced vs Pinnacle.')
    raise SystemExit(0)

multi_leg_events = (signals.groupby('event_id').size() > 1).sum()
print(f'  {len(signals)} signal(s) across {signals["event_id"].nunique()} event(s)'
      + (f'  |  {multi_leg_events} multi-leg' if multi_leg_events else ''))

for _, group in signals.groupby('event_id', sort=False):
    _event_header(group)
    for _, row in group.iterrows():
        ev = _ev(row['fair_prob'], row['yes_ask'], args.taker_fee)
        print(f"    {row['outcome']:28s}  fair={row['fair_prob']:.3f}  "
              f"ask={row['yes_ask']:.2f}  ev(taker)={ev:+.3f}  {row['k_ticker']}")

# ── Step 4: Trade (live) or preview (dry run) ────────────────────────────────

print('\n' + '=' * 60)
if not args.live:
    print('STEP 4 — DRY RUN (pass --live to place real orders)')
    print('=' * 60)
    size_tag = f'  (size ×{args.size})' if args.size != 1.0 else ''
    print(f'\n  Bankroll: ${bankroll:.2f}{size_tag}')

    for _, group in signals.groupby('event_id', sort=False):
        _event_header(group)
        for _, row in group.iterrows():
            p = _order_params(row, args.taker_fee, args.maker_fee, args.mode)
            if p is None:
                print(f"    {row['outcome']:28s}  [skip — no bid]")
                continue
            print(f"    {row['outcome']:28s}  [{p['mode']}]  "
                  f"{p['n']:3d}x @ {round(p['price']*100)}¢  "
                  f"cost=${p['cost']:.2f}  ev/contract=${p['ev']:.3f}")

else:
    print('STEP 4 — LIVE TRADING')
    print('=' * 60)
    print(f'\n  Bankroll : ${bankroll:.2f}')
    print(f'  Signals  : {len(signals)}')
    print(f'  Orders   : limit YES @ ask/bid+1¢, max 30 min, cancel 5 min before start')
    print(f'  Monitor  : re-check Pinnacle every 2 min\n')
    print('  Approve each signal:  y = trade  |  n = skip  |  q = abort all\n')

    approved_rows = []
    for _, group in signals.groupby('event_id', sort=False):
        _event_header(group)
        for _, row in group.iterrows():
            p = _order_params(row, args.taker_fee, args.maker_fee, args.mode)
            if p is None:
                print(f"    [skip — no bid]  {row['outcome']}")
                continue
            ans = input(
                f"    [{p['mode']}] {row['outcome']:28s}  "
                f"{p['n']}x @ {round(p['price']*100)}¢  "
                f"cost=${p['cost']:.2f}  ev={p['tev']:+.3f}  [y/n/q] "
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
    from bot import run_all_signals
    from dashboard import Dashboard

    stop_event = threading.Event()
    results    = []

    try:
        with Dashboard(api_limit=used + remaining) as dash:
            dash.set_api_usage(used, remaining)
            results = run_all_signals(approved_df, bankroll=bankroll,
                                      taker_fee=args.taker_fee, maker_fee=args.maker_fee,
                                      limit_only=(args.mode == 'rest'),
                                      force_cross=(args.mode == 'cross'),
                                      side=args.side,
                                      size_mult=args.size,
                                      dashboard=dash, stop_event=stop_event)
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
