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

def load_all_mkts(SERIES_TICKER: str):
    resp = requests.get(
        f'{BASE_URL}/markets',
        headers=kalshi_headers('GET', '/trade-api/v2/markets'),
        params={'series_ticker': SERIES_TICKER, 'status': 'open', 'limit': 500})
    resp.raise_for_status()
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

def _taker_ev(fair_prob: float, price: float, taker_fee: float) -> float:
    """EV per contract. Kalshi fee = fee_rate * price * (1-price), charged on entry."""
    return fair_prob - price - taker_fee * price * (1 - price)

def _maker_ev(fair_prob: float, price: float, maker_fee: float) -> float:
    """EV per contract. Kalshi fee = fee_rate * price * (1-price), charged on entry."""
    return fair_prob - price - maker_fee * price * (1 - price)


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
                fees: float = 0.07, maker_fees: float = 0.03,
                max_delta: float = 0.25) -> pd.DataFrame:
    """
    Takes a Pinnacle odds DataFrame and returns a merged DataFrame pairing each
    outcome row with its corresponding Kalshi market's bid/ask prices.

    `fees`       — taker fee rate. signal     = YES EV > 0 at taker rate.
    `maker_fees` — maker fee rate. signal_no  = NO EV > 0 at maker rate.
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

    # 1. Map sports to Kalshi series tickers
    sport_to_series = {}
    for sport in pinnacle_df['sport'].unique():
        if sport in SPORTS_C:
            sport_to_series[sport] = SPORTS_C[sport]['ticker']

    # 2. Load all Kalshi markets, tagging each row with its series ticker
    series_dfs = []
    for series in set(sport_to_series.values()):
        mkt_df = load_all_mkts(series)
        mkt_df['series_ticker'] = series
        series_dfs.append(mkt_df)

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

            yes_bid         = k_row['yes_bid']
            no_bid          = k_row['no_bid']
            rest_price_yes  = round(yes_ask - 0.01, 2) if yes_ask is not None else None
            rest_price_no   = round(no_ask  - 0.01, 2) if no_ask  is not None else None

            fp     = p_row['fair_prob']
            fp_no  = 1 - fp
            MIN_EDGE = 0.01  # require at least 1¢ probability edge above entry price

            # YES cross: taker fills at yes_ask
            signal           = (not mismatched and yes_ask is not None and
                                fp - yes_ask >= MIN_EDGE and
                                _taker_ev(fp, yes_ask, fees) > 0)
            # YES rest: maker posts at yes_ask-1¢ (top of book)
            signal_yes_rest  = (not mismatched and rest_price_yes is not None and
                                fp - rest_price_yes >= MIN_EDGE and
                                _maker_ev(fp, rest_price_yes, maker_fees) > 0)
            # NO cross: taker fills at no_ask
            signal_no_cross  = (not mismatched and no_ask is not None and
                                fp_no - no_ask >= MIN_EDGE and
                                _taker_ev(fp_no, no_ask, fees) > 0)
            # NO rest: maker posts at no_ask-1¢
            signal_no        = (not mismatched and rest_price_no is not None and
                                fp_no - rest_price_no >= MIN_EDGE and
                                _maker_ev(fp_no, rest_price_no, maker_fees) > 0)

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
                'signal':           signal,
                'signal_yes_rest':  signal_yes_rest,
                'signal_no_cross':  signal_no_cross,
                'signal_no':        signal_no,
            })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset='k_ticker')
    return df
    

"""
System Process: 
1) Get odds from theODDS API, turn into list[dict] (add empty flag for ticker found in 3)
2) For each event series, load ALL events into a list
3) Use multithreading, using a thread for each dictionary to find the coresponding ticker
    3a) Store ticker in dict 
4) Present user with option to trade (trading bot)

Considerations: 
- If I buy the 30k requests, set a flag for how often we re-ping the API
"""


# once we've handled datafreame building, we can move to trading bot 
    



