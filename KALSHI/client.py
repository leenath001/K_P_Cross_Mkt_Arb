"""
KALSHI/client.py — the one way to talk to Kalshi's REST API.

Every Kalshi call in the app goes through get() / post() / delete() here, which handle:
  - auth: RSA-PSS request signing (kalshi_headers) with the key from .env / Streamlit secrets
  - pacing: a process-wide token bucket for reads, so the MM engine, pin board, balance feed and order monitors
    sharing one API key can't burst past Kalshi's per-second read limit (that caused 429s on everything)
  - connection reuse: one pooled requests.Session instead of a fresh TCP+TLS connection per call (per-call
    connections piled up in TIME_WAIT and contributed to running the machine out of local ports)
  - timeouts: reads default to READ_TIMEOUT. Writes default to NO timeout, deliberately — a timed-out order POST
    leaves it unknown whether the order exists; callers that want one pass timeout= explicitly.

Paths are relative to the API root: get('/portfolio/balance'), not the full URL. Responses are plain
requests.Response objects and errors are plain requests exceptions, so callers keep their resp.ok /
raise_for_status() / except RequestException handling unchanged.

Adding a new endpoint: just call get/post/delete with its path — no headers, URLs or timeouts to repeat.
"""
import base64
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

BASE_URL     = 'https://api.elections.kalshi.com/trade-api/v2'
API_PREFIX   = '/trade-api/v2'      # what gets signed: the path from the host, without the query string
READ_TIMEOUT = 15                   # seconds

# ── credentials ──────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'), override=True)


def _secret(key: str) -> Optional[str]:
    """Read from env first, fall back to st.secrets (Streamlit Cloud)."""
    val = os.getenv(key)
    if not val:
        try:
            import streamlit as st
            val = st.secrets.get(key)
        except Exception:
            pass
    return val


API_KEY     = _secret('API_KEY')
API_PRIVATE = _secret('API_PRIVATE')
assert API_PRIVATE, 'API_PRIVATE not found — set it in .env or Streamlit Cloud secrets'

# API_PRIVATE is a raw base64-encoded DER key — wrap in PEM headers to load it
_private_key = serialization.load_pem_private_key(
    b'-----BEGIN RSA PRIVATE KEY-----\n' + API_PRIVATE.strip().encode() + b'\n-----END RSA PRIVATE KEY-----\n',
    password=None)

# ── read pacing ──────────────────────────────────────────────────────────────
# Writes (orders / cancels) are not paced: they're latency-sensitive and cancel_order() already backs off on 429.
KALSHI_READS_PER_SEC = 15      # Kalshi Basic tier allows 20 reads/s; headroom for clock jitter
KALSHI_READ_BURST    = 5
_read_lock   = threading.Lock()
_read_tokens = float(KALSHI_READ_BURST)
_read_last   = time.monotonic()


def _pace_read():
    """Token bucket: take one read token, sleeping (outside the lock) until one is available."""
    global _read_tokens, _read_last
    while True:
        with _read_lock:
            now = time.monotonic()
            _read_tokens = min(KALSHI_READ_BURST, _read_tokens + (now - _read_last) * KALSHI_READS_PER_SEC)
            _read_last = now
            if _read_tokens >= 1:
                _read_tokens -= 1
                return
            wait = (1 - _read_tokens) / KALSHI_READS_PER_SEC
        time.sleep(wait)


def kalshi_headers(method: str, path: str) -> dict:
    """Signed auth headers for one request. `path` is the full path from the host (API_PREFIX + endpoint).
    GETs are paced here, so even code that still builds its own requests shares the read budget."""
    if method.upper() == 'GET':
        _pace_read()
    ts  = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sig = _private_key.sign((ts + method.upper() + path).encode(),
                            asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()),
                                             salt_length=asym_padding.PSS.MAX_LENGTH),
                            hashes.SHA256())
    return {
        'KALSHI-ACCESS-KEY':       API_KEY,
        'KALSHI-ACCESS-TIMESTAMP': ts,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
    }


# ── transport ────────────────────────────────────────────────────────────────
_session = requests.Session()
# Pool sized for the app's peak concurrency (MM book reads run 10 in parallel, cancels 12, plus feeds/monitors).
_session.mount('https://', HTTPAdapter(pool_connections=4, pool_maxsize=32))


def request(method: str, path: str, *, params=None, json=None, timeout=None, auth: bool = True) -> requests.Response:
    """One Kalshi call. `path` is relative to the API root, e.g. '/markets/KX-TICKER/orderbook'."""
    method = method.upper()
    if auth:
        headers = kalshi_headers(method, API_PREFIX + path)
    else:
        headers = {}
        if method == 'GET':
            _pace_read()
    if json is not None:
        headers['Content-Type'] = 'application/json'
    return _session.request(method, BASE_URL + path, headers=headers, params=params, json=json, timeout=timeout)


def get(path: str, params=None, *, timeout=READ_TIMEOUT, auth: bool = True) -> requests.Response:
    return request('GET', path, params=params, timeout=timeout, auth=auth)


def post(path: str, json=None, *, timeout=None) -> requests.Response:
    return request('POST', path, json=json, timeout=timeout)


def delete(path: str, params=None, *, timeout=None) -> requests.Response:
    return request('DELETE', path, params=params, timeout=timeout)
