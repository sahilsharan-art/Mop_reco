"""Carry the human-made records of a replaced store code over to its new code (2026-10-27).

    python scripts/migrate_store_aliases.py            # dry run: counts and collisions only
    python scripts/migrate_store_aliases.py --apply    # do it, in one transaction

For every OLD -> NEW pair in config `store_code_aliases` this renames the store (and the store inside the deterministic key) on the
records people made - exceptions with their remarks, store tasks with their replies, manual matches, float and CMS remarks and
overrides - so that after the re-run they sit under the same keys the run now produces. Reconciliation RESULTS of earlier runs
(r1_bill_lines, store_day_agg, ...) are history and are not touched. A rename that would collide with an existing record is
reported and skipped, never merged silently.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import masters  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

aliases = masters.store_aliases()
if not aliases:
    sys.exit("No store_code_aliases configured.")

# (table, store column, key column or None, unique-constraint columns to test collisions on)
TARGETS = [
    ("exceptions", "store", "exception_key"),
    ("store_tasks", "store", "task_key"),
    ("manual_matches", "store", "bill_key"),
    ("float_remarks", "store", None),
    ("float_remark_events", "store", None),
    ("cms_reco_remarks", "store", None),
    ("cms_reco_remark_events", "store", None),
    ("cms_overrides", "store", None),
]

con = sqlite3.connect(str(ROOT / "db" / "reco.sqlite"))
con.execute("pragma busy_timeout=60000")
total = 0
try:
    for old, new in aliases.items():
        print(f"== {old} -> {new}")
        for table, scol, kcol in TARGETS:
            n = con.execute(f"select count(*) from {table} where {scol}=?", (old,)).fetchone()[0]
            if not n:
                print(f"  {table}: nothing")
                continue
            skipped = 0
            if kcol:
                rows = con.execute(f"select id, {kcol} from {table} where {scol}=?", (old,)).fetchall()
                for rid, key in rows:
                    new_key = key.replace(f"|{old}|", f"|{new}|").replace(f":{old}|", f":{new}|")
                    clash = con.execute(f"select 1 from {table} where {kcol}=? and id<>?", (new_key, rid)).fetchone()
                    if clash:
                        skipped += 1
                        print(f"  {table}: key collision, skipped #{rid} {key}")
                        continue
                    if args.apply:
                        con.execute(f"update {table} set {scol}=?, {kcol}=? where id=?", (new, new_key, rid))
                    total += 1
            else:
                try:
                    if args.apply:
                        con.execute(f"update {table} set {scol}=? where {scol}=?", (new, old))
                    total += n
                except sqlite3.IntegrityError as exc:
                    skipped = n
                    print(f"  {table}: collision ({exc}); left unchanged")
            print(f"  {table}: {n} record(s), {skipped} skipped")
    if args.apply:
        con.commit()
        print(f"APPLIED: {total} record(s) renamed")
    else:
        con.rollback()
        print(f"DRY RUN: {total} record(s) would be renamed; nothing changed")
finally:
    con.close()
