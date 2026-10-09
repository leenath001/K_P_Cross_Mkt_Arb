"""
trade/core/shutdown.py — make Ctrl+C always quit.

At interpreter exit Python joins every ThreadPoolExecutor worker (whoever created the pool) and every non-daemon thread.
One worker stuck in a network call with no timeout (an order POST, a half-open connection) then blocks exit forever,
and Ctrl+C appears to do nothing. install_exit_watchdog() arms a daemon timer at the very start of that shutdown: if
the process is still alive EXIT_GRACE_SEC later, it exits hard.

Resting orders are unaffected either way: they live on Kalshi with their own expiry, exactly as with a normal exit.
"""
import os
import threading

EXIT_GRACE_SEC = 5.0
_installed = False


def install_exit_watchdog(grace: float = EXIT_GRACE_SEC) -> None:
    global _installed
    if _installed:
        return
    import concurrent.futures.thread  # noqa: F401 — registers its own exit hook first, so ours (run in reverse) goes first

    def _arm():
        t = threading.Timer(grace, lambda: os._exit(0))
        t.daemon = True
        t.start()

    # threading._register_atexit hooks run at the start of interpreter shutdown, before thread/executor joins (atexit
    # handlers run only after those joins — too late to help). Present since Python 3.9.
    threading._register_atexit(_arm)
    _installed = True
