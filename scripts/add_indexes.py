"""Create the indexes added after a database was first built (db._ADDED_INDEXES), on the database named by
MOP_RECO_DB or db/reco.sqlite. Safe to repeat. On the 3.7M-row bill-line table an index takes a minute or two.

    python scripts/add_indexes.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import db  # noqa: E402

t = time.time()
engine = db.init_db(db.get_engine())
with engine.connect() as conn:
    names = [r[1] for r in conn.exec_driver_sql("PRAGMA index_list(r1_bill_lines)")]
print(f"{db.DEFAULT_DB_PATH}: indexes on r1_bill_lines = {names}  ({time.time() - t:.0f}s)")
