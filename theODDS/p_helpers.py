import os
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import json
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.getcwd()), '.env'))
import warnings

API_KEY = os.getenv('ODDS_API_KEY')
BASE_URL = 'https://api.the-odds-api.com/v4'

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

        print(f'\nRequests used: {resp_odds.headers.get("x-requests-used")} / {resp_odds.headers.get("x-requests-remaining")} remaining')

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

    pinnacle = df[df['bookmaker'] == 'pinnacle'][['home', 'away', 'commence', 'outcome', 'decimal_odds', 'fair_prob', 'vig_pct']].copy()
    pinnacle['commence'] = pinnacle['commence'].dt.tz_convert('America/New_York')
    
    return pinnacle