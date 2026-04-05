"""
Configuration file, mapping out sports leagues to their respective Kalshi codes.

SPORTS = list(str): theODDS group keys (see api_POC.ipynb cell 1. List In-Szn)
SPORTS_CONFIG = dict(dict(event)): dictionaries host mapping of theODDS group keys to KALSHI Event Codes (Manual)
"""

SPORTS = ["soccer_argentina_primera_division", "basketball_ncaab", "soccer_mexico_ligamx", "soccer_brazil_campeonato"] # "basketball_nba", "lacrosse_ncaa",

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
    "soccer_brazil_serie_a": {
        "label": "Brasileiro Serie A",
        "ticker": "KXBRASILEIROGAME"
    },
    "soccer_brazil_campeonato": {
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
}