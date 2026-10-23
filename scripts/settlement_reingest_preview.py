"""PREVIEW the effect of re-ingesting Paytm and Axis settlement files - writes nothing that the application reads.

    set MOP_RECO_DB=D:\\MOP_Reco\\exports\\rehearsal\\reco_copy.sqlite
    python scripts/settlement_reingest_preview.py [preview_warehouse_dir]

Reads every Paytm zip and every Axis EDC workbook with the project's own readers into a THROW-AWAY warehouse directory and a COPY
of the database (refuses db/reco.sqlite), then writes exports/settlement_reingest/preview_ingest_report.json: files read, rows
before / after de-duplication, the transaction ids / RRNs that more than one file carried (which file won, which lost), rows without
a key, and anything that could not be parsed. The real warehouse and database are untouched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

DB = os.environ.get("MOP_RECO_DB", "")
if not DB or Path(DB).name == "reco.sqlite":
    sys.exit("Set MOP_RECO_DB to a COPY of the database (never db/reco.sqlite).")

from mop_reco import ingest  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="settle_preview_wh_"))
target.mkdir(parents=True, exist_ok=True)
ingest.WAREHOUSE_ROOT = target
cfg = load_config()
session = get_session(init_db(get_engine(Path(DB))))
result = {"preview_warehouse": str(target)}

df, rep = ingest.ingest_paytm(cfg, session)
result["paytm"] = {
    "files": [{"path": f.path, "mtime": str(f.mtime)} for f in rep.files], "rows_read": rep.rows_read,
    "rows_after_dedupe": rep.rows_after_dedupe, "duplicates": rep.breakdown.get("duplicates"),
    "ragged": rep.breakdown.get("ragged_rows"), "rows_without_date": rep.rows_without_date,
    "unmapped_mids": [(u.identifier, u.row_count, round(u.value, 2), u.status) for u in rep.unmapped],
    "superseded": getattr(rep, "superseded", [])}
del df

df, rep = ingest.ingest_axis_edc(cfg, session)
result["axis"] = {
    "files": [{"path": str(f.path), "mtime": str(f.mtime)} for f in rep.files], "rows_read": rep.rows_read,
    "rows_after_dedupe": len(df), "rows_without_date": rep.rows_without_date,
    "unmapped_mids": [(u.identifier, u.row_count, round(u.value, 2), u.status) for u in rep.unmapped],
    "notes": [str(n)[:4000] for n in rep.notes]}
out = ROOT / "exports" / "settlement_reingest"
out.mkdir(parents=True, exist_ok=True)
(out / "preview_ingest_report.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
print("preview ingest done ->", target)
