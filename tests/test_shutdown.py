"""Ctrl+C / normal exit must not hang on a worker stuck in a call that never returns."""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUCK = """
import sys, threading
sys.path.insert(0, {root!r})
from concurrent.futures import ThreadPoolExecutor
{install}
ThreadPoolExecutor(1).submit(threading.Event().wait)   # a worker that never finishes (like a hung request)
print('main done', flush=True)
"""


def _run(install, timeout):
    code = STUCK.format(root=ROOT, install=install)
    t0 = time.time()
    try:
        subprocess.run([sys.executable, '-c', code], timeout=timeout, capture_output=True)
        return time.time() - t0
    except subprocess.TimeoutExpired:
        return None


def test_exit_watchdog_ends_a_hung_shutdown():
    assert _run('', timeout=8) is None, 'control: without the watchdog this process should hang'
    took = _run('from trade.core.shutdown import install_exit_watchdog; install_exit_watchdog(grace=1.0)', timeout=15)
    assert took is not None and took < 10, 'watchdog should force the exit'
