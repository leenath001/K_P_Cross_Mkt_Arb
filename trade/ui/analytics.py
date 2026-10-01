"""
trade/ui/analytics.py — pure calculations behind the dashboard's charts (no Streamlit), so they can be unit-tested.
"""
import numpy as np
import pandas as pd


def open_trade_outcomes(live: pd.DataFrame):
    """
    Exact distribution of the PnL still to come from unsettled trades: (outcome $ offsets, probabilities, expected $),
    or None if there's nothing to project. Same payoff model as settle.compute_pnl (fee charged at entry, win or
    lose): win = c·((1−e) − fee), loss = c·(−e − fee), fee = fee_rate·e·(1−e). A win pays exactly c dollars more than
    a loss, so the total is (sum of all losses) + Σ cᵢ·Bernoulli(pᵢ) — convolved exactly on a 1-contract grid.
    pᵢ = our side's Pinnacle fair_prob at entry (falls back to the entry price).
    """
    if live is None or live.empty:
        return None
    c = pd.to_numeric(live.get('contracts'), errors='coerce')
    e = pd.to_numeric(live.get('entry_price'), errors='coerce')
    fr = pd.to_numeric(live.get('fee_rate'), errors='coerce').fillna(0)
    p = pd.to_numeric(live.get('fair_prob'), errors='coerce').fillna(e).clip(0, 1)
    ok = c.notna() & e.notna() & (c > 0)
    if not ok.any():
        return None
    c, e, fr, p = c[ok].round().astype(int), e[ok], fr[ok], p[ok]
    base = float((c * (-e - fr * e * (1 - e))).sum())
    dist = np.array([1.0])
    for ci, pi in zip(c, p):
        nxt = np.zeros(len(dist) + ci)
        nxt[:len(dist)] += dist * (1 - pi)
        nxt[ci:] += dist * pi
        dist = nxt
    vals = base + np.arange(len(dist), dtype=float)
    return vals, dist, float((vals * dist).sum())
