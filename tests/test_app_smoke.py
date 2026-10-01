"""
Smoke test: run the whole Streamlit page headlessly (every tab executes) with all trading, Kalshi and Odds API calls
faked. Catches the NameErrors / bad imports / broken wiring a refactor tends to introduce. Network is blocked by
conftest.py; trade logs are only read, never written (settling is faked).
"""
import os
import time

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'trade', 'web_app.py')


def _refuse(*a, **k):
    raise AssertionError('a trading write was attempted during the smoke test')


@pytest.fixture
def faked(monkeypatch):
    import config
    import settle
    import theODDS.p_helpers as ph
    import KALSHI.k_helpers as kh
    import trade.core.execution as ex
    import trade.core.positions as pos
    import trade.core.balance_feed as bf
    import trade.clv as clv
    import trade.mm.engine as eng

    monkeypatch.setattr(ph, 'fetch_usage', lambda: (100, 19900))
    monkeypatch.setattr(ph, 'check_sports_with_events', lambda keys, hrs: {k: 0 for k in keys})
    monkeypatch.setattr(ph, 'pinnacle_odds', lambda *a, **k: (_ for _ in ()).throw(ValueError('no events (faked)')))
    monkeypatch.setattr(kh, 'fee_rate_for', lambda series, maker=False: 0.0 if maker else 0.07)
    for name in ('place_order', 'cancel_order', 'ensure_canceled', 'cancel_all_resting_orders',
                 'cross_and_cancel_order', 'cancel_and_rerest', 'resize_resting_order', 'extend_resting_order'):
        monkeypatch.setattr(ex, name, _refuse)
    monkeypatch.setattr(ex, 'get_balance', lambda: 50.0)
    monkeypatch.setattr(ex, 'list_resting_orders', lambda: [])
    monkeypatch.setattr(pos, 'open_tickers', lambda: set())
    monkeypatch.setattr(bf, 'start', lambda: {'port': 1, 'token': 'test'})
    monkeypatch.setattr(bf, 'latest', lambda: {'cash': 50.0, 'portfolio': 60.0})
    monkeypatch.setattr(clv, 'start_background', lambda *a, **k: None)
    monkeypatch.setattr(settle, 'refresh_statuses', lambda path: 0)
    monkeypatch.setattr(settle, '_settle_file', lambda *a, **k: 0)
    monkeypatch.setattr(eng.MMEngine, 'start', lambda self: None)
    monkeypatch.setattr(eng.MMEngine, 'screen_async', lambda self, sports, hrs: None)
    monkeypatch.setattr(eng.MMEngine, '_persist', lambda self: None)
    return config


def test_page_renders_without_errors(faked):
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # the top-bar look-ahead box exists and drives the session value
    box = [n for n in at.number_input if n.key == '_hrs']
    assert box, 'look-ahead box missing'
    box[0].set_value(24).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state['_hrs'] == 24
