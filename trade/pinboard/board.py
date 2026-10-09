"""
trade/pinboard/board.py — live board for the Pinnacle (K/P arbitrage) strategy.

One card per market with an order in this trading session, in the same style as the market-making board: a 5-level order
book with our resting order as a draggable YOU row, filled / average-price boxes, per-card Cancel, fill notifications.

PinBoard reads the session's StreamlitDashboard (which the monitor threads keep current) and adds what a live board needs on
top of it: order books (every 2 s), order fills straight from Kalshi (every 2.5 s, instead of the monitor's 10 s cadence) and
an event feed. Card actions (drag to re-price, resize, cancel) come back over a loopback HTTP channel — no Streamlit rerun.
"""
import os, sys, time, queue, threading
from concurrent.futures import ThreadPoolExecutor


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from KALSHI import client as kalshi
from KALSHI.k_helpers import MAKER_FEE_BASE
from trade.core.actionserver import ActionServer
from trade.core.execution import ensure_canceled, get_order_status, list_resting_orders, resize_resting_order, _ev
from trade.core.pricing import parse_ranges, step_at
from trade.strategies.kp_arb import resume_monitoring
from applog import get_logger

log = get_logger('kparb.pinboard')

BOOK_LEVELS = 15
POLL_SEC = 2.0
FILL_POLL_SEC = 2.5
META_SEC = 30
BOOKS_PER_CYCLE = 12          # order books read per 2 s cycle (visible + expanded cards first, the rest round-robin)
NEW_WATCH_SEC = 0.25          # how often to look for a market whose order just fired (loaded at once, reserved read lane)
POST_CLOSE_WATCH_SEC = 15 * 60  # keep refreshing a filled/closed market's book this long after it closes ("where's it going")
OPEN = {'resting', 'open', 'pending', 'unknown'}
LABELS = {'resting': 'RESTING', 'executed': 'FILLED', 'filled': 'FILLED', 'canceled': 'CANCELED', 'expired': 'EXPIRED',
          'signal_flipped': 'EDGE GONE', 'max_duration_exceeded': 'TIMED OUT', 'event_imminent': 'EVENT SOON',
          'skipped': 'SKIPPED'}


def _get(path, params=None, priority=False):
    if priority:                                   # reserved read lane (KALSHI/client.py)
        return kalshi.get(path, params, timeout=8, priority=True)
    return kalshi.get(path, params, timeout=8)     # plain call, so normal polling never depends on the newer argument


def _fetch_book(ticker, full=False, priority=False):
    r = _get(f'/markets/{ticker}/orderbook', {'depth': 0 if full else BOOK_LEVELS}, priority=priority)
    r.raise_for_status()
    ob = r.json().get('orderbook_fp') or {}
    yes = [(round(float(p) * 100, 3), float(q)) for p, q in (ob.get('yes_dollars') or [])]
    no = [(round(float(p) * 100, 3), float(q)) for p, q in (ob.get('no_dollars') or [])]
    return {'bids': sorted(yes, key=lambda t: -t[0]), 'asks': sorted(((round(100 - p, 3), q) for p, q in no), key=lambda t: t[0])}


def _fetch_meta(ticker):
    r = _get(f'/markets/{ticker}')
    r.raise_for_status()
    mk = r.json().get('market', {})
    lp = mk.get('last_price_dollars')
    return {'ranges': parse_ranges(mk.get('price_ranges')),
            'last_c': round(float(lp) * 100, 3) if lp and float(lp) > 0 else None}


class PinBoard:
    def __init__(self, dash, stop_event=None):
        self.dash, self.stop_event = dash, stop_event
        self.lock = threading.RLock()
        self.books, self.live, self.px_override = {}, {}, {}
        self.events, self._ev_id, self._seen = [], 0, {}
        self.focus, self.ack, self._rr = None, None, 0
        self.visible = set()     # tickers whose cards are on screen right now (sent by the board), refreshed every cycle
        self._primed = set()     # markets whose instant first read has been attempted (once each — see _watch_new)
        self._closed_at = {}   # ticker -> time.time() when we first saw it close, for POST_CLOSE_WATCH_SEC
        self._first_polled = {}   # order_id -> first time _poll_fills looked at it (grace for Kalshi's listing lag)
        self.notes, self._busy = {}, set()
        self._act_seen, self._act_lock = [], threading.Lock()
        self._stop = threading.Event()
        self.server = ActionServer(self.snapshot, self.handle_action)
        self._thread = threading.Thread(target=self._loop, name='pin-board', daemon=True)
        self._thread.start()
        self._prime_q = queue.Queue()          # first reads run on daemon threads: pool workers are joined at exit
        for i in range(4):
            threading.Thread(target=self._prime_worker, name=f'pin-prime-{i}', daemon=True).start()
        threading.Thread(target=self._watch_new, name='pin-board-new', daemon=True).start()

    def stop(self):
        self._stop.set()
        self.server.stop()

    # ── aggregation ────────────────────────────────────────────────────────
    def _groups(self):
        """{ticker: [position dicts in placement order]} from the dashboard."""
        out = {}
        for pos in self.dash.snapshot()['positions'].values():
            out.setdefault(pos['ticker'], []).append(pos)
        return out

    def _entry_fill(self, pos):
        """(filled, avg ¢, status) for one order, merging Kalshi (self.live, polled here) with the dashboard (kept by the
        order's monitor). A close either side has seen is final — a stale or 'unknown' poll must never put a card back
        to RESTING after its monitor recorded the cancel/fill — and fill counts only ever go up."""
        avg_d = pos.get('avg_fill_price')
        filled_d, avg_d, status_d = pos.get('filled') or 0, (avg_d * 100 if avg_d is not None else None), pos['status']
        lv = self.live.get(pos['order_id'])
        if not lv:
            return filled_d, avg_d, status_d
        status = lv['status']
        if status in OPEN and status_d not in OPEN:
            status = status_d
        if lv['filled'] >= filled_d:
            return lv['filled'], lv['avg_c'], status
        return filled_d, avg_d, status

    def _card(self, ticker, entries, focus):
        act = entries[-1]
        side = act['side']
        filled = vw = 0.0
        for e in entries:
            f, a, _ = self._entry_fill(e)
            filled += f
            vw += f * (a or 0.0)
        avg = vw / filled if filled else None
        f_act, _, st_act = self._entry_fill(act)
        contracts = act['contracts'] + sum(self._entry_fill(e)[0] for e in entries[:-1])
        open_now = st_act in OPEN
        if filled >= contracts > 0:
            status, label = 'filled', 'FILLED'
        elif not open_now:
            status, label = 'closed', LABELS.get(st_act, st_act.upper())
            if filled > 0:
                status, label = 'partial', 'PARTIAL · ' + label
        else:
            status, label = ('partial', 'PARTIAL') if filled > 0 else ('resting', 'RESTING')
        entry_c = self.px_override.get(act['order_id'], act['entry_price'])
        yes_px = entry_c if side == 'yes' else 100 - entry_c
        bk = self.books.get(ticker) or {}
        bids, asks = bk.get('bids', []), bk.get('asks', [])
        n = None if focus else 6                      # the expanded card gets the whole book
        bb, ba = (bids[0][0] if bids else None), (asks[0][0] if asks else None)
        tick = step_at(bb if bb is not None else 50, bk.get('ranges')) if bk else 1.0
        mid = (bb + ba) / 2 if bb is not None and ba is not None else None
        exit_c = (bb if side == 'yes' else (100 - ba if ba is not None else None)) if bids or asks else None
        ask_own = (ba if side == 'yes' else (100 - bb if bb is not None else None))
        remaining = max(act['contracts'] - f_act, 0)
        fair_last = act.get('fair_last')
        fair_yes = None if fair_last is None else round((fair_last if side == 'yes' else 1 - fair_last) * 100, 2)
        orders = []
        if open_now and remaining > 0:
            orders.append({'side': 'bid' if side == 'yes' else 'ask', 'price_c': yes_px, 'size': remaining,
                           'filled': f_act, 'manual': False})
        unreal = None if (avg is None or exit_c is None) else round(filled * (exit_c - avg) / 100, 3)
        note = self.notes.get(ticker)
        sp = config.SPORTS_CONFIG.get(act.get('sport', ''), {})
        return {
            'ticker': ticker, 'title': act['outcome'], 'outcome': sp.get('label', act.get('sport', '')),
            'sport': act.get('sport', ''), 'side': side, 'start_iso': act.get('commence', ''),
            'status': status, 'status_label': label, 'contracts': contracts, 'filled': filled, 'remaining': remaining,
            'avg_c': None if avg is None else round(avg, 2), 'entry_c': entry_c, 'fair_entry': act.get('fair_entry'),
            'fair_last': fair_last, 'edge': act.get('edge_last'), 'fee_rate': act.get('fee_rate'),
            'mkt_ask_c': ask_own, 'exit_c': exit_c, 'unreal_usd': unreal,
            'cost_usd': round(sum(self._entry_fill(e)[0] * (self._entry_fill(e)[1] or 0) for e in entries) / 100, 2),
            'to_win_usd': None if avg is None else round(filled * (100 - avg) / 100, 2),
            'last_ping': act.get('last_ping'), 'fair_c': fair_yes,
            'bids': bids[:n], 'asks': asks[:n], 'book_loaded': 'bids' in bk, 'tick_c': tick, 'last_c': bk.get('last_c'), 'mid_c': mid,
            'orders': orders, 'can_cancel': open_now and remaining > 0,
            'note': note[0] if note and note[1] > time.time() else '',
            'stats': {'spread_c': None if (bb is None or ba is None) else round(ba - bb, 3),
                      'depth_bid_5c': round(sum(q for p, q in bids if bb is not None and p >= bb - 5), 1),
                      'depth_ask_5c': round(sum(q for p, q in asks if ba is not None and p <= ba + 5), 1)},
        }

    def snapshot(self):
        with self.lock:
            groups = self._groups()
            markets = [self._card(t, es, t == self.focus) for t, es in groups.items()]
            return {'ts': time.time(), 'markets': markets, 'events': list(self.events[-15:]), 'ack': self.ack,
                    'act': self.server.info()}

    # ── background poller ──────────────────────────────────────────────────
    def _loop(self):
        last_fill = last_meta = 0.0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                groups = self._groups()
                now = time.time()
                # Every cycle: the expanded card, cards on screen, and any market never read yet (normally already
                # loaded by _watch_new the moment its order fired — this is the fallback if that read failed).
                # Everything else shares the rest of a fixed budget, round-robin: a 200-order batch re-reading every
                # book each cycle swamped the shared read budget and new cards sat on "no book".
                always = {t for t in groups if t == self.focus or t in self.visible or 'bids' not in self.books.get(t, {})}
                # then open markets, then recently-closed ones (kept fresh a while to show where the market went next)
                rest = [t for t, es in groups.items() if t not in always and es[-1]['status'] in OPEN]
                rest += [t for t, ts in self._closed_at.items()
                         if t in groups and t not in always and t not in rest and now - ts < POST_CLOSE_WATCH_SEC]
                picked = list(always)
                if rest:
                    room = max(0, BOOKS_PER_CYCLE - len(always))
                    start = self._rr % len(rest)
                    picked += (rest[start:] + rest[:start])[:room]
                    self._rr = (start + room) % len(rest)
                live_t = picked   # meta refresh below piggybacks on the same eligibility
                if picked:
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        for t, bk in pool.map(self._read_book, picked):
                            if bk:
                                with self.lock:
                                    self.books.setdefault(t, {}).update(bk)
                if time.time() - last_fill > FILL_POLL_SEC:
                    last_fill = time.time()
                    self._poll_fills(groups)
                if time.time() - last_meta > META_SEC and live_t:
                    last_meta = time.time()
                    for t in live_t:
                        try:
                            m = _fetch_meta(t)
                            with self.lock:
                                self.books.setdefault(t, {}).update(m)
                        except Exception:
                            pass
                self._diff_events(groups)
            except Exception:
                log.exception('pinboard: loop error')
            self._stop.wait(max(0.3, POLL_SEC - (time.time() - t0)))

    def _watch_new(self):
        """Load a market's book the moment its order shows up, instead of on the next poll cycle behind every other
        read: first read on the reserved lane (KALSHI/client.py), market metadata right after."""
        while not self._stop.is_set():
            try:
                for t in self._groups():
                    if t not in self.books and t not in self._primed:
                        # ONE fast attempt per market. If it fails, the regular poll loop keeps retrying on its normal
                        # cadence (it always reads unloaded markets) — retrying here every 0.25 s turned one failure
                        # into a flood of reads and log lines.
                        self._primed.add(t)
                        self._prime_q.put(t)
            except Exception:
                log.exception('pinboard: new-market watch failed')
            self._stop.wait(NEW_WATCH_SEC)

    def _prime_worker(self):
        while not self._stop.is_set():
            try:
                t = self._prime_q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._prime(t)

    def _prime(self, t):
        try:
            bk = _fetch_book(t, priority=True)
            with self.lock:
                self.books.setdefault(t, {}).update(bk)
            m = _fetch_meta(t)
            with self.lock:
                self.books[t].update(m)
        except Exception as exc:
            log.info('pinboard: first read for %s failed (%s) — the poll loop will retry', t, exc)

    def _read_book(self, t):
        try:
            return t, (_fetch_book(t, True) if t == self.focus else _fetch_book(t))
        except Exception:
            return t, None

    def _poll_fills(self, groups):
        """Fills straight from Kalshi so they show within seconds, not on the monitor's 10 s cadence. One call lists every
        resting order (with its fill count); an order that has left that list is looked up individually once."""
        todo = [e['order_id'] for es in groups.values() for e in es
                if e['status'] in OPEN                                                  # monitor hasn't closed it
                and self.live.get(e['order_id'], {}).get('status', 'resting') in OPEN]   # until Kalshi says it is closed
        if not todo:
            return
        try:
            resting = {o['order_id']: o for o in list_resting_orders()}
        except Exception:
            resting = None

        def _view(st):
            f = float(st.get('fill_count_fp') or 0)
            cost = float(st.get('maker_fill_cost_dollars') or 0) + float(st.get('taker_fill_cost_dollars') or 0)
            return {'filled': f, 'avg_c': (cost / f * 100) if f else None, 'status': st.get('status', 'unknown')}

        now = time.time()
        for oid in todo:
            self._first_polled.setdefault(oid, now)

        def _one(oid):
            try:
                if resting is not None and oid in resting:
                    return oid, _view(resting[oid])
                v = _view(get_order_status(oid))
                # Not in the (complete, paginated) resting list and Kalshi can't find it: it's gone — Kalshi drops a
                # cancelled/expired order that never filled. 'unknown' would otherwise count as open forever. The grace
                # period covers Kalshi's list lagging a brand-new order.
                if v['status'] == 'unknown' and resting is not None and now - self._first_polled[oid] > 20:
                    prev = self.live.get(oid, {})
                    v = {'filled': prev.get('filled', 0.0), 'avg_c': prev.get('avg_c'), 'status': 'canceled'}
                return oid, v
            except Exception:
                return oid, None
        with ThreadPoolExecutor(max_workers=6) as pool:
            for oid, v in pool.map(_one, todo):
                if v:
                    with self.lock:
                        self.live[oid] = v

    def _diff_events(self, groups):
        with self.lock:
            for t, es in groups.items():
                card = self._card(t, es, False)
                prev = self._seen.get(t)
                if prev is None:
                    self._seen[t] = {'filled': card['filled'], 'status': card['status']}
                    if card['filled'] > 0:
                        self._event(card, 'fill', card['filled'])
                    continue
                if card['filled'] > prev['filled'] + 1e-6:
                    self._event(card, 'fill', card['filled'] - prev['filled'])
                if card['status'] == 'closed' and prev['status'] != 'closed':
                    self._event(card, 'closed', 0)
                    self._closed_at[t] = time.time()
                self._seen[t] = {'filled': card['filled'], 'status': card['status']}

    def _event(self, card, kind, qty):
        self._ev_id += 1
        bids, asks = card['bids'], card['asks']
        self.events.append({'id': self._ev_id, 'ts': time.time(), 'ticker': card['ticker'], 'title': card['title'],
                            'outcome': card['outcome'], 'kind': kind, 'side': card['side'], 'qty': qty,
                            'price_c': card['avg_c'] if kind == 'fill' else card['entry_c'],
                            'bid': bids[0][0] if bids else None, 'ask': asks[0][0] if asks else None,
                            'filled': card['filled'], 'contracts': card['contracts'], 'label': card['status_label']})
        del self.events[:-40]

    # ── actions from the board ─────────────────────────────────────────────
    def handle_action(self, act):
        nonce = act.get('nonce')
        with self._act_lock:
            if nonce is not None:
                if nonce in self._act_seen:
                    return
                self._act_seen.append(nonce)
                del self._act_seen[:-300]
        a, t = act.get('action'), act.get('ticker')
        try:
            if a == 'focus':
                self.focus = t
            elif a == 'visible':
                self.visible = set(act.get('tickers') or [])
            elif a == 'cancel_market':
                self._cancel(t)
            elif a == 'set_quote':
                self._replace(t, price_yes=float(act['price_c']))
            elif a == 'set_size':
                self._replace(t, new_total=int(act['size']))
        except Exception:
            log.exception('pinboard: action %s failed', a)
            self._note(t, 'action failed — see app.log')

    def _note(self, t, msg, secs=8):
        self.notes[t] = (msg, time.time() + secs)

    def _active(self, ticker):
        es = self._groups().get(ticker)
        return es[-1] if es else None

    def _cancel(self, ticker):
        pos = self._active(ticker)
        if not pos or pos['status'] not in OPEN or ticker in self._busy:
            return
        self._busy.add(ticker)
        try:
            if ensure_canceled(ticker, pos['order_id']):
                st = get_order_status(pos['order_id'])
                with self.lock:
                    self.live[pos['order_id']] = {'filled': float(st.get('fill_count_fp') or 0),
                                                  'avg_c': self.live.get(pos['order_id'], {}).get('avg_c'),
                                                  'status': 'canceled'}
                self.dash.update(pos['order_id'], status='canceled')
                log.info('pinboard: cancelled %s from the board', ticker)
            else:
                self._note(ticker, 'could not confirm the cancel')
        finally:
            self._busy.discard(ticker)

    def _replace(self, ticker, price_yes=None, new_total=None):
        """Drag = cancel and re-rest at the new price (same remaining size); size box = new total contracts."""
        pos = self._active(ticker)
        if not pos or pos['status'] not in OPEN or ticker in self._busy:
            return
        side = pos['side']
        old_entry = self.px_override.get(pos['order_id'], pos['entry_price'])
        entry = old_entry
        if price_yes is not None:
            entry = round(price_yes if side == 'yes' else 100 - price_yes, 3)
            bk = self.books.get(ticker) or {}
            bb = bk['bids'][0][0] if bk.get('bids') else None
            ba = bk['asks'][0][0] if bk.get('asks') else None
            crosses = (side == 'yes' and ba is not None and price_yes >= ba - 1e-9) or \
                      (side == 'no' and bb is not None and price_yes <= bb + 1e-9)
            if crosses:
                self._note(ticker, 'not moved: that price would cross the book')
                return
        prior = sum(self._entry_fill(e)[0] for e in self._groups().get(ticker, [])[:-1])   # fills from an earlier, re-priced order
        total = (new_total - prior) if new_total is not None else pos['contracts']
        if total < 1:
            self._note(ticker, 'not changed: below what is already filled')
            return
        if abs(entry - old_entry) < 1e-9 and total == pos['contracts']:
            return
        self._busy.add(ticker)
        try:
            res = resize_resting_order(ticker, pos['order_id'], side, int(total), entry)
            if res['action'] != 'resized':
                self._note(ticker, 'not changed: ' + str(res.get('reason', 'error'))[:60])
                return
            price = entry / 100
            fee_rate = pos.get('fee_rate', MAKER_FEE_BASE)
            fair = pos.get('fair_last', pos.get('fair_entry', 0.5))
            new_id = res['new_order_id']
            with self.lock:
                self.px_override[new_id] = entry
            threading.Thread(target=resume_monitoring, kwargs=dict(
                order_id=new_id, ticker=ticker, event_id=pos.get('event_id', ''), sport=pos.get('sport', ''),
                outcome=pos.get('raw_outcome', pos['outcome']), order_price=price, fee_rate=fee_rate,
                commence=pos.get('commence', ''), side=side, contracts=res['new_remaining'], fair_prob=fair,
                ev_per_contract=_ev(fair, price, fee_rate), dashboard=self.dash, stop_event=self.stop_event),
                daemon=True).start()
            log.info('pinboard: re-rested %s at %s (total %s)', ticker, entry, total)
        finally:
            self._busy.discard(ticker)
