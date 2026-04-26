# K/P Cross-Market Arbitrage

A sports betting **cross-market arbitrage system** that identifies pricing discrepancies between **Pinnacle** (traditional sportsbook) and **Kalshi** (regulated US prediction market). Pinnacle's sharp odds are used as a fair-value benchmark; a trade fires when Kalshi's ask falls below that fair value after accounting for fees.

**Core constraint:** Kalshi contracts cannot be short-sold — the system only buys underpriced lines, never sells overpriced ones.

---

## Strategy Logic

```
1. Fetch Pinnacle odds via The Odds API
2. Strip the vig → fair probabilities
3. Fetch open Kalshi markets for matching sports series
4. Fuzzy-match outcomes by date + team name
5. Generate signals: fair_prob vs Kalshi top-of-book ask
6. Place orders: REST at ask−1¢ (maker) or CROSS at ask (taker)
7. Monitor: re-ping Pinnacle every 2 min; cancel if edge flips negative
8. Cancel if unfilled after 30 min or event starts in < 5 min
9. Size with partial Kelly (ROI-scaled 10–50%)
```

### Signal Model

Four independent signal types are computed for every matched outcome:

| Signal | Entry price | Fee | Fires when |
|---|---|---|---|
| `signal` (YES cross) | `yes_ask` | 7% taker | `fair_prob − yes_ask ≥ 0.01` and taker EV > 0 |
| `signal_yes_rest` (YES rest) | `yes_ask − 0.01` | 3% maker | `fair_prob − yes_ask ≥ 0` and maker EV > 0 |
| `signal_no_cross` (NO cross) | `no_ask` | 7% taker | `(1−fair_prob) − no_ask ≥ 0.01` and taker EV > 0 |
| `signal_no` (NO rest) | `no_ask − 0.01` | 3% maker | `(1−fair_prob) − no_ask ≥ 0` and maker EV > 0 |

**REST = top of book:** resting orders are placed at `ask − 1¢` with `post_only=True`, which is the highest passive buy price without crossing. A live price re-fetch happens immediately before each order is placed so the price is always current.

**Binary equivalence:** for a game A vs B, the NO signal on B's row (`1−fair_B = fair_A` vs `no_ask_B`) is mathematically equivalent to a YES bet on A winning via B's market. Both are generated automatically.

**EV formula** (same for all four types):
```
EV = fair_prob × (1 − entry_price) × (1 − fee_rate) − (1 − fair_prob) × entry_price
```

### Kelly Sizing

```
full_kelly  = EV / win_amount          where win_amount = (1−price)×(1−fee_rate)
ROI         = EV / price               (return per dollar at risk)
partial     = 10% if ROI < 2%
              20% if ROI 2–5%
              33% if ROI 5–10%
              50% if ROI > 10%
contracts   = floor(bankroll × full_kelly × partial / price)  [min 1]
final       = max(1, round(contracts × size_mult))
```

**Size multiplier (`--size` / web app "Size ×"):** scales the Kelly output after it's computed. `2.0` doubles every position, `0.5` halves them. If the multiplied contract count would cost more than the available bankroll, the order is **skipped** — no partial fill, no crash, just a clean skip with `reason='insufficient_cash'`.

---

## Repository Structure

```
K_P_Cross_Mkt_Arb/
├── README.md
├── requirements.txt
├── config.py                    # sport → Kalshi series mapping, global settings
├── .env                         # API credentials (git-ignored)
├── KALSHI/
│   └── k_helpers.py             # Kalshi auth, market fetch, signal computation
├── theODDS/
│   └── p_helpers.py             # Pinnacle odds via The Odds API
└── trade/
    ├── web_app.py               # Streamlit web UI (signal viewer + trade executor)
    ├── quickstart.py            # CLI entry point (dry run + live)
    ├── bot.py                   # Order placement, monitor loop, Kelly sizer
    ├── dashboard.py             # Rich terminal dashboard + Streamlit state store
    ├── logger.py                # Appends one row to logs/trades.csv per trade
    ├── settle.py                # Auto-fills WIN/LOSS by querying Kalshi results
    ├── review.py                # Rich table + matplotlib edge realization charts
    ├── nothing.py               # Standalone bot for non-sports Kalshi markets
    ├── nothing_config.py        # Series list for the nothing bot
    ├── nothing_review.py        # Charts for nothing bot trade log
    ├── cancel_all.py            # Emergency cancel all open orders
    └── logs/
        ├── trades.csv           # K/P arb trade log
        └── no_trades.csv        # Nothing bot trade log
```

---

## Usage

### Web App (recommended)

```bash
streamlit run trade/web_app.py
```

Opens a browser UI with four tabs:

- **Trade** — fetch signals, select YES/NO side + order mode, review and execute
- **Review** — edge realization charts from `trades.csv`
- **Settle** — auto-settle pending trades against Kalshi results
- **Nothing** — standalone interface for non-sports Kalshi markets

### CLI

```bash
# Check API budget (costs 1 request)
python trade/quickstart.py --usage

# Dry run — show signals + Kelly sizing, no orders placed
python trade/quickstart.py

# Live trading
python trade/quickstart.py --live

# Settle results after events resolve
python trade/settle.py --dry-run        # preview
python trade/settle.py                  # write WIN/LOSS to trades.csv

# Review edge realization
python trade/review.py                  # table + charts
python trade/review.py --table          # table only

# Emergency cancel all open orders
python trade/cancel_all.py
```

**`quickstart.py` flags:**

| Flag | Default | Description |
|---|---|---|
| `--usage` | — | Print API usage and exit |
| `--live` | off | Place real orders |
| `--bankroll <$>` | Kalshi balance | Override balance |
| `--hrs <n>` | `config.LOOKAHEAD_HRS` | Look-ahead window in hours |
| `--taker-fee <f>` | 0.07 | Taker fee rate (fraction of winnings) |
| `--maker-fee <f>` | 0.03 | Maker fee rate (fraction of winnings) |
| `--limit-only` | off | REST only — never cross the book |
| `--force-cross` | off | CROSS only — never rest |
| `--threshold <f>` | 0.85 | Min fuzzy-match score (0–1) |
| `--side yes/no` | yes | Trade YES or NO contracts |
| `--size <f>` | 1.0 | Size multiplier on Kelly contracts (e.g. `2.0` = double; order skipped if cost exceeds balance) |

---

## Module Reference

### `theODDS/p_helpers.py`

#### `pinnacle_odds(sports, hrs, live=False) → pd.DataFrame`

Fetches H2H odds from Pinnacle via The Odds API, strips the vig, and returns fair probabilities.

**Parameters:**
- `sports` — list of sport keys (e.g. `['basketball_nba', 'soccer_brazil_campeonato']`)
- `hrs` — look-ahead window in hours; look-back window if `live=True`
- `live` — if True, fetches recently started games instead of upcoming ones

**Returns columns:** `sport`, `event_id`, `home`, `away`, `commence`, `bookmaker`, `outcome`, `decimal_odds`, `implied_prob`, `fair_prob`, `vig_pct`

**Vig removal:**
```
implied_prob = 1 / decimal_odds
fair_prob    = implied_prob / sum(implied_probs_for_event)
```

#### `fetch_usage() → (used, remaining)`

Makes one live request to `/v4/sports` to read fresh API usage counters from response headers. Also populates `_active_sports`. Costs 1 request.

#### `check_sports_with_events(sport_keys, hrs) → dict[str, int]`

For each sport key, queries `/v4/sports/{sport}/events` with a time window to count actual upcoming events. Returns `{sport_key: event_count}`. More reliable than the `active` flag from `/v4/sports`. Costs 1 request per sport.

#### `get_api_usage() → (used, remaining)`

Returns usage counters cached from the most recent API call. No network request.

---

### `KALSHI/k_helpers.py`

#### `kalshi_headers(method, path) → dict`

Generates Kalshi RSA-PSS authentication headers for a single request.

**Parameters:**
- `method` — HTTP method string (`'GET'`, `'POST'`)
- `path` — API path string (e.g. `'/trade-api/v2/markets'`)

Signs `timestamp_ms + METHOD + path` with the RSA private key loaded from `API_PRIVATE` env var (base64-encoded DER). Returns `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.

#### `load_all_mkts(SERIES_TICKER) → pd.DataFrame`

Fetches all open markets for one Kalshi series.

**Parameters:**
- `SERIES_TICKER` — Kalshi series identifier (e.g. `'KXNBAGAME'`)

**Returns columns:** `event_ticker`, `ticker`, `title`, `yes_sub_title`, `no_sub_title`, `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `volume`, `open_int`

#### `kalshi_odds(df, threshold=0.6, fees=0.07, maker_fees=0.03, max_delta=0.25) → pd.DataFrame`

Core matching and signal function. Takes a Pinnacle odds DataFrame and returns one row per matched outcome with Kalshi prices and four signal columns.

**Parameters:**
- `df` — Pinnacle odds DataFrame from `pinnacle_odds()`
- `threshold` — minimum fuzzy-match score to accept a match (0–1); default 0.6, CLI uses 0.85
- `fees` — taker fee rate used for YES/NO cross signal EV check
- `maker_fees` — maker fee rate used for YES/NO rest signal EV check
- `max_delta` — drop signals where `|fair_prob − yes_ask|` or `|(1−fair_prob) − no_ask|` exceeds this; guards against fuzzy-match failures masquerading as edge. Set to 1.0 to disable.

**Matching algorithm:**
1. Map each Pinnacle sport to its Kalshi series ticker via `config.SPORTS_CONFIG`
2. Load all open Kalshi markets for each series
3. Parse game date from Kalshi event ticker (`{PREFIX}-{yy}{MON}{dd}-...`)
4. For each Pinnacle event, filter Kalshi markets by exact date match
5. **Event-level lock** — every outcome in the game must fuzzy-match a market under the *same* Kalshi `event_ticker`; if any leg fails, the whole game is dropped. Prevents one outcome matching the wrong game on the same day.
6. For each matched outcome, compute all four signal columns (see Signal Model above)

**Returns columns:** `sport`, `event_id`, `home`, `away`, `commence`, `outcome`, `fair_prob`, `k_event_ticker`, `k_ticker`, `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `volume`, `OI`, `match_score`, `price_delta`, `mismatched`, `signal`, `signal_yes_rest`, `signal_no_cross`, `signal_no`

---

### `trade/bot.py`

#### `_ev(fair_prob, price, fee_rate) → float`

Expected value per contract. `fair_prob × (1−price) × (1−fee_rate) − (1−fair_prob) × price`

#### `kelly_contracts(fair_prob, price, bankroll, fee_rate) → int`

Partial Kelly contract count. Returns 0 if EV ≤ 0, otherwise at least 1.

**Parameters:**
- `fair_prob` — fair probability for the side being traded
- `price` — entry price in dollars (0–1)
- `bankroll` — available capital in dollars
- `fee_rate` — taker or maker fee rate

#### `place_order(ticker, price_cents, count, side='yes', expiration_ts=None, post_only=False) → dict`

Places a Kalshi limit buy order and returns the raw order dict.

**Parameters:**
- `ticker` — Kalshi market ticker
- `price_cents` — entry price in cents (integer, 1–99)
- `count` — number of contracts
- `side` — `'yes'` or `'no'`; NO orders use `yes_price = 100 − price_cents` internally
- `expiration_ts` — Unix timestamp for GTC expiry (order auto-cancels at this time)
- `post_only` — if True, Kalshi rejects the order if it would cross (guarantees maker fee)

Raises `requests.HTTPError` on non-2xx responses.

#### `get_market_price(ticker) → int | None`

Fetches current `yes_ask` for a market in cents. Returns None on failure.

#### `get_market_prices(ticker) → dict`

Fetches both `yes_ask` and `no_ask` for a market in cents. Returns `{'yes_ask': int, 'no_ask': int}` or `{}` on failure.

#### `run_trade(signal_row, bankroll, taker_fee=0.07, maker_fee=0.03, limit_only=False, force_cross=False, side='yes', max_duration=1800, pre_event_buffer=300, dashboard=None, stop_event=None, order_registry=None) → dict`

Executes one trade end-to-end for a single signal row.

**Parameters:**
- `signal_row` — one row from `kalshi_odds()` output as a pandas Series
- `bankroll` — capital available for this trade in dollars
- `taker_fee` — taker fee rate (default 0.07 = 7%)
- `maker_fee` — maker fee rate (default 0.03 = 3%)
- `limit_only` — REST only; skip if taker EV would fire first
- `force_cross` — CROSS only; skip if no taker EV
- `side` — `'yes'` or `'no'`
- `max_duration` — hard order lifetime cap in seconds (default 1800 = 30 min)
- `pre_event_buffer` — cancel this many seconds before game starts (default 300 = 5 min)
- `dashboard` — `Dashboard` or `StreamlitDashboard` instance; None for headless
- `stop_event` — `threading.Event` shared across all trades; set to cancel all
- `order_registry` — list to which placed order IDs are appended (for cleanup)

**Order decision logic (same structure for YES and NO):**
```
if force_cross:
    cross at ask if taker EV > 0, else skip
elif AUTO and taker EV > 0:
    cross at ask
elif maker EV > 0 at ask−1¢:
    rest at ask−1¢ (post_only=True)
else:
    skip
```

A live price re-fetch (`get_market_prices`) happens immediately before placing so `post_only` orders never use stale prices.

**Monitor loop:** polls Kalshi every 10 s and Pinnacle every 2 min. Cancels on signal flip (EV gone negative), 30-min cap, event imminent, or `stop_event`.

#### `run_all_signals(signals_df, bankroll, taker_fee=0.07, maker_fee=0.03, limit_only=False, force_cross=False, side='yes', max_duration=1800, dashboard=None, stop_event=None) → list[dict]`

Runs trades in parallel, batched to avoid Kalshi rate limits.

**Parameters:** same as `run_trade` except takes a full DataFrame.

**Behavior:**
- Deduplicates on `k_ticker` — each Kalshi market is entered at most once
- Skips tickers with existing open/pending positions (`already_bet_tickers()`)
- Sends trades in **batches of 10** concurrently; waits for each batch to complete before starting the next
- Ctrl+C cancels all open orders cleanly

#### `already_bet_tickers() → set[str]`

Reads both `trades.csv` and `no_trades.csv` and returns the set of tickers that have a `PENDING` result with an active order status (`resting`, `executed`, or `filled`). Used to block re-trading the same market within a run.

---

### `trade/logger.py`

Appends one CSV row per placed order. Skipped orders are not logged.

**Fields:** `logged_at`, `order_id`, `sport`, `outcome`, `k_ticker`, `commence`, `order_type`, `fair_prob`, `yes_ask_at_signal`, `entry_price`, `entry_price_cents`, `fee_rate`, `ev_per_contract`, `edge`, `contracts`, `total_cost`, `ev_total`, `final_status`, `close_reason`, `result`, `actual_pnl`

`result` and `actual_pnl` start as `PENDING` / blank and are filled by `settle.py`.

---

### `trade/settle.py`

Queries Kalshi for each `PENDING` row. When `status == 'settled'`:

| Kalshi result | `result` written | `actual_pnl` formula |
|---|---|---|
| `yes` | WIN | `contracts × (1 − entry_price) × (1 − fee_rate)` |
| `no` | LOSS | `−contracts × entry_price` |
| `void` | VOID | `0` |

Run with `--dry-run` to preview without writing.

---

### `trade/review.py`

Loads and merges `trades.csv` and `no_trades.csv`, then displays:

1. **Rich summary table** — all trades with status, edge, EV, result, PnL
2. **Edge per trade** — bar chart of `fair_prob − entry_price`
3. **Cumulative EV vs actual PnL** — projected vs realized over time
4. **EV per contract histogram** — distribution of edge quality
5. **Win/loss by order type** — cross vs rest win rates with annotated fair_prob

Run with `--table` to skip charts.

---

### `trade/nothing.py` + `nothing_config.py`

A standalone bot for non-sports Kalshi markets (earnings mentions, macro events, etc.).

`nothing_config.py` defines `NOTHING_SERIES` — a list of `(series_ticker, label, category)` tuples. Current categories: `mentions`, `macro`.

The nothing bot has its own review module (`nothing_review.py`) that generates separate charts and is invoked by `review.py` automatically.

---

### `trade/dashboard.py`

#### `Dashboard`

Rich live terminal dashboard. Used when running `quickstart.py --live`.

| Method | Description |
|---|---|
| `add_position(order_id, ticker, outcome, contracts, yes_price_cents, fair_prob, edge)` | Register a new order row |
| `update(order_id, status, fair_prob, edge, filled, market_ask)` | Update any field on an existing row |
| `set_api_usage(used, remaining)` | Update the API usage bar |

Use as a context manager (`with Dashboard() as dash: ...`).

#### `StreamlitDashboard`

Thread-safe state store with the same interface as `Dashboard`, designed for Streamlit. Worker threads call `add_position` / `update`; the Streamlit main thread calls `snapshot()` and renders the table.

---

## Configuration (`config.py`)

**Global settings:**
- `LOOKAHEAD_HRS` — hours ahead to search for events (default: 72)
- `SEASON_MONTHS` — fallback month-based season check per sport key

**`SPORTS_CONFIG` — sport key → Kalshi series mapping:**

| Sport key | Label | Kalshi series |
|---|---|---|
| `basketball_nba` | NBA | `KXNBAGAME` |
| `basketball_ncaab` | College Basketball | `KXNCAAMBGAME` |
| `soccer_mexico_ligamx` | Liga MX | `KXLIGAMXGAME` |
| `soccer_argentina_primera_division` | Argentina Primera División | `KXARGPREMDIVGAME` |
| `soccer_brazil_campeonato` | Brasileiro Serie A | `KXBRASILEIROGAME` |
| `baseball_mlb` | MLB | `KXMLBGAME` |
| `icehockey_nhl` | NHL | `KXNHLGAME` |
| `soccer_usa_mls` | MLS | `KXMLSGAME` |

To add a sport: add it to `SPORTS` and add an entry to `SPORTS_CONFIG` with `ticker` and `label`.

---

## Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `ODDS_API_KEY` | The Odds API key |
| `API_KEY` | Kalshi API key |
| `API_PRIVATE` | Kalshi RSA private key (base64-encoded DER, no PEM headers) |

On Streamlit Cloud, set these in the **Secrets** panel instead of `.env`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `pandas` | DataFrame operations throughout |
| `numpy` | Numerical operations in charts |
| `requests` | HTTP calls to Kalshi and The Odds API |
| `cryptography` | RSA-PSS signing for Kalshi auth |
| `python-dotenv` | Load `.env` credentials |
| `rich` | Terminal dashboard and review table |
| `streamlit` | Web app UI |
| `matplotlib` | Edge realization charts |

`difflib`, `threading`, `uuid`, `csv`, `signal` are standard library.

---

## Data Flow

```
pinnacle_odds(sports, hrs)
    ↓ vig removal → fair_prob per outcome
kalshi_odds(pinnacle_df)
    ↓ date filter + fuzzy match + event lock
    ↓ 4 signal columns (yes/no × cross/rest)
web_app.py or quickstart.py
    ↓ user selects side + mode, reviews signals
run_all_signals(signals_df, bankroll)
    ↓ batched 10 at a time (rate limit)
    ↓ live price re-fetch → place_order (post_only for REST)
    ↓ _monitor: Kalshi poll 10s, Pinnacle re-check 2min
    ↓ cancel on: signal flip | 30min | event imminent | Ctrl+C
logger.log_trade() → logs/trades.csv
    ↓
settle.py → WIN/LOSS/VOID + actual_pnl
    ↓
review.py → edge realization charts
```
