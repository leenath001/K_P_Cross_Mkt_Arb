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

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.getcwd()), '.env'))

# -- Global Variables -------
SPORTS = config.SPORTS
SPORTS_C = config.SPORTS_CONFIG
KALSHI_KEY_ID   = os.getenv('KALSHI_KEY_ID')
KALSHI_KEY_PATH = os.getenv('KALSHI_PRIVATE_KEY_PATH')
BASE_URL        = 'https://api.elections.kalshi.com/trade-api/v2'
PATH = '/trade-api/v2/markets'

assert KALSHI_KEY_PATH

_pem_path = os.path.join(os.path.dirname(os.getcwd()), KALSHI_KEY_PATH)
with open(_pem_path, 'rb') as f:
    _private_key = serialization.load_pem_private_key(f.read(), password=None)
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
        'KALSHI-ACCESS-KEY':       KALSHI_KEY_ID,
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

def kalshi_odds(df: pd.DataFrame, threshold: float = 0.6) -> pd.DataFrame:
    """
    Takes a Pinnacle odds DataFrame and returns a merged DataFrame pairing each
    outcome row with its corresponding Kalshi market's bid/ask prices.
    """
    pinnacle_df = df.copy()

    # 1. Map sports to Kalshi series tickers
    kalshi_series = set()
    for sport in pinnacle_df['sport'].unique():
        if sport in SPORTS_C:
            kalshi_series.add(SPORTS_C[sport]['ticker'])

    # 2. Load all Kalshi markets for those series
    all_k = pd.concat([load_all_mkts(s) for s in kalshi_series], axis=0, ignore_index=True)

    # 3. Parse dates from event_ticker
    all_k['k_date'] = all_k['event_ticker'].apply(_parse_ticker_date)

    # 4. Match each Pinnacle outcome to a Kalshi market
    rows = []
    for event_id, group in pinnacle_df.groupby('event_id'):
        game_date = group['commence'].iloc[0].date()
        day_k = all_k[all_k['k_date'] == game_date]
        if day_k.empty:
            continue

        for _, p_row in group.iterrows():
            idx, score = _best_match(p_row['outcome'], day_k['yes_sub_title'])
            if score < threshold:
                continue
            k_row = day_k.loc[idx]
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
                'yes_ask':        k_row['yes_ask'],
                'no_bid':         k_row['no_bid'],
                'no_ask':         k_row['no_ask'],
                'match_score':    round(score, 3),
            })

    return pd.DataFrame(rows)

    

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
    



