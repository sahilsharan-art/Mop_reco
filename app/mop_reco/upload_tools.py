"""The Upload screen's engine (2026-11-26): what can be uploaded, what has been uploaded for each month, what each file holds per date, and
how that compares with what the numbered folders held before they stopped being read.

Pure of Streamlit and FastAPI (CLAUDE.md section 8). Nothing here edits or deletes a source file (rule 7); a stored file is read, never changed.

* `uploadable_specs`   - the reports a person supplies: not the masters (database) and not POS / petty cash while the ERP supplies them.
* `compute_stats` / `record_stats` - a file is read ONCE when it is stored, into per-date row counts and totals (`upload_file_stats`).
* `coverage_grid`      - report x month: which months hold a file, how many dates they cover, who supplied it and when.
* `compare_with_folders` - uploaded totals per source and date against `warehouse/_folder_baseline/BASELINE.json`, written by
  `scripts/snapshot_folder_baseline.py` before the link with the folders was broken. Exact comparison (rule 1): a difference of a paisa
  is a difference.
"""
from __future__ import annotations

import calendar
import json
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from . import uploads as up
from .db import UploadedFile, UploadFileStat

logger = logging.getLogger("mop_reco")

# The reports whose files are read into per-date statistics, with the column that carries the money and the date it is grouped by.
STAT_SOURCES = {
    "paytm_settlement": "amount",
    "axis_edc": "GROSS_AMT",
    "bajaj": "Transaction Amount",
    "axis_cms": "SLIPAMOUNT",
}
BASELINE_PATH = Path(__file__).resolve().parents[2] / "warehouse" / "_folder_baseline" / "BASELINE.json"


# ---------------------------------------------------------------------------------------------------- what can be uploaded

def erp_fed_keys() -> set:
    """Sources the ERP supplies in this environment: nothing is uploaded for them."""
    from . import ingest
    out = set()
    for source in ingest.SOURCE_MODE_ENV:
        try:
            if ingest.source_mode(source) == "erp":
                out.add(source)
        except ingest.IngestError:
            continue
    return out


def uploadable_specs(session, config=None) -> list:
    """The registry less what is replaced: masters held in the database, and POS / petty cash while the ERP is their source."""
    from . import masters_db
    replaced = set(erp_fed_keys())
    if masters_db.in_database(session):
        replaced |= set(masters_db.UPLOAD_KEYS_REPLACED)
    return [s for s in up.SOURCE_REGISTRY if s.key not in replaced]


# ---------------------------------------------------------------------------------------------------- reading a file into statistics

def _clean_amount(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.strip().str.strip("'").str.replace(",", "", regex=False), errors="coerce")


def _paytm_stats(path: Path, config) -> pd.DataFrame:
    """date, amount from a Paytm settlement zip: only the two columns needed, so a 50 MB part is read in seconds."""
    frames = []
    with zipfile.ZipFile(path) as z:
        for name in (n for n in z.namelist() if n.lower().endswith(".csv")):
            with z.open(name) as f:
                header = pd.read_csv(f, nrows=0, index_col=False).columns.tolist()
            if "transaction_date" not in header or "amount" not in header:
                continue
            with z.open(name) as f:
                part = pd.read_csv(f, usecols=["transaction_date", "amount"], index_col=False, dtype=str)
            # Paytm writes 'dd-mm-yyyy hh:mm:ss' (quoted). An explicit format, never dayfirst guessing: dayfirst would read an ISO date
            # 2026-11-01 as 11 January. What does not fit the format is counted under no date, not guessed.
            when = pd.to_datetime(part["transaction_date"].str.strip().str.strip("'"), format="%d-%m-%Y %H:%M:%S", errors="coerce")
            frames.append(pd.DataFrame({"business_date": when.dt.date, "amount": _clean_amount(part["amount"])}))
    if not frames:
        raise ValueError("no Paytm settlement CSV (transaction_date and amount columns) found inside the zip")
    return pd.concat(frames, ignore_index=True)


def _frame_for(spec_key: str, path: Path, config) -> pd.DataFrame:
    from . import ingest
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    if spec_key == "paytm_settlement":
        return _paytm_stats(path, config)
    if spec_key == "axis_edc":
        df = ingest._read_axis_edc_file(path, mtime)
    elif spec_key == "bajaj":
        df = ingest._read_bajaj_file(path, mtime)
    elif spec_key == "axis_cms":
        df = ingest._read_axis_cms_file(config, path, mtime)
    else:
        raise ValueError(f"{spec_key} has no per-date statistics")
    money = STAT_SOURCES[spec_key]
    if money not in df.columns:
        raise ValueError(f"column {money!r} is missing from the {spec_key} file as the run reads it")
    return pd.DataFrame({"business_date": df["business_date"], "amount": _clean_amount(df[money])})


def compute_stats(spec_key: str, path: Path, config) -> list:
    """[(ISO date, rows, amount)] for one stored file, from the same readers a run uses. Rows with no readable date are not attributed to
    a day and are counted under date '' so they are never invisible."""
    frame = _frame_for(spec_key, Path(path), config)
    frame["d"] = frame["business_date"].map(lambda v: v.isoformat() if hasattr(v, "isoformat") and not pd.isna(v) else "")
    grouped = frame.groupby("d", dropna=False).agg(rows=("amount", "size"), amount=("amount", "sum")).reset_index()
    return [(r.d, int(r.rows), round(float(r.amount), 2)) for r in grouped.itertuples(index=False)]


def record_stats(session, row: UploadedFile, config) -> None:
    """Read the stored file once and write its per-date statistics. A failure is recorded on the upload, never raised: the file is stored
    either way and the run reads it itself."""
    session.query(UploadFileStat).filter(UploadFileStat.upload_id == row.id).delete()
    row.stats_at, row.stats_error = None, None
    if row.source_key not in STAT_SOURCES:
        row.stats_at = datetime.now()
        session.commit()
        return
    try:
        for d, n, amount in compute_stats(row.source_key, Path(row.stored_path), config):
            session.add(UploadFileStat(upload_id=row.id, source_key=row.source_key, business_date=d, rows=n, amount=amount))
        row.stats_at = datetime.now()
    except Exception as exc:  # noqa: BLE001 - recorded on the upload, shown on the screen
        logger.warning("upload %s: statistics could not be read: %s", row.id, exc)
        session.rollback()
        row = session.get(UploadedFile, row.id)
        row.stats_error = f"{type(exc).__name__}: {exc}"[:500]
    session.commit()


def pending_stats(session) -> list:
    """Stored uploads whose statistics have not been read (and did not fail): a restart or a crash left them behind."""
    return (session.query(UploadedFile)
            .filter(UploadedFile.status == "stored", UploadedFile.stats_at.is_(None), UploadedFile.stats_error.is_(None),
                    UploadedFile.source_key.in_(list(STAT_SOURCES))).order_by(UploadedFile.id).all())


# ---------------------------------------------------------------------------------------------------- the month grid

def first_month(config) -> str:
    """The first month the grid shows, from config (uploads.first_month, 'YYYY-MM') - never a date typed into code (rule 8)."""
    value = ((config.raw or {}).get("uploads") or {}).get("first_month")
    if not value:
        raise ValueError("config.yaml has no uploads.first_month ('YYYY-MM') - the first month the Upload grid starts from.")
    date.fromisoformat(f"{value}-01")
    return str(value)


def months_between(first: str, last: date) -> list:
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def coverage_grid(session, config, today: Optional[date] = None, specs: Optional[list] = None) -> dict:
    """Report x month. A cell holds the active (not withdrawn) files for that month: how many, rows, the dates actually present (read from
    the file) against the days that month has so far, and who supplied the latest one and when."""
    today = today or date.today()
    specs = [s for s in (specs if specs is not None else uploadable_specs(session, config)) if s.month_specific]
    months = months_between(first_month(config), today)
    rows = session.query(UploadedFile).filter(UploadedFile.status == "stored").all()
    stats = {}
    for s in session.query(UploadFileStat.upload_id, UploadFileStat.business_date, UploadFileStat.rows).all():
        stats.setdefault(s.upload_id, []).append((s.business_date, s.rows))
    cells = {}
    for r in rows:
        cell = cells.setdefault((r.source_key, r.month), {"files": 0, "rows": 0, "dates": set(), "pending": 0, "failed": 0,
                                                           "start": None, "end": None, "latest": None})
        cell["files"] += 1
        cell["rows"] += int(r.row_count or 0)
        if r.stats_error:
            cell["failed"] += 1
        elif r.stats_at is None and r.source_key in STAT_SOURCES:
            cell["pending"] += 1
        for d, _n in stats.get(r.id, []):
            if d and d[:7] == r.month:
                cell["dates"].add(d)
        for edge, pick in (("start", min), ("end", max)):
            v = getattr(r, "date_range_" + edge)
            if v:
                cell[edge] = v if cell[edge] is None else pick(cell[edge], v)
        if cell["latest"] is None or r.uploaded_at > cell["latest"][0]:
            cell["latest"] = (r.uploaded_at, r.uploaded_by)
    out_rows = []
    for spec in specs:
        line = {"source_key": spec.key, "report": spec.display_name, "months": {}}
        for month in months:
            c = cells.get((spec.key, month))
            if c is None:
                line["months"][month] = {"state": "none"}
                continue
            y, m = int(month[:4]), int(month[5:7])
            so_far = calendar.monthrange(y, m)[1] if (y, m) < (today.year, today.month) else today.day
            have = len(c["dates"])
            state = "present"
            if have and have < so_far and spec.key in STAT_SOURCES:
                state = "partial"
            line["months"][month] = {
                "state": state, "files": c["files"], "rows": c["rows"], "dates_present": have, "days_so_far": so_far,
                "from": str(c["start"] or "")[:10], "to": str(c["end"] or "")[:10],
                "pending": c["pending"], "failed": c["failed"],
                "by": c["latest"][1] if c["latest"] else "", "at": c["latest"][0].isoformat(timespec="minutes") if c["latest"] else "",
            }
        out_rows.append(line)
    return {"months": months, "rows": out_rows}


# ---------------------------------------------------------------------------------------------------- the comparison with the folders

def _load_baseline(path: Optional[Path] = None) -> Optional[dict]:
    p = Path(path or BASELINE_PATH)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _diff_source(source: str, folder: dict, uploaded: dict, limit: int) -> dict:
    """folder / uploaded: {ISO date: (rows, amount)}. Exact to the paisa; nothing is netted away."""
    both = sorted(set(folder) & set(uploaded))
    same = {d for d in both if folder[d][0] == uploaded[d][0] and abs(round(folder[d][1], 2) - round(uploaded[d][1], 2)) < 0.005}
    different = [d for d in both if d not in same]
    only_folder = sorted(set(folder) - set(uploaded))
    only_upload = sorted(set(uploaded) - set(folder))
    details = []
    for d in different:
        details.append({"date": d, "kind": "differs", "folder_rows": folder[d][0], "upload_rows": uploaded[d][0],
                        "folder_amount": round(folder[d][1], 2), "upload_amount": round(uploaded[d][1], 2),
                        "amount_difference": round(uploaded[d][1] - folder[d][1], 2)})
    for d in only_folder:
        details.append({"date": d, "kind": "folder only", "folder_rows": folder[d][0], "upload_rows": 0,
                        "folder_amount": round(folder[d][1], 2), "upload_amount": 0.0, "amount_difference": round(-folder[d][1], 2)})
    for d in only_upload:
        details.append({"date": d, "kind": "upload only", "folder_rows": 0, "upload_rows": uploaded[d][0],
                        "folder_amount": 0.0, "upload_amount": round(uploaded[d][1], 2), "amount_difference": round(uploaded[d][1], 2)})
    details.sort(key=lambda r: (-abs(r["amount_difference"]), r["date"]))
    return {
        "source_key": source, "report": up.SOURCE_BY_KEY[source].display_name if source in up.SOURCE_BY_KEY else source,
        "folder_dates": len(folder), "upload_dates": len(uploaded), "dates_equal": len(same), "dates_differ": len(different),
        "dates_folder_only": len(only_folder), "dates_upload_only": len(only_upload),
        "folder_rows": sum(v[0] for v in folder.values()), "upload_rows": sum(v[0] for v in uploaded.values()),
        "folder_amount": round(sum(v[1] for v in folder.values()), 2), "upload_amount": round(sum(v[1] for v in uploaded.values()), 2),
        "details": details[:limit], "details_total": len(details),
    }


def _folder_side(base: dict) -> dict:
    return {d: (int(v["rows"]), round(float(v["amount"] or 0.0), 2)) for d, v in base["per_date"].items()}


def compare_with_folders(session, baseline_path: Optional[Path] = None, limit: int = 300) -> dict:
    """QUICK view: each uploaded file's own per-date statistics, summed, against what the numbered folders held. Fast (no file is read), but
    files that overlap each other are counted twice here although a run keeps one row (a re-exported month, a late row in the next
    month's file) - so a date where the uploads show MORE than the folders is usually that overlap, and a date where they show LESS is
    missing data. The exact, deduplicated comparison is `run_exact_comparison`."""
    baseline = _load_baseline(baseline_path)
    if baseline is None:
        return {"available": False,
                "message": "No folder baseline was recorded (scripts/snapshot_folder_baseline.py) before the folders stopped being read."}
    out = {"available": True, "baseline_created_at": baseline.get("created_at"), "sources": []}
    for source, base in baseline["sources"].items():
        ids = [r.id for r in session.query(UploadedFile.id).filter(UploadedFile.status == "stored", UploadedFile.source_key == source).all()]
        read = session.query(UploadedFile).filter(UploadedFile.status == "stored", UploadedFile.source_key == source,
                                                  UploadedFile.stats_at.isnot(None)).count()
        uploaded = {}
        if ids:
            for st in session.query(UploadFileStat).filter(UploadFileStat.upload_id.in_(ids)).all():
                cur = uploaded.setdefault(st.business_date, [0, 0.0])
                cur[0] += int(st.rows)
                cur[1] += float(st.amount or 0.0)
        row = _diff_source(source, _folder_side(base), {d: (v[0], v[1]) for d, v in uploaded.items()}, limit)
        row.update({"uploaded_files": len(ids), "files_read": read})
        out["sources"].append(row)
    return out


# ---------------------------------------------------------------------------------------------------- the exact comparison (a background job)

COMPARE_PATH = Path(__file__).resolve().parents[2] / "warehouse" / "_upload_compare.json"
_INGEST = {"paytm_settlement": "ingest_paytm", "axis_edc": "ingest_axis_edc", "bajaj": "ingest_bajaj", "axis_cms": "ingest_axis_cms"}


def read_comparison(path: Optional[Path] = None) -> dict:
    p = Path(path or COMPARE_PATH)
    if not p.exists():
        return {"state": "never"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "never"}


def _save(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
    tmp.replace(path)


def _per_date(df: pd.DataFrame, money: str) -> dict:
    if df is None or len(df) == 0 or "business_date" not in df.columns:
        return {}
    keep = df[df["business_date"].notna()]
    amounts = _clean_amount(keep[money]).fillna(0.0) if money in keep.columns else pd.Series(0.0, index=keep.index)
    frame = pd.DataFrame({"d": keep["business_date"].map(lambda v: pd.Timestamp(v).strftime("%Y-%m-%d")), "a": amounts})
    grouped = frame.groupby("d").agg(rows=("a", "size"), amount=("a", "sum"))
    return {d: (int(r.rows), round(float(r.amount), 2)) for d, r in grouped.iterrows()}


def run_exact_comparison(config, session, baseline_path: Optional[Path] = None, out_path: Optional[Path] = None, limit: int = 300) -> dict:
    """Read every uploaded file of each source with the run's OWN readers (so overlaps are resolved exactly as a run resolves them, rows
    deduplicated, stores resolved) and compare the result per date with what the folders produced. The warehouse is not written. Slow
    (minutes for Paytm), so it runs in the background; its state and result are saved to disk for the screen."""
    from . import ingest
    out_path = Path(out_path or COMPARE_PATH)
    baseline = _load_baseline(baseline_path)
    if baseline is None:
        raise ValueError("No folder baseline was recorded (scripts/snapshot_folder_baseline.py).")
    doc = {"state": "running", "started_at": datetime.now().isoformat(timespec="seconds"), "progress": [], "sources": []}
    _save(out_path, doc)
    try:
        for source, base in baseline["sources"].items():
            doc["progress"].append(f"reading {source} ...")
            _save(out_path, doc)
            with ingest.dry_run():
                df, report = getattr(ingest, _INGEST[source])(config, session)
            row = _diff_source(source, _folder_side(base), _per_date(df, base["money_column"]), limit)
            row["note"] = "; ".join(getattr(report, "notes", [])[:3]) if report is not None else ""
            doc["sources"].append(row)
            doc["progress"][-1] = f"{source} done"
            _save(out_path, doc)
        doc.update(state="done", finished_at=datetime.now().isoformat(timespec="seconds"), baseline_created_at=baseline.get("created_at"))
    except Exception as exc:  # noqa: BLE001 - recorded for the screen, never swallowed
        logger.exception("exact comparison failed")
        doc.update(state="failed", finished_at=datetime.now().isoformat(timespec="seconds"), error=f"{type(exc).__name__}: {exc}"[:500])
    _save(out_path, doc)
    return doc
