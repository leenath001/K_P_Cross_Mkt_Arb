import os
import sys
import requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from scipy.optimize import brentq
import json
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from applog import get_logger

log = get_logger(__name__)


def _power_devig(implied_probs: list) -> list:
    """
    Power-method de-vig. Replaces naive proportional removal (fair_prob = r_i /
    sum(r_i)), which is well-documented to systematically OVERSTATE lower-probability
    legs (draws, longshots) relative to favorites — bookmakers load a
    disproportionate share of their margin onto the draw/longshot side, and
    proportional removal assumes the margin is spread uniformly, so it doesn't
    correct for that skew.

    Confirmed empirically on this bot's own trade history (notebooks/win_loss_analysis.ipynb):
    non-draw soccer bets were near-perfectly calibrated (predicted 30.5% win,
    actual 30.2%) while draw bets were predicted at 24.1% and actually won only
    16.1% of the time — proportional de-vig was overpricing the draw leg.

    Method: solve for exponent k such that sum(r_i^k) = 1, then p_i = r_i^k. Since
    each r_i < 1, raising to a power k > 1 shrinks smaller r_i (draws/longshots)
    proportionally MORE than larger r_i (favorites), which is exactly the
    correction needed. Falls back to proportional de-vig if there's no overround
    to correct, a leg has zero probability, or the market isn't 2+ outcomes.
    """
    r = np.array(implied_probs, dtype=float)
    total = r.sum()
    if total <= 1.0 or len(r) < 2 or np.any(r <= 0):
        return (r / total).tolist() if total > 0 else r.tolist()
    try:
        k = brentq(lambda k: np.sum(r ** k) - 1.0, 1.0, 100.0)
    except ValueError:
        log.warning('_power_devig: could not bracket a root for implied_probs=%s — falling back to proportional', implied_probs)
        return (r / total).tolist()
    p = r ** k
    return (p / p.sum()).tolist()   # renormalize for float safety

def _secret(key: str):
    val = os.getenv(key)
    if not val:
        try:
            import streamlit as st
            val = st.secrets.get(key)
        except Exception:
            pass
    return val

API_KEY = _secret('ODDS_API_KEY')
BASE_URL = 'https://api.the-odds-api.com/v4'

# Tracks usage from the most recent API call — read via get_api_usage()
_api_usage:     dict = {'used': 0, 'remaining': 500}
# sport_key -> True/False (None = not yet fetched)
_active_sports: dict = {}

def get_api_usage() -> tuple:
    """Return (requests_used, requests_remaining) from the last Pinnacle call."""
    return _api_usage['used'], _api_usage['remaining']

def get_active_sports() -> dict:
    """Return {sport_key: bool} populated by the most recent fetch_usage() call."""
    return dict(_active_sports)

def fetch_usage() -> tuple:
    """
    Make a live request to /v4/sports to get fresh usage counters from headers.
    Also captures each sport's `active` flag into _active_sports at no extra cost.
    Costs 1 request. Returns (used, remaining).
    """
    try:
        resp = requests.get(f'{BASE_URL}/sports', params={'apiKey': API_KEY})
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        log.exception('fetch_usage failed')
        raise
    used      = int(resp.headers.get('x-requests-used', 0))
    remaining = int(resp.headers.get('x-requests-remaining', 0))
    _api_usage['used']      = used
    _api_usage['remaining'] = remaining
    for entry in resp.json():
        _active_sports[entry['key']] = bool(entry.get('active', False))
    return used, remaining


def check_sports_with_events(sport_keys: list, hrs: int) -> dict:
    """
    For each sport key, query the /v4/sports/{sport}/events endpoint to check
    whether Pinnacle actually has events in the next `hrs` hours.
    Returns {sport_key: event_count}. Costs 1 API credit per sport.
    Sports missing from the response or raising errors get count=0.
    """
    now_utc = datetime.now(timezone.utc)
    soon    = now_utc + timedelta(hours=hrs)
    t_from  = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    t_to    = soon.strftime('%Y-%m-%dT%H:%M:%SZ')

    counts = {}
    for key in sport_keys:
        try:
            resp = requests.get(
                f'{BASE_URL}/sports/{key}/events',
                params={
                    'apiKey':            API_KEY,
                    'commenceTimeFrom':  t_from,
                    'commenceTimeTo':    t_to,
                },
                timeout=10,
            )
            if resp.ok:
                _api_usage['used']      = int(resp.headers.get('x-requests-used',      _api_usage['used']))
                _api_usage['remaining'] = int(resp.headers.get('x-requests-remaining',  _api_usage['remaining']))
                counts[key] = len(resp.json()) if isinstance(resp.json(), list) else 0
            else:
                log.warning('check_sports_with_events: %s -> %s %s', key, resp.status_code, resp.reason)
                counts[key] = 0
        except Exception:
            log.exception('check_sports_with_events failed for %s', key)
            counts[key] = 0
    return counts

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
        try:
            resp_odds = requests.get(f'{BASE_URL}/sports/{sport}/odds', params=params)
            resp_odds.raise_for_status()
        except requests.exceptions.RequestException:
            # One bad/rate-limited sport shouldn't blank out every other sport's odds.
            log.exception('pinnacle_odds: fetch failed for sport %s — skipping', sport)
            continue

        _api_usage['used']      = int(resp_odds.headers.get('x-requests-used', 0))
        _api_usage['remaining'] = int(resp_odds.headers.get('x-requests-remaining', 500))

        events = resp_odds.json()
        if not events:
            log.info('pinnacle_odds: no events for %s', sport)
            continue

        for event in events:
            for book in event.get('bookmakers', []):
                for market in book.get('markets', []):
                    if market['key'] != 'h2h':
                        continue
                    outcomes = market['outcomes']
                    implied = [1 / o['price'] for o in outcomes]
                    overround = sum(implied) - 1
                    fair_probs = _power_devig(implied)
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