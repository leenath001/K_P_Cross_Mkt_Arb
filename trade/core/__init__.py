"""
trade/core — strategy-agnostic execution primitives shared by every strategy
in trade/strategies/.

    execution.py   place_order, cancel_order/ensure_canceled, monitoring loop,
                   Kelly sizing, EV math — anything that talks to Kalshi's order
                   API or decides how much to risk, independent of which
                   strategy generated the signal.
    positions.py   open_tickers() — live Kalshi state (positions + resting
                   orders), the single dedup source every strategy uses instead
                   of reading its own CSV log.
    logging_io.py  log_trade() / log_unfilled_attempt() — the filled/unfilled
                   split so an order that never filled doesn't pollute the
                   win/loss trade history.
"""
