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
from KALSHI.k_helpers import kalshi_headers, fee_rate_for, kalshi_fee_dollars, kalshi_odds, BASE_URL
import theODDS.p_helpers as _pin
from theODDS.p_helpers import pinnacle_odds
from trade.core.execution import (place_order, cancel_order, ensure_canceled, get_order_status,
                                  list_resting_orders, get_balance, PRE_EVENT_BUFFER)
from trade.core.pricing import parse_ranges, step_at, snap_down
from trade.mm.ledger import log_fill, log_markout, fills_for_ticker, per_order_totals
from applog import get_logger

log = get_logger(__name__)

SELECTION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'logs', 'mm_selection.json')
ENGINE_VERSION = 18    # bump when MMEngine's state/attributes change: the UI then swaps out a stale cached engine
MAX_SIZE, DEFAULT_SIZE, BOOK_DEPTH, UI_DEADMAN_SEC, MM_PREFIX = 25, 5, 15, 90, 'mm-'
MAX_INV_LIMIT, DEFAULT_MAX_INV = 500, 20      # per-market cap on net contracts held (either direction)
OFFLOAD_MIN_NET_C = 0.5                        # only take profit when it nets at least this per contract AFTER the taker fee
OFFLOAD_MIN_USD = 0.02
OFFLOAD_EVERY_SEC = 10                         # per market
MARKOUT_SECS = (60, 300)
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


def fetch_book(ticker: str, full: bool = False) -> dict:
    """Ladder in cents: bids desc, asks asc (YES asks derived from NO bids). `full` reads every level (depth 0) — the
    expanded card shows the whole 1-99c book; everything else needs only the top BOOK_DEPTH."""
    r = _get(f'/markets/{ticker}/orderbook', {'depth': 0 if full else BOOK_DEPTH})
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
    return {'max_inv': DEFAULT_MAX_INV, 'spec': spec, 'paused': False, 'on': False, 'side_add': {'bid': False, 'ask': False},
            'side_off': {'bid': False, 'ask': False}, 'size': DEFAULT_SIZE, 'manual': {'bid': None, 'ask': None},
            'force': {'bid': False, 'ask': False}, 'ranges': None, 'book': {'bids': [], 'asks': []}, 'last_c': None, 'volume': None,
            'fair_c': spec.get('fair_c'), 'fair_at': time.time(), 'target': {}, 'orders': {'bid': None, 'ask': None},
            'fills': [], 'inv': 0.0, 'cycles': 0, 'cycles_two_sided': 0, 'api_err': 0, 'requotes': 0,
            'off_top': [], 'note': '', 'meta_at': 0}


class MMEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.mkts: dict = {}
        self._cancel_busy = 0
        self._reconciled_at = time.time()
        self._purging = set()      # markets with a purge in flight (a second click must not send a second order)
        self._httpd, self.action_port, self._token = None, None, uuid.uuid4().hex
        self._act_lock, self._act_seen = threading.Lock(), []
        self.screen_failed = []
        self.events, self._ev_id, self._markouts, self._last_offload = [], 0, [], {}
        self._sweeping = set()
        self.params = {'live': False,
                       'fair_refresh_sec': 30, 'auto_offload': True, 'poll_sec': 2.0}
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
                    st['max_inv'] = max(1, min(MAX_INV_LIMIT, int(sp.get('max_inv', DEFAULT_MAX_INV))))
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
        with self.lock:
            tracked = {q['order_id'] for m in self.mkts.values() for q in m['orders'].values() if q}
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
                        if o.get('order_id') in tracked:
                            continue                                  # still being accounted for live
                        try:
                            if (pd.Timestamp.now(tz='UTC') - pd.Timestamp(o.get('created_time'))).total_seconds() < 90:
                                continue                              # in flight: the live path will log it
                        except Exception:
                            pass
                        side = 'ask' if (o.get('book_side') == 'ask' or o.get('outcome_side') == 'no') else 'bid'
                        price, liq, fee = round(float(o['yes_price_dollars']) * 100, 3), 'maker', 0.0
                        tc = float(o.get('taker_fill_cost_dollars') or 0)
                        if tc > 0 and filled > 0:                    # an offload / purge: record the real average price and fee
                            avg_c = tc / filled * 100
                            price = round(avg_c if side == 'bid' else 100 - avg_c, 3)
                            liq, fee = 'taker', float(o.get('taker_fees_dollars') or 0)
                        log_fill(o['ticker'], side, price, missing, None, o['order_id'], liq=liq, fee_usd=fee)
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
            specs = [{**m['spec'], 'size': m['size'], 'max_inv': m['max_inv']} for m in self.mkts.values()]
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
                self._clear_flags(m)
        if m:
            self._cancel_bg(tickers={ticker})

    def resume_market(self, ticker: str):
        """Per-market ON: quote this market only, whether or not the global ON is pressed."""
        with self.lock:
            m = self.mkts.get(ticker)
            if m:
                m['paused'], m['on'] = False, True
                m['side_off'] = {'bid': False, 'ask': False}
                self._wake.set()

    @staticmethod
    def _clear_flags(m):
        m['on'] = False
        m['side_add'] = {'bid': False, 'ask': False}
        m['side_off'] = {'bid': False, 'ask': False}

    @staticmethod
    def _running(m, live):
        """Is anything being quoted on this market: the global ON (and not cancelled), its own ON, or an added side."""
        return (live and not m['paused']) or m['on'] or any(m['side_add'].values())

    @staticmethod
    def _side_enabled(m, side, live):
        return ((live and not m['paused']) or m['on']) and not m['side_off'][side] or m['side_add'][side]

    def add_side(self, ticker: str, side: str):
        """Per-side ADD: quote just this side of this market (or bring back a side that was pulled while the market is on)."""
        with self.lock:
            m = self.mkts.get(ticker)
            if m and side in ('bid', 'ask'):
                if (self.params['live'] and not m['paused']) or m['on']:
                    m['side_off'][side] = False
                else:
                    m['side_add'][side] = True
                self._wake.set()

    def pull_side(self, ticker: str, side: str):
        """Per-side PULL: cancel this side's quote and keep it off until added again."""
        with self.lock:
            m = self.mkts.get(ticker)
            if not (m and side in ('bid', 'ask')):
                return
            m['side_add'][side] = False
            if (self.params['live'] and not m['paused']) or m['on']:
                m['side_off'][side] = True
        self._cancel_bg(tickers={ticker}, sides=(side,))

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

    def apply_size_to_new(self, size: int, seen: set, max_inv=None):
        """Give the master size / max inventory to markets this session hasn't applied them to yet (so the boxes never
        disagree with the cards)."""
        size = max(1, min(MAX_SIZE, int(size)))
        with self.lock:
            fresh = [m for t, m in self.mkts.items() if t not in seen]
            for m in fresh:
                m['size'] = size
                if max_inv is not None:
                    m['max_inv'] = max(1, min(MAX_INV_LIMIT, int(max_inv)))
                seen.add(m['spec']['ticker'])
        if fresh:
            self._persist()

    def set_all_max_inv(self, n: int):
        n = max(1, min(MAX_INV_LIMIT, int(n)))
        with self.lock:
            for m in self.mkts.values():
                m['max_inv'] = n
        self._persist()

    def set_market_max_inv(self, ticker: str, n: int):
        with self.lock:
            m = self.mkts.get(ticker)
            if m:
                m['max_inv'] = max(1, min(MAX_INV_LIMIT, int(n)))
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
                self.screen_failed = list(_pin._last_failed)
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
                    st['side_off'] = {'bid': False, 'ask': False}
            if self.params['live'] != was_live:
                self._wake.set()
        if was_live and not self.params['live']:
            with self.lock:
                for x in self.mkts.values():
                    self._clear_flags(x)
            self._cancel_bg()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._start_action_server()
        self.cancel_orphans()          # GTC quotes from a previous run/crash would otherwise sit unmanaged
        threading.Thread(target=self.reconcile_from_kalshi, name='mm-reconcile', daemon=True).start()
        self._thread = threading.Thread(target=self._loop, name='mm-engine', daemon=True)
        self._thread.start()

    # -- browser <-> engine side channel -------------------------------------
    def handle_action(self, act: dict):
        """Apply one board action (size, max inventory, drag, add/pull, ON/Cancel, focus). De-duplicated by nonce, because
        an action can arrive both over the direct HTTP channel and, as a fallback, through Streamlit."""
        nonce = act.get('nonce')
        with self._act_lock:
            if nonce is not None:
                if nonce in self._act_seen:
                    return
                self._act_seen.append(nonce)
                del self._act_seen[:-300]
        a, t = act.get('action'), act.get('ticker')
        try:
            if a == 'set_quote':
                self.set_manual_quote(t, act['side'], float(act['price_c']))
            elif a == 'clear_quote':
                self.clear_manual_quote(t, act['side'])
            elif a == 'set_size':
                self.set_market_size(t, int(act['size']))
            elif a == 'set_max_inv':
                self.set_market_max_inv(t, int(act['max_inv']))
            elif a == 'cancel_market':
                self.pause_market(t)
            elif a == 'resume_market':
                self.resume_market(t)
            elif a == 'add_side':
                self.add_side(t, act['side'])
            elif a == 'pull_side':
                self.pull_side(t, act['side'])
            elif a == 'focus':
                self.set_focus(t)
            elif a == 'purge':
                self.purge({t})
        except Exception:
            log.exception('mm: action %s failed', a)

    def _start_action_server(self):
        """Loopback-only HTTP channel so the board can send actions and pull fresh snapshots WITHOUT a Streamlit rerun
        (editing a card no longer makes the page reload). Token-gated; the board falls back to Streamlit if unreachable."""
        if self._httpd:
            return
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        eng = self

        def clean(o):
            if isinstance(o, float):
                return o if math.isfinite(o) else None
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [clean(v) for v in o]
            return o

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b'{}'):
                self.send_response(code)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Headers', 'content-type')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204, b'')

            def do_GET(self):
                if self.path.split('?token=')[-1] != eng._token:
                    return self._send(403)
                self._send(200, json.dumps(clean(eng.snapshot()), default=str).encode())

            def do_POST(self):
                try:
                    act = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0) or b'{}')
                except ValueError:
                    return self._send(400)
                if act.pop('token', None) != eng._token:
                    return self._send(403)
                threading.Thread(target=eng.handle_action, args=(act,), daemon=True).start()   # never make the browser wait on the engine lock
                self._send(200, b'{"ok":true}')

        try:
            self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
            self._httpd.daemon_threads = True
            self.action_port = self._httpd.server_address[1]
            threading.Thread(target=self._httpd.serve_forever, name='mm-actions', daemon=True).start()
        except OSError:
            log.exception('mm: could not start the action server; the board will use Streamlit for actions')
            self._httpd = None

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
        with self.lock:
            for t in list(self.mkts):
                self._cancel_all(t)

    def kill(self):
        """Panic: stop quoting and cancel every MM order — tracked ones and any orphans from an earlier run."""
        with self.lock:
            self.params['live'] = False
            for x in self.mkts.values():
                self._clear_flags(x)
            self._wake.set()
        self._cancel_bg(orphans=True)

    def _cancel_bg(self, tickers=None, orphans=False, sides=('bid', 'ask')):
        """Cancel quotes in the background, without holding the lock across the network: the UI keeps ticking and each
        market's YOU row disappears the moment Kalshi confirms that order is closed."""
        with self.lock:
            jobs = [(t, s, m['orders'][s]) for t, m in self.mkts.items() if tickers is None or t in tickers
                    for s in sides if m['orders'][s]]
            self._cancel_busy += 1

        def _run():
            try:
                self._fast_cancel(jobs, orphans)
            except Exception:
                log.exception('mm: background cancel failed')
            finally:
                with self.lock:
                    self._cancel_busy -= 1
        threading.Thread(target=_run, name='mm-cancel', daemon=True).start()

    def _fast_cancel(self, jobs, orphans):
        """Cancel many orders quickly: fire every DELETE at once (12 in parallel), confirm ALL of them with a single
        list-resting-orders call instead of polling each order, retry only the stragglers, and settle fill accounting
        afterwards so the board clears first. (The per-order path costs a status GET, a DELETE, a 0.5 s sleep and another
        GET for every order, plus a second, sequential pass for stray orders.)"""
        tracked = {q['order_id']: (t, side, q) for t, side, q in jobs}
        ticker_of = {oid: t for oid, (t, _, _) in tracked.items()}
        if orphans:                                       # strays from an earlier run, cancelled in the same burst
            try:
                for o in list_resting_orders():
                    if str(o.get('client_order_id', '')).startswith(MM_PREFIX) and o['order_id'] not in ticker_of:
                        ticker_of[o['order_id']] = o.get('ticker')
            except Exception:
                log.exception('mm: could not list stray orders')
        if not ticker_of:
            return

        def _burst(ids):
            with ThreadPoolExecutor(max_workers=12) as ex:
                list(ex.map(lambda i: cancel_order(ticker_of[i], i), ids))

        done_acc, pending = [], list(ticker_of)
        for attempt in range(4):
            _burst(pending)
            try:
                resting = {o['order_id'] for o in list_resting_orders()}
            except Exception:
                resting = None                            # can't verify in bulk: fall back to per-order checks below
            if resting is None:
                break
            gone = [i for i in ticker_of if i not in resting and i in pending]
            with self.lock:
                for oid in gone:
                    if oid in tracked:
                        t, side, q = tracked[oid]
                        m = self.mkts.get(t)
                        if m and m['orders'][side] and m['orders'][side]['order_id'] == oid:
                            if self._cash is not None:
                                rem = max(0.0, q['size'] - q['filled'])
                                self._cash += (q['price_c'] if side == 'bid' else 100 - q['price_c']) / 100 * rem
                            m['orders'][side] = None
                            done_acc.append((t, side, q))
            pending = [i for i in pending if i in resting]
            if not pending:
                break
            time.sleep(0.3 * (attempt + 1))
        for oid in pending:                               # stubborn or unverifiable: the careful per-order path
            if oid in tracked:
                self._cancel_one(tracked[oid][0], tracked[oid][1])
            else:
                ensure_canceled(ticker_of[oid], oid)

        def _account(x):                                   # partial fills that landed before the cancel
            t, side, q = x
            try:
                fp = get_order_status(q['order_id']).get('fill_count_fp')
            except Exception:
                return
            with self.lock:
                m = self.mkts.get(t)
                if m and fp is not None:
                    self._account_fills(m, side, q, float(fp))
        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(_account, done_acc))

    def _sweep_orphans(self, resting):
        """Safety net: any of OUR (mm-tagged) resting orders that no market is tracking gets cancelled. Catches an order
        left behind by a lost handle, a stale-list race or an earlier run, so old quotes can never pile up beside new ones."""
        with self.lock:
            known = {q['order_id'] for m in self.mkts.values() for q in m['orders'].values() if q}
        now = pd.Timestamp.now(tz='UTC')
        orphans = []
        for oid, o in resting.items():
            if oid in known or oid in self._sweeping or not str(o.get('client_order_id', '')).startswith(MM_PREFIX):
                continue
            try:
                if (now - pd.Timestamp(o.get('created_time'))).total_seconds() < 15:
                    continue                                   # too fresh to judge: may be mid-placement
            except Exception:
                pass
            orphans.append((o.get('ticker'), oid))
        if not orphans:
            return
        self._sweeping.update(oid for _, oid in orphans)
        log.warning('mm: cancelling %d untracked resting order(s): %s', len(orphans), [t for t, _ in orphans])

        def _run():
            try:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(lambda x: ensure_canceled(*x), orphans))
            except Exception:
                log.exception('mm: orphan sweep failed')
            finally:
                self._sweeping.difference_update(oid for _, oid in orphans)
        threading.Thread(target=_run, name='mm-sweep', daemon=True).start()

    def _cancel_one(self, ticker, side):
        with self.lock:
            m = self.mkts.get(ticker)
            q = m['orders'][side] if m else None
            if not q:
                return
            oid = q['order_id']
        try:
            closed = ensure_canceled(ticker, oid)
            fp = get_order_status(oid).get('fill_count_fp')
        except Exception:
            log.exception('mm: cancel failed for %s', oid)
            with self.lock:
                m['api_err'] += 1
            return
        with self.lock:
            q = m['orders'][side]
            if not q or q['order_id'] != oid:
                return
            if fp is not None:
                self._account_fills(m, side, q, float(fp))
            if not closed:
                m['api_err'] += 1
                return
            if self._cash is not None:
                rem = max(0.0, q['size'] - q['filled'])
                self._cash += (q['price_c'] if side == 'bid' else 100 - q['price_c']) / 100 * rem
            m['orders'][side] = None

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
            self._record_event(m, side, q['price_c'], d, 'maker')
            self._markouts.append({'due': time.time() + MARKOUT_SECS[0], 'stage': 1, 'ticker': m['spec']['ticker'],
                                   'side': side, 'price_c': q['price_c'], 'qty': d, 'fair0': fair, 'f1': None,
                                   'order_id': q['order_id']})

    def _record_event(self, m, side, price_c, qty, liq, fee=0.0):
        """A fill the UI should announce (flash + toast): what happened, where, book b/a and inventory on each side."""
        bk = m['book']
        bf = sum(f['qty'] for f in m['fills'] if f['side'] == 'bid')
        af = sum(f['qty'] for f in m['fills'] if f['side'] == 'ask')
        self._ev_id += 1
        self.events.append({'id': self._ev_id, 'ts': time.time(), 'ticker': m['spec']['ticker'], 'title': m['spec']['title'],
                            'outcome': m['spec']['outcome'], 'side': side, 'price_c': price_c, 'qty': qty, 'liq': liq,
                            'fee_usd': round(fee, 4), 'bid': bk['bids'][0][0] if bk['bids'] else None,
                            'ask': bk['asks'][0][0] if bk['asks'] else None, 'bought': round(bf, 2), 'sold': round(af, 2),
                            'inv': m['inv']})
        del self.events[:-40]

    def _run_markouts(self):
        """Adverse-selection tracking: Pinnacle fair 1 and 5 minutes after each maker fill."""
        now, keep = time.time(), []
        with self.lock:
            for x in self._markouts:
                if now < x['due']:
                    keep.append(x)
                    continue
                m = self.mkts.get(x['ticker'])
                fair = m['fair_c'] if m else None
                if x['stage'] == 1:
                    x.update(stage=2, f1=fair, due=now + MARKOUT_SECS[1] - MARKOUT_SECS[0])
                    keep.append(x)
                else:
                    try:
                        log_markout(x['ticker'], x['side'], x['price_c'], x['qty'], x['fair0'], x['f1'], fair, x['order_id'])
                    except Exception:
                        log.exception('mm: could not write markout')
            self._markouts = keep

    # -- inventory ----------------------------------------------------------
    @staticmethod
    def _room(m, side):
        """Contracts we may still ADD on this side before net inventory hits the per-market max (buying YES raises
        it, selling YES lowers it). Reducing inventory is always allowed, so the far side stays fully open."""
        return m['max_inv'] - m['inv'] if side == 'bid' else m['max_inv'] + m['inv']

    @staticmethod
    def _basis(m):
        """(net position, average entry price in cents) by average-cost accounting over this market's fills."""
        pos, avg = 0.0, 0.0
        for f in m['fills']:
            q, p = f['qty'], f['price_c']
            signed = q if f['side'] == 'bid' else -q
            if pos == 0 or (pos > 0) == (signed > 0):
                avg = (avg * abs(pos) + p * q) / (abs(pos) + q)
                pos += signed
            else:
                closing = min(q, abs(pos))
                pos += signed
                if abs(pos) < 1e-9:
                    pos, avg = 0.0, 0.0
                elif (pos > 0) == (signed > 0):          # flipped through zero: the remainder opens a new position
                    avg = p
        return pos, avg

    @staticmethod
    def _await_fill(oid, timeout=8.0):
        """Filled quantity of an immediate-or-cancel order, read only once Kalshi reports it CLOSED. Reading the count
        straight after placing can return 0 while the fill is still being booked, which used to leave our inventory
        (and the ledger) unaware of a trade that had really happened."""
        end, fp = time.time() + timeout, 0.0
        while True:
            st = get_order_status(oid)
            fp = float(st.get('fill_count_fp') or 0)
            if st.get('status') in ('executed', 'filled', 'canceled', 'expired') or time.time() > end:
                return fp
            time.sleep(0.4)

    @staticmethod
    def _kalshi_position(ticker):
        """Signed net YES position on Kalshi for this market (+ long YES, − long NO); None if it can't be read."""
        try:
            r = _get('/portfolio/positions', {'ticker': ticker, 'limit': 50})
            r.raise_for_status()
            body = r.json()
            for p in body.get('market_positions', body.get('positions', [])):
                if p.get('ticker') == ticker:
                    return float(p.get('position_fp') or 0)
            return 0.0
        except Exception:
            log.exception('mm: could not read Kalshi position for %s', ticker)
            return None

    def _exit_plan(self, m, book=None):
        """What flattening this market RIGHT NOW would look like: sweep the touch until the position is gone.
        Returns None if flat, else {side, qty, px_worst, vwap, fee, pnl_usd, short} — pnl is all-in (average entry vs the
        prices actually swept, minus the taker fee); `short` = qty that the visible book cannot absorb."""
        pos, avg = self._basis(m)
        qty = int(round(abs(pos)))
        bk = book or m['book']
        if qty < 1:
            return None
        levels = bk['bids'] if pos > 0 else bk['asks']
        got, cost, worst = 0.0, 0.0, None
        for px, sz in levels:
            take = min(qty - got, sz)
            got += take
            cost += take * px
            worst = px
            if got >= qty - 1e-9:
                break
        if got < 1:
            return {'side': 'ask' if pos > 0 else 'bid', 'qty': qty, 'px_worst': None, 'vwap': None, 'fee': 0.0,
                    'pnl_usd': None, 'short': qty}
        vwap = cost / got
        fee = kalshi_fee_dollars(int(round(got)), vwap / 100, m['spec']['ticker'].split('-')[0], maker=False)
        gross = got * ((vwap - avg) if pos > 0 else (avg - vwap)) / 100
        return {'side': 'ask' if pos > 0 else 'bid', 'qty': qty, 'px_worst': worst, 'vwap': vwap, 'fee': fee,
                'pnl_usd': gross - fee, 'short': round(qty - got, 2)}

    def purge(self, tickers=None):
        """Flatten inventory now (every market, or just `tickers`), profit or not: stop quoting those markets, pull their orders, then take
        the book with an immediate-or-cancel order. Runs in the background; fills show up as ordinary events."""
        with self.lock:
            targets = [t for t, m in self.mkts.items() if (tickers is None or t in tickers) and t not in self._purging and self._exit_plan(m)]
            for t in targets:
                self.mkts[t]['paused'] = True
                self._clear_flags(self.mkts[t])
            self._purging.update(targets)
        if not targets:
            return
        log.warning('mm: PURGE %d market(s): %s', len(targets), targets)

        def _one(t):
            try:
                for side in ('bid', 'ask'):
                    self._cancel_one(t, side)
                m = self.mkts.get(t)
                book = fetch_book(t)                       # fresh touch: the cached one can be a couple of seconds old
                with self.lock:
                    plan = self._exit_plan(m, book)
                    if not plan or plan['px_worst'] is None:
                        m['note'] = 'purge: no bids/asks to hit'
                        return
                    side, qty, worst = plan['side'], plan['qty'], plan['px_worst']
                # Never trust our own bookkeeping alone with a market order: size it against the real Kalshi position so a
                # stale display or a repeat click can never flip us the other way.
                kpos = self._kalshi_position(t)
                if kpos is None:
                    with self.lock:
                        m['note'] = 'purge: could not verify the Kalshi position'
                    return
                if abs(kpos) < 0.5 or (kpos > 0) != (side == 'ask'):
                    self.reconcile_from_kalshi()
                    with self.lock:
                        m['note'] = 'purge skipped: already flat on Kalshi'
                    return
                qty = int(min(qty, round(abs(kpos))))
                cid = MM_PREFIX + 'purge-' + str(uuid.uuid4())
                if side == 'ask':
                    o = place_order(t, round(100 - worst, 3), qty, side='no', client_order_id=cid, time_in_force='immediate_or_cancel')
                else:
                    o = place_order(t, worst, qty, side='yes', client_order_id=cid, time_in_force='immediate_or_cancel')
                oid = o.get('order_id')
                filled = self._await_fill(oid)
                with self.lock:
                    if filled > 0:
                        px = plan['vwap']
                        fee = kalshi_fee_dollars(int(round(filled)), px / 100, t.split('-')[0], maker=False)
                        fair = m['fair_c']
                        m['fills'].append({'side': side, 'price_c': px, 'qty': filled, 'ts': time.time(), 'taker': True,
                                           'edge_c': None if fair is None else round((fair - px) if side == 'bid' else (px - fair), 3)})
                        m['inv'] = round(m['inv'] + (filled if side == 'bid' else -filled), 4)
                        try:
                            log_fill(t, side, px, filled, fair, oid, liq='taker', fee_usd=fee)
                        except Exception:
                            log.exception('mm: could not write purge fill')
                        self._record_event(m, side, px, filled, 'offload', fee)
                    if filled < qty - 0.5:
                        m['note'] = f'purge filled {filled:.0f} of {qty}: book too thin'
            except Exception:
                log.exception('mm: purge failed for %s', t)

        def _run():
            try:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    list(ex.map(_one, targets))
            finally:
                self._purging.difference_update(targets)
        threading.Thread(target=_run, name='mm-purge', daemon=True).start()

    def _purge_view(self, m):
        p = self._exit_plan(m)
        return None if not p else {'pnl_usd': None if p['pnl_usd'] is None else round(p['pnl_usd'], 2), 'qty': p['qty'], 'short': p['short']}

    def purge_estimate(self):
        """(all-in PnL in $, contracts, markets) if we purged right now."""
        tot, n, k = 0.0, 0.0, 0
        with self.lock:
            for m in self.mkts.values():
                p = self._exit_plan(m)
                if p:
                    k += 1
                    n += p['qty']
                    tot += p['pnl_usd'] or 0.0
        return round(tot, 2), n, k

    def _offload(self, m, mins):
        """Take profit on held inventory: if the touch pays more than our average entry PLUS the taker fee (with a
        margin), hit it right now with an immediate-or-cancel order. Runs only for markets that are switched on."""
        t = m['spec']['ticker']
        if time.time() - self._last_offload.get(t, 0) < OFFLOAD_EVERY_SEC or mins * 60 <= PRE_EVENT_BUFFER:
            return
        pos, avg = self._basis(m)
        bk = m['book']
        if abs(pos) < 0.5 or not bk['bids'] or not bk['asks']:
            return
        series = t.split('-')[0]
        if pos > 0:                                       # long YES: sell into the best bid
            px, depth = bk['bids'][0]
            side, edge = 'ask', px - avg
            if m['orders']['bid'] and abs(m['orders']['bid']['price_c'] - px) < 1e-6:
                return                                    # that bid is (partly) ours: self-trade guard
        else:                                             # long NO: buy YES back at the best ask
            px, depth = bk['asks'][0]
            side, edge = 'bid', avg - px
            if m['orders']['ask'] and abs(m['orders']['ask']['price_c'] - px) < 1e-6:
                return
        qty = int(min(abs(pos), depth))
        if qty < 1:
            return
        fee = kalshi_fee_dollars(qty, px / 100, series, maker=False)
        net_total = qty * edge / 100 - fee
        if net_total < OFFLOAD_MIN_USD or net_total / qty * 100 < OFFLOAD_MIN_NET_C:
            return
        self._last_offload[t] = time.time()
        cid = MM_PREFIX + 'ofl-' + str(uuid.uuid4())
        try:
            if side == 'ask':
                o = place_order(t, round(100 - px, 3), qty, side='no', client_order_id=cid, time_in_force='immediate_or_cancel')
            else:
                o = place_order(t, px, qty, side='yes', client_order_id=cid, time_in_force='immediate_or_cancel')
            oid = o.get('order_id')
            filled = self._await_fill(oid)
        except Exception:
            log.exception('mm: offload failed %s', t)
            m['api_err'] += 1
            return
        if filled > 0:
            fee = kalshi_fee_dollars(int(round(filled)), px / 100, series, maker=False)
            fair = m['fair_c']
            m['fills'].append({'side': side, 'price_c': px, 'qty': filled, 'ts': time.time(), 'taker': True,
                               'edge_c': None if fair is None else round((fair - px) if side == 'bid' else (px - fair), 3)})
            m['inv'] = round(m['inv'] + (filled if side == 'bid' else -filled), 4)
            try:
                log_fill(t, side, px, filled, fair, oid, liq='taker', fee_usd=fee)
            except Exception:
                log.exception('mm: could not write offload fill')
            self._record_event(m, side, px, filled, 'offload', fee)
            log.info('mm: offloaded %s %s x%s @ %s (avg entry %.2f, net %.3f$ after fee)', t, side, filled, px, avg, net_total)

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

    def _place_quote(self, m, side, price_c, size=None):
        t, size = m['spec']['ticker'], (size or m['size'])
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
                    if st.get('status') in ('resting', 'open', 'pending'):
                        continue        # Kalshi's list lags a fresh order: it is still live, so keep tracking it (never orphan it)
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
                    kf = float(k.get('fill_count_fp') or 0)
                    if kf > q['filled'] + 1e-6:                     # partial fill while still resting: announce it now
                        self._account_fills(m, side, q, kf)
                    if abs(ks - q['size']) > 0.01 or abs(kp - q['price_c']) > 0.01:
                        log.warning('mm: %s %s resting on Kalshi as %s @ %s, we thought %s @ %s — resyncing',
                                    m['spec']['ticker'], side, ks, kp, q['size'], q['price_c'])
                        q['size'], q['price_c'] = ks, kp
                except (TypeError, ValueError):
                    pass
            target = None if not self._side_enabled(m, side, self.params['live']) else m['target'].get(side)
            room = self._room(m, side)
            want = min(m['size'], int(room + 1e-9))
            if want < 1 and not m['paused']:
                m['note'] = f"at max inventory: not {'buying' if side == 'bid' else 'selling'} more"
                target = None
            if minutes_to_start * 60 <= PRE_EVENT_BUFFER or target is None:
                if q:
                    self._cancel_quote(m, side)
                continue
            if q and abs(q['price_c'] - target) < 1e-6 and (
                    q['size'] == want or (q['size'] == m['size'] and q['size'] - q['filled'] <= room + 1e-9)):
                continue
            if q and time.time() - q['placed'] < 5 and not m['force'][side]:   # don't churn faster than every 5s
                continue
            m['force'][side] = False
            if q and not self._cancel_quote(m, side):
                continue                                          # old order still live: never stack a second one
            self._place_quote(m, side, target, want)

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
                    if (self.params['live'] or any(self._running(x, False) for x in self.mkts.values())) \
                            and time.time() - self.last_ui > UI_DEADMAN_SEC:
                        # dead-man switch: nobody is watching the board (browser closed / app stalled)
                        log.warning('mm: no UI heartbeat for %ss — quoting off, cancelling', UI_DEADMAN_SEC)
                        self.params['live'] = False
                        for x in self.mkts.values():
                            self._clear_flags(x)
                        self._cancel_bg()
                    live = self.params['live'] or any(self._running(x, False) for x in self.mkts.values())
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
                    if resting_ids:
                        self._sweep_orphans(resting_ids)
                    self._refresh_fair()      # Pinnacle credits are only spent for markets we're quoting
                self._run_markouts()
                if live and time.time() - self._reconciled_at > 90:
                    self._reconciled_at = time.time()
                    threading.Thread(target=self.reconcile_from_kalshi, name='mm-reconcile', daemon=True).start()
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
                        return t, (fetch_book(t, True) if t == self.focus else fetch_book(t)), None
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
                        m_active = self._running(m, self.params['live'])
                        if m_active and live and resting_ids is not None:   # re-check: may have been switched off mid-cycle
                            had = bool(m['orders']['bid'] or m['orders']['ask'])
                            if not had and not m['paused']:
                                if new_started >= MAX_NEW_MKTS_PER_CYCLE:
                                    continue                                          # pace order writes
                                new_started += 1
                            self._manage_live(m, resting_ids, mins)
                            if self.params['auto_offload'] and m_active:
                                self._offload(m, mins)
                        elif not m_active and not self._cancel_busy and (m['orders']['bid'] or m['orders']['ask']):
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
                    'max_inv': m['max_inv'], 'note': m['note'], 'paused': m['paused'], 'active': (live_now and not m['paused']) or m['on'],
                    'purge': self._purge_view(m),
                    'sides': {s: bool(self._side_enabled(m, s, live_now)) for s in ('bid', 'ask')},
                    'status': ('paused' if (m['paused'] and not self._running(m, live_now)) else 'quoting' if orders else ('waiting' if self._running(m, live_now) else 'off')),
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
                    'ack': self.ack, 'cash': self._cash, 'purge': dict(zip(('pnl_usd', 'contracts', 'markets'), self.purge_estimate())), 'act': {'port': self.action_port, 'token': self._token}, 'events': list(self.events[-15:])}
