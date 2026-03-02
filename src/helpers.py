import os
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import json
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.getcwd()), '.env'))

API_KEY = os.getenv('ODDS_API_KEY')
BASE_URL = 'https://api.the-odds-api.com/v4'

def pinnacle_odds(sport: str, lookahead_hrs:int):
    global API_KEY, BASE_URL
    now_utc = datetime.now(timezone.utc)
    soon = now_utc + timedelta(hours=lookahead_hrs)
    params = {
    'apiKey': API_KEY,
    'regions': 'us',
    'markets': 'h2h',
    'oddsFormat': 'decimal',
    'bookmakers': 'pinnacle',
    'commenceTimeFrom': now_utc.strftime('%Y-%m-%dT%H:%M:%SZ'),
    'commenceTimeTo':   soon.strftime('%Y-%m-%dT%H:%M:%SZ'),}
    resp_odds = requests.get(f'{BASE_URL}/sports/{sport}/odds', params=params)
    resp_odds.raise_for_status()

    print(f'\nRequests used: {resp_odds.headers.get("x-requests-used")} / {resp_odds.headers.get("x-requests-remaining")} remaining')

    events = resp_odds.json()
    if not events:
        return 'No events returned for this sport right now.'

    rows = []

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
                        'commence': event['commence_time'],
                        'bookmaker': book['key'],
                        'outcome': o['name'],
                        'decimal_odds': o['price'],
                        'implied_prob': round(imp, 4),
                        'vig_pct': round(overround * 100, 3),
                        'fair_prob': round(fair, 4),
                    })

    df = pd.DataFrame(rows)

    if not df.empty: 
        pinnacle = df[df['bookmaker'] == 'pinnacle'][['home', 'away', 'commence', 'outcome', 'decimal_odds', 'fair_prob', 'vig_pct']]

    return pinnacle.to_string(index=False)