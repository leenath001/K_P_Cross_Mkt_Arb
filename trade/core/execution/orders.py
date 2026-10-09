"""
trade/core/execution/orders.py — Order primitives: place, cancel (with 429 backoff and sharding-safe endpoint), verify-cancel, status lookups, and listing / cancelling resting orders.
"""
import time, uuid, random
import requests
from typing import Optional

from KALSHI import client as kalshi
from applog import get_logger
from trade.core.execution.constants import OPEN_ORDER_STATUSES, _CLOSED_STATUSES

log = get_logger('trade.core.execution')   # one logger name for the whole package, as before the split


# ---------------------------------------------------------------------------
# Order Placement, Cancellation, Status
# ---------------------------------------------------------------------------

def _price_str(cents: float) -> str:
    """Dollar price string: 2 decimals for whole cents (unchanged), more only when the market's grid needs it (0.445)."""
    s = f'{cents / 100:.4f}'
    while s.endswith('0') and len(s.split('.')[1]) > 2:
        s = s[:-1]
    return s


def place_order(ticker: str, price_cents: float, count: int,
                side: str = 'yes',
                expiration_ts: Optional[int] = None,
                post_only: bool = False,
                client_order_id: Optional[str] = None,
                time_in_force: str = 'good_till_canceled') -> dict:
    """
    Place a limit buy order on Kalshi for YES or NO contracts.
    time_in_force='immediate_or_cancel' takes what is available at the price and cancels the rest.
    post_only=True guarantees the order rests (maker fee) — rejected if it would cross.
    `price_cents` is interpreted as yes_price for side='yes', no_price for side='no'.

    Uses the V2 order endpoint (the legacy /portfolio/orders POST returns 410 Gone).
    V2 quotes everything in YES terms: buying NO is submitted as `ask` (sell YES) at
    the complementary price, since selling YES at P is economically buying NO at 1-P.
    """
    # Kalshi V2 always quotes from the YES side.
    # For NO orders the yes-equivalent price is the complement: 100 - no_price_cents.
    yes_price_cents = round(100 - price_cents, 3) if side == 'no' else price_cents
    body: dict = {
        'ticker':                     ticker,
        'client_order_id':            client_order_id or str(uuid.uuid4()),
        'side':                       'ask' if side == 'no' else 'bid',
        'count':                      f'{count:.2f}',
        'price':                      _price_str(yes_price_cents),
        'time_in_force':              time_in_force,
        'self_trade_prevention_type': 'taker_at_cross',
    }
    if expiration_ts:
        body['expiration_time'] = expiration_ts
    if post_only:
        body['post_only'] = True
    log.debug('place_order body=%s', body)
    try:
        resp = kalshi.post('/portfolio/events/orders', json=body)
    except requests.exceptions.RequestException as exc:
        log.error('place_order network failure for %s: %s', ticker, exc)
        raise
    if not resp.ok:
        if resp.status_code == 404 and 'user_not_found' in resp.text:
            # Kalshi shards some categories onto separate exchange indices (e.g. shard 3
            # = Tennis & Baseball) and requires collateral pre-funded on that specific
            # shard before it will accept an order there — see
            # https://docs.kalshi.com/getting_started/exchange_sharding. This is NOT a
            # transient failure; it recurs on every order for that category until the
            # shard is funded via the Intra-Account Transfer endpoint
            # (POST /portfolio/intra_exchange_instance_transfer).
            log.error('place_order failed for %s: shard-funding gap (404 user_not_found) — '
                     'this ticker routes to an exchange shard with no collateral. Fund it via '
                     'POST /portfolio/intra_exchange_instance_transfer. Response: %s',
                     ticker, resp.text)
        else:
            log.error('place_order failed for %s: %s %s — %s',
                      ticker, resp.status_code, resp.reason, resp.text)
        raise requests.HTTPError(
            f'{resp.status_code} {resp.reason} — {resp.text}', response=resp
        )
    return resp.json()


def cancel_order(ticker: str, order_id: str, max_retries: int = 4) -> bool:
    """
    Cancel an open Kalshi order. Returns True on success, False on any failure (logged).

    `ticker` is REQUIRED and passed as the `market_ticker` query param so Kalshi can
    auto-route the DELETE to the correct exchange shard. Without it, Kalshi defaults
    to shard 0 and 404s on anything else — confirmed live: MLB/Tennis lives on shard 3,
    Combos on 1, Crypto on 2, and every cancel attempt against those was silently
    failing (this is what made cancel_all.py look "broken for MLB games" — it wasn't
    MLB-specific, every non-default-shard order was affected the same way).

    Retries with exponential backoff + jitter on 429 (rate limit). Bulk-cancel paths
    ("Cancel all" waking every _monitor() thread at once, _force_cancel_all() looping
    over many orders) can burst past Kalshi's write-token budget — observed in
    production as repeated 429s on cancel_order. Without a retry, that 429 was final:
    cancel_order returned False and the order stayed open, silently, even though the
    user believed "Cancel all" had closed it.
    """
    for attempt in range(max_retries + 1):
        try:
            resp = kalshi.delete(f'/portfolio/events/orders/{order_id}', params={'market_ticker': ticker},
                                 timeout=15)   # safe to bound: callers verify the cancel (ensure_canceled / 404 check)
        except requests.exceptions.RequestException as exc:
            log.error('cancel_order network failure for %s (%s): %s', order_id, ticker, exc)
            return False
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get('Retry-After')
            try:
                delay = float(retry_after) if retry_after else (0.4 * (2 ** attempt))
            except ValueError:
                delay = 0.4 * (2 ** attempt)
            delay += random.uniform(0, 0.3)  # jitter — desyncs threads that got rate-limited together
            log.warning('cancel_order rate-limited for %s — retrying in %.1fs (attempt %d/%d)',
                       order_id, delay, attempt + 1, max_retries)
            time.sleep(delay)
            continue
        if resp.status_code == 404:
            # Kalshi 404s a DELETE on an order that's already gone. Concurrent cancellers
            # (a monitor thread + "Cancel all", or ensure_canceled's retry after a
            # first success) routinely race — the loser sees 404 even though the order
            # IS canceled. Confirm with a GET rather than reporting a false failure.
            status = get_order_status(order_id).get('status')
            if status in _CLOSED_STATUSES:
                log.info('cancel_order: %s already closed (lost a cancel race) — ok', order_id)
                return True
            # Kalshi also drops a canceled/expired order that never filled from its lookups, so the GET above can
            # come back 'unknown' for an order that is simply gone. What matters is whether it is still RESTING:
            # check that market's resting orders directly before reporting a failure.
            if status == 'unknown' and not _is_resting(ticker, order_id):
                log.info('cancel_order: %s no longer exists on Kalshi and is not resting — ok', order_id)
                return True
        log.warning('cancel_order failed for %s (%s): %s %s — %s',
                    order_id, ticker, resp.status_code, resp.reason, resp.text)
        return False
    return False


def _is_resting(ticker: str, order_id: str) -> bool:
    """True if order_id is among `ticker`'s resting orders — or if that can't be checked (fail safe: assume it is)."""
    try:
        resp = kalshi.get('/portfolio/orders', params={'ticker': ticker, 'status': 'resting', 'limit': 200})
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        log.exception('_is_resting: could not list resting orders for %s', ticker)
        return True
    return any(o.get('order_id') == order_id for o in resp.json().get('orders', []))


def ensure_canceled(ticker: str, order_id: str, max_attempts: int = 5,
                    poll_delay: float = 0.5) -> bool:
    """
    Cancel an order and VERIFY it actually closed — don't just trust cancel_order()'s
    return value as proof the order is dead. A 200/204 means Kalshi accepted the
    cancel REQUEST; eventual consistency means a follow-up GET can still show it
    resting for a beat (see _final_order_status). Re-issues the cancel if a
    confirming GET still shows the order open, instead of firing one DELETE and
    hoping. Checks status FIRST each loop so an already-closed order costs one GET,
    not a wasted DELETE.

    Use this (not bare cancel_order) anywhere the caller is about to act on the
    assumption the order is gone — e.g. re-placing as a cross, re-resting at a new
    price. A false "canceled" there risks double exposure (old + new order both live).

    Returns True once Kalshi confirms closed, False if it gives up after
    max_attempts (logged as an error — this is a real "still can't confirm cancel"
    situation, not a routine retry).
    """
    status = 'unknown'
    for attempt in range(max_attempts):
        status = get_order_status(order_id).get('status', 'unknown')
        if status in _CLOSED_STATUSES:
            return True
        cancel_order(ticker, order_id)
        time.sleep(poll_delay * (attempt + 1))
    status = get_order_status(order_id).get('status', 'unknown')
    if status in _CLOSED_STATUSES:
        return True
    log.error('ensure_canceled: gave up on %s (%s) after %d attempts — still %s',
             order_id, ticker, max_attempts, status)
    return False


def list_resting_orders() -> list:
    """
    Every currently-resting order on Kalshi, live — the same live-first
    principle as positions.open_tickers(): don't infer "what's open" from our
    own logs, ask Kalshi. Used by cancel_all.py and the web UI's "Cancel All
    Orders" panic button, which both cancel EVERYTHING resting regardless of
    which strategy or session placed it.

    Paginated: a single page caps at 200, and a big batch can leave more than that resting — the panic button must
    see every one of them.
    """
    orders, cursor = [], None
    for _ in range(50):
        params = {'status': 'resting', 'limit': 200, **({'cursor': cursor} if cursor else {})}
        resp = kalshi.get('/portfolio/orders', params=params)
        resp.raise_for_status()
        body = resp.json()
        orders.extend(body.get('orders', []))
        cursor = body.get('cursor')
        if not cursor:
            break
    return [o for o in orders if o.get('status') in OPEN_ORDER_STATUSES]


def cancel_all_resting_orders() -> dict:
    """
    Cancel every resting order on Kalshi, verified (via ensure_canceled — ping
    and keep trying, not trust-the-first-response). Returns
    {'total', 'canceled', 'failed': [(order_id, ticker), ...]}.
    """
    orders = list_resting_orders()
    result = {'total': len(orders), 'canceled': 0, 'failed': []}
    for o in orders:
        oid, ticker = o.get('order_id'), o.get('ticker')
        if ensure_canceled(ticker, oid):
            result['canceled'] += 1
        else:
            result['failed'].append((oid, ticker))
    return result


def get_order_status(order_id: str) -> dict:
    """
    Fetch the current state of an order directly from Kalshi.
    Tries the single-order endpoint first; falls back to searching
    the open orders list if that returns 404.
    Status is always taken verbatim from Kalshi — never assumed.

    Both underlying calls are unaffected by the exchange-sharding issue that hits
    cancel_order(): the list endpoint returns cross-shard results by default
    (verified live), so no ticker/exchange_index is needed here.
    """
    try:
        # 1. Try direct lookup
        resp = kalshi.get(f'/portfolio/orders/{order_id}')
        if resp.ok:
            return resp.json().get('order', {})

        # 2. Fallback: scan all recent orders for this order_id
        resp2 = kalshi.get('/portfolio/orders', params={'limit': 100})
        if resp2.ok:
            for o in resp2.json().get('orders', []):
                if o.get('order_id') == order_id:
                    return o
    except requests.exceptions.RequestException as exc:
        log.warning('get_order_status network failure for %s: %s', order_id, exc)

    # 3. Could not confirm status — return unknown so monitor keeps running
    return {'status': 'unknown', 'order_id': order_id}


def filled_count(order: dict) -> int:
    """Contracts actually filled on an order, per Kalshi's fill_count_fp."""
    fill_fp = order.get('fill_count_fp')
    return round(float(fill_fp)) if fill_fp is not None else 0


def _final_order_status(order_id: str, retries: int = 4, delay: float = 0.5) -> dict:
    """
    Fetch the definitive post-monitor order status for logging.

    _monitor() may have just canceled moments before returning — Kalshi's
    cancellation can lag behind the DELETE response by a beat, so a GET issued
    immediately after can catch a stale 'resting' snapshot. If that gets written to
    log_trade() as the "final" status, the CSV permanently shows
    result=PENDING/final_status=resting even though the order is long since
    canceled. (Confirmed empirically: every "PENDING+resting" row sampled from the
    log showed 'canceled' or aged-out 'unknown' when queried live, never resting.)
    ensure_canceled() at the call site now makes this far less likely, but this
    retry stays as a second line of defense for the final status read itself.

    Retry briefly until the status is no longer 'resting', or give up and
    return the last read after `retries` attempts.
    """
    order = {}
    for attempt in range(retries):
        order = get_order_status(order_id)
        if order.get('status') != 'resting':
            return order
        time.sleep(delay)
    log.warning('_final_order_status: %s still showing resting after %d retries '
               '(%.1fs) — logging as-is', order_id, retries, retries * delay)
    return order


def force_cancel_all(order_registry: list) -> int:
    """
    Cancel every (order_id, ticker) in the registry that isn't already terminal.
    order_registry: list of (order_id, ticker) tuples.
    """
    canceled = 0
    for i, entry in enumerate(list(order_registry)):
        if not entry:
            continue
        oid, ticker = entry
        if not oid:
            continue
        try:
            status = get_order_status(oid).get('status', 'unknown')
            if status in _CLOSED_STATUSES:
                continue
            if ensure_canceled(ticker, oid):
                canceled += 1
        except Exception:
            # Best-effort — try remaining orders even if one fails
            log.exception('force_cancel_all: failed to cancel order %s (%s)', oid, ticker)
        # Small throttle between orders — this loop can run 20-30+ cancels back to
        # back (e.g. Ctrl+C mid-session), which was enough on its own to trip
        # Kalshi's write-token rate limit even before cancel_order()'s own retry logic.
        if i < len(order_registry) - 1:
            time.sleep(0.15)
    return canceled
