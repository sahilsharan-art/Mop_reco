"""Read-only inspection of the source files for ONE period, before it is run.

    python scripts/inspect_period.py "Sep'26"

For each source, reads the files in the folders whose name contains the hint (a diagnostic convenience only -
the reconciliation itself never interprets a folder name, CLAUDE.md 4) with the project's OWN ingest functions, into a
throw-away warehouse directory so the real warehouse is never touched, and reports per source: file, size, modified
date, rows, the business-date range INSIDE the data, load notes, and anything the code would not recognise (a new MOP,
payment_mode, CMS TYPE or pickup code), so a schema or vocabulary change is seen before it is reconciled.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from mop_reco import ingest  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session  # noqa: E402

hint = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
if not hint:
    sys.exit("usage: inspect_period.py <folder-name hint, e.g. Sep'26>")

ingest.WAREHOUSE_ROOT = Path(tempfile.mkdtemp(prefix="inspect_warehouse_"))       # never the real one
config = load_config()
session = get_session(get_engine())
CFG = yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8"))


def pick(source: str) -> list:
    st = config.source_status(source)
    return [f for f in st.files if hint in str(f.relative_to(st.resolved_path)).lower()]


def meta(f: Path) -> str:
    return f"{f.name} | {f.stat().st_size / 1_048_576:,.1f} MB | modified {datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}"


def report(source, f, df, rep, date_col="business_date"):
    print(f"\n### {source}: {meta(f) if isinstance(f, Path) else f}")
    print(f"    rows read: {rep.rows_read:,}   status: {rep.status}")
    if date_col in df.columns and len(df):
        d = pd.to_datetime(df[date_col], errors="coerce").dropna()
        print(f"    business dates inside the data: {d.min():%Y-%m-%d} to {d.max():%Y-%m-%d}  ({d.dt.date.nunique()} distinct)")
        by_month = d.dt.to_period("M").value_counts().sort_index()
        print("    rows by month: " + ", ".join(f"{k}: {v:,}" for k, v in by_month.items()))
    print(f"    columns: {list(df.columns)}")
    for n in rep.notes:
        print(f"    note: {n}")
    for u in rep.unmapped[:40]:
        print(f"    UNMAPPED {u.source}: {u.identifier} rows={u.row_count} value={u.value:,.2f} ({u.status})")
    if len(rep.unmapped) > 40:
        print(f"    ... {len(rep.unmapped) - 40} more unmapped")
    if rep.breakdown:
        print(f"    breakdown: {rep.breakdown}")


known_mops = set(CFG["mop_classification"])
# --- POS ----------------------------------------------------------------------------------------
for f in pick("pos_bill_wise"):
    df, rep = ingest.ingest_pos_bill_wise(config, session, file_path=f, use_warehouse=False)
    report("POS bill-wise", f, df, rep)
    mops = df.groupby("mop").agg(rows=("amount", "size"), total=("amount", "sum"))
    new = [m for m in mops.index if m not in known_mops]
    print(f"    MOPs in the file: {len(mops)}; NOT classified in config: {new or 'none'}")
    print(mops.to_string())

# --- Paytm --------------------------------------------------------------------------------------
files = pick("paytm_settlement")
if files:
    df, rep = ingest.ingest_paytm(config, session, file_paths=files)
    print("\n### Paytm files: " + "; ".join(meta(f) for f in files))
    report("Paytm settlement", "(all parts)", df, rep)
    for col in ("payment_mode", "transaction_type", "status"):
        if col in df.columns:
            print(f"    {col}: {df[col].value_counts(dropna=False).to_dict()}")

# --- Axis EDC, Bajaj, CMS ----------------------------------------------------------------------
for f in pick("axis_edc"):
    df, rep = ingest.ingest_axis_edc(config, session, file_path=f)
    report("Axis EDC", f, df, rep)
for f in pick("bajaj"):
    df, rep = ingest.ingest_bajaj(config, session, file_path=f)
    report("Bajaj", f, df, rep)
    for col in ("status", "txn_status", "transaction_status"):
        if col in df.columns:
            print(f"    {col}: {df[col].value_counts(dropna=False).to_dict()}")
for f in pick("axis_cms"):
    try:
        df, rep = ingest.ingest_axis_cms(config, session, file_path=f)
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        print(f"\n### Axis CMS: {meta(f)}\n    COULD NOT BE LOADED - {type(exc).__name__}: {exc}")
        continue
    report("Axis CMS", f, df, rep, date_col="business_date" if "business_date" in df.columns else "DEPDATE")
    for col in ("TYPE", "type"):
        if col in df.columns:
            print(f"    TYPE: {df[col].value_counts(dropna=False).to_dict()}")
# --- PTC ----------------------------------------------------------------------------------------
for f in pick("ptc"):
    df, rep = ingest.ingest_ptc(config, session, file_path=f)
    report("PTC", f, df, rep, date_col="business_date" if "business_date" in df.columns else "date")
print("\n(done - the real warehouse was not touched)")
