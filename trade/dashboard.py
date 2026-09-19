"""
dashboard.py — Live terminal dashboard for K/P arbitrage positions.

Displays a live-updating table of every open/closed position and an
API request usage bar. Designed to be used as a context manager:

    with Dashboard() as dash:
        run_all_signals(df, bankroll=bankroll, dashboard=dash)
"""

import threading
from typing import Optional
from datetime import datetime
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.console import Group
from rich.text import Text
from rich import box

# Colour per order status
_STATUS_STYLE = {
    'resting':               'yellow',
    'executed':              'bold green',
    'filled':                'bold green',
    'canceled':              'red',
    'signal_flipped':        'red',
    'max_duration_exceeded': 'dim white',
    'event_imminent':        'dim white',
    'skipped':               'dim white',
    'unknown':               'dim white',
}


class Dashboard:
    """
    Thread-safe Rich live dashboard.

    Methods
    -------
    add_position(order_id, ...)   — register a new order row
    update(order_id, ...)         — update status / latest fair prob
    set_api_usage(used, remaining)— update the API bar
    """

    def __init__(self, api_limit: int = 500):
        self._lock          = threading.Lock()
        self._positions: dict[str, dict] = {}   # order_id → row data
        self._api_used      = 0
        self._api_limit     = api_limit
        self._extra_seconds = 0   # session-wide extension, added to every order's max_duration
        self._live          = Live(
            self._render(),
            refresh_per_second=4,
            screen=True,
        )

    def __enter__(self):
        self._live.__enter__()
        return self

    def __exit__(self, *args):
        self._live.__exit__(*args)

    # ── Public API ───────────────────────────────────────────────────────────

    def add_position(self, order_id: str, ticker: str, outcome: str,
                     contracts: int, yes_price_cents: int,
                     fair_prob: float, edge: float,
                     event_id: str = '', sport: str = '',
                     raw_outcome: str = '', fee_rate: float = 0.07,
                     side: str = 'yes', commence: str = ''):
        with self._lock:
            self._positions[order_id] = {
                'order_id':     order_id,          # also the dict key — kept as a field
                                                    # too so callers iterating .values()
                                                    # (e.g. the live-resize UI) still have it
                'ticker':       ticker,
                'outcome':      outcome,
                'contracts':    contracts,
                'filled':       0,
                'entry_price':  yes_price_cents,  # our order price (fixed)
                'avg_fill_price': None,            # VWAP fill price (dollars), once filled
                'market_ask':   None,              # live Kalshi market ask
                'fair_entry':   fair_prob,
                'fair_last':    fair_prob,
                'edge_last':    edge,
                'status':       'resting',
                'last_ping':    datetime.now().strftime('%H:%M:%S'),
                'event_id':     event_id,
                'sport':        sport,
                'raw_outcome':  raw_outcome or outcome,
                'fee_rate':     fee_rate,
                'side':         side,
                'commence':     commence,
            }
            self._refresh()

    def update(self, order_id: str, status: Optional[str] = None,
               fair_prob: Optional[float] = None, edge: Optional[float] = None,
               filled: Optional[int] = None, market_ask: Optional[int] = None,
               avg_fill_price: Optional[float] = None):
        with self._lock:
            pos = self._positions.get(order_id)
            if pos is None:
                return
            if status is not None:
                pos['status'] = status
            if fair_prob is not None:
                pos['fair_last'] = fair_prob
                pos['last_ping'] = datetime.now().strftime('%H:%M:%S')
            if edge is not None:
                pos['edge_last'] = edge
            if filled is not None:
                pos['filled'] = filled
            if market_ask is not None:
                pos['market_ask'] = market_ask
            if avg_fill_price is not None:
                pos['avg_fill_price'] = avg_fill_price
            self._refresh()

    def set_api_usage(self, used: int, remaining: int):
        with self._lock:
            self._api_used  = used
            self._api_limit = used + remaining
            self._refresh()

    def extend_time(self, seconds: int):
        """Push back every order's max_duration kill condition by `seconds`, cumulative."""
        with self._lock:
            self._extra_seconds += seconds
            self._refresh()

    def get_extra_seconds(self) -> int:
        with self._lock:
            return self._extra_seconds

    # ── Rendering ────────────────────────────────────────────────────────────

    def _refresh(self):
        self._live.update(self._render())

    def _render(self) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAD,
            expand=True,
            show_footer=False,
            padding=(0, 1),
        )
        table.add_column('Ticker',       style='cyan',    no_wrap=True, max_width=36)
        table.add_column('Outcome',      style='white',   width=10)
        table.add_column('Cts',          style='white',   justify='right', width=5)
        table.add_column('Filled',       style='white',   justify='right', width=7)
        table.add_column('Avg Fill',     style='white',   justify='right', width=9)
        table.add_column('Price',        style='white',   justify='right', width=7)
        table.add_column('Last Mkt Ask', style='white',   justify='right', width=12)
        table.add_column('Fair (entry)', style='white',   justify='right', width=12)
        table.add_column('Fair (last)',  style='white',   justify='right', width=11)
        table.add_column('Edge',         justify='right', width=7)
        table.add_column('Status',       width=10)
        table.add_column('Last Ping',    style='white',   width=10)

        for pos in self._positions.values():
            cts    = pos['contracts']
            filled = pos.get('filled', 0)
            if filled == cts and cts > 0:
                disp_status = 'executed'
            elif 0 < filled < cts:
                disp_status = 'partial'
            else:
                disp_status = pos['status']
            style   = _STATUS_STYLE.get(disp_status, 'white')
            edge_c  = 'green' if pos['edge_last'] > 0 else 'red'
            mkt_ask = f"{pos['market_ask']}¢" if pos['market_ask'] is not None else '—'
            avg_fp  = pos.get('avg_fill_price')
            avg_fill = f"{round(avg_fp * 100)}¢" if avg_fp is not None else '—'
            table.add_row(
                pos['ticker'][-36:],
                pos['outcome'],
                str(cts),
                str(filled),
                avg_fill,
                f"{pos['entry_price']}¢",
                mkt_ask,
                f"{pos['fair_entry']:.3f}",
                f"{pos['fair_last']:.3f}",
                f"[{edge_c}]{pos['edge_last']:+.3f}[/{edge_c}]",
                f"[{style}]{disp_status}[/{style}]",
                pos['last_ping'],
            )

        api_bar = self._api_bar()
        _extend_note = (f'  [dim]· +{self._extra_seconds // 60}m extended[/dim]'
                        if self._extra_seconds else '')
        return Panel(
            Group(table, Text(''), api_bar),
            title='[bold blue]K/P Cross-Market Arbitrage[/bold blue]  [dim]Ctrl+C to cancel all & quit[/dim]'
                  + _extend_note,
            border_style='blue',
        )

    def _api_bar(self) -> Text:
        used      = self._api_used
        limit     = self._api_limit
        remaining = limit - used
        width     = 40
        filled    = int(width * used / max(limit, 1))

        pct = used / max(limit, 1)
        color = 'green' if pct < 0.7 else ('yellow' if pct < 0.9 else 'red')

        bar  = '█' * filled + '░' * (width - filled)
        text = Text()
        text.append('API  ')
        text.append(bar, style=color)
        text.append(f'  {used} used / {remaining} remaining', style='dim white')
        return text


class StreamlitDashboard:
    """
    Thread-safe state store with the same interface as `Dashboard`, but
    designed for rendering from the Streamlit main loop instead of Rich.

    The bot's worker threads call add_position / update / set_api_usage.
    The Streamlit script reads state via snapshot() and renders a table.
    """

    def __init__(self, api_limit: int = 500):
        self._lock = threading.Lock()
        self._positions: dict[str, dict] = {}
        self._api_used  = 0
        self._api_limit = api_limit
        self._extra_seconds = 0   # session-wide extension, added to every order's max_duration

    # Same signatures as Dashboard so run_trade can use either
    def add_position(self, order_id: str, ticker: str, outcome: str,
                     contracts: int, yes_price_cents: int,
                     fair_prob: float, edge: float,
                     event_id: str = '', sport: str = '',
                     raw_outcome: str = '', fee_rate: float = 0.07,
                     side: str = 'yes', commence: str = ''):
        with self._lock:
            self._positions[order_id] = {
                'order_id':    order_id,          # also the dict key — kept as a field
                                                   # too so callers iterating .values()
                                                   # (e.g. the live-resize UI) still have it
                'ticker':      ticker,
                'outcome':     outcome,
                'contracts':   contracts,
                'filled':      0,
                'entry_price': yes_price_cents,
                'avg_fill_price': None,
                'market_ask':  None,
                'fair_entry':  fair_prob,
                'fair_last':   fair_prob,
                'edge_last':   edge,
                'status':      'resting',
                'last_ping':   datetime.now().strftime('%H:%M:%S'),
                'event_id':    event_id,
                'sport':       sport,
                'raw_outcome': raw_outcome or outcome,
                'fee_rate':    fee_rate,
                'side':        side,
                'commence':    commence,
            }

    def update(self, order_id: str, status: Optional[str] = None,
               fair_prob: Optional[float] = None, edge: Optional[float] = None,
               filled: Optional[int] = None, market_ask: Optional[int] = None,
               avg_fill_price: Optional[float] = None):
        with self._lock:
            pos = self._positions.get(order_id)
            if pos is None:
                return
            if status is not None:     pos['status']     = status
            if fair_prob is not None:
                pos['fair_last']  = fair_prob
                pos['last_ping']  = datetime.now().strftime('%H:%M:%S')
            if edge is not None:          pos['edge_last']       = edge
            if filled is not None:        pos['filled']          = filled
            if market_ask is not None:    pos['market_ask']      = market_ask
            if avg_fill_price is not None: pos['avg_fill_price'] = avg_fill_price

    def set_api_usage(self, used: int, remaining: int):
        with self._lock:
            self._api_used  = used
            self._api_limit = used + remaining

    def extend_time(self, seconds: int):
        """Push back every order's max_duration kill condition by `seconds`, cumulative."""
        with self._lock:
            self._extra_seconds += seconds

    def get_extra_seconds(self) -> int:
        with self._lock:
            return self._extra_seconds

    def snapshot(self) -> dict:
        """Return a deep-ish copy safe to render outside the lock."""
        with self._lock:
            return {
                'positions': {k: dict(v) for k, v in self._positions.items()},
                'extra_seconds': self._extra_seconds,
                'api_used':  self._api_used,
                'api_limit': self._api_limit,
            }
