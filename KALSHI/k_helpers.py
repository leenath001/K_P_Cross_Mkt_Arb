import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pandas as pd
import config
import json
import base64
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, date
from difflib import SequenceMatcher
from typing import Optional
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from applog import get_logger

log = get_logger(__name__)

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
load_dotenv(dotenv_path=_env_path, override=True)

def _secret(key: str) -> Optional[str]:
    """Read from env first, fall back to st.secrets (Streamlit Cloud)."""
    val = os.getenv(key)
    if not val:
        try:
            import streamlit as st
            val = st.secrets.get(key)
        except Exception:
            pass
    return val

# -- Global Variables -------
SPORTS = config.SPORTS
SPORTS_C = config.SPORTS_CONFIG
API_KEY     = _secret('API_KEY')
API_PRIVATE = _secret('API_PRIVATE')
BASE_URL    = 'https://api.elections.kalshi.com/trade-api/v2'
PATH        = '/trade-api/v2/markets'

assert API_PRIVATE, f'API_PRIVATE not found — set it in .env or Streamlit Cloud secrets'

# API_PRIVATE is a raw base64-encoded DER key — wrap in PEM headers to load it
_pem_bytes = (
    b'-----BEGIN RSA PRIVATE KEY-----\n' +
    API_PRIVATE.strip().encode() +
    b'\n-----END RSA PRIVATE KEY-----\n'
)
_private_key = serialization.load_pem_private_key(_pem_bytes, password=None)
# ---------------------------

_DRAW_ALIASES = {'draw', 'tie', 'draw/tie', 'x'}

def _parse_ticker_date(event_ticker: str) -> Optional[date]:
    try:
        segment = event_ticker.split('-')[1]  # e.g. '26MAR17'
        return datetime.strptime(segment[:7], '%y%b%d').date()
    except Exception:
        return None

def _normalize(name: str) -> str:
    if name.strip().lower() in _DRAW_ALIASES:
        return '__DRAW__'
    return name.strip().lower()

def _best_match(query: str, candidates: pd.Series) -> tuple:
    norm_query = _normalize(query)
    best_idx, best_score = -1, 0.0
    for idx, val in candidates.items():
        norm_val = _normalize(str(val))
        if norm_query == '__DRAW__' and norm_val == '__DRAW__':
            return idx, 1.0
        if norm_query == '__DRAW__' or norm_val == '__DRAW__':
            continue
        score = SequenceMatcher(None, norm_query, norm_val).ratio()
        # Boost score when Kalshi uses city-only names (e.g. "Atlanta" vs "Atlanta Hawks")
        q_words, v_words = set(norm_query.split()), set(norm_val.split())
        if q_words and v_words and (q_words.issubset(v_words) or v_words.issubset(q_words)):
            score = max(score, 0.95)
        if score > best_score:
            best_score, best_idx = score, idx
    return best_idx, best_score

def kalshi_headers(method: str, path: str) -> dict:
    ts  = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = _private_key.sign(
        msg,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return {
        'KALSHI-ACCESS-KEY':       API_KEY,
        'KALSHI-ACCESS-TIMESTAMP': ts,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
    }


# ---------------------------------------------------------------------------
# Trading fees
#
# Kalshi's published quadratic fee formula (kalshi.com/docs/kalshi-fee-schedule.pdf,
# effective Feb 5 2026):
#   taker fee = ceil_to_cent(0.07   x fee_multiplier x C x P x (1-P))
#   maker fee = ceil_to_cent(0.0175 x fee_multiplier x C x P x (1-P))   [0.25x taker]
#   combo maker fee (rare) uses a 0.5x multiplier instead of 0.25x, i.e. 0.035 base.
# `fee_multiplier` and whether maker fees apply at all ('quadratic' = no maker fee,
# 'quadratic_with_maker_fees' = maker fee applies, 'quadratic_with_combo_maker_fees' =
# maker fee at the 0.5x rate) are set PER SERIES and verified to genuinely differ
# (e.g. KXMLBGAME has fee_multiplier=0.5; KXLIGAMXGAME and KXBOXING are plain
# 'quadratic' with NO maker fee at all) — so a single hardcoded global rate is wrong.
# ---------------------------------------------------------------------------

import math

TAKER_FEE_BASE       = 0.07
MAKER_FEE_BASE       = 0.0175   # 0.25x TAKER_FEE_BASE
COMBO_MAKER_FEE_BASE = 0.035    # 0.5x  TAKER_FEE_BASE

_series_fee_cache: dict = {}   # series_ticker -> (fee_type, fee_multiplier)


def get_series_fee_info(series_ticker: str) -> tuple:
    """
    (fee_type, fee_multiplier) for a series, fetched live from Kalshi's public
    /series/{ticker} endpoint (no auth required) and cached for the process
    lifetime — fee schedules are announced in advance, not changed intra-session.
    Falls back to ('quadratic_with_maker_fees', 1.0) — Kalshi's general-table
    default — if the lookup fails, rather than silently assuming no fee.
    """
    if series_ticker in _series_fee_cache:
        return _series_fee_cache[series_ticker]
    try:
        resp = requests.get(f'{BASE_URL}/series/{series_ticker}')
        if resp.ok:
            s = resp.json().get('series', {})
            info = (s.get('fee_type') or 'quadratic_with_maker_fees',
                    float(s.get('fee_multiplier') or 1.0))
            _series_fee_cache[series_ticker] = info
            return info
        log.warning('get_series_fee_info: %s -> %s %s', series_ticker, resp.status_code, resp.reason)
    except requests.exceptions.RequestException:
        log.exception('get_series_fee_info network failure for %s', series_ticker)
    return ('quadratic_with_maker_fees', 1.0)


def fee_rate_for(series_ticker: str, maker: bool) -> float:
    """
    Smooth per-contract fee RATE — exact only in the limit of large contract count
    (no cent-rounding). Use for pre-sizing EV/signal estimates. For the true dollar
    fee on an actual order, use kalshi_fee_dollars() instead.
    """
    fee_type, mult = get_series_fee_info(series_ticker)
    if not maker:
        return TAKER_FEE_BASE * mult
    if fee_type == 'quadratic_with_combo_maker_fees':
        return COMBO_MAKER_FEE_BASE * mult
    if fee_type == 'quadratic_with_maker_fees':
        return MAKER_FEE_BASE * mult
    return 0.0   # plain 'quadratic' or 'flat' series: resting orders are free


def kalshi_fee_dollars(contracts: int, price: float, series_ticker: str, maker: bool) -> float:
    """
    EXACT Kalshi trading fee in dollars for a real order of `contracts` at `price` —
    the closed-form ceil-to-cent formula from the fee schedule, not the smooth
    per-contract approximation. Use this once contract count is known (i.e. after
    Kelly sizing), for the final go/no-go check before firing an order.
    """
    rate = fee_rate_for(series_ticker, maker)
    if rate == 0 or contracts <= 0:
        return 0.0
    raw = rate * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100   # round up to next cent; epsilon guards float noise

def load_all_mkts(SERIES_TICKER: str):
    try:
        resp = requests.get(
            f'{BASE_URL}/markets',
            headers=kalshi_headers('GET', '/trade-api/v2/markets'),
            params={'series_ticker': SERIES_TICKER, 'status': 'open', 'limit': 500})
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        log.exception('load_all_mkts failed for series %s', SERIES_TICKER)
        raise
    markets =  resp.json().get('markets', [])

    z = pd.DataFrame([{
            'event_ticker':  m.get('event_ticker'),
            'ticker':        m['ticker'],
            'title':         m.get('title'),
            'yes_sub_title': m.get('yes_sub_title'),
            'no_sub_title':  m.get('no_sub_title'),
            'yes_bid':       float(m['yes_bid_dollars']) if m.get('yes_bid_dollars') else None,
            'yes_ask':       float(m['yes_ask_dollars']) if m.get('yes_ask_dollars') else None,
            'no_bid':        float(m['no_bid_dollars'])  if m.get('no_bid_dollars')  else None,
            'no_ask':        float(m['no_ask_dollars'])  if m.get('no_ask_dollars')  else None,
            'volume':        m.get('volume_fp'),
            'open_int':      m.get('open_interest_fp'),
        } for m in markets])
    
    return z   

def _ev(fair_prob: float, price: float, fee_rate: float) -> float:
    """EV per contract. Kalshi fee = fee_rate * price * (1-price), charged on entry."""
    return fair_prob - price - fee_rate * price * (1 - price)


def _match_event(group: pd.DataFrame, day_k: pd.DataFrame,
                 threshold: float) -> Optional[str]:
    """
    Find the Kalshi `event_ticker` where ALL Pinnacle outcomes in `group`
    match a market, i.e. every leg of the game lines up with the same Kalshi
    event. Returns the event_ticker with the highest aggregate match score,
    or None if no event passes.

    This prevents mismatches where a single outcome fuzzy-matches a market
    in the wrong Kalshi game on the same day.
    """
    outcomes = [str(o) for o in group['outcome'].tolist()]
    best_ticker = None
    best_total  = 0.0
    for ev_ticker, ev_k in day_k.groupby('event_ticker'):
        total = 0.0
        passes = True
        for outcome in outcomes:
            _, score = _best_match(outcome, ev_k['yes_sub_title'])
            if score < threshold:
                passes = False
                break
            total += score
        if passes and total > best_total:
            best_total, best_ticker = total, ev_ticker
    return best_ticker


def kalshi_odds(df: pd.DataFrame, threshold: float = 0.6,
                max_delta: float = 0.25) -> pd.DataFrame:
    """
    Takes a Pinnacle odds DataFrame and returns a merged DataFrame pairing each
    outcome row with its corresponding Kalshi market's bid/ask prices.

    Taker/maker fee rates are looked up LIVE per series (kalshi_fee_dollars /
    fee_rate_for) rather than assumed globally — verified to genuinely differ
    per series (e.g. KXMLBGAME has fee_multiplier=0.5; several soccer/boxing
    series charge NO maker fee at all). A single hardcoded rate would misprice
    signals in both directions: overstating maker-fee drag on series that don't
    charge one (missing real edge), and understating it on discounted series.

    `max_delta`  — drop signals where |fair_prob − price| exceeds this. A big
                   delta is almost always a fuzzy-match failure, not real edge.
                   Set to 1.0 to disable.

    Match safety:
      1. Event-level verification — every Pinnacle outcome in a game must
         resolve to a market under the SAME Kalshi event_ticker. If any leg
         fails, the whole event is dropped.
      2. Delta sanity — signals with an unrealistically large gap between
         fair_prob and the Kalshi price are flagged false.
    """
    pinnacle_df = df.copy()

    # 1. Map sports to Kalshi series tickers (skip anything paused — see config.PAUSED_SPORTS)
    paused = getattr(config, 'PAUSED_SPORTS', set())
    sport_to_series = {}
    for sport in pinnacle_df['sport'].unique():
        if sport in paused:
            continue
        if sport in SPORTS_C:
            sport_to_series[sport] = SPORTS_C[sport]['ticker']

    # 2. Load all Kalshi markets, tagging each row with its series ticker
    series_dfs = []
    for series in set(sport_to_series.values()):
        try:
            mkt_df = load_all_mkts(series)
        except Exception:
            # Don't let one bad series (rate limit, transient 5xx, etc.) blank out
            # every other sport's matches — skip it and keep going.
            log.exception('kalshi_odds: skipping series %s after fetch failure', series)
            continue
        mkt_df['series_ticker'] = series
        series_dfs.append(mkt_df)

    if not series_dfs:
        log.warning('kalshi_odds: no Kalshi series loaded successfully — returning empty result')
        return pd.DataFrame()

    all_k = pd.concat(series_dfs, axis=0, ignore_index=True)

    # 3. Parse dates from event_ticker
    all_k['k_date'] = all_k['event_ticker'].apply(_parse_ticker_date)

    # 4. Match each Pinnacle outcome to a Kalshi market — event-locked
    rows = []
    for event_id, group in pinnacle_df.groupby('event_id'):
        sport     = group['sport'].iloc[0]
        series    = sport_to_series.get(sport)
        game_date = group['commence'].iloc[0].date()

        day_k = all_k[(all_k['k_date'] == game_date) & (all_k['series_ticker'] == series)]
        if day_k.empty:
            continue

        # Event-level verification: pick the Kalshi event whose markets cover
        # every Pinnacle outcome above threshold. If none pass, skip the game.
        locked_event = _match_event(group, day_k, threshold)
        if locked_event is None:
            continue
        event_k = day_k[day_k['event_ticker'] == locked_event]

        for _, p_row in group.iterrows():
            idx, score = _best_match(p_row['outcome'], event_k['yes_sub_title'])
            if score < threshold:
                continue
            k_row = event_k.loc[idx]

            # Sanity: a huge gap is a mismatch, not edge
            yes_ask    = k_row['yes_ask']
            no_ask     = k_row['no_ask']
            delta_yes  = abs(p_row['fair_prob'] - yes_ask) if yes_ask is not None else 0
            delta_no   = abs((1 - p_row['fair_prob']) - no_ask) if no_ask is not None else 0
            mismatched = (delta_yes > max_delta) or (delta_no > max_delta)

            yes_bid  = k_row['yes_bid']
            no_bid   = k_row['no_bid']

            # Spread-aware rest price: 2¢ wide → bid+1 (midpoint); 1¢ wide → bid; else ask-1
            def _rp(bid, ask):
                if bid is not None and ask is not None:
                    s = round((ask - bid) * 100)
                    if s == 2: return round(ask - 0.01, 2)
                    if s == 1: return round(bid, 2)
                return round(ask - 0.01, 2) if ask is not None else None

            rest_price_yes = _rp(yes_bid, yes_ask)
            rest_price_no  = _rp(no_bid,  no_ask)

            fp    = p_row['fair_prob']
            fp_no = 1 - fp
            MIN_CROSS_EV = 0.005  # minimum EV required to fire a taker (cross) signal

            # MAX_EDGE_OVER_PRICE: a calculated edge that's large RELATIVE to the price
            # paid (cheap longshot + huge apparent mispricing) is the single strongest
            # predictor of a bad trade in this bot's own settled history — top quartile
            # of edge/price won 14% of the time vs 34.5% for the rest (n=228,
            # notebooks/win_loss_analysis.ipynb). It's almost always a fuzzy-match
            # error or a fair_prob miscalibration, not real alpha. 0.14 = that
            # top-quartile cutoff (Q3=0.138), rounded.
            MAX_EDGE_OVER_PRICE = 0.14

            # Segment-aware MIN_EDGE: draws are a confirmed weak spot even after the
            # power-method de-vig fix (non-draw soccer was calibrated to within 0.3pp;
            # draws still missed by ~8pp) — demand more edge before trusting one.
            # Non-draw signals with fair_prob > 0.3 were calibrated almost exactly
            # (predicted 30.5%, actual 30.2%) — safe to admit smaller edges there,
            # which is also where the "signals are too rare" complaint bites hardest.
            is_draw = (('draw' in str(p_row['outcome']).lower() or 'tie' in str(p_row['outcome']).lower())
                       or str(k_row['ticker']).endswith('-TIE'))
            if is_draw:
                MIN_EDGE = 0.03
            elif fp > 0.3:
                MIN_EDGE = 0.005
            else:
                MIN_EDGE = 0.01

            taker_rate = fee_rate_for(series, maker=False)
            maker_rate = fee_rate_for(series, maker=True)

            def _not_suspicious(edge_val, price_val):
                return edge_val / max(price_val, 0.01) <= MAX_EDGE_OVER_PRICE

            # YES cross: taker fills at yes_ask
            signal           = (not mismatched and yes_ask is not None and
                                fp - yes_ask >= MIN_EDGE and
                                _not_suspicious(fp - yes_ask, yes_ask) and
                                _ev(fp, yes_ask, taker_rate) >= MIN_CROSS_EV)
            # YES rest: maker posts at spread-aware price (bid+1 for 2¢ wide, bid for 1¢ wide)
            signal_yes_rest  = (not mismatched and rest_price_yes is not None and
                                fp - rest_price_yes >= MIN_EDGE and
                                _not_suspicious(fp - rest_price_yes, rest_price_yes) and
                                _ev(fp, rest_price_yes, maker_rate) > 0)
            # NO cross: taker fills at no_ask
            signal_no_cross  = (not mismatched and no_ask is not None and
                                fp_no - no_ask >= MIN_EDGE and
                                _not_suspicious(fp_no - no_ask, no_ask) and
                                _ev(fp_no, no_ask, taker_rate) >= MIN_CROSS_EV)
            # NO rest: maker posts at spread-aware price
            signal_no        = (not mismatched and rest_price_no is not None and
                                fp_no - rest_price_no >= MIN_EDGE and
                                _not_suspicious(fp_no - rest_price_no, rest_price_no) and
                                _ev(fp_no, rest_price_no, maker_rate) > 0)

            rows.append({
                'sport':          p_row['sport'],
                'event_id':       event_id,
                'home':           p_row['home'],
                'away':           p_row['away'],
                'commence':       p_row['commence'],
                'outcome':        p_row['outcome'],
                'fair_prob':      p_row['fair_prob'],
                'k_event_ticker': k_row['event_ticker'],
                'k_ticker':       k_row['ticker'],
                'yes_bid':        k_row['yes_bid'],
                'yes_ask':        yes_ask,
                'no_bid':         k_row['no_bid'],
                'no_ask':         no_ask,
                'volume':         k_row['volume'],
                'OI':             k_row['open_int'],
                'match_score':    round(score, 3),
                'price_delta':    round(max(delta_yes, delta_no), 3),
                'mismatched':     mismatched,
                'is_draw':        is_draw,
                'signal':           signal,
                'signal_yes_rest':  signal_yes_rest,
                'signal_no_cross':  signal_no_cross,
                'signal_no':        signal_no,
                'taker_fee_rate':   taker_rate,
                'maker_fee_rate':   maker_rate,
            })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset='k_ticker')
    return df


def prospect_signals(
    df: pd.DataFrame,
    longshot_lo: float = 0.05,
    longshot_hi: float = 0.15,
    favorite_lo: float = 0.75,
    favorite_hi: float = 0.92,
) -> pd.DataFrame:
    """
    Filter kalshi_odds() output to behavioral-bias price zones (prospect theory).

    Longshot zone (yes_ask $0.05–$0.15): retail traders overprice low-prob events
      → signal fires when NO EV is positive  (buy NO)
    Favorite zone (yes_ask $0.75–$0.92): retail traders underprice high-prob events
      → signal fires when YES EV is positive (buy YES)

    Adds columns:
      pt_zone   : 'longshot' | 'favorite' | None
      pt_side   : 'no'       | 'yes'      | None
      pt_signal : bool — in a bias zone AND the corresponding EV signal fires
    """
    df = df.copy()
    in_long = df['yes_ask'].between(longshot_lo, longshot_hi)
    in_fav  = df['yes_ask'].between(favorite_lo, favorite_hi)

    df['pt_zone'] = None
    df.loc[in_long, 'pt_zone'] = 'longshot'
    df.loc[in_fav,  'pt_zone'] = 'favorite'

    df['pt_side'] = None
    df.loc[in_long, 'pt_side'] = 'no'
    df.loc[in_fav,  'pt_side'] = 'yes'

    # Requires BOTH: being in a bias zone AND the relevant EV signal already firing
    long_sig = in_long & (df['signal_no'] | df['signal_no_cross'])
    fav_sig  = in_fav  & (df['signal']    | df['signal_yes_rest'])
    df['pt_signal'] = long_sig | fav_sig

    return df




