"""'+15 min' (extend_resting_order) must re-rest each order at the price it had, on its own side."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.parametrize('order, side, price', [
    # the real San Jose order: NO @ 58c (YES-equivalent 42c) came back as NO @ 42c before the fix
    ({'book_side': 'ask', 'outcome_side': 'no', 'no_price_dollars': '0.5800', 'yes_price_dollars': '0.4200'}, 'no', 58.0),
    ({'book_side': 'bid', 'outcome_side': 'yes', 'no_price_dollars': '0.5800', 'yes_price_dollars': '0.4200'}, 'yes', 42.0),
])
def test_extend_keeps_price_on_its_own_side(monkeypatch, order, side, price):
    import trade.core.execution.order_mgmt as om
    now = datetime.now(timezone.utc)
    full = {**order, 'status': 'resting', 'initial_count_fp': '3.00',
            'expiration_time': (now + timedelta(minutes=10)).isoformat()}
    placed = {}
    monkeypatch.setattr(om, 'get_order_status', lambda oid: full)
    monkeypatch.setattr(om, 'resize_resting_order',
                        lambda t, oid, s, total, p, expiration_ts=None: placed.update(side=s, price=p) or {'action': 'resized'})
    om.extend_resting_order('KX-T', 'o1', (now + timedelta(hours=3)).isoformat())
    assert placed == {'side': side, 'price': price}
