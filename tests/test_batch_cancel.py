"""Pressing Cancel mid-batch: orders already sent stay with their monitors (which cancel them); queued waves are never
sent, and the run returns at once instead of sleeping out the wave delay."""
import threading
import time

import pandas as pd


def test_cancel_mid_batch_stops_unsent_waves(monkeypatch):
    import trade.strategies.kp_arb as kp
    sent = []

    def fake_run_trade(row, **kw):
        sent.append(row['k_ticker'])
        return {'status': 'placed', 'ticker': row['k_ticker'], 'order_id': 'o-' + row['k_ticker'], 'contracts': 1}

    monkeypatch.setattr(kp, 'run_trade', fake_run_trade)
    monkeypatch.setattr(kp, 'open_tickers', lambda: set())
    monkeypatch.setattr(kp, 'opposite_leg_blocked', lambda tickers, open_: set())
    monkeypatch.setattr(kp, 'drop_same_event_duplicates', lambda df: df)
    monkeypatch.setattr(kp, 'force_cancel_all', lambda registry: 0)
    df = pd.DataFrame({'k_ticker': [f'KX-{i}' for i in range(80)], 'signal': True, 'signal_yes_rest': False,
                       'fair_prob': 0.6, 'yes_ask': 0.5})
    stop = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault('r', kp.run_all_signals(df, 100, stop_event=stop)), daemon=True)
    t.start()
    while len(sent) < 10:
        time.sleep(0.01)
    time.sleep(0.2)                      # first wave sent, the loop is now in its 10 s wait
    t0 = time.time()
    stop.set()                           # Cancel
    t.join(5)
    assert not t.is_alive() and time.time() - t0 < 1.5, 'run did not stop promptly'
    assert len(sent) == 10, f'{len(sent)} orders sent; only the first wave should have gone out'
    reasons = [r['reason'] for r in out['r'] if r and r.get('status') == 'skipped']
    assert reasons.count('canceled_before_send') == 70
