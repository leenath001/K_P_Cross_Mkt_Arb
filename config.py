"""
Configuration file, mapping out sports leagues to their respective Kalshi codes.

SPORTS = list(str): theODDS group keys (see api_POC.ipynb cell 1. List In-Szn)
SPORTS_CONFIG = dict(dict(event)): dictionaries host mapping of theODDS group keys to KALSHI Event Codes (Manual)
"""

SPORTS = ["basketball_ncaab"] # "basketball_ncaab","soccer_mexico_ligamx","soccer_argentina_primera_division","soccer_brazil_campeonato"

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
}