"""
trade/scripts/migrate_orphans.py — one-time cleanup: split trades.csv / no_trades.csv
into real fills vs. never-filled attempts, using trade/core/logging_io.py's
is_filled()/log_unfilled_attempt() so the split matches what every strategy now
writes going forward.

Rows with final_status already terminal and non-filled (canceled/expired) are
orphans by definition — moved as-is. Rows still showing final_status='resting'
are stale, not necessarily accurate (that snapshot was taken whenever the row was
last written, sometimes months ago) — this script re-queries Kalshi for the TRUE
current status of each before deciding: if it actually filled since, it stays
(with final_status corrected); otherwise it's an orphan too.

Run once: python3 trade/scripts/migrate_orphans.py [--live]
Without --live, prints the plan and touches nothing.
"""

import os, sys, csv, argparse, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from trade.core.execution import get_order_status
from trade.core.logging_io import is_filled, log_unfilled_attempt, write_row, FILLED_STATUSES

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
TARGETS = [
    (os.path.join(LOG_DIR, 'trades.csv'),    'kp_arb', 'yes'),
    (os.path.join(LOG_DIR, 'no_trades.csv'), 'kp_arb', 'no'),
]


def _recheck(order_id: str) -> str:
    """Live status for an ambiguous 'resting' row. Falls back to 'resting' on lookup failure."""
    if not order_id:
        return 'resting'
    try:
        od = get_order_status(order_id)
        return od.get('status', 'resting') or 'resting'
    except Exception:
        return 'resting'


def plan(path: str, strategy: str) -> tuple:
    """Returns (keep_rows, orphan_rows, fields, recheck_count)."""
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)

    keep, orphans, rechecked = [], [], 0
    for r in rows:
        status = (r.get('final_status') or '').lower()
        if status in FILLED_STATUSES:
            keep.append(r)
            continue
        if status == 'resting':
            rechecked += 1
            live_status = _recheck(r.get('order_id'))
            if is_filled(live_status):
                r = dict(r)
                r['final_status'] = live_status
                keep.append(r)
                continue
            r = dict(r)
            r['final_status'] = live_status
        orphans.append(r)

    return keep, orphans, fields, rechecked


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true', help='Actually write changes (default: dry run)')
    args = p.parse_args()

    total_kept, total_orphaned = 0, 0
    for path, strategy, side in TARGETS:
        if not os.path.exists(path):
            print(f'  {path} — not found, skipping')
            continue
        keep, orphans, fields, rechecked = plan(path, strategy)
        print(f'\n{path}')
        print(f'  total={len(keep) + len(orphans)}  keep(filled)={len(keep)}  '
              f'orphan={len(orphans)}  (re-checked {rechecked} stale-resting rows live)')
        total_kept += len(keep)
        total_orphaned += len(orphans)

        if not args.live:
            continue

        for r in orphans:
            log_unfilled_attempt(
                strategy=strategy, side=side,
                order_id=r.get('order_id', ''), k_ticker=r.get('k_ticker', ''),
                sport=r.get('sport', ''), outcome=r.get('outcome', ''),
                entry_price=float(r.get('entry_price') or 0) if r.get('entry_price') else 0.0,
                contracts=int(float(r.get('contracts') or 0)) if r.get('contracts') else 0,
                final_status=r.get('final_status', ''),
                close_reason=r.get('close_reason', ''),
            )

        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(keep)
        print(f'  -> wrote {len(keep)} filled rows back, {len(orphans)} orphans -> unfilled_attempts.csv')

    print(f'\nTOTAL  keep={total_kept}  orphan={total_orphaned}')
    if not args.live:
        print('\nDRY RUN — pass --live to apply.')


if __name__ == '__main__':
    main()
