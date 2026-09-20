"""
trade/clv.py — closing-line-value (CLV) capture.

CLV asks the low-noise question "did we get a better price than the market's
final, sharpest price?" instead of waiting for win/loss outcomes. For each filled
trade we record Pinnacle's de-vigged fair probability for OUR side in the minutes
before the event starts (the "closing line"), then CLV = close_fair − entry_price
(probability points; positive = we beat the close).

Closing lines go to their OWN append-only file (trade/logs/closing_lines.csv, keyed
by order_id) — never into trades.csv / no_trades.csv, so this background job can
never race the bot's appends or settle.py's rewrites. Review joins the two.

Runs as a daemon thread started from the web app (start_background), or from the
CLI:  python -m trade.clv [--once]
"""
import os, sys, csv, time, threading
from datetime import datetime, timezone
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from theODDS.p_helpers import pinnacle_odds
from applog import get_logger

log = get_logger(__name__)

LOG_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
TRADE_LOGS   = [(os.path.join(LOG_DIR, 'trades.csv'), 'yes'),
                (os.path.join(LOG_DIR, 'no_trades.csv'), 'no')]
CLOSING_PATH = os.path.join(LOG_DIR, 'closing_lines.csv')
FIELDS       = ['order_id', 'k_ticker', 'side', 'close_fair', 'close_at', 'minutes_to_start']
WINDOW_MIN   = 12      # snapshot trades whose event starts within this many minutes
INTERVAL_SEC = 300     # each pass re-snapshots, so the last one lands within ~5 min of the start


def _candidates(now: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for path, side in TRADE_LOGS:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            continue
        df = pd.read_csv(path, usecols=lambda c: c in ('order_id', 'sport', 'outcome', 'k_ticker', 'commence'))
        df['side'] = side
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True)
    df['start'] = pd.to_datetime(df['commence'], utc=True, errors='coerce')
    df['mins']  = (df['start'] - now).dt.total_seconds() / 60
    return df[(df['mins'] > 0) & (df['mins'] <= WINDOW_MIN)].copy()


def run_once() -> int:
    """One capture pass. Returns the number of closing-line rows written."""
    now  = pd.Timestamp.now(tz='UTC')
    cand = _candidates(now)
    if cand.empty:
        return 0
    written = []
    for sport, grp in cand.groupby('sport'):
        try:
            pin = pinnacle_odds([str(sport)], hrs=1, live=False)
        except Exception:
            log.info('clv: no Pinnacle events for %s this pass', sport)
            continue
        pin = pin.assign(start=pin['commence'].dt.tz_convert('UTC'))
        for _, t in grp.iterrows():
            name = str(t['outcome'])
            name = name[3:] if name.startswith('NO:') else name
            hit  = pin[(pin['outcome'] == name) &
                       ((pin['start'] - t['start']).abs() < pd.Timedelta(seconds=90))]
            if hit.empty:
                continue
            fair = float(hit.iloc[0]['fair_prob'])
            if t['side'] == 'no':
                fair = 1 - fair
            written.append({'order_id': t['order_id'], 'k_ticker': t['k_ticker'], 'side': t['side'],
                            'close_fair': round(fair, 4),
                            'close_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                            'minutes_to_start': round(float(t['mins']), 1)})
    if written:
        os.makedirs(LOG_DIR, exist_ok=True)
        new = not os.path.exists(CLOSING_PATH) or os.path.getsize(CLOSING_PATH) == 0
        with open(CLOSING_PATH, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(written)
        log.info('clv: captured %d closing line(s)', len(written))
    return len(written)


def load_closing_lines() -> pd.DataFrame:
    """Latest closing line per order_id (the snapshot closest to the start)."""
    if not os.path.exists(CLOSING_PATH) or os.path.getsize(CLOSING_PATH) == 0:
        return pd.DataFrame(columns=FIELDS)
    df = pd.read_csv(CLOSING_PATH)
    return df.sort_values('close_at').drop_duplicates('order_id', keep='last')


def start_background(interval: int = INTERVAL_SEC) -> None:
    """Start the capture loop once per process (idempotent across Streamlit reruns)."""
    if any(t.name == 'clv-snapshotter' and t.is_alive() for t in threading.enumerate()):
        return

    def _loop():
        while True:
            try:
                run_once()
            except Exception:
                log.exception('clv: capture pass failed')
            time.sleep(interval)

    threading.Thread(target=_loop, name='clv-snapshotter', daemon=True).start()


if __name__ == '__main__':
    if '--once' in sys.argv:
        print(f'{run_once()} closing line(s) captured')
    else:
        while True:
            print(f'{datetime.now():%H:%M:%S}  captured {run_once()}')
            time.sleep(INTERVAL_SEC)
