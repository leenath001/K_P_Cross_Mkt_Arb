"""
Read-load behaviour with Kalshi faked: the live board loads a new market's book at once (reserved read lane) and keeps
its per-cycle reads bounded; order monitors share one resting-orders snapshot; resting-order listing paginates.
"""
import threading
import time

import pytest


class FakeDash:
    def __init__(self):
        self.pos = {}

    def add(self, oid, ticker, status='resting'):
        self.pos[oid] = {'order_id': oid, 'ticker': ticker, 'status': status, 'side': 'yes', 'contracts': 2,
                         'entry_price': 50, 'outcome': ticker, 'sport': ''}

    def snapshot(self):
        return {'positions': dict(self.pos)}


@pytest.fixture
def board(monkeypatch):
    import trade.pinboard.board as pb
    calls = []

    def fake_book(t, full=False, priority=False):
        calls.append((t, priority, time.time()))
        return {'bids': [(49.0, 10.0)], 'asks': [(51.0, 10.0)]}

    monkeypatch.setattr(pb, '_fetch_book', fake_book)
    monkeypatch.setattr(pb, '_fetch_meta', lambda t: {'ranges': None, 'last_c': None})
    monkeypatch.setattr(pb.PinBoard, '_poll_fills', lambda self, groups: None)
    monkeypatch.setattr(pb, 'POLL_SEC', 60.0)            # keep the regular cycle out of the way: test the fast path
    dash = FakeDash()
    b = pb.PinBoard(dash)
    yield b, dash, calls, pb
    b.stop()


def test_new_market_book_loads_immediately_on_reserved_lane(board):
    b, dash, calls, _ = board
    time.sleep(0.3)
    dash.add('o1', 'KX-NEW')
    t0 = time.time()
    while 'bids' not in b.books.get('KX-NEW', {}) and time.time() - t0 < 2:
        time.sleep(0.02)
    assert 'bids' in b.books['KX-NEW'], 'book never loaded'
    assert time.time() - t0 < 0.6
    assert any(t == 'KX-NEW' and prio for t, prio, _ in calls), 'first read should use the reserved lane'
    card = b._card('KX-NEW', b._groups()['KX-NEW'], False)
    assert card['book_loaded'] is True


def test_cycle_reads_are_bounded_and_visible_first(board, monkeypatch):
    b, dash, calls, pb = board
    for i in range(100):
        dash.add(f'o{i}', f'KX-{i}')
        b.books[f'KX-{i}'] = {'bids': [], 'asks': []}       # already loaded once
    b.visible = {'KX-97', 'KX-98'}
    picked = []
    monkeypatch.setattr(pb.PinBoard, '_read_book', lambda self, t: (picked.append(t), (t, None))[1])
    b._stop.set(); time.sleep(0.05); b._stop.clear()
    # run one cycle of the loop body by hand
    loop = threading.Thread(target=b._loop, daemon=True)
    loop.start(); time.sleep(0.3); b._stop.set(); loop.join(2)
    assert len(picked) <= pb.BOOKS_PER_CYCLE
    assert {'KX-97', 'KX-98'} <= set(picked)


def test_monitors_share_one_resting_snapshot(monkeypatch):
    import trade.core.execution.live_cache as lc
    reads = {'list': 0, 'direct': 0}
    resting = {f'o{i}': {'order_id': f'o{i}', 'status': 'resting', 'fill_count_fp': '0.00'} for i in range(50)}

    def fake_fetch():
        reads['list'] += 1
        return resting

    monkeypatch.setattr(lc, '_resting', lc._Snapshot(fake_fetch))
    monkeypatch.setattr(lc, 'get_order_status', lambda oid: (reads.__setitem__('direct', reads['direct'] + 1),
                                                              {'status': 'executed', 'order_id': oid})[1])
    threads = [threading.Thread(target=lc.order_status, args=(f'o{i}',)) for i in range(50)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert reads == {'list': 1, 'direct': 0}                 # 50 monitors, one read
    assert lc.order_status('gone')['status'] == 'executed'   # closed order: exact state from the direct lookup
    assert reads['direct'] == 1


def test_list_resting_orders_paginates(monkeypatch):
    import trade.core.execution.orders as orders
    pages = {None: ({'orders': [{'order_id': f'a{i}', 'status': 'resting'} for i in range(200)], 'cursor': 'p2'}),
             'p2': ({'orders': [{'order_id': f'b{i}', 'status': 'resting'} for i in range(37)], 'cursor': ''})}

    class R:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    monkeypatch.setattr(orders.kalshi, 'get', lambda path, params=None, **k: R(pages[params.get('cursor')]))
    assert len(orders.list_resting_orders()) == 237


def test_failed_first_read_is_not_retried_in_a_loop_and_old_client_still_works(monkeypatch):
    """Regression: a hot-reload left an older KALSHI.client without `priority`; every first read failed and was retried
    every 0.25 s per market, flooding app.log. Now: one fast attempt per market, and normal polling (which never
    passes `priority`) still loads the books."""
    import trade.pinboard.board as pb
    attempts = {}

    def old_client_get(path, params=None, *, timeout=15, auth=True):        # the pre-`priority` signature
        ticker = path.split('/')[2]
        attempts[ticker] = attempts.get(ticker, 0) + 1

        class R:
            def raise_for_status(self): pass
            def json(self): return {'orderbook_fp': {'yes_dollars': [['0.49', '10']], 'no_dollars': [['0.49', '10']]},
                                    'market': {}}
        return R()

    monkeypatch.setattr(pb.kalshi, 'get', old_client_get)
    monkeypatch.setattr(pb.PinBoard, '_poll_fills', lambda self, groups: None)
    monkeypatch.setattr(pb, 'POLL_SEC', 0.2)
    primes = []
    real_prime = pb.PinBoard._prime
    monkeypatch.setattr(pb.PinBoard, '_prime', lambda self, t: (primes.append(t), real_prime(self, t)))
    dash = FakeDash()
    for i in range(5):
        dash.add(f'o{i}', f'KX-{i}')
    b = pb.PinBoard(dash)
    try:
        time.sleep(1.5)
        assert all('bids' in b.books.get(f'KX-{i}', {}) for i in range(5)), 'normal polling should load every book'
        assert len(primes) == len(set(primes)) <= 5      # at most ONE fast attempt per market, never re-queued
    finally:
        b.stop()


def test_card_never_reverts_to_resting_after_a_recorded_close(board):
    b, dash, calls, pb = board
    dash.add('o1', 'KX-SJ')
    dash.pos['o1']['status'] = 'canceled'                          # the monitor cancelled it
    b.live['o1'] = {'filled': 0.0, 'avg_c': None, 'status': 'resting'}   # a stale poll still says resting
    card = b._card('KX-SJ', b._groups()['KX-SJ'], False)
    assert card['status'] == 'closed' and card['status_label'] == 'CANCELED'


def test_order_kalshi_no_longer_returns_is_treated_as_closed(monkeypatch):
    import trade.pinboard.board as pb
    monkeypatch.setattr(pb, '_fetch_book', lambda t, full=False, priority=False: {'bids': [], 'asks': []})
    monkeypatch.setattr(pb, '_fetch_meta', lambda t: {'ranges': None, 'last_c': None})
    monkeypatch.setattr(pb, 'POLL_SEC', 60.0)
    monkeypatch.setattr(pb, 'list_resting_orders', lambda: [])                                         # not resting
    monkeypatch.setattr(pb, 'get_order_status', lambda oid: {'status': 'unknown', 'order_id': oid})   # Kalshi can't find it
    dash = FakeDash()
    b = pb.PinBoard(dash)
    try:
        dash.add('o2', 'KX-GONE')
        b._first_polled['o2'] = time.time() - 60          # past the new-order grace period
        b._poll_fills(b._groups())
        assert b.live['o2']['status'] == 'canceled'
        assert b._card('KX-GONE', b._groups()['KX-GONE'], False)['status'] == 'closed'
        # ...but a brand-new order Kalshi hasn't listed yet keeps its grace period
        dash.add('o3', 'KX-NEW')
        b._poll_fills(b._groups())
        assert b.live['o3']['status'] == 'unknown'
    finally:
        b.stop()
