import os
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import json
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))
import warnings

API_KEY = os.getenv('ODDS_API_KEY')
BASE_URL = 'https://api.the-odds-api.com/v4'

# Tracks usage from the most recent API call — read via get_api_usage()
_api_usage: dict[str, int] = {'used': 0, 'remaining': 500}

def get_api_usage() -> tuple[int, int]:
    """Return (requests_used, requests_remaining) from the last Pinnacle call."""
    return _api_usage['used'], _api_usage['remaining']

def pinnacle_odds(sports: list[str], hrs:int, live: bool = False) -> pd.DataFrame:
    """
    sport: americanfootball_ncaaf, basketball_nba, basketball_ncaab (see 1. List In-Season Sports)
    """
    global API_KEY, BASE_URL
    now_utc = datetime.now(timezone.utc)
    soon = now_utc + timedelta(hours=hrs)
    past = now_utc - timedelta(hours=hrs)
    m = { True: (past.strftime('%Y-%m-%dT%H:%M:%SZ'), now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')),
          False: (now_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), soon.strftime('%Y-%m-%dT%H:%M:%SZ')),}
    x1, x2 = m[live]

    rows = []

    for sport in sports:
        params = {
        'apiKey': API_KEY,
        'regions': 'us',
        'markets': 'h2h',
        'oddsFormat': 'decimal',
        'bookmakers': 'pinnacle',
        'commenceTimeFrom': x1,
        'commenceTimeTo':   x2,}
        resp_odds = requests.get(f'{BASE_URL}/sports/{sport}/odds', params=params)
        resp_odds.raise_for_status()

        _api_usage['used']      = int(resp_odds.headers.get('x-requests-used', 0))
        _api_usage['remaining'] = int(resp_odds.headers.get('x-requests-remaining', 500))

        events = resp_odds.json()
        if not events:
            print(f"No events for: {sport}")
            continue

        for event in events:
            for book in event.get('bookmakers', []):
                for market in book.get('markets', []):
                    if market['key'] != 'h2h':
                        continue
                    outcomes = market['outcomes']
                    implied = [1 / o['price'] for o in outcomes]
                    overround = sum(implied) - 1
                    fair_probs = [p / sum(implied) for p in implied]
                    for o, imp, fair in zip(outcomes, implied, fair_probs):
                        rows.append({
                            'sport': str(sport),
                            'event_id': event['id'],
                            'home': event['home_team'],
                            'away': event['away_team'],
                            'commence': pd.to_datetime(event['commence_time'], utc=True),
                            'bookmaker': book['key'],
                            'outcome': o['name'],
                            'decimal_odds': o['price'],
                            'implied_prob': round(imp, 4),
                            'vig_pct': round(overround * 100, 3),
                            'fair_prob': round(fair, 4),
                        })

    df = pd.DataFrame(rows)

    if df.empty or df[df['bookmaker'] == 'pinnacle'].empty:
        raise ValueError("No events found. Pinnacle odds may not be live.")

    df['commence'] = df['commence'].dt.tz_convert('America/New_York')
    
    return df