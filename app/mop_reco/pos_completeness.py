"""Is the POS export COMPLETE? (2026-11-21)

Incident: the November 2026 POS export was cut off at Excel's row limit - the single sheet held exactly 1,048,575
data rows - and every store was missing about 18% of its bills. Nothing in the load noticed; the symptom was 109,000
gateway payments (Rs 13.1 Cr) with "no bill behind them". A reconciliation can only be as good as its input, so the
input is now tested for completeness on every load, from two independent signals:

1. EXCEL ROW LIMIT. A workbook sheet holds at most 1,048,576 rows. If the LAST sheet of a file is full, the export
   very probably continued past it and was truncated (a month that spans several sheets is normal - the earlier
   sheets are full by design; only a full last sheet means rows were lost).

2. BILL-NUMBER SEQUENCE. A bill number is `<series>/<sequence>/<MMYY>` (e.g. SA25/043732/1026) and a store numbers
   its bills consecutively inside a series, so the sequence numbers present between the lowest and the highest must
   be (almost) all of them. A missing share well above the normal trickle (cancelled/never-printed numbers: 0.04%
   in October 2026) means rows are missing. Measured per month, with the worst stores named.

Pure functions (CLAUDE.md 8): frames in, dicts out. No database, no Streamlit. The thresholds are parameters
(`config.yaml` `pos_completeness`), with the defaults below.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger("mop_reco.pos_completeness")

EXCEL_SHEET_ROW_LIMIT = 1_048_576          # rows in a sheet, header included
MAX_MISSING_PCT = 1.0                       # a month with more than this share of bill numbers missing is flagged
MIN_STORE_GAP = 20                          # a store is named only if it lost at least this many bill numbers
BILL_NO_PATTERN = r"^([A-Za-z0-9]+)/(\d+)/(\d+)$"


def excel_row_limit_hits(sheet_row_counts_by_file: dict, limit: int = EXCEL_SHEET_ROW_LIMIT) -> list:
    """Files whose LAST sheet is full. `sheet_row_counts_by_file` = {file: {sheet_name: data_rows}} in sheet order.
    A sheet carries its header row too, so `data_rows + 1 >= limit` means the sheet is full."""
    hits = []
    for file, sheets in (sheet_row_counts_by_file or {}).items():
        if not sheets:
            continue
        last_name, last_rows = list(sheets.items())[-1]
        if int(last_rows) + 1 >= limit:
            hits.append({"file": str(file), "last_sheet": str(last_name), "data_rows": int(last_rows),
                         "sheets": len(sheets)})
    return hits


def _month_gap_table(chunk: pd.DataFrame):
    """Per (store, series, suffix): expected = highest - lowest + 1, present = distinct sequence numbers."""
    import polars as pl
    t = pl.from_pandas(chunk[["store", "bill_no"]].astype({"store": "string", "bill_no": "string"}))
    t = t.filter(pl.col("bill_no").is_not_null() & pl.col("store").is_not_null())
    t = t.with_columns(
        pl.col("bill_no").str.extract(BILL_NO_PATTERN, 1).alias("series"),
        pl.col("bill_no").str.extract(BILL_NO_PATTERN, 2).cast(pl.Int64, strict=False).alias("seq"),
        pl.col("bill_no").str.extract(BILL_NO_PATTERN, 3).alias("suffix"),
    ).filter(pl.col("seq").is_not_null())
    if t.height == 0:
        return None
    g = t.group_by(["store", "series", "suffix"]).agg(
        pl.col("seq").min().alias("lo"), pl.col("seq").max().alias("hi"), pl.col("seq").n_unique().alias("present"))
    return g.with_columns((pl.col("hi") - pl.col("lo") + 1).alias("expected")).with_columns(
        (pl.col("expected") - pl.col("present")).alias("missing"))


def sequence_gaps(df: pd.DataFrame, max_missing_pct: float = MAX_MISSING_PCT,
                  min_store_gap: int = MIN_STORE_GAP) -> dict:
    """The bill-number completeness of a POS frame (columns: store, bill_no, business_date), month by month.

    Returns {"by_month": {YYYY-MM: {expected, present, missing, missing_pct}}, "flagged_months": [...],
             "worst_stores": {month: [{store, missing, expected, missing_pct}]}, "threshold_pct": ...}.
    A bill number that does not look like <series>/<sequence>/<MMYY> is simply not part of the test."""
    out = {"by_month": {}, "flagged_months": [], "worst_stores": {}, "threshold_pct": max_missing_pct}
    if df is None or len(df) == 0:
        return out
    frame = df[["store", "bill_no", "business_date"]].copy()
    frame = frame[frame["bill_no"].notna() & frame["business_date"].notna()]
    frame["_month"] = frame["business_date"].astype(str).str[:7]
    for month, chunk in frame.groupby("_month"):
        g = _month_gap_table(chunk)
        if g is None:
            continue
        expected, present = int(g["expected"].sum()), int(g["present"].sum())
        missing = expected - present
        pct = round(100.0 * missing / expected, 2) if expected else 0.0
        out["by_month"][str(month)] = {"expected": expected, "present": present, "missing": missing, "missing_pct": pct}
        if pct > max_missing_pct:
            out["flagged_months"].append(str(month))
            per = {}
            for row in g.iter_rows(named=True):
                s = per.setdefault(row["store"], [0, 0])
                s[0] += int(row["missing"]); s[1] += int(row["expected"])
            ranked = sorted(((st, m, e) for st, (m, e) in per.items() if m >= min_store_gap),
                            key=lambda x: -x[1])[:15]
            out["worst_stores"][str(month)] = [
                {"store": st, "missing": m, "expected": e, "missing_pct": round(100.0 * m / e, 1)} for st, m, e in ranked]
    return out


def assess(df: pd.DataFrame, sheet_row_counts_by_file: Optional[dict] = None,
           max_missing_pct: float = MAX_MISSING_PCT) -> dict:
    """Both signals together. `ok` is False when either fires."""
    seq = sequence_gaps(df, max_missing_pct=max_missing_pct)
    limit_hits = excel_row_limit_hits(sheet_row_counts_by_file or {})
    result = dict(seq)
    result["excel_row_limit"] = limit_hits
    result["ok"] = not seq["flagged_months"] and not limit_hits
    return result


def warning_lines(result: dict) -> list:
    """Plain-language lines for the load notes, the log and the dashboard banner. Empty when all is well."""
    lines = []
    for h in result.get("excel_row_limit", []):
        lines.append(
            f"POS file '{h['file']}': its last sheet ('{h['last_sheet']}') holds {h['data_rows']:,} data rows - Excel's "
            f"limit ({EXCEL_SHEET_ROW_LIMIT:,} rows incl. header). The export was almost certainly cut off; ask for it "
            "split across sheets or as .xlsb.")
    for month in result.get("flagged_months", []):
        m = result["by_month"][month]
        worst = ", ".join(f"{w['store']} {w['missing']:,}" for w in result.get("worst_stores", {}).get(month, [])[:5])
        lines.append(
            f"POS {month}: {m['missing']:,} of {m['expected']:,} bill numbers are missing ({m['missing_pct']}%, "
            f"limit {result.get('threshold_pct', MAX_MISSING_PCT)}%)"
            + (f" - most affected: {worst}" if worst else "")
            + ". Gateway payments for those bills will show as unmapped; the export is incomplete.")
    return lines
