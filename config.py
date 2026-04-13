"""
Configuration file, mapping out sports leagues to their respective Kalshi codes.

SPORTS = list(str): theODDS group keys (see api_POC.ipynb cell 1. List In-Szn)
SPORTS_CONFIG = dict(dict(event)): dictionaries host mapping of theODDS group keys to KALSHI Event Codes (Manual)
"""

SPORTS = []   

# Pinnacle fetch window
LOOKAHEAD_HRS = 72    # how many hours ahead to search for upcoming events
LIVE          = False # True = fetch currently live games, False = fetch upcoming

SPORTS_CONFIG = {
    # ── American Football ────────────────────────────────────────────
    "americanfootball_ncaaf": {
        "label": "College Football",
        "ticker": "KXNCAAFGAME"      # confirmed
    },
    # ── Aussie Rules ─────────────────────────────────────────────────
    "aussierules_afl": {
        "label": "AFL",
        "ticker": "KXAFLGAME"        # confirmed
    },
    # ── Baseball ─────────────────────────────────────────────────────
    "baseball_mlb": {
        "label": "MLB",
        "ticker": "KXMLBGAME"        # confirmed
    },
    # ── Basketball ───────────────────────────────────────────────────
    "basketball_euroleague": {
        "label": "Basketball Euroleague",
        "ticker": "KXEUROLEAGUEGAME" # confirmed
    },
    "basketball_nba": {
        "label": "NBA",
        "ticker": "KXNBAGAME"        # confirmed
    },
    "basketball_ncaab": {
        "label": "College Basketball (M)",
        "ticker": "KXNCAAMBGAME"     # confirmed
    },
    # ── Boxing / MMA ─────────────────────────────────────────────────
    "boxing_boxing": {
        "label": "Boxing",
        "ticker": "KXBOXING"         # confirmed
    },
    "mma_mixed_martial_arts": {
        "label": "UFC / MMA",
        "ticker": "KXUFCFIGHT"       # confirmed
    },
    # ── Ice Hockey ───────────────────────────────────────────────────
    "icehockey_ahl": {
        "label": "AHL",
        "ticker": "KXAHLGAME"        # confirmed
    },
    "icehockey_nhl": {
        "label": "NHL",
        "ticker": "KXNHLGAME"        # confirmed
    },
    # ── Lacrosse ─────────────────────────────────────────────────────
    "lacrosse_ncaa": {
        "label": "NCAA Men's Lacrosse",
        "ticker": "KXNCAAMLAXGAME"   # confirmed
    },
    # ── Rugby ────────────────────────────────────────────────────────
    "rugbyleague_nrl": {
        "label": "NRL",
        "ticker": "KXRUGBYNRLMATCH"  # confirmed
    },
    # ── Soccer ───────────────────────────────────────────────────────
    "soccer_argentina_primera_division": {
        "label": "Argentina Primera Division",
        "ticker": "KXARGPREMDIVGAME" # unverified
    },
    "soccer_australia_aleague": {
        "label": "A-League",
        "ticker": "KXALEAGUEGAME"    # confirmed
    },
    "soccer_belgium_first_div": {
        "label": "Belgian Pro League",
        "ticker": "KXBELGIANPLGAME"  # confirmed
    },
    "soccer_brazil_campeonato": {
        "label": "Brasileirão Série A",
        "ticker": "KXBRASILEIROGAME" # unverified
    },
    "soccer_conmebol_copa_libertadores": {
        "label": "Copa Libertadores",
        "ticker": "KXCONMEBOLLIBGAME" # confirmed
    },
    "soccer_efl_champ": {
        "label": "EFL Championship",
        "ticker": "KXEFLCHAMPIONSHIPGAME" # confirmed
    },
    "soccer_epl": {
        "label": "English Premier League",
        "ticker": "KXEPLGAME"        # confirmed
    },
    "soccer_fa_cup": {
        "label": "FA Cup",
        "ticker": "KXFACUPGAME"      # confirmed
    },
    "soccer_fifa_world_cup": {
        "label": "FIFA World Cup",
        "ticker": "KXWCGAME"         # confirmed
    },
    "soccer_france_ligue_one": {
        "label": "Ligue 1",
        "ticker": "KXLIGUE1GAME"     # confirmed
    },
    "soccer_germany_bundesliga": {
        "label": "Bundesliga",
        "ticker": "KXBUNDESLIGAGAME" # confirmed
    },
    "soccer_germany_bundesliga2": {
        "label": "Bundesliga 2",
        "ticker": "KXBUNDESLIGA2GAME" # confirmed
    },
    "soccer_germany_dfb_pokal": {
        "label": "DFB Pokal",
        "ticker": "KXDFBPOKALGAME"   # confirmed
    },
    "soccer_greece_super_league": {
        "label": "Super League Greece",
        "ticker": "KXSLGREECEGAME"   # confirmed
    },
    "soccer_italy_serie_a": {
        "label": "Serie A",
        "ticker": "KXSERIEAGAME"     # confirmed
    },
    "soccer_italy_serie_b": {
        "label": "Serie B",
        "ticker": "KXSERIEBGAME"     # confirmed
    },
    "soccer_mexico_ligamx": {
        "label": "Liga MX",
        "ticker": "KXLIGAMXGAME"     # unverified
    },
    "soccer_netherlands_eredivisie": {
        "label": "Eredivisie",
        "ticker": "KXEREDIVISIEGAME" # confirmed
    },
    "soccer_portugal_primeira_liga": {
        "label": "Liga Portugal",
        "ticker": "KXLIGAPORTUGALGAME" # confirmed
    },
    "soccer_saudi_arabia_pro_league": {
        "label": "Saudi Pro League",
        "ticker": "KXSAUDIPLGAME"    # confirmed
    },
    "soccer_spain_copa_del_rey": {
        "label": "Copa del Rey",
        "ticker": "KXCOPADELREYGAME" # confirmed
    },
    "soccer_spain_la_liga": {
        "label": "La Liga",
        "ticker": "KXLALIGAGAME"     # confirmed
    },
    "soccer_spain_segunda_division": {
        "label": "La Liga 2",
        "ticker": "KXLALIGA2GAME"    # confirmed
    },
    "soccer_spl": {
        "label": "Scottish Premiership",
        "ticker": "KXSCOTTISHPREMGAME" # confirmed
    },
    "soccer_turkey_super_league": {
        "label": "Turkish Super Lig",
        "ticker": "KXSUPERLIGGAME"   # confirmed
    },
    "soccer_usa_mls": {
        "label": "MLS",
        "ticker": "KXMLSGAME"        # confirmed
    },
    "soccer_uefa_champs_league": {
        "label": "UEFA Champions League",
        "ticker": "KXUCLGAME"        # confirmed
    },
    "soccer_uefa_europa_conference_league": {
        "label": "UEFA Europa Conference League",
        "ticker": "KXUECLGAME"       # confirmed
    },
    "soccer_uefa_europa_league": {
        "label": "UEFA Europa League",
        "ticker": "KXUELGAME"        # confirmed
    },
}