"""
trade/mm/engine.py — market-making prototype engine.

Two-sided quoting on a Kalshi market: a resting YES bid at `b` and a resting YES ask
at `a` (an ask at `a` on YES is a NO bid at 100−a). If both fill, you hold YES and NO
and are paid exactly $1, so `a − b` is locked in (zero maker fee markets only — the
screen refuses anything else).

Quoting is either ON (real post_only GTC orders, cancelled + re-fired whenever the fair-driven
quote moves) or OFF
(the board just shows the order book; nothing is placed and no quotes are shown).

Quote rule (pure function `compute_targets`): a market ONE TICK WIDE around Pinnacle fair
(bid = fair floored to the grid, ask = bid + 1 tick), never marketable. Both sides stay quoted
after a one-sided fill (the quote just re-centres on the new fair); size per side is per market.
"""
import os, sys, json, math, time, uuid, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from KALSHI.k_helpers import kalshi_headers, fee_rate_for, kalshi_odds, BASE_URL
from theODDS.p_helpers import pinnacle_odds
from trade.core.execution import (place_order, ensure_canceled, get_order_status,
                                  list_resting_orders, get_balance, PRE_EVENT_BUFFER)
from trade.core.pricing import parse_ranges, step_at, snap_down
from trade.mm.ledger import log_fill, fills_for_ticker, per_order_totals
from applog import get_logger

log = get_logger(__name__)

SELECTION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'logs', 'mm_selection.json')
ENGINE_VERSION = 8     # bump when MMEngine's state/attributes change: the UI then swaps out a stale cached engine
MAX_SIZE, DEFAULT_SIZE, BOOK_DEPTH, UI_DEADMAN_SEC, MM_PREFIX = 25, 5, 15, 90, 'mm-'
POLL_BUDGET, MAX_NEW_MKTS_PER_CYCLE, CASH_RESERVE, FETCH_WORKERS = 24, 8, 1.0, 10   # book reads per 2s cycle; new markets quoted per cycle; $ kept back


def snap_up(cents: float, ranges=None) -> float:
    s = step_at(cents, ranges)
    return round(math.ceil(cents / s - 1e-9) * s, 3)


def compute_targets(bids, asks, fair_c, ranges) -> dict:
    """
    bids desc / asks asc: [(cents, qty)]. Quote ONE TICK WIDE around fair: bid = fair floored to the
    grid, ask = bid + 1 tick (so bid <= fair < ask: both sides have non-negative edge and both filled
    locks 1 tick). Never marketable: if the book sits away from fair, a side is pushed out to the
    nearest non-crossing price (so the quote can be wider than one tick in that case).
    """
    bb = bids[0][0] if bids else None
    ba = asks[0][0] if asks else None
    if bb is None or ba is None:
        return {'bid': None, 'ask': None, 'bid_top': bb, 'ask_top': ba, 'tick': step_at(50, ranges)}
    tick = step_at(ba - 1e-6, ranges)
    ref  = fair_c if fair_c is not None else (bb + ba) / 2
    bid  = snap_down(ref, ranges)
    ask  = round(bid + step_at(bid, ranges), 3)
    bid  = min(bid, snap_down(ba - tick, ranges))
    ask  = max(ask, snap_up(bb + tick, ranges))
    return {'bid': round(bid, 3) if bid > 0 else None, 'ask': round(ask, 3) if ask < 100 else None,
            'bid_top': bb, 'ask_top': ba, 'tick': tick}


def _get(path, params=None):
    return requests.get(f'{BASE_URL}{path}', headers=kalshi_headers('GET', '/trade-api/v2' + path),
                        params=params, timeout=8)


def fetch_book(ticker: str) -> dict:
    """Full ladder in cents: bids desc, asks asc (YES asks derived from NO bids)."""
    r = _get(f'/markets/{ticker}/orderbook', {'depth': BOOK_DEPTH})
    r.raise_for_status()
    ob = r.json().get('orderbook_fp') or {}
    yes = [(round(float(p) * 100, 3), float(q)) for p, q in (ob.get('yes_dollars') or [])]
    no  = [(round(float(p) * 100, 3), float(q)) for p, q in (ob.get('no_dollars') or [])]
    return {'bids': sorted(yes, key=lambda t: -t[0]),
            'asks': sorted(((round(100 - p, 3), q) for p, q in no), key=lambda t: t[0])}


# ── Screening ────────────────────────────────────────────────────────────────

def zero_fee_sports(candidates=None) -> list:
    """Configured sports whose Kalshi series charge NO maker fee (optionally intersected with `candidates`)."""
    out = []
    for k, v in config.SPORTS_CONFIG.items():
        if candidates is not None and k not in candidates:
            continue
        if fee_rate_for(v['ticker'], maker=True) == 0:
            out.append(k)
    return out


def screen_candidates(sports: list, hrs: int = 18, top=None) -> pd.DataFrame:
    """Rank zero-fee, Pinnacle-matched markets for market making. One market (best open interest) per game."""
    pin = pinnacle_odds(sports, hrs=hrs, live=False)
    m = kalshi_odds(pin, threshold=0.85)
    if m.empty:
        return pd.DataFrame()
    m = m[(~m['is_draw']) & m['yes_bid'].notna() & m['yes_ask'].notna()].copy()
    for c in ('volume', 'OI'):
        m[c] = pd.to_numeric(m[c], errors='coerce').fillna(0)
    m['spread_c'] = ((m['yes_ask'] - m['yes_bid']) * 100).round(2)
    m['mid']      = (m['yes_ask'] + m['yes_bid']) / 2
    now = pd.Timestamp.now(tz='UTC')
    m['hrs_to_start'] = (m['commence'].dt.tz_convert('UTC') - now).dt.total_seconds() / 3600
    m = m[m['hrs_to_start'] > 0.5]
    m['score'] = (m['OI'].apply(math.log1p) + 0.5 * m['volume'].apply(math.log1p)
                  - 0.4 * (m['spread_c'] - 3).abs() - 3 * (m['mid'] - 0.5).abs())
    m = m.sort_values('score', ascending=False).drop_duplicates('event_id')
    cols = ['k_ticker', 'sport', 'home', 'away', 'outcome', 'commence', 'fair_prob', 'yes_bid', 'yes_ask',
            'spread_c', 'volume', 'OI', 'hrs_to_start', 'score', 'k_event_ticker']
    m = m[cols] if top is None else m[cols].head(top)
    return m.reset_index(drop=True)


def spec_from_row(r) -> dict:
    return {'ticker': r['k_ticker'], 'event_ticker': r['k_event_ticker'], 'sport': r['sport'],
            'title': f"{r['away']} @ {r['home']}", 'outcome': r['outcome'],
            'commence': pd.Timestamp(r['commence']).tz_convert('UTC').isoformat(),
            'fair_c': round(float(r['fair_prob']) * 100, 2)}


# ── Engine ───────────────────────────────────────────────────────────────────

def _new_state(spec):
    return {'spec': spec, 'paused': False, 'size': DEFAULT_SIZE, 'manual': {'bid': None, 'ask': None},
            'force': {'bid': False, 'ask': False}, 'ranges': None, 'book': {'bids': [], 'asks': []}, 'last_c': None, 'volume': None,
            'fair_c': spec.get('fair_c'), 'fair_at': time.time(), 'target': {}, 'orders': {'bid': None, 'ask': None},
            'fills': [], 'inv': 0.0, 'cycles': 0, 'cycles_two_sided': 0, 'api_err': 0, 'requotes': 0,
            'off_top': [], 'note': '', 'meta_at': 0}


class MMEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.mkts: dict = {}
        self.params = {'live': False,
                       'fair_refresh_sec': 30, 'poll_sec': 2.0}
        self._stop = threading.Event()
        self._wake = threading.Event()      # set to cut the loop's sleep short (e.g. quoting just switched ON)
        self._thread = None
        self.last_ui = time.time()
        self.focus = None            # expanded market on the board: polled every cycle, full book sent
        self.ack = None              # last board-action nonce processed (board queues actions until acked)
        self._cash, self._cash_at, self._rr = None, 0.0, 0
        self.screen_key, self.screen_at, self.screen_state = None, 0.0, 'idle'
        self._load_selection()

    # -- config -------------------------------------------------------------
    def _load_selection(self):
        try:
            with open(SELECTION_PATH) as f:
                self.set_markets(json.load(f), persist=False)
        except (OSError, ValueError):
            pass

    def set_markets(self, specs: list, persist=True):
        """Replace the market list (order = funding priority). Markets that currently have resting quotes are kept."""
        with self.lock:
            keep = {sp['ticker'] for sp in specs}
            new: dict = {}
            for sp in specs:
                st = self.mkts.get(sp['ticker'])
                if st is None:
                    st = _new_state(sp)
                    st['size'] = max(1, min(MAX_SIZE, int(sp.get('size', DEFAULT_SIZE))))
                    self._seed_from_ledger(st)
                else:
                    st['spec'] = {**st['spec'], **sp}
                new[sp['ticker']] = st
            for t, st in self.mkts.items():
                if t in keep:
                    continue
                if st['orders']['bid'] or st['orders']['ask']:
                    new[t] = st                     # still quoting: leave it until it stops on its own
                else:
                    self._cancel_all(t)
            self.mkts = new
        if persist:
            self._persist()

    @staticmethod
    def _seed_from_ledger(m):
        """Carry a market's earlier fills (inventory, average prices, edge at fill) into this session."""
        try:
            f = fills_for_ticker(m['spec']['ticker'])
        except Exception:
            log.exception('mm: could not read ledger for %s', m['spec']['ticker'])
            return
        m['fills'], m['inv'] = [], 0.0
        for _, r in f.iterrows():
            side, price, qty = r['side'], float(r['price_c']), float(r['qty'])
            fair = None if pd.isna(r['fair_c']) or r['fair_c'] == '' else float(r['fair_c'])
            m['fills'].append({'side': side, 'price_c': price, 'qty': qty, 'ts': 0,
                               'edge_c': None if fair is None else round((fair - price) if side == 'bid' else (price - fair), 3)})
            m['inv'] = round(m['inv'] + (qty if side == 'bid' else -qty), 4)

    def reconcile_from_kalshi(self, max_pages: int = 5) -> int:
        """
        Add fills that happened while we weren't watching (a GTC quote can fill after the app closed): compare each
        tagged (mm-) order's filled quantity on Kalshi with what the ledger already holds, append only the difference,
        then re-seed every market. Tagged orders only — raw positions would mix in the K/P bot's fills.
        """
        added, cursor, have = 0, None, per_order_totals()
        try:
            for _ in range(max_pages):
                params = {'limit': 200, **({'cursor': cursor} if cursor else {})}
                r = _get('/portfolio/orders', params)
                r.raise_for_status()
                body = r.json()
                for o in body.get('orders', []):
                    if not str(o.get('client_order_id', '')).startswith(MM_PREFIX):
                        continue
                    filled = float(o.get('fill_count_fp') or 0)
                    missing = round(filled - have.get(str(o.get('order_id')), 0.0), 4)
                    if missing > 0.005:
                        side = 'ask' if (o.get('book_side') == 'ask' or o.get('outcome_side') == 'no') else 'bid'
                        log_fill(o['ticker'], side, round(float(o['yes_price_dollars']) * 100, 3), missing, None, o['order_id'])
                        added += 1
                cursor = body.get('cursor')
                if not cursor:
                    break
        except Exception:
            log.exception('mm: Kalshi reconcile failed')
        with self.lock:
            for m in self.mkts.values():
                if not (m['orders']['bid'] or m['orders']['ask']):
                    self._seed_from_ledger(m)
        if added:
            log.info('mm: reconcile added %d fill(s) missing from the ledger', added)
        return added

    def _persist(self):
        with self.lock:
            specs = [{**m['spec'], 'size': m['size']} for m in self.mkts.values()]
        os.makedirs(os.path.dirname(SELECTION_PATH), exist_ok=True)
        with open(SELECTION_PATH, 'w') as f:
            json.dump(specs, f)

    def set_manual_quote(self, ticker: str, side: str, price_c: float):
        """Pin one side's resting price (dragged on the board). Stays until cleared; clamped so it never crosses."""
        with self.lock:
            m = self.mkts.get(ticker)
            if m and side in ('bid', 'ask'):
                m['manual'][side] = float(price_c)
                m['force'][side] = True

    def clear_manual_quote(self, ticker: str, side: str):
        with self.lock:
            m = self.mkts.get(ticker)
            if m and side in ('bid', 'ask'):
                m['manual'][side] = None
                m['force'][side] = True

    def pause_market(self, ticker: str):
        """Per-market Cancel: pull this market's quotes and keep it out until resumed (or quoting is switched on again)."""
        with self.lock:
            m = self.mkts.get(ticker)
            if m:
                m['paused'] = True
                self._cancel_all(ticker)

    def resume_market(self, ticker: str):
        with self.lock:
            m = self.mkts.get(ticker)
            if m:
                m['paused'] = False

    def set_focus(self, ticker):
        with self.lock:
            self.focus = ticker

    def set_all_sizes(self, size: int):
        """Master size: contracts offered on each side of EVERY market (per-market boxes can still be edited after)."""
        size = max(1, min(MAX_SIZE, int(size)))
        with self.lock:
            for m in self.mkts.values():
                m['size'] = size
        self._persist()

    def snapshot_tickers(self):
        with self.lock:
            return list(self.mkts)

    def apply_size_to_new(self, size: int, seen: set):
        """Give the master size to markets this session hasn't applied it to yet (so the box never disagrees with the cards)."""
        size = max(1, min(MAX_SIZE, int(size)))
        with self.lock:
            fresh = [m for t, m in self.mkts.items() if t not in seen]
            for m in fresh:
                m['size'] = size
                seen.add(m['spec']['ticker'])
        if fresh:
            self._persist()

    def set_market_size(self, ticker: str, size: int):
        """Contracts offered on EACH side of this market (per-market)."""
        with self.lock:
            m = self.mkts.get(ticker)
            if m:
                m['size'] = max(1, min(MAX_SIZE, int(size)))
        self._persist()

    def screen_async(self, sports: list, hrs: int):
        """Load every zero-fee Pinnacle-matched market in the background so the page never blocks on it."""
        if self.screen_state == 'loading':
            return
        self.screen_state, self.screen_key, self.screen_at = 'loading', hrs, time.time()

        def _run():
            try:
                cands = screen_candidates(sports, hrs=hrs)
                self.set_markets([spec_from_row(r) for _, r in cands.iterrows()])
                self.screen_state = 'ok'
            except Exception as exc:
                log.exception('mm: market screen failed')
                self.screen_state = f'error: {exc}'
            self.screen_at = time.time()

        threading.Thread(target=_run, name='mm-screen', daemon=True).start()

    def set_params(self, **kw):
        with self.lock:
            was_live = self.params['live']
            self.params.update(kw)
            if self.params['live'] and not was_live:
                for st in self.mkts.values():
                    st['paused'] = False
            if self.params['live'] != was_live:
                self._wake.set()
            if was_live and not self.params['live']:
                for t in list(self.mkts):
                    self._cancel_all(t)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.cancel_orphans()          # GTC quotes from a previous run/crash would otherwise sit unmanaged
        threading.Thread(target=self.reconcile_from_kalshi, name='mm-reconcile', daemon=True).start()
        self._thread = threading.Thread(target=self._loop, name='mm-engine', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self.lock:
            for t in list(self.mkts):
                self._cancel_all(t)

    def kill(self):
        """Panic: stop quoting and cancel every MM order — tracked ones and any orphans from an earlier run."""
        self.set_params(live=False)
        self.cancel_orphans()

    def cancel_orphans(self) -> int:
        """Cancel every resting order tagged as ours (client_order_id 'mm-*'), tracked or not."""
        n = 0
        try:
            for o in list_resting_orders():
                if str(o.get('client_order_id', '')).startswith(MM_PREFIX):
                    if ensure_canceled(o.get('ticker'), o.get('order_id')):
                        n += 1
        except Exception:
            log.exception('mm: orphan cancel failed')
        return n

    # -- order helpers (all under self.lock) --------------------------------
    def _cancel_all(self, ticker):
        m = self.mkts.get(ticker)
        if not m:
            return
        for side in ('bid', 'ask'):
            q = m['orders'][side]
            if q:
                self._cancel_quote(m, side)

    def _account_fills(self, m, side, q, filled_now):
        d = round(filled_now - q['filled'], 4)
        if d > 0:
            q['filled'] = filled_now
            fair = m['fair_c']
            m['fills'].append({'side': side, 'price_c': q['price_c'], 'qty': d, 'ts': time.time(),
                               'edge_c': None if fair is None else round((fair - q['price_c']) if side == 'bid'
                                                                         else (q['price_c'] - fair), 3)})
            m['inv'] = round(m['inv'] + (d if side == 'bid' else -d), 4)
            try:
                log_fill(m['spec']['ticker'], side, q['price_c'], d, fair, q['order_id'])
            except Exception:
                log.exception('mm: could not write fill to ledger')

    def _cancel_quote(self, m, side) -> bool:
        """Cancel and CONFIRM (ensure_canceled polls + retries) before anything new is placed. False = still open."""
        q = m['orders'][side]
        try:
            closed = ensure_canceled(m['spec']['ticker'], q['order_id'])
            st = get_order_status(q['order_id'])
            fp = st.get('fill_count_fp')
            if fp is not None:
                self._account_fills(m, side, q, float(fp))
        except Exception:
            log.exception('mm: cancel failed for %s', q.get('order_id'))
            m['api_err'] += 1
            return False
        if not closed:
            m['api_err'] += 1
            return False
        if self._cash is not None:                       # cash frees up now, not at the next balance resync
            rem = max(0.0, q['size'] - q['filled'])
            self._cash += (q['price_c'] if side == 'bid' else 100 - q['price_c']) / 100 * rem
        m['orders'][side] = None
        return True

    def _place_quote(self, m, side, price_c):
        t, size = m['spec']['ticker'], m['size']
        cost = (price_c if side == 'bid' else 100 - price_c) / 100 * size
        if self._cash is not None and cost > self._cash - CASH_RESERVE:
            m['note'] = 'not quoted: insufficient balance'
            return
        cid = MM_PREFIX + str(uuid.uuid4())
        try:
            if side == 'bid':
                o = place_order(t, price_c, size, side='yes', post_only=True, client_order_id=cid)
            else:
                o = place_order(t, round(100 - price_c, 3), size, side='no', post_only=True, client_order_id=cid)
            m['orders'][side] = {'order_id': o.get('order_id'), 'price_c': price_c, 'size': size,
                                 'filled': 0.0, 'placed': time.time()}
            m['requotes'] += 1
            if self._cash is not None:
                self._cash -= cost
        except Exception:
            log.exception('mm: place failed %s %s @ %s', t, side, price_c)
            m['api_err'] += 1

    def _manage_live(self, m, resting_ids, minutes_to_start):
        for side in ('bid', 'ask'):
            q = m['orders'][side]
            if q and q['order_id'] not in resting_ids:           # filled / expired / canceled elsewhere
                try:
                    st = get_order_status(q['order_id'])
                    fp = st.get('fill_count_fp')
                    if fp is not None:
                        self._account_fills(m, side, q, float(fp))
                except Exception:
                    m['api_err'] += 1
                m['orders'][side] = None
                q = None
            elif q:                                              # still resting: trust Kalshi's size and price over our memory
                k = resting_ids[q['order_id']]
                try:
                    ks, kp = float(k.get('initial_count_fp')), float(k.get('yes_price_dollars')) * 100
                    if abs(ks - q['size']) > 0.01 or abs(kp - q['price_c']) > 0.01:
                        log.warning('mm: %s %s resting on Kalshi as %s @ %s, we thought %s @ %s — resyncing',
                                    m['spec']['ticker'], side, ks, kp, q['size'], q['price_c'])
                        q['size'], q['price_c'] = ks, kp
                except (TypeError, ValueError):
                    pass
            target = None if m['paused'] else m['target'].get(side)
            if minutes_to_start * 60 <= PRE_EVENT_BUFFER or target is None:
                if q:
                    self._cancel_quote(m, side)
                continue
            if q and abs(q['price_c'] - target) < 1e-6 and q['size'] == m['size']:
                continue
            if q and time.time() - q['placed'] < 5 and not m['force'][side]:   # don't churn faster than every 5s
                continue
            m['force'][side] = False
            if q and not self._cancel_quote(m, side):
                continue                                          # old order still live: never stack a second one
            self._place_quote(m, side, target)

    @staticmethod
    def _apply_manual(m, tg):
        """Override auto targets with dragged prices: snap to grid, never marketable, never self-crossing."""
        bb, ba, tick, ranges = tg.get('bid_top'), tg.get('ask_top'), tg.get('tick') or 1.0, m['ranges']
        if bb is None or ba is None:
            return
        man = m['manual']
        if man['bid'] is not None:
            tg['bid'] = max(tick, min(snap_down(man['bid'], ranges), snap_down(ba - tick, ranges)))
        if man['ask'] is not None:
            tg['ask'] = min(100 - tick, max(snap_up(man['ask'], ranges), snap_up(bb + tick, ranges)))
        if tg['bid'] is not None and tg['ask'] is not None and tg['bid'] >= tg['ask']:
            if man['ask'] is None:
                tg['ask'] = round(tg['bid'] + tick, 3)
            else:
                tg['bid'] = round(tg['ask'] - tick, 3)

    # -- background loop ----------------------------------------------------
    def _refresh_meta(self, m):
        r = _get(f"/markets/{m['spec']['ticker']}")
        if r.ok:
            mk = r.json().get('market', {})
            m['ranges'] = parse_ranges(mk.get('price_ranges'))
            lp = mk.get('last_price_dollars')
            m['last_c'] = round(float(lp) * 100, 3) if lp and float(lp) > 0 else None
            m['volume'] = float(mk.get('volume_fp') or 0)
        m['meta_at'] = time.time()

    def _refresh_fair(self):
        by_sport = {}
        for m in self.mkts.values():
            if m['orders']['bid'] or m['orders']['ask']:       # credits only for sports we're actually quoting
                by_sport.setdefault(m['spec']['sport'], []).append(m)
        for sport, ms in by_sport.items():
            if time.time() - min(x['fair_at'] for x in ms) < self.params['fair_refresh_sec']:
                continue
            try:
                pin = pinnacle_odds([sport], hrs=48, live=False)
            except Exception:
                log.info('mm: no Pinnacle refresh for %s', sport)
                for x in ms:
                    x['fair_at'] = time.time()
                continue
            pin = pin.assign(start=pin['commence'].dt.tz_convert('UTC'))
            for x in ms:
                sp = x['spec']
                hit = pin[(pin['outcome'] == sp['outcome']) &
                          ((pin['start'] - pd.Timestamp(sp['commence'])).abs() < pd.Timedelta(seconds=90))]
                if not hit.empty:
                    x['fair_c'] = round(float(hit.iloc[0]['fair_prob']) * 100, 2)
                x['fair_at'] = time.time()

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                with self.lock:
                    if self.params['live'] and time.time() - self.last_ui > UI_DEADMAN_SEC:
                        # dead-man switch: nobody is watching the board (browser closed / app stalled)
                        log.warning('mm: no UI heartbeat for %ss — quoting off, cancelling', UI_DEADMAN_SEC)
                        self.set_params(live=False)
                    live = self.params['live']
                    tickers = list(self.mkts)
                resting_ids = {}
                if time.time() - self._cash_at > 20:                  # available cash, refreshed whether or not we're quoting
                    try:
                        self._cash, self._cash_at = float(get_balance()), time.time()
                    except Exception:
                        log.warning('mm: balance refresh failed')
                if live:
                    try:
                        resting_ids = {o['order_id']: o for o in list_resting_orders()}
                    except Exception:
                        log.exception('mm: resting-orders refresh failed'); resting_ids = None
                    self._refresh_fair()      # Pinnacle credits are only spent for markets we're quoting
                # Book reads: quoted + focused markets every cycle; everything else round-robin within the budget.
                active = [t for t in tickers if t in self.mkts and (self.mkts[t]['orders']['bid'] or self.mkts[t]['orders']['ask']
                                                                    or t == self.focus)]
                others = [t for t in tickers if t not in active]
                room = max(4, POLL_BUDGET - len(active))
                if others:
                    start = self._rr % len(others)
                    picked = (others[start:] + others[:start])[:room]
                    self._rr = (start + room) % max(1, len(others))
                else:
                    picked = []
                new_started = 0

                def _read(t):
                    m = self.mkts.get(t)
                    if not m:
                        return t, None, None
                    try:
                        if time.time() - m['meta_at'] > 15 or m['ranges'] is None:
                            self._refresh_meta(m)
                        return t, fetch_book(t), None
                    except Exception as exc:
                        return t, None, exc

                # Book reads run in parallel: done one-by-one, a slow API stretched a cycle to tens of seconds
                # and quoting (or switching ON) appeared to do nothing.
                with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
                    reads = list(pool.map(_read, active + picked))
                for t, book, err in reads:
                    m = self.mkts.get(t)
                    if not m:
                        continue
                    if err is not None or book is None:
                        m['api_err'] += 1; m['note'] = 'data error'
                        continue
                    with self.lock:
                        m['book'] = book
                        tg = compute_targets(book['bids'], book['asks'], m['fair_c'], m['ranges'])
                        self._apply_manual(m, tg)
                        m['target'] = tg
                        m['cycles'] += 1
                        if tg['bid'] is not None and tg['ask'] is not None:
                            m['cycles_two_sided'] += 1
                        mins = (pd.Timestamp(m['spec']['commence']) - pd.Timestamp.now(tz='UTC')).total_seconds() / 60
                        m['note'] = 'quoting stopped: event starting' if mins * 60 <= PRE_EVENT_BUFFER else ''
                        if self.params['live'] and live and resting_ids is not None:   # re-check: may have been switched off mid-cycle
                            had = bool(m['orders']['bid'] or m['orders']['ask'])
                            if not had and not m['paused']:
                                if new_started >= MAX_NEW_MKTS_PER_CYCLE:
                                    continue                                          # pace order writes
                                new_started += 1
                            self._manage_live(m, resting_ids, mins)
                        elif not self.params['live'] and (m['orders']['bid'] or m['orders']['ask']):
                            self._cancel_all(t)                                         # sweep anything left resting while off
            except Exception:
                log.exception('mm: loop error')
            self._wake.wait(max(0.2, self.params['poll_sec'] - (time.time() - t0)))
            self._wake.clear()

    # -- snapshot for the UI ------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        self.last_ui = now
        live_now = self.params['live']
        with self.lock:
            out = []
            for t, m in self.mkts.items():
                sp, tg, bk = m['spec'], m['target'], m['book']
                tick = tg.get('tick') or 1.0
                bids, asks = bk['bids'], bk['asks']
                fills = m['fills']
                bf = [f for f in fills if f['side'] == 'bid']; af = [f for f in fills if f['side'] == 'ask']
                bq, aq = sum(f['qty'] for f in bf), sum(f['qty'] for f in af)
                avg_b = sum(f['price_c'] * f['qty'] for f in bf) / bq if bq else None
                avg_a = sum(f['price_c'] * f['qty'] for f in af) / aq if aq else None
                matched = min(bq, aq)
                capture = round(matched * (avg_a - avg_b) / 100, 4) if matched else 0.0
                fair = m['fair_c']
                inv = m['inv']
                if inv > 0 and avg_b is not None and fair is not None:
                    mtm = round(inv * (fair - avg_b) / 100, 4)
                elif inv < 0 and avg_a is not None and fair is not None:
                    mtm = round(-inv * (avg_a - fair) / 100, 4)
                else:
                    mtm = 0.0
                edges = [f['edge_c'] for f in fills if f['edge_c'] is not None]
                orders = []
                live = self.params['live']
                qb, qa = m['orders']['bid'], m['orders']['ask']
                for side in ('bid', 'ask'):
                    q = m['orders'][side]
                    if q:
                        orders.append({'side': side, 'price_c': q['price_c'], 'size': q['size'],
                                       'filled': q['filled'], 'status': 'live',
                                       'manual': m['manual'][side] is not None})
                top_b = tg.get('bid_top'); top_a = tg.get('ask_top')
                mids = (bids[0][0] + asks[0][0]) / 2 if bids and asks else None
                out.append({
                    'ticker': t, 'title': sp['title'], 'outcome': sp['outcome'], 'sport': sp['sport'],
                    'start_iso': sp['commence'],
                    'mins_to_start': round((pd.Timestamp(sp['commence']) - pd.Timestamp.now(tz='UTC')).total_seconds() / 60, 1),
                    'fair_c': fair, 'fair_age_s': round(now - m['fair_at']), 'size': m['size'],
                    'bids': bids if t == self.focus else bids[:9], 'asks': asks if t == self.focus else asks[:9], 'mid_c': mids, 'last_c': m['last_c'], 'volume': m['volume'],
                    'tick_c': tick, 'orders': orders,
                    'note': m['note'], 'paused': m['paused'],
                    'status': ('paused' if m['paused'] else 'quoting' if orders else ('waiting' if live_now else 'off')),
                    'stats': {
                        'spread_c': round(asks[0][0] - bids[0][0], 3) if bids and asks else None,
                        'quote_spread_c': (round(qa['price_c'] - qb['price_c'], 3) if qa and qb else None),
                        'bid_off_top_ticks': (round((top_b - qb['price_c']) / tick, 1) if qb and top_b is not None else None),
                        'ask_off_top_ticks': (round((qa['price_c'] - top_a) / tick, 1) if qa and top_a is not None else None),
                        'mid_minus_fair_c': round(mids - fair, 3) if mids is not None and fair is not None else None,
                        'depth_bid_5c': round(sum(q for p, q in bids if bids and p >= bids[0][0] - 5), 1) if bids else 0,
                        'depth_ask_5c': round(sum(q for p, q in asks if asks and p <= asks[0][0] + 5), 1) if asks else 0,
                        'two_sided_pct': round(100 * m['cycles_two_sided'] / m['cycles']) if m['cycles'] else 0,
                        'fills_bid': len(bf), 'fills_ask': len(af), 'matched': round(matched, 2),
                        'avg_bid_fill_c': round(avg_b, 3) if avg_b is not None else None, 'bid_qty': round(bq, 2),
                        'avg_ask_fill_c': round(avg_a, 3) if avg_a is not None else None, 'ask_qty': round(aq, 2),
                        'capture_usd': capture, 'inventory': round(inv, 2), 'mtm_usd': mtm,
                        'avg_edge_at_fill_c': round(sum(edges) / len(edges), 3) if edges else None,
                        'requotes': m['requotes'], 'api_err': m['api_err'],
                    },
                })
            return {'ts': now, 'live': self.params['live'], 'params': dict(self.params), 'markets': out,
                    'ack': self.ack, 'cash': self._cash}
