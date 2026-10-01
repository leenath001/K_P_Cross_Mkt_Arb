"""
applog.py — Central application logging.

Distinct from trade/logger.py (which appends one CSV row per filled trade).
This module is for operational events: API failures, unexpected exceptions,
monitor-loop retries — anything that would otherwise only ever appear in a
terminal that later scrolls away or a Streamlit process that gets restarted.

Root-level (like config.py) so KALSHI/, theODDS/, and trade/ can all import
it the same way without a layering inversion.

Usage:
    from applog import get_logger
    log = get_logger(__name__)
    log.warning('Kalshi ask unavailable for %s, skipping', ticker)
    log.exception('order placement failed')   # inside an except block — includes traceback

Writes to trade/logs/app.log (rotating, 5MB x 5 backups) and mirrors
INFO-and-above to the console so existing terminal visibility is unchanged.
"""

import logging
import os
import re
import socket
from logging.handlers import RotatingFileHandler

_LOG_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade', 'logs')
_LOG_PATH = os.path.join(_LOG_DIR, 'app.log')

_FORMAT   = '%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s'
_DATEFMT  = '%Y-%m-%d %H:%M:%S'

# requests/urllib3 bury the useful part of a network error in a very long repr — condense it for the console.
_NET_PATTERNS = [
    (re.compile(r"HTTPS?ConnectionPool\(host='([^']+)', port=\d+\): Read timed out\.? ?(?:\(read timeout=([^)]*)\))?"),
     lambda m: f"{m.group(1)}: read timed out" + (f" ({m.group(2)}s)" if m.group(2) not in (None, 'None') else '')),
    (re.compile(r"HTTPS?ConnectionPool\(host='([^']+)', port=\d+\): Max retries exceeded with url: (\S+?)(?:\?\S*)? \(Caused by (\w+)\(.*"),
     lambda m: f"{m.group(1)}{m.group(2)}: connection failed ({m.group(3)})"),
]


def _shorten_net(text: str) -> str:
    for pat, repl in _NET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _is_network_error(exc) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError)):
        return True
    try:
        import requests, urllib3
        return isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                                urllib3.exceptions.TimeoutError, urllib3.exceptions.ProtocolError,
                                urllib3.exceptions.NewConnectionError))
    except ImportError:
        return False


class _ConsoleFormatter(logging.Formatter):
    """Terminal output: timeouts / connection errors print as ONE line (no traceback) with the noisy urllib3 text
    condensed. Every other exception keeps its full traceback; app.log always keeps the full record."""
    def format(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None and _is_network_error(exc):
            line = super().format(logging.makeLogRecord({**record.__dict__, 'exc_info': None, 'exc_text': None}))
            return _shorten_net(f'{line} — {type(exc).__name__}: {exc}')
        return _shorten_net(super().format(record)) if record.exc_info is None else super().format(record)


_ROOT_NAME  = 'kparb'
_configured = False


def _configure():
    global _configured
    if _configured:
        return
    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False  # don't double-print through the default root logger

    file_handler = RotatingFileHandler(_LOG_PATH, maxBytes=5_000_000, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(_ConsoleFormatter(_FORMAT, _DATEFMT))
    root.addHandler(console_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the shared `kparb` namespace, writing to trade/logs/app.log."""
    _configure()
    short = name.rsplit('.', 1)[-1]  # trim package prefix — module name is enough
    return logging.getLogger(f'{_ROOT_NAME}.{short}')
