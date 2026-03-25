#!/usr/bin/env python3
"""Regenerate all existing digests with the new style variation system.

Processes each digest date sequentially, calling generate_digest() which
overwrites the existing digest in the database via save_digest().

Usage:
    python3 regen_digests.py              # regenerate all
    python3 regen_digests.py --from 2026-02-20  # from a specific date
    python3 regen_digests.py --date 2026-03-01  # single date only
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Configure logging before imports that use it
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

from digest import generate_digest, _select_digest_style


def main():
    parser = argparse.ArgumentParser(description="Regenerate digests with style variation")
    parser.add_argument("--date", help="Regenerate single date (YYYY-MM-DD)")
    parser.add_argument("--from", dest="date_from", help="Start from date (YYYY-MM-DD)")
    parser.add_argument("--db", default=str(Path.home() / "data" / "sieve.db"))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    if args.date:
        dates = [args.date]
    else:
        query = "SELECT digest_date FROM digests"
        params = []
        if args.date_from:
            query += " WHERE digest_date >= ?"
            params.append(args.date_from)
        query += " ORDER BY digest_date ASC"
        dates = [r[0] for r in conn.execute(query, params).fetchall()]

    conn.close()

    if not dates:
        print("No digests found matching criteria")
        return 1

    print(f"\n{'='*60}")
    print(f"  Regenerating {len(dates)} digest(s)")
    print(f"  Date range: {dates[0]} to {dates[-1]}")
    print(f"{'='*60}\n")

    # Show style assignments
    for d in dates:
        seed = int(datetime.strptime(d, "%Y-%m-%d").toordinal())
        style = _select_digest_style(seed=seed)
        print(f"  {d}: {style.name}")
    print()

    succeeded = 0
    failed = 0
    total_time = 0

    for i, date_str in enumerate(dates):
        seed = int(datetime.strptime(date_str, "%Y-%m-%d").toordinal())
        style = _select_digest_style(seed=seed)

        print(f"\n[{i+1}/{len(dates)}] {date_str} — style: {style.name}")
        print("-" * 40)

        start = time.time()
        try:
            result = generate_digest(target_date=date_str)
            elapsed = time.time() - start
            total_time += elapsed

            if result["success"]:
                succeeded += 1
                print(
                    f"  OK — {result['article_count']} articles, "
                    f"{elapsed:.0f}s"
                )
            else:
                failed += 1
                print(f"  FAILED — {result.get('error', 'unknown error')}")
        except Exception as e:
            elapsed = time.time() - start
            total_time += elapsed
            failed += 1
            print(f"  ERROR — {e}")

    print(f"\n{'='*60}")
    print(f"  Done: {succeeded} succeeded, {failed} failed")
    print(f"  Total time: {total_time/60:.1f} minutes")
    print(f"{'='*60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
