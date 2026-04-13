"""
Configuration file, mapping out sports leagues to their respective Kalshi codes.

SPORTS = list(str): theODDS group keys (see api_POC.ipynb cell 1. List In-Szn)
SPORTS_CONFIG = dict(dict(event)): dictionaries host mapping of theODDS group keys to KALSHI Event Codes (Manual)
"""

SPORTS = ["soccer_italy_serie_a", "soccer_uefa_europa_league", "soccer_uefa_champs_league"] 

# Pinnacle fetch window
LOOKAHEAD_HRS = 72    # how many hours ahead to search for upcoming events
LIVE          = False # True = fetch currently live games, False = fetch upcoming

SPORTS_CONFIG = {
    "basketball_ncaab": {
        "label": "College Basketball (M)",
        "ticker": "KXNCAAMBGAME"
    },
    "soccer_mexico_ligamx": {
        "label": "Liga MX",
        "ticker": "KXLIGAMXGAME"
    },
    "soccer_argentina_primera_division": {
        "label": "Argentina Primera Division",
        "ticker": "KXARGPREMDIVGAME"
    },
    "soccer_brazil_serie_a": {      # incorrect 
        "label": "Brasileiro Serie A",
        "ticker": "KXBRASILEIROGAME"
    },
    "soccer_brazil_campeonato": {       # check, incorrect
        "label": "Brazil Campeonato",
        "ticker": "KXBRASILEIROGAME"
    },    
    "basketball_nba": {
        "label": "NBA",
        "ticker": "KXNBAGAME"
    },
    "lacrosse_ncaa": {
        "label": "NCAA Men's Lacrosse",
        "ticker": "KXNCAAMLAXGAME"
    },
    "soccer_italy_serie_a": {
        "label": "Serie A",
        "ticker": "KXSERIEAGAME"
    },
    "soccer_uefa_europa_league": {
        "label": "UEFA Europa League",
        "ticker": "KXUELGAME"
    },
    "soccer_uefa_champs_league": {
        "label": "UEFA Champions League",
        "ticker": "KXUCLGAME"
    },
    "soccer_spain_la_liga": {
        "label": "La Liga",
        "ticker": "KXLALIGAGAME"
    },
    "soccer_italy_serie_b": {
        "label": "Serie B",
        "ticker": "KXSERIEBGAME"
    },
    "soccer_usa_mls": {
        "label": "MLS",
        "ticker": "KXMLSGAME"
    },
}