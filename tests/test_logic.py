"""
Pins the money math to its current outputs (fees, EV, sizing, settlement PnL, price grid, quoting, marks) so moving
code around can't silently change what gets traded. Expected values were recorded from the code before refactoring.
"""
import pytest

from trade.core.execution import (_ev, kelly_contracts, resolve_contracts, _price_str, _position_mark_price,
                                  rest_price_dollars, filled_count)
from trade.core.pricing import parse_ranges, step_at, snap_down, to_cents, rest_price_cents
from trade.settle import compute_pnl
from trade.mm.engine import compute_targets

# (fair_prob, price, bankroll, fee_rate)
SIZING = [(0.55, 0.50, 100, 0.07), (0.62, 0.58, 50, 0.0175), (0.30, 0.25, 200, 0.07),
          (0.51, 0.50, 1000, 0.0), (0.40, 0.45, 100, 0.07)]
HALF_CENT = [{'start': '0.0000', 'end': '0.1000', 'step': '0.0010'},
             {'start': '0.1000', 'end': '0.9000', 'step': '0.0100'},
             {'start': '0.9000', 'end': '1.0000', 'step': '0.0010'}]


def test_ev():
    assert [round(_ev(f, p, r), 6) for f, p, _, r in SIZING] == [0.0325, 0.035737, 0.036875, 0.01, -0.067325]


def test_kelly_contracts():
    assert [kelly_contracts(f, p, b, r) for f, p, b, r in SIZING] == [4, 3, 20, 8, 0]


def test_resolve_contracts():
    assert resolve_contracts({'contracts_override': 7}, 0.55, 0.5, 100, 0.07) == 7      # manual size wins
    assert resolve_contracts({}, 0.55, 0.5, 100, 0.07, 2.0) == 8                         # Kelly x multiplier
    assert resolve_contracts({}, 0.40, 0.45, 100, 0.07) is None                          # no edge -> skip


def test_price_str():
    assert [_price_str(c) for c in (50, 50.5, 7, 99.25)] == ['0.50', '0.505', '0.07', '0.9925']


def test_settlement_pnl():
    # 5 contracts @ 60c, 7% fee charged at entry win or lose
    got = [compute_pnl(r, 5, 0.6, 0.07, s) for r, s in
           [('yes', 'yes'), ('no', 'yes'), ('no', 'no'), ('yes', 'no'), ('void', 'yes')]]
    assert got == [1.916, -3.084, 1.916, -3.084, 0.0]


def test_price_grid():
    half = parse_ranges(HALF_CENT)
    assert half == [(0.0, 10.0, 0.1), (10.0, 90.0, 1.0), (90.0, 100.0, 0.1)]
    assert [step_at(c, half) for c in (5, 50, 95)] == [0.1, 1.0, 0.1] and step_at(50) == 1.0
    assert [snap_down(c, half) for c in (5.37, 50.7, 95.55)] == [5.3, 50.0, 95.5] and snap_down(50.7) == 50.0
    assert [to_cents(d) for d in (0.5, 0.505, 0.07)] == [50, 50.5, 7]


def test_rest_price():
    assert [rest_price_cents(b, a) for b, a in [(48, 52), (None, 52), (51, 52), (50, 51)]] == [51.0, 51.0, 51.0, 50.0]
    assert rest_price_cents(9.5, 9.8, parse_ranges(HALF_CENT)) == 9.7
    prices = {'yes_bid': 48, 'yes_ask': 52, 'no_bid': 46, 'no_ask': 50}           # get_market_prices() is in cents
    assert rest_price_dollars(prices, 'yes') == 0.51
    assert rest_price_dollars(prices, 'no') == 0.49
    assert rest_price_dollars({}, 'yes') is None


def test_position_marks():
    assert [_position_mark_price(m) for m in [
        {'result': 'yes'}, {'result': 'no'},                                             # settled: pay 1 / 0
        {'result': '', 'yes_bid_dollars': '0.33', 'yes_ask_dollars': '0.34'},            # live book: mid
        {'result': '', 'yes_bid_dollars': '0', 'yes_ask_dollars': '1', 'last_price_dollars': '0.38'},  # empty book
        {'result': ''}]] == [1.0, 0.0, 0.335, 0.38, None]


def test_filled_count():
    assert filled_count({'fill_count_fp': '3.00'}) == 3 and filled_count({}) == 0


def test_mm_quote_targets():
    assert compute_targets([(48, 10), (47, 5)], [(52, 10)], 50.3, None) == \
        {'bid': 50.0, 'ask': 51.0, 'bid_top': 48, 'ask_top': 52, 'tick': 1.0}
    # book below fair: bid pushed down to stay non-marketable, ask stays at fair + 1 tick
    assert compute_targets([(48, 10)], [(49, 10)], 50.3, None) == \
        {'bid': 48.0, 'ask': 51.0, 'bid_top': 48, 'ask_top': 49, 'tick': 1.0}
    assert compute_targets([], [], 50, None)['bid'] is None


def test_open_trade_outcomes():
    import numpy as np
    import pandas as pd
    from trade.ui.analytics import open_trade_outcomes
    # two trades @ 50c x 5 contracts, no fee: -5 / 0 / +5 at 25 / 50 / 25 %
    live = pd.DataFrame({'contracts': [5, 5], 'entry_price': [0.5, 0.5], 'fee_rate': [0, 0], 'fair_prob': [0.5, 0.5]})
    vals, probs, exp = open_trade_outcomes(live)
    nz = probs > 0
    assert list(vals[nz]) == [-5.0, 0.0, 5.0] and list(probs[nz]) == [0.25, 0.5, 0.25] and exp == 0.0
    # entry fee comes off every outcome (5 x 0.07 x 0.5 x 0.5 = 0.0875 per trade)
    vals, probs, _ = open_trade_outcomes(live.assign(fee_rate=0.07))
    assert np.allclose(vals[probs > 0], [-5.175, -0.175, 4.825])
    assert open_trade_outcomes(pd.DataFrame()) is None
