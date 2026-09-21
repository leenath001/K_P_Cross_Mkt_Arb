# K/P Cross-Market Arbitrage + Market Making

Two systems on **Kalshi** (regulated US prediction market), both anchored to **Pinnacle**'s sharp odds as a fair-value benchmark:

1. **Cross-market arbitrage** — buys a Kalshi contract when its price is below Pinnacle's vig-free fair probability, after Kalshi fees.
2. **Market making** — quotes both sides of zero-maker-fee Kalshi sports markets around Pinnacle fair value, with live inventory limits, auto-offload, and market-maker analytics.

**Core constraint (arbitrage):** Kalshi contracts cannot be short-sold — the system only buys underpriced lines, never sells overpriced ones. (Market making can hold either side, because selling YES is buying NO.)

> **These bots place real orders with real money.** Dry-run modes exist for the CLI arbitrage bot; the Market Making tab has no paper mode — ON, Add, Purge and auto-offload all trade for real. See [Safety](#safety-and-operating-notes).

---

## Contents

- [Arbitrage strategy](#arbitrage-strategy)
- [Market making](#market-making)
- [Repository structure](#repository-structure)
- [Usage](#usage)
- [Module reference](#module-reference)
- [Configuration](#configuration-configpy)
- [Logs and data files](#logs-and-data-files)
- [Environment and dependencies](#environment-variables-env)
- [Data flow](#data-flow)
- [Safety and operating notes](#safety-and-operating-notes)

---

## Arbitrage strategy

```
1. Fetch Pinnacle odds via The Odds API (sports fetched in parallel, with 429 retry/backoff)
2. Strip the vig (power method) → fair probabilities
3. Fetch open Kalshi markets for the matching sports series
4. Fuzzy-match outcomes by date + team name, locked at the event level
5. Generate signals: fair_prob vs Kalshi top-of-book, after per-series fees
6. Place orders: REST one tick below the ask (maker) or CROSS at the ask (taker)
7. Monitor: re-ping Pinnacle every 2 min; cancel if the edge flips negative
8. Cancel if unfilled after 30 min or the event starts in < 5 min
9. Size with partial Kelly (ROI-scaled 10–50%)
```

### Fee model

Fees are **per series and fetched from Kalshi** (`get_series_fee_info`), not a flat rate:

```
taker fee = ceil_to_cent( 0.07   × fee_multiplier × C × P × (1−P) )
maker fee = ceil_to_cent( 0.0175 × fee_multiplier × C × P × (1−P) )   only on series with maker fees
```

`fee_multiplier` and whether maker fees exist differ by series (e.g. MLB has a 0.5× multiplier; most soccer, tennis, boxing and other series charge **no maker fee at all**). `fee_rate_for(series, maker)` gives the smooth per-contract rate for sizing; `kalshi_fee_dollars(contracts, price, series, maker)` gives the exact cent-rounded dollar fee for a real order and is used for the final go/no-go check.

### Signal model

Four independent signal types are computed for every matched outcome:

| Signal | Entry price | Fee | Fires when |
|---|---|---|---|
| `signal` (YES cross) | `yes_ask` | taker | `fair_prob − yes_ask ≥ MIN_EDGE` and taker EV ≥ 0.005 |
| `signal_yes_rest` (YES rest) | one tick below `yes_ask` | maker | `fair_prob − rest_price ≥ MIN_EDGE` and maker EV > 0 |
| `signal_no_cross` (NO cross) | `no_ask` | taker | `(1−fair_prob) − no_ask ≥ MIN_EDGE` and taker EV ≥ 0.005 |
| `signal_no` (NO rest) | one tick below `no_ask` | maker | `(1−fair_prob) − rest_price ≥ MIN_EDGE` and maker EV > 0 |

- **`MIN_EDGE` is segment-aware:** 0.03 for draws (a confirmed weak spot), 0.005 when `fair_prob > 0.3`, otherwise 0.01.
- **`MAX_EDGE_OVER_PRICE = 0.14`:** an edge that is large *relative to the price* (cheap longshot, huge apparent mispricing) was the strongest predictor of a losing trade in settled history (top quartile of edge÷price won 14% vs 34.5% for the rest). Those rows are treated as fuzzy-match / calibration errors and never signalled. The Trade tab also has an edge÷price quartile filter (Q4 is never offered).
- **REST price = top of book on Kalshi's real price grid.** Markets use either 1¢ steps (`linear_cent`) or 0.5¢ steps (`center_half_edge_half_cent`, e.g. Brasileirão). The rest price is one tick below the ask on that grid (`trade/core/pricing.py`), sent as `post_only` so it can never cross. A live price re-fetch happens immediately before each order.
- **Binary equivalence:** for a game A vs B, the NO signal on B's row is mathematically equivalent to a YES bet on A. Both are generated, but **only one leg per two-way event is ever traded** (`positions.drop_same_event_duplicates`, `opposite_leg_blocked`) so the bot never holds both sides of one game.

**EV** (Kalshi charges the fee at entry, win or lose):
```
EV = fair_prob − price − fee_rate × price × (1 − price)
```

### Kelly sizing

```
full_kelly  = EV / win_amount          win_amount = (1−price) × (1 − fee_rate×price)
ROI         = EV / price
partial     = 10% if ROI < 2%  |  20% if 2–5%  |  33% if 5–10%  |  50% if > 10%
contracts   = floor( max(bankroll × full_kelly × partial, MIN_NOTIONAL) / price )   [min 1]
final       = max(1, round(contracts × size_mult))
```

`MIN_NOTIONAL` floors the dollar bet because real fees round **up** to the next cent per order, which makes 1-contract orders far more expensive than the smooth estimate. The web app's per-row "contracts" override beats Kelly entirely. If the sized order would cost more than the available cash it is **skipped** (`insufficient_cash`), never partially placed.

---

## Market making

The **Market Making** tab quotes a YES **bid** and a YES **ask** (an ask is a NO bid at 100−price) around Pinnacle's fair value on **zero-maker-fee** Kalshi sports markets. If both sides fill you hold YES and NO and are paid $1, so `ask − bid` is locked in — with no maker fee to eat it.

### Market selection

- Only sports whose Kalshi series have **no maker fee** (about 47 of the 61 configured sports) are eligible.
- A background **screen** ranks Pinnacle-matched markets by open interest, volume, and closeness to 50/50 (penalising spreads far from 3¢), keeps the best market per game, and drops draws and games starting within 30 minutes.
- The screen runs at first load, when *Games starting within* changes, every 45 minutes, or when you press **Refresh** (about 1 OddsAPI credit per zero-fee sport). It runs in a background thread; the last saved list stays on screen meanwhile.
- New markets get the master size / max inventory; markets with resting quotes are never dropped mid-quote.

### Quote rule

One tick wide around fair: `bid = fair floored to the price grid`, `ask = bid + 1 tick`, clamped so a quote is never marketable (`post_only`). If the ideal ask would cross the book, it rests at the top of the ask side instead. **Drag** a yellow YOU row on a card to pin a price; ↺ returns it to auto.

Orders are GTC, tagged with a `client_order_id` prefix `mm-`, and replaced only after the old order's cancellation is **confirmed** (`ensure_canceled`) — a new quote is never placed beside a live old one.

### The Market Making Trade tab

A live-ticking board of market cards. The board talks to the engine over a loopback-only, token-gated HTTP channel, so editing a card **never triggers a Streamlit rerun** (it falls back to Streamlit if the channel is unreachable).

**Top row:** *Games starting within*, *Size, all markets*, *Max inventory, all*, *Pinnacle refresh* (seconds; default 30), **ON/OFF**, **Refresh**, **Cancel all**, *Auto-offload* toggle, then a caption with markets loaded, quoting count, available cash (funding is in ranking order until cash runs out).

**Each card:** order book with our YOU rows and the spread / last / **theo** (Pinnacle fair) line, an inventory strip (net position vs max, average buy, average sell), and controls:

| Control | Behaviour |
|---|---|
| **Size** | Contracts offered per side |
| **Max inv** | Most net contracts held either way |
| **Add B / Add A** ↔ **Pull B / Pull A** | Quote (or cancel and keep off) just that side of just that market. When a side is up the button reads *Pull*, otherwise *Add* |
| **Purge (±$x)** | Flatten this market now, profit or not; the label shows the all-in result if you purged right now |
| **ON** | Quote this market only, even if the top ON is off |
| **Cancel** | Pull this market's quotes and keep it off |
| ⤢ | Expand: depth chart, stats, orders table |

Global **OFF / Cancel all** cancel every order **in parallel in the background** (the board keeps ticking and each YOU row disappears as Kalshi confirms), plus any stray `mm-` orders from an earlier run.

**Fill alerts:** when an order is hit the card flashes red and a toast appears top-right for 5 seconds: what happened, in which market, the current bid/ask, contracts bought, sold and net inventory.

### Inventory management

- **Max inventory (per market):** at the cap, the side that would add to the position stops quoting and the other side stays up so we can reduce. Bid size shrinks to the remaining room. Default 20, set per card or for all markets.
- **Auto-offload (toggle, default on):** for active markets, if the touch pays more than our **average entry plus the taker fee** — netting at least 0.5¢ per contract and $0.02 — the engine sells (or buys back) at the touch with an immediate-or-cancel order. It skips a level that is our own resting order. It never trades a market you haven't switched on.
- **Purge:** cancels the market's quotes, pauses it, then sweeps the book with IOC. The order is **sized against your real Kalshi position** first: if you are already flat, or the position is the wrong direction, it does nothing; if Kalshi holds fewer contracts than the card thinks it only sends that many. Fills are read only after Kalshi reports the order closed.
- **Cash cap:** quotes are only placed if cash covers them (balance refreshed every 20 s, decremented locally).
- **Kalshi is the source of truth:** every cycle the engine compares each resting order's size and price with Kalshi's list and resyncs (and re-quotes) on a mismatch. Any `mm-` order on Kalshi that no market is tracking is cancelled (orphan sweep). A resting order missing from Kalshi's list is only forgotten once Kalshi says it is closed (the list can lag a fresh order). Every 90 s a reconcile adds any filled `mm-` orders the ledger missed (real average price and taker fee for offload/purge fills).
- **Inventory carries across sessions:** each market's fills are replayed from the ledger on load, so inventory and average prices survive restarts.
- **Dead-man switch:** if no browser has polled the engine for 90 s, all quotes are cancelled.

### Polling cadence

2 s cycle. Quoted or expanded markets have their order book read every cycle; the rest share a budget of 24 reads per cycle round-robin (10 parallel workers). Market metadata refreshes about every 15 s, your resting-orders list once per cycle while quoting, your balance every 20 s. Pinnacle fair values refresh every *Pinnacle refresh* seconds (default 30), and only for sports with live quotes. The browser never calls Kalshi.

### The Market Making Review tab

Separate from the arbitrage PnL. Headline metrics (hover for definitions): fills and contracts, notional traded, **realized PnL net of fees** (average-cost accounting), fees paid, **average edge at fill** vs Pinnacle, **markout at 1 and 5 minutes** (fair value after our fill vs our price — negative means we were picked off), round trips and average spread captured, buy/sell balance, maker share, peak position, and the hit-rate of fills on settled markets. Charts: cumulative realized PnL, net inventory over time, edge-at-fill histogram; a quoting-quality table (two-sided share, re-quotes, API errors) and a per-market table (fills, round trips, spread captured, peak/average position, realized PnL, fees, settlement).

### Market-making files

| File | Purpose |
|---|---|
| `trade/mm/engine.py` | `MMEngine`: quoting loop, inventory, offload, purge, action server, snapshot |
| `trade/mm/ui.py` | Streamlit glue: controls fragment, board, Review tab, engine lifecycle |
| `trade/mm/ledger.py` | Fill ledger, PnL table, markouts, analytics (`walk_fills`, `mm_summary`) |
| `trade/mm/frontend/index.html` | The board (custom Streamlit component: drag, toasts, controls) |

`ENGINE_VERSION` in `engine.py` must be bumped whenever the engine's state changes: the UI then replaces the cached engine (stopping the old one and cancelling its quotes).

---

## Repository structure

```
K_P_Cross_Mkt_Arb/
├── README.md
├── requirements.txt
├── config.py                    # sport → Kalshi series mapping, global settings
├── applog.py                    # rotating file logger (trade/logs/app.log)
├── .env                         # API credentials (git-ignored)
├── .streamlit/config.toml       # runOnSave, minimal toolbar
├── KALSHI/k_helpers.py          # Kalshi auth, market fetch, fees, signal computation
├── theODDS/p_helpers.py         # Pinnacle odds via The Odds API (parallel, 429 retry)
├── POLYMARKET/                  # Polymarket helpers (experimental)
├── notebooks/                   # win/loss, calibration, edge÷price quartile analysis
├── tests/                       # fill-speed test
└── trade/
    ├── web_app.py               # Streamlit UI: Pinnacle (Trade, Review) + Market Making (Trade, Review)
    ├── core/
    │   ├── execution.py         # place/cancel/status, Kelly sizing, monitor loop, fees
    │   ├── positions.py         # live open-position dedup, opposite-leg blocking
    │   ├── pricing.py           # Kalshi price grid: rest price, tick math
    │   └── logging_io.py        # trade / unfilled-attempt logging
    ├── strategies/
    │   ├── base.py              # the strategy contract (documented duck typing)
    │   ├── kp_arb.py            # K/P arbitrage
    │   ├── prospect.py          # prospect-theory strategy
    │   └── nothing.py           # non-sports markets bot
    ├── mm/                      # market making (engine, ui, ledger, frontend)
    ├── clv.py                   # closing-line-value capture (background thread)
    ├── settle.py                # WIN/LOSS/VOID + PnL for settled trades
    ├── review.py                # terminal review + charts
    ├── dashboard.py             # Rich terminal dashboard + Streamlit state store
    ├── quickstart.py            # K/P arb CLI (dry run + live)
    ├── prospect_quickstart.py   # prospect CLI
    ├── cancel_all.py            # emergency: cancel every resting order
    ├── scripts/                 # one-time data migrations
    └── logs/                    # CSV logs and app.log (see below)
```

---

## Usage

### Web app (recommended)

```bash
streamlit run trade/web_app.py
```

Two top-level tabs, each with **Trade** and **Review**:

- **Pinnacle → Trade** — fetch signals, choose YES/NO and order mode (rest / cross / auto), filter by edge÷price quartile, edit contracts per row, execute; live dashboard with per-order Cancel all / Cross & Cancel / Keep Rest / +15 min. Cancelled orders lose their red highlight as they cancel.
- **Pinnacle → Review** — auto-settles pending trades on load, then: projected vs realized PnL with a ±2σ luck band, outcome-distribution chart (Monte Carlo of settled trades with EV tooltip), CLV chart, calibration, win rate by sport and by edge÷price quartile, click-a-bar detail panels, and an All Trades table with filters.
- **Market Making → Trade / Review** — see [Market making](#market-making).

Nothing on the page reloads on a click: controls live in Streamlit fragments and the Market Making board updates over its own channel.

### CLI

```bash
python trade/quickstart.py --usage            # API budget (1 request)
python trade/quickstart.py                    # dry run: signals + Kelly sizing
python trade/quickstart.py --live             # live trading

python trade/settle.py --dry-run              # preview settlement
python trade/settle.py                        # write WIN/LOSS/VOID
python trade/review.py [--table]              # terminal review (+ charts)

python trade/prospect_quickstart.py [--live]  # prospect-theory strategy
python trade/prospect_review.py [--table]

python trade/cancel_all.py                    # emergency: cancel all open orders
```

**`quickstart.py` flags:**

| Flag | Default | Description |
|---|---|---|
| `--usage` | — | Print API usage and exit |
| `--live` | off | Place real orders |
| `--bankroll <$>` | Kalshi balance | Override balance |
| `--hrs <n>` | `config.LOOKAHEAD_HRS` | Look-ahead window in hours |
| `--fetch-live` | `config.LIVE` | Fetch in-progress games instead of upcoming |
| `--mode rest/cross/auto` | rest | Order mode |
| `--side yes/no` | yes | Trade YES or NO contracts |
| `--taker-fee <f>` / `--maker-fee <f>` | 0.07 / 0.0175 | Rates for this script's own order placement (signal generation uses live per-series rates) |
| `--threshold <f>` | 0.85 | Min fuzzy-match score |
| `--size <f>` | 1.0 | Kelly size multiplier (order skipped if cost exceeds balance) |

---

## Module reference

### `theODDS/p_helpers.py`

- **`pinnacle_odds(sports, hrs, live=False) → DataFrame`** — H2H odds from Pinnacle, vig removed by the power method. Sports are fetched in parallel (5 workers) with backoff on 429/5xx; sports that still fail are logged and exposed in `_last_failed`. Columns: `sport, event_id, home, away, commence, bookmaker, outcome, decimal_odds, implied_prob, fair_prob, vig_pct`.
- **`check_sports_with_events(sport_keys, hrs) → dict`** — counts real upcoming events per sport (1 credit each; parallel with retry).
- **`fetch_usage()`**, **`get_api_usage()`** — API credit counters.

### `KALSHI/k_helpers.py`

- **`kalshi_headers(method, path)`** — RSA-PSS request signing (`API_PRIVATE`).
- **`load_all_mkts(series)`** — open markets for a series, including `price_ranges` (the tick grid).
- **`fee_rate_for(series, maker)`**, **`kalshi_fee_dollars(contracts, price, series, maker)`** — see [Fee model](#fee-model).
- **`kalshi_odds(df, threshold, ...)`** — date filter, fuzzy match with event-level lock, then the four signals plus `rest_price_yes/no`. Series load in parallel.

### `trade/core/execution.py`

`kelly_contracts`, `resolve_contracts`, `_ev`, `_exact_ev_ok`; `place_order(..., post_only, client_order_id, time_in_force)` (V2 order endpoint; GTC or immediate-or-cancel); `cancel_order` (routes by market ticker so orders on other exchange shards cancel correctly), `ensure_canceled` (cancel and verify closed); `get_market_prices`, `get_orderbook_depth`, `get_balance`; `list_resting_orders`, `get_order_status`; `cross_and_cancel_order`, `cancel_and_rerest`; and `_monitor`, the per-order loop (Kalshi poll 10 s, Pinnacle re-check 2 min; cancels on signal flip, 30-minute cap, event imminent, or stop event).

### `trade/core/positions.py`, `pricing.py`

`open_tickers()` reads **live** Kalshi positions and resting orders (not the CSV logs) for dedup; `drop_same_event_duplicates` / `opposite_leg_blocked` enforce one leg per two-way event. `pricing.py` parses Kalshi's price grid and computes rest prices.

### `trade/strategies/`

`kp_arb.run_trade` / `run_all_signals` (batches of 10, dedup on ticker and event, skips tickers with open exposure), `prospect`, and `nothing`. Contract described in `base.py`.

### `trade/settle.py`

Queries Kalshi for `PENDING` rows and writes `result`, `actual_pnl` and `settled_at`. PnL is fills-based where available. Market-making order IDs are skipped, so MM fills never leak into the arbitrage PnL. The Review tab runs it automatically (throttled).

### `trade/clv.py`

A background thread captures the Pinnacle closing line for each open position in the last 12 minutes before the event and writes `closing_lines.csv`, which feeds the Review tab's CLV chart.

### `trade/prospect.py` — prospect-theory strategy

Exploits probability-weighting distortions using Pinnacle `fair_prob` as the anchor: buy **NO** on longshots (`yes_ask` $0.05–$0.15) and **YES** on favourites ($0.75–$0.92), only when the normal EV signal also fires. Own log (`prospect_trades.csv`), CLI and review. Zone bounds are CLI flags (`--longshot-lo/hi`, `--favorite-lo/hi`).

### `trade/nothing.py` + `nothing_config.py`

Bot for non-sports Kalshi markets (mentions, macro). `NOTHING_SERIES` lists the series; own log and review.

### `trade/dashboard.py`

`Dashboard` (Rich terminal) and `StreamlitDashboard` (thread-safe state store with the same interface).

---

## Configuration (`config.py`)

- **`LOOKAHEAD_HRS`** — hours ahead to search (default 72). **`SEASON_MONTHS`** — month-based fallback when the event check fails.
- **`SPORTS_CONFIG`** — Odds API sport key → Kalshi series (`ticker`, `label`). **61 sports**: NBA, WNBA, NBL, college basketball, NFL, college football, MLB, KBO, NPB, NHL and other hockey leagues, PLL, tennis, boxing, MMA, and many soccer leagues (Liga MX, Argentina, Brazil A/B, MLS, European leagues, cups, Nations League, J/K-League, Nordic leagues and more). Entries are tagged as confirmed against a live match or unverified; sports with no Kalshi series are listed in a comment.
- **`PAUSED_SPORTS`** — sports to skip.

To add a sport: add its Odds API key to `SPORTS` and an entry in `SPORTS_CONFIG`; `fee_rate_for` then decides whether it is zero-maker-fee (and therefore eligible for market making).

---

## Logs and data files

`trade/logs/`:

| File | Contents |
|---|---|
| `trades.csv`, `no_trades.csv` | K/P arbitrage trades (YES / NO side); only orders that actually filled |
| `unfilled_attempts.csv` | Cancelled / expired attempts — diagnostic only, never used for dedup or stats |
| `nothing_trades.csv`, `prospect_trades.csv` | Other strategies |
| `closing_lines.csv` | Pinnacle closing lines for CLV |
| `mm_fills.csv` | Market-making fills: `ts, ticker, side, price_c, qty, fair_c, order_id, liq (maker/taker), fee_usd` |
| `mm_markouts.csv` | Fair value 1 and 5 minutes after each maker fill |
| `mm_selection.json` | Saved market list with per-market size and max inventory |
| `mm_ignored_orders.txt` | Order IDs closed by hand — excluded from MM inventory/PnL and never re-added by reconcile |
| `app.log` (+ `.1`, `.2`) | Rotating application log |

`data/theodds_sports.csv` is a reference list of Odds API sports. `notebooks/` holds the win/loss, calibration and quartile studies.

---

## Environment variables (`.env`)

| Variable | Description |
|---|---|
| `ODDS_API_KEY` | The Odds API key |
| `API_KEY` | Kalshi API key |
| `API_PRIVATE` | Kalshi RSA private key (base64 DER, no PEM headers) |

On Streamlit Cloud set these in the **Secrets** panel instead.

### Dependencies

`requests`, `pandas`, `numpy`, `rich`, `python-dotenv`, `cryptography`, `streamlit`, `matplotlib`, and `plotly` (Review charts). Install with `pip install -r requirements.txt`.

---

## Data flow

### K/P arbitrage
```
pinnacle_odds → vig removal → fair_prob
kalshi_odds → date filter + fuzzy match + event lock → 4 signals + rest prices
web_app (Pinnacle → Trade) or quickstart.py
run_all_signals → batches of 10 → live price re-fetch → place_order (post_only for REST)
_monitor → cancel on flip | 30 min | event imminent | stop
logging_io → trades.csv / no_trades.csv (fills) or unfilled_attempts.csv
settle → WIN / LOSS / VOID + PnL → Review (+ CLV from clv.py)
```

### Market making
```
screen: zero-fee sports → pinnacle_odds → kalshi_odds → rank → market list
engine loop (2 s): read books → compute_targets (fair-driven, one tick wide)
    → inventory caps / side switches → cancel-confirm → place post_only GTC quotes
    → resync with Kalshi's resting orders, sweep orphans, account fills
    → auto-offload / purge (IOC) → ledger (mm_fills.csv) + markouts
board ⇄ engine over loopback HTTP (actions, snapshots); Streamlit fragment as fallback + heartbeat
Review: mm_summary over the ledger (realized PnL, edge, markouts, inventory, per-market)
```

---

## Safety and operating notes

- **Real money.** The Market Making tab has no paper mode. ON, Add, Purge and auto-offload all place real orders. Auto-offload and Purge cross the book and pay the **taker fee**.
- **Purge and other IOC orders** are sized against your real Kalshi position and their fills are read only after Kalshi reports the order closed, but a purge still trades at whatever the book pays. Do not click it repeatedly.
- **Stale sessions.** With `runOnSave = true` (`.streamlit/config.toml`) a saved file reruns the app, and a bumped `ENGINE_VERSION` swaps the engine and cancels its quotes. Turn `runOnSave` off for uninterrupted live trading.
- **Dead-man switch:** closing the browser cancels market-making quotes within about 90 seconds.
- **API credits.** Each market screen costs about 1 OddsAPI credit per zero-fee sport, and each Pinnacle fair refresh costs credits for the sports you are quoting. Watch the usage bar in the sidebar.
- **Separation.** Market-making fills and PnL never enter the arbitrage logs, Review or settlement.
- **Manual closes.** If you close a market-making position by hand on Kalshi, add the order ID to `mm_ignored_orders.txt` (or ask to have it removed) so the ledger doesn't re-add it.
