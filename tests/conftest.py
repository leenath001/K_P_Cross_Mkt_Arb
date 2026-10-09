"""Shared test setup: import paths the app itself relies on, and a hard block on real network access."""
import os
import socket
import sys

import tempfile

import pytest

# Keep test runs out of the real trade/logs/app.log (must be set before anything imports applog).
os.environ.setdefault('KPARB_LOG_DIR', tempfile.mkdtemp(prefix='kparb-test-logs-'))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, 'trade')):      # web_app.py imports `dashboard` / `settle` from trade/ directly
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test runs offline: any connection to a non-loopback address raises. Guarantees no test can place or
    cancel a real order, or spend Odds API credits, even if a fake is missing somewhere."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise ConnectionRefusedError(f'network blocked in tests: {address}')
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k: (_ for _ in ()).throw(
        socket.gaierror(f'DNS blocked in tests: {host}')) if host not in ('127.0.0.1', 'localhost', '::1')
        else socket.__dict__['_real_getaddrinfo'](host, *a, **k))


socket._real_getaddrinfo = socket.getaddrinfo
