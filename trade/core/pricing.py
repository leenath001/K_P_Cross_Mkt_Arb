"""
trade/core/pricing.py — per-market price grids and the resting-order price rule.

Kalshi markets are NOT all 1¢-tick: each market publishes `price_ranges`
([{start, end, step}]) under its `price_level_structure`. Most are `linear_cent`
(step 0.01), but some (e.g. Brasileirão Série A) are `center_half_edge_half_cent`
(step 0.005). Everything here works in CENTS (floats) and snaps to the market's own grid.

Resting price rule — sit at the top of the book without taking: one tick below the
ask on the market's own grid (49/50 -> 49; 49/51 -> 50; half-cent 44.5/45.5 -> 45).
"""
import math
from typing import Optional

DEFAULT_RANGES = [(0.0, 100.0, 1.0)]


def parse_ranges(price_ranges) -> list:
    """Kalshi `price_ranges` ([{start,end,step} in dollars]) -> [(start_c, end_c, step_c)]."""
    try:
        out = [(float(r['start']) * 100, float(r['end']) * 100, float(r['step']) * 100)
               for r in (price_ranges or [])]
        out = [r for r in out if r[2] > 0]
        return sorted(out) or DEFAULT_RANGES
    except (KeyError, TypeError, ValueError):
        return DEFAULT_RANGES


def step_at(cents: float, ranges=None) -> float:
    ranges = ranges or DEFAULT_RANGES
    for start, end, step in ranges:
        if start <= cents < end:
            return step
    return ranges[-1][2]


def snap_down(cents: float, ranges=None) -> float:
    s = step_at(cents, ranges)
    return round(math.floor(cents / s + 1e-9) * s, 3)


def to_cents(dollars: float):
    """Dollars -> cents, int when whole (keeps whole-cent output identical to the old round())."""
    c = round(float(dollars) * 100, 3)
    return int(c) if c == int(c) else c


def rest_price_cents(bid_c: Optional[float], ask_c: float, ranges=None) -> float:
    """Non-marketable top-of-book resting price in cents: one tick below the ask, on the grid."""
    price = snap_down(ask_c - step_at(ask_c - 1e-6, ranges), ranges)
    if bid_c is not None and bid_c > 0:
        price = max(price, min(bid_c, price))          # never below a sane bid (no-op on normal books)
    return round(max(price, 0.0), 3)                   # 0 when the ask is already the minimum tick — callers skip it
