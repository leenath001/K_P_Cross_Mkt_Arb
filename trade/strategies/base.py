"""
trade/strategies/base.py — the strategy contract.

Deliberately a documented duck-typed convention, not a class hierarchy or ABC —
this is a small solo-dev trading bot, not a framework fielding third-party
plugins, and each strategy's signal shape genuinely differs (kp_arb prices
against Pinnacle fair_prob; nothing.py buys NO on the assumption almost
everything resolves NO with no external fair-value source at all). Forcing a
common base class here would either be too thin to be worth it or too rigid for
nothing.py's batch-buy model. What's actually shared (order placement,
monitoring, cancellation, dedup, logging) already lives in trade/core/ — that's
the real reuse boundary, not the strategy's own control flow.

A strategy module is expected to provide:

    generate_signals(pinnacle_df: pd.DataFrame, **kwargs) -> pd.DataFrame
        Turn raw Pinnacle odds into a DataFrame of candidate signals. Whatever
        columns the strategy needs downstream (fair_prob, entry price, signal
        booleans, ...) — the shape is the strategy's own business, web_app.py
        just passes the result through to run_all_signals.

    run_trade(signal_row: pd.Series, bankroll: float, **kwargs) -> dict
        Execute one signal: size it, place the order, monitor it via
        trade.core.execution._monitor, log the result via trade.core.logging_io
        (log_trade for real fills, log_unfilled_attempt otherwise), return a
        result dict with at least {'status', 'ticker', 'order_id', 'contracts'}.

    run_all_signals(signals_df: pd.DataFrame, bankroll: float, **kwargs) -> list
        Batch-run generate_signals()' output through run_trade(), one thread per
        signal. Dedup via trade.core.positions.open_tickers() before firing —
        every strategy shares this, not its own CSV-log check.

nothing.py doesn't fit the generate_signals/run_trade split as cleanly (it scans
whole series for cheap markets and sizes a budget across all of them in one
pass, not one Pinnacle-priced signal at a time) — it keeps its own entry point
shape but still builds on trade.core for placement/cancellation/dedup/logging.
"""
