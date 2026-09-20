"""
Configuration file, mapping out sports leagues to their respective Kalshi codes.

SPORTS = list(str): theODDS group keys (see api_POC.ipynb cell 1. List In-Szn)
SPORTS_CONFIG = dict(dict(event)): dictionaries host mapping of theODDS group keys to KALSHI Event Codes (Manual)
"""

SPORTS = [
    "basketball_nba",              # NBA playoffs
    "icehockey_nhl",               # NHL playoffs
    "baseball_mlb",                # MLB regular season
    "mma_mixed_martial_arts",      # year-round
    "soccer_uefa_champs_league",   # semifinals
    "soccer_uefa_europa_league",   # semifinals
    "soccer_epl",                  # end of season
    "soccer_italy_serie_a",        # end of season
    "soccer_spain_la_liga",        # end of season
    "soccer_usa_mls",              # active
]

# Month numbers (1=Jan…12=Dec) when each sport typically has games.
# Used in the dashboard to show an in-season indicator per sport.
SEASON_MONTHS = {
    "americanfootball_ncaaf":               [8, 9, 10, 11, 12, 1],
    "americanfootball_nfl":                 [9, 10, 11, 12, 1],
    "baseball_mlb":                         [3, 4, 5, 6, 7, 8, 9, 10],
    "basketball_euroleague":                [10, 11, 12, 1, 2, 3, 4, 5],
    "basketball_nba":                       [10, 11, 12, 1, 2, 3, 4, 5, 6],
    "basketball_ncaab":                     [11, 12, 1, 2, 3, 4],
    "boxing_boxing":                        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "mma_mixed_martial_arts":               [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "icehockey_ahl":                        [10, 11, 12, 1, 2, 3, 4, 5, 6],
    "icehockey_nhl":                        [10, 11, 12, 1, 2, 3, 4, 5, 6],
    "lacrosse_ncaa":                        [2, 3, 4, 5],
    "rugbyleague_nrl":                      [3, 4, 5, 6, 7, 8, 9, 10],
    "soccer_argentina_primera_division":    [2, 3, 4, 5, 6, 8, 9, 10, 11, 12],
    "soccer_australia_aleague":             [10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_belgium_first_div":             [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_brazil_campeonato":             [4, 5, 6, 7, 8, 9, 10, 11, 12],
    "soccer_conmebol_copa_libertadores":    [2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    "soccer_efl_champ":                     [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_epl":                           [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_fa_cup":                        [1, 2, 3, 4, 5],
    "soccer_fifa_world_cup":                [6, 7],  # 2026 World Cup Jun–Jul
    "soccer_france_ligue_one":              [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_germany_bundesliga":            [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_germany_bundesliga2":           [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_germany_dfb_pokal":             [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_greece_super_league":           [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_italy_serie_a":                 [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_italy_serie_b":                 [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_mexico_ligamx":                 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "soccer_netherlands_eredivisie":        [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_portugal_primeira_liga":        [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_saudi_arabia_pro_league":       [9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_spain_copa_del_rey":            [9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_spain_la_liga":                 [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_spain_segunda_division":        [8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6],
    "soccer_spl":                           [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_turkey_super_league":           [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_usa_mls":                       [2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    "soccer_uefa_champs_league":            [9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_uefa_europa_conference_league": [9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_uefa_europa_league":            [9, 10, 11, 12, 1, 2, 3, 4, 5],
    # ── Added from the OddsAPI ↔ Kalshi cross-check (see SPORTS_CONFIG notes) ──
    "basketball_wnba":                      [5, 6, 7, 8, 9, 10],
    "basketball_nbl":                       [9, 10, 11, 12, 1, 2, 3],
    "baseball_kbo":                         [3, 4, 5, 6, 7, 8, 9, 10],
    "baseball_npb":                         [3, 4, 5, 6, 7, 8, 9, 10],
    "icehockey_liiga":                      [9, 10, 11, 12, 1, 2, 3, 4],
    "icehockey_sweden_hockey_league":       [9, 10, 11, 12, 1, 2, 3, 4],
    "lacrosse_pll":                         [6, 7, 8, 9],
    "soccer_brazil_serie_b":                [4, 5, 6, 7, 8, 9, 10, 11],
    "soccer_conmebol_copa_sudamericana":    [3, 4, 5, 6, 7, 8, 9, 10, 11],
    "soccer_denmark_superliga":             [7, 8, 9, 10, 11, 3, 4, 5],
    "soccer_england_efl_cup":               [8, 9, 10, 11, 12, 1, 2, 3],
    "soccer_england_league1":               [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_germany_liga3":                 [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    "soccer_japan_j_league":                [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "soccer_korea_kleague1":                [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "soccer_norway_eliteserien":            [3, 4, 5, 6, 7, 8, 9, 10, 11],
    "soccer_poland_ekstraklasa":            [7, 8, 9, 10, 11, 3, 4, 5, 6],
    "soccer_sweden_allsvenskan":            [4, 5, 6, 7, 8, 9, 10, 11],
    "soccer_switzerland_superleague":       [7, 8, 9, 10, 11, 12, 2, 3, 4, 5],
    "soccer_uefa_nations_league":           [9, 10, 11, 3, 6],
}

# Pinnacle fetch window
LOOKAHEAD_HRS = 72    # how many hours ahead to search for upcoming events
LIVE          = False # True = fetch currently live games, False = fetch upcoming

# Sports temporarily excluded from signal generation (enforced in KALSHI.k_helpers.kalshi_odds).
PAUSED_SPORTS = set()


SPORTS_CONFIG = {
    # ── American Football ────────────────────────────────────────────
    "americanfootball_ncaaf": {
        "label": "College Football",
        "ticker": "KXNCAAFGAME"      # confirmed
    },
    "americanfootball_nfl": {
        "label": "NFL",
        "ticker": "KXNFLGAME"        # confirmed
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

    # ── Added from the OddsAPI ↔ Kalshi cross-check ──────────────────────
    # "# confirmed" = series exists with open markets AND the OddsAPI key returned
    # events that matched Kalshi in a live run; "# unverified" = series exists but
    # the match hasn't been observed yet (no upcoming events at check time).
    "basketball_wnba":     {"label": "WNBA",                  "ticker": "KXWNBAGAME"},          # confirmed (live match 2026-09-19)
    "basketball_nbl":      {"label": "Australia NBL",         "ticker": "KXNBLGAME"},           # unverified — no upcoming events to test the match
    "baseball_kbo":        {"label": "KBO",                   "ticker": "KXKBOGAME"},           # confirmed (live match 2026-09-19)
    "baseball_npb":        {"label": "NPB",                   "ticker": "KXNPBGAME"},           # confirmed (live match 2026-09-19)
    "icehockey_liiga":     {"label": "Liiga",                 "ticker": "KXLIIGAGAME"},         # confirmed (live match 2026-09-19)
    "icehockey_sweden_hockey_league": {"label": "SHL",        "ticker": "KXSHLGAME"},           # confirmed (live match 2026-09-19)
    "lacrosse_pll":        {"label": "PLL",                   "ticker": "KXPLLGAME"},           # unverified — no upcoming events to test the match
    "soccer_brazil_serie_b": {"label": "Brasileirão Série B", "ticker": "KXBRASILEIROBGAME"},   # confirmed (live match 2026-09-19)
    "soccer_conmebol_copa_sudamericana": {"label": "Copa Sudamericana", "ticker": "KXCONMEBOLSUDGAME"},  # unverified — no upcoming events to test the match
    "soccer_denmark_superliga": {"label": "Danish Superliga", "ticker": "KXDENSUPERLIGAGAME"},  # confirmed (live match 2026-09-19)
    "soccer_england_efl_cup": {"label": "EFL Cup",           "ticker": "KXEFLCUPGAME"},        # unverified — no upcoming events to test the match
    "soccer_england_league1": {"label": "EFL League One",    "ticker": "KXEFLL1GAME"},         # unverified — no upcoming events to test the match
    "soccer_germany_liga3": {"label": "3. Liga",             "ticker": "KXGER3LGAME"},         # confirmed (live match 2026-09-19)
    "soccer_japan_j_league": {"label": "J1 League",          "ticker": "KXJLEAGUEGAME"},       # confirmed (live match 2026-09-19)
    "soccer_korea_kleague1": {"label": "K League 1",         "ticker": "KXKLEAGUEGAME"},       # confirmed (live match 2026-09-19)
    "soccer_norway_eliteserien": {"label": "Eliteserien",    "ticker": "KXELITESERIENGAME"},   # confirmed (live match 2026-09-19)
    "soccer_poland_ekstraklasa": {"label": "Ekstraklasa",    "ticker": "KXEKSTRAKLASAGAME"},   # confirmed (live match 2026-09-19)
    "soccer_sweden_allsvenskan": {"label": "Allsvenskan",    "ticker": "KXALLSVENSKANGAME"},   # confirmed (live match 2026-09-19)
    "soccer_switzerland_superleague": {"label": "Swiss Super League", "ticker": "KXSWISSLEAGUEGAME"},  # confirmed (live match 2026-09-19)
    "soccer_uefa_nations_league": {"label": "UEFA Nations League", "ticker": "KXUEFANLGAME"},  # unverified — no upcoming events to test the match
    # ── UNKNOWN: on OddsAPI but no Kalshi game-winner series found (not addable) ──
    # aussierules_aflw, rugbyleague_nrlw, soccer_austria_bundesliga, soccer_england_league2,
    # icehockey_sweden_allsvenskan, soccer_league_of_ireland, soccer_sweden_superettan,
    # handball_germany_bundesliga, soccer_germany_bundesliga_women
}
