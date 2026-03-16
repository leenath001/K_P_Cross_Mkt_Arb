import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pandas as pd
import config
import json
import base64
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone
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
    params={'series_ticker': SERIES_TICKER, 'status': 'open', 'limit': 100})
    resp.raise_for_status()
    markets =  resp.json().get('markets', [])

    z = pd.DataFrame([{
            'event_ticker': m.get('event_ticker'),
            'ticker':       m['ticker'],
            'title':        m.get('title'),
            'yes_sub_title': m.get('yes_sub_title'),
            'yes_bid':      m.get('yes_bid'),
            'yes_ask':      m.get('yes_ask'),
            'no_bid':       m.get('no_bid'),
            'no_ask':       m.get('no_ask'),
            'volume':       m.get('volume'),
            'open_int':     m.get('open_interest'),
        } for m in markets])
    
    return z   

def kalshi_odds(df: pd.DataFrame):
    """
    This function takes in a df from pinnacle_odds and outputs each event with it's corresponding KALSHI odds and KALSHI ticker.  
    """
    pinnacle_df = df.copy()
    theODDS_series = set(pinnacle_df['sport'])
    kalshi_series = set()
    for item in theODDS_series: 
        kalshi_series.add(SPORTS_C[item]['ticker']) # works

    # load all events for the tickers in kalshi odds 
    for item in kalshi_series: 
        x = load_all_mkts(item) 
        ## need to figure out how we want to do matching. 
    

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
    



