"""
trade/strategies — pluggable trading strategies.

Each module here (kp_arb, prospect, nothing) implements the loose contract
documented in base.py, building on trade/core/ for order execution, live-state
dedup, and trade logging. To add a new strategy: write a new module in this
package following the same contract, wire it into a web_app.py tab, done — no
need to touch order placement, monitoring, cancellation, or dedup logic, since
that's all shared.
"""
