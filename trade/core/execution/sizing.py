"""
trade/core/execution/sizing.py — EV and position sizing: per-contract EV after fees, partial Kelly, manual size overrides, and the exact cent-rounded fee check run right before an order goes out.
"""
import pandas as pd
from math import floor
from typing import Optional

from KALSHI.k_helpers import kalshi_fee_dollars
from applog import get_logger
from trade.core.execution.constants import MIN_NOTIONAL

log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split


# ---------------------------------------------------------------------------
# Kelly Sizing + Edge Calculation
# ---------------------------------------------------------------------------

def _exact_ev_ok(fair_prob: float, price: float, contracts: int, series: str,
                 maker: bool, min_ev: float) -> bool:
    """
    Final go/no-go check using Kalshi's REAL ceil-to-cent fee (kalshi_fee_dollars),
    not the smooth per-contract rate. The smooth rate _ev()/kelly_contracts() use is
    exact only as contracts -> infinity; cent-rounding can matter a lot at contracts=1
    (e.g. a 5c longshot: smooth fee ~$0.003, real fee rounds up to $0.01 — 3x higher).
    Call this once contracts is known, right before placing the order.
    """
    fee_total  = kalshi_fee_dollars(contracts, price, series, maker)
    ev_exact   = fair_prob - price - fee_total / contracts
    return ev_exact >= min_ev


def _ev(fair_prob: float, price: float, fee_rate: float) -> float:
    """
    Expected value per contract in dollars.
    Kalshi fee = fee_rate * price * (1-price), charged at entry regardless of outcome.
      EV = fair_prob - price - fee_rate * price * (1-price)
    """
    return fair_prob - price - fee_rate * price * (1 - price)


def _kelly_fraction(roi: float) -> float:
    """
    Partial Kelly multiplier based on return-on-investment (ev/price).
    Higher ROI → rarer opportunity → size more aggressively.
    """
    if roi < 0.02:
        return 0.10
    elif roi < 0.05:
        return 0.20
    elif roi < 0.10:
        return 0.33
    else:
        return 0.50


def kelly_contracts(fair_prob: float, price: float, bankroll: float,
                    fee_rate: float) -> int:
    """
    Returns the number of YES contracts to buy using partial Kelly.

    Kalshi fee model: fee = fee_rate * price * (1-price) per contract (charged at entry).
    Net win per contract = (1-price) - fee = (1-price)*(1 - fee_rate*price)
        f* = EV / win_amount   [full Kelly fraction of bankroll]
    Scaled by a ROI-adjusted partial fraction.
    Each contract costs `price` dollars.

    dollar_bet is floored at MIN_NOTIONAL: real Kalshi fees round UP to the next
    cent per order (see KALSHI/k_helpers.kalshi_fee_dollars), so a 1-contract order
    pays a wildly higher effective rate than the smooth per-contract estimate — e.g.
    a 5c longshot's real fee is ~3x the smooth estimate at 1 contract. The
    monitoring/execution overhead per signal is roughly fixed regardless of size, so
    capturing a real edge at a few dollars notional instead of a few cents is close
    to free extra expected value on trades already being taken (caller's existing
    insufficient_cash check still applies — this floor never forces a trade past
    what the bankroll can afford).
    """
    ev = _ev(fair_prob, price, fee_rate)
    if ev <= 0:
        return 0
    win_amount = (1 - price) * (1 - fee_rate * price)
    full_kelly = ev / win_amount
    partial    = _kelly_fraction(ev / price)   # ROI = ev per dollar at risk
    dollar_bet = max(bankroll * full_kelly * partial, MIN_NOTIONAL)
    return max(floor(dollar_bet / price), 1)


def resolve_contracts(signal_row, fair_prob: float, price: float, bankroll: float,
                      fee_rate: float, size_mult: float = 1.0) -> Optional[int]:
    """
    Contract count for one order: a manual size typed directly into the web
    UI's signals table (signal_row['contracts_override']) always wins over
    Kelly sizing when present and positive — the trader took direct control,
    so size_mult (a blanket Kelly multiplier) doesn't apply on top of it.
    Absent an override, falls back to kelly_contracts() * size_mult, same as
    before this existed.

    Returns None if the resulting size is non-positive — the caller should
    treat that exactly like kelly_contracts() returning 0 (skip the trade),
    not place a zero-contract order.
    """
    override = signal_row.get('contracts_override') if hasattr(signal_row, 'get') else None
    if override is not None and not pd.isna(override) and override > 0:
        return int(round(override))
    contracts = kelly_contracts(fair_prob, price, bankroll, fee_rate)
    if contracts <= 0:
        return None
    return max(1, round(contracts * size_mult))
