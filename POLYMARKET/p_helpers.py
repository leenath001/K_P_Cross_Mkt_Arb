"""
POLYMARKET/p_helpers.py — Polymarket signal generation using Pinnacle fair probs.

Architecture mirrors k_helpers.py / theODDS/p_helpers.py:
  1. Fetch active game-winner markets from Polymarket Gamma API (public, no auth).
  2. Fetch best bid/ask for each token from CLOB API (public, no auth).
  3. Fuzzy-match events to a Pinnacle odds DataFrame (from theODDS/p_helpers.pinnacle_odds).
  4. Compute taker EV (3% fee) and maker EV (0% fee) per matched outcome.
  5. Return a signals DataFrame, same schema as k_helpers.kalshi_odds output.

Order placement requires a Polygon wallet — see poly_trade.py (TBD).

Usage:
    from theODDS.p_helpers import pinnacle_odds
    from POLYMARKET.p_helpers import polymarket_signals

    pin_df = pinnacle_odds(sports=['basketball_nba'], hrs=72)
    sigs   = polymarket_signals(pin_df, sports=['basketball_nba'])
    print(sigs[sigs['taker_signal']][['poly_title','outcome','fair_prob','poly_ask','taker_ev']])
"""

import os, sys, requests, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import Optional

GAMMA_URL = 'https://gamma-api.polymarket.com'
CLOB_URL  = 'https://clob.polymarket.com'

TAKER_FEE = 0.03   # sports taker fee (charged on profit only, standard formula)
MAKER_FEE = 0.0    # polymarket maker fee is zero

MIN_TAKER_EV = 0.005   # minimum EV to flag as a taker signal

# theODDS sport key → Polymarket series_id (from /sports endpoint)
POLY_SPORT_MAP: dict[str, int] = {
    'basketball_nba':               10345,
    'icehockey_nhl':                10346,
    'baseball_mlb':                 3,
    'mma_mixed_martial_arts':       10500,   # UFC
    'soccer_epl':                   10188,
    'soccer_uefa_champs_league':    10204,
    'soccer_uefa_europa_league':    10209,
    'soccer_spain_la_liga':         10193,
    'soccer_usa_mls':               10189,
    'soccer_germany_bundesliga':    10194,
    'soccer_france_ligue_one':      10195,
    'soccer_italy_serie_a':         10863,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DRAW_ALIASES = {'draw', 'tie', 'draw/tie', 'x'}

def _normalize(name: str) -> str:
    if name.strip().lower() in _DRAW_ALIASES:
        return '__DRAW__'
    return re.sub(r'[^a-z0-9 ]', '', name.strip().lower())


def _best_match(query: str, candidates: list[str]) -> tuple[int, float]:
    """Fuzzy-match query against a list of strings. Returns (index, score)."""
    norm_q = _normalize(query)
    best_i, best_s = -1, 0.0
    for i, val in enumerate(candidates):
        norm_v = _normalize(str(val))
        score  = SequenceMatcher(None, norm_q, norm_v).ratio()
        # Boost when one name is a subset of the other (e.g. "Thunder" vs "Oklahoma City Thunder")
        qw, vw = set(norm_q.split()), set(norm_v.split())
        if qw and vw and (qw.issubset(vw) or vw.issubset(qw)):
            score = max(score, 0.90)
        if score > best_s:
            best_s, best_i = score, i
    return best_i, best_s


def _ev(fair_prob: float, price: float, fee_rate: float) -> float:
    """
    EV per dollar risked for a binary prediction market position.
    price is 0-1 (Polymarket native format, NOT cents).
    """
    return fair_prob * (1 - price) * (1 - fee_rate) - (1 - fair_prob) * price


def _rest_price(bid: Optional[float], ask: float) -> float:
    """
    Spread-aware maker rest price (0-1).
    Mirrors bot.py _rest_price_cents logic — tighten a 2-tick spread to 1-tick.
    """
    if bid is not None:
        spread_ticks = round((ask - bid) / 0.01)
        if spread_ticks == 2:
            return round(ask - 0.01, 2)
        if spread_ticks == 1:
            return round(bid, 2)
    return round(ask - 0.01, 2)


# ---------------------------------------------------------------------------
# Gamma API — market discovery
# ---------------------------------------------------------------------------

def get_game_markets(series_ids: list[int], hrs: int = 72) -> list[dict]:
    """
    Fetch active game-winner markets from Gamma API for the given Polymarket series IDs.
    Returns a list of flat dicts, one per matched YES/NO token pair:

      poly_title     — event title e.g. "Spurs vs. Thunder"
      condition_id   — Polymarket condition ID
      yes_token      — clobTokenIds[0]  (YES = team1 wins)
      no_token       — clobTokenIds[1]  (NO  = team2 wins)
      team1          — first  team name parsed from title
      team2          — second team name parsed from title
      yes_bid        — best bid from Gamma (YES token)
      yes_ask        — best ask from Gamma (YES token)
      game_start     — ISO8601 string
      series_id      — source series_id
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hrs)
    results = []

    for sid in series_ids:
        try:
            resp = requests.get(
                f'{GAMMA_URL}/events',
                params={'series_id': sid, 'active': 'true', 'closed': 'false', 'limit': 100},
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f'[poly] Gamma fetch failed for series_id={sid}: {exc}')
            continue

        for event in resp.json():
            title = event.get('title', '')

            for mkt in event.get('markets', []):
                question = mkt.get('question', '')

                # Keep only the main game-winner market (question == event title)
                if question.strip() != title.strip():
                    continue
                if not mkt.get('active') or mkt.get('closed'):
                    continue
                if not mkt.get('enableOrderBook'):
                    continue

                # Use gameStartTime (actual tip-off) for time filtering
                game_start_raw = mkt.get('gameStartTime') or event.get('startDate', '')
                try:
                    game_start_dt = datetime.fromisoformat(
                        str(game_start_raw).replace('Z', '+00:00').replace(' ', 'T')
                    )
                    if not game_start_dt.tzinfo:
                        game_start_dt = game_start_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    game_start_dt = None

                if game_start_dt and game_start_dt > cutoff:
                    continue

                token_ids = mkt.get('clobTokenIds')
                if not token_ids:
                    continue
                try:
                    tokens = (
                        token_ids if isinstance(token_ids, list)
                        else __import__('json').loads(token_ids)
                    )
                except Exception:
                    continue
                if len(tokens) < 2:
                    continue

                # Parse team names from title ("Team1 vs. Team2")
                parts = re.split(r'\s+vs\.?\s+', title, maxsplit=1, flags=re.IGNORECASE)
                if len(parts) != 2:
                    continue

                results.append({
                    'poly_title':    title,
                    'condition_id':  mkt.get('conditionId', ''),
                    'yes_token':     tokens[0],
                    'no_token':      tokens[1],
                    'team1':         parts[0].strip(),
                    'team2':         parts[1].strip(),
                    'yes_bid':       mkt.get('bestBid'),
                    'yes_ask':       mkt.get('bestAsk'),
                    'game_start':    str(game_start_raw),
                    'game_start_dt': game_start_dt,
                    'series_id':     sid,
                })

    return results


def get_clob_prices(token_id: str) -> dict:
    """
    Fetch best bid/ask for a token from the CLOB orderbook.
    Returns {'bid': float|None, 'ask': float|None, 'last': float|None}.
    """
    try:
        resp = requests.get(f'{CLOB_URL}/book', params={'token_id': token_id}, timeout=8)
        if not resp.ok:
            return {}
        data = resp.json()
        bid = float(data['bids'][0]['price']) if data.get('bids') else None
        ask = float(data['asks'][0]['price']) if data.get('asks') else None
        return {
            'bid':  bid,
            'ask':  ask,
            'last': float(data['last_trade_price']) if data.get('last_trade_price') else None,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Signal generation — mirrors k_helpers.kalshi_odds
# ---------------------------------------------------------------------------

def polymarket_signals(
    pinnacle_df: pd.DataFrame,
    sports: Optional[list[str]] = None,
    hrs: int = 72,
    taker_fee: float = TAKER_FEE,
    maker_fee: float = MAKER_FEE,
    match_threshold: float = 0.70,
    fetch_clob: bool = False,
) -> pd.DataFrame:
    """
    Cross-reference Pinnacle fair probs against live Polymarket game-winner markets.

    Parameters
    ----------
    pinnacle_df     : output of theODDS.p_helpers.pinnacle_odds()
    sports          : list of theODDS sport keys to scan; None = all in POLY_SPORT_MAP
    hrs             : look-ahead window (hours) for game start
    taker_fee       : Polymarket taker fee rate (default 0.03 for sports)
    maker_fee       : Polymarket maker fee rate (default 0.00)
    match_threshold : minimum fuzzy-match score to accept a team match (0-1)
    fetch_clob      : if True, re-fetch bid/ask from CLOB instead of using Gamma's bestBid/Ask
                      (more accurate but doubles API calls)

    Returns
    -------
    DataFrame with one row per matched Pinnacle outcome, columns:
      sport, poly_title, team1, team2,
      outcome (Pinnacle name), fair_prob,
      token_id, token_side ('yes'|'no'),
      poly_ask, poly_bid, rest_price,
      edge, taker_ev, maker_ev,
      taker_signal (bool), maker_signal (bool),
      commence (Pinnacle), game_start (Polymarket),
      condition_id, event_id (Pinnacle)
    """
    if pinnacle_df is None or pinnacle_df.empty:
        return pd.DataFrame()

    target_sports = [s for s in (sports or list(POLY_SPORT_MAP.keys()))
                     if s in POLY_SPORT_MAP]
    if not target_sports:
        return pd.DataFrame()

    series_ids = list({POLY_SPORT_MAP[s] for s in target_sports})
    pin_df     = pinnacle_df[pinnacle_df['sport'].isin(target_sports)].copy()
    if pin_df.empty:
        return pd.DataFrame()

    poly_markets = get_game_markets(series_ids, hrs=hrs)
    if not poly_markets:
        print('[poly] No active game-winner markets found.')
        return pd.DataFrame()

    rows = []

    for mkt in poly_markets:
        team1 = mkt['team1']
        team2 = mkt['team2']

        poly_start = mkt.get('game_start_dt')

        # Find Pinnacle events within 12 hours of this game start
        if poly_start is not None:
            # Ensure Pinnacle commence is tz-aware for comparison
            pin_commence = pd.to_datetime(pin_df['commence'], utc=True)
            window_lo = poly_start - timedelta(hours=12)
            window_hi = poly_start + timedelta(hours=12)
            pin_window = pin_df[
                (pin_commence >= window_lo) &
                (pin_commence <= window_hi)
            ]
        else:
            pin_window = pin_df

        if pin_window.empty:
            continue

        # Unique outcomes in this time window for candidate matching
        candidates = pin_window['outcome'].tolist()

        # Match team1 to a Pinnacle outcome
        i1, s1 = _best_match(team1, candidates)
        # Match team2 to a Pinnacle outcome
        i2, s2 = _best_match(team2, candidates)

        if max(s1, s2) < match_threshold:
            continue

        matched_rows = []

        _ya = mkt['yes_ask']
        _yb = mkt['yes_bid']
        # NO token prices are complementary: NO bid ≈ 1-YES ask, NO ask ≈ 1-YES bid
        _no_bid = (1 - _ya)       if _ya is not None else None
        _no_ask = (1 - (_yb or 0)) if _yb is not None else None

        for team_name, match_idx, match_score, token_id, token_side, t_bid, t_ask in [
            (team1, i1, s1, mkt['yes_token'], 'yes', _yb,    _ya),
            (team2, i2, s2, mkt['no_token'],  'no',  _no_bid, _no_ask),
        ]:
            if match_score < match_threshold:
                continue

            pin_row = pin_window.iloc[match_idx]
            outcome    = pin_row['outcome']
            fair_prob  = float(pin_row['fair_prob'])
            commence   = pin_row['commence']
            event_id   = pin_row.get('event_id', '')

            # Optionally re-fetch live CLOB prices for this token
            if fetch_clob:
                clob = get_clob_prices(token_id)
                bid = clob.get('bid')
                ask = clob.get('ask')
            else:
                bid = t_bid
                ask = t_ask

            if ask is None:
                continue

            ask = float(ask)
            bid = float(bid) if bid is not None else None

            rest  = _rest_price(bid, ask)
            t_ev  = _ev(fair_prob, ask,  taker_fee)
            m_ev  = _ev(fair_prob, rest, maker_fee)
            edge  = round(fair_prob - ask, 4)

            matched_rows.append({
                'sport':          pin_row['sport'],
                'poly_title':     mkt['poly_title'],
                'team1':          team1,
                'team2':          team2,
                'outcome':        outcome,
                'fair_prob':      round(fair_prob, 4),
                'token_id':       token_id,
                'token_side':     token_side,
                'poly_ask':       round(ask, 4),
                'poly_bid':       round(bid, 4) if bid is not None else None,
                'rest_price':     round(rest, 4),
                'edge':           edge,
                'taker_ev':       round(t_ev, 4),
                'maker_ev':       round(m_ev, 4),
                'taker_signal':   t_ev >= MIN_TAKER_EV,
                'maker_signal':   m_ev > 0,
                'match_score':    round(match_score, 3),
                'commence':       commence,
                'game_start':     mkt['game_start'],
                'condition_id':   mkt['condition_id'],
                'event_id':       str(event_id),
            })

        rows.extend(matched_rows)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values('taker_ev', ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Entry point — quick scan
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from theODDS.p_helpers import pinnacle_odds
    import config

    print('Fetching Pinnacle odds...')
    pin = pinnacle_odds(sports=config.SPORTS, hrs=72)
    print(f'  {len(pin)} Pinnacle outcomes across {pin["sport"].nunique()} sports')

    print('Fetching Polymarket signals...')
    sigs = polymarket_signals(pin, sports=config.SPORTS, hrs=72)

    if sigs.empty:
        print('No matched markets found.')
    else:
        print(f'\n{len(sigs)} matched outcomes  '
              f'({sigs["taker_signal"].sum()} taker signals, '
              f'{sigs["maker_signal"].sum()} maker signals)\n')

        cols = ['sport', 'poly_title', 'outcome', 'fair_prob',
                'poly_ask', 'poly_bid', 'rest_price', 'edge',
                'taker_ev', 'maker_ev', 'taker_signal', 'maker_signal']
        print(sigs[cols].to_string(index=False))
