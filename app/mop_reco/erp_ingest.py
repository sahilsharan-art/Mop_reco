"""ERP as a source for a reconciliation run (2026-11-24): POS bill lines and petty-cash vouchers read from the ERP's `gold_mop` views.

Pure engine - no Streamlit. Selected per environment by `ingest.source_mode()` (MOP_SOURCE_POS / MOP_SOURCE_PTC = erp); the default stays `file`.
Both functions return exactly what the file readers return - the normalised frame and a LoadReport that names its per-date warehouse partitions -
so R1, the store sheet, the cash book and every page cannot tell where the rows came from.

POS (`ingest_pos_from_erp`), the one that is big:
  * pulls ONE DAY per query (the DBA's rule: always a half-open date range; the login's statement limit is 120 s) through mop_reco/erp_source.py, which
    only renames columns and hands the frame to the SAME normaliser the Excel reader uses;
  * caches each pulled day as the usual warehouse partition (warehouse/pos_bill_wise/<date>.parquet) and records it in `_erp_manifest.json`;
  * the platform re-reads the last 60 days from Oracle every day (a bill IT deletes later disappears), so a day is RE-PULLED when it is inside
    `erp.reread_days` (config, default 65 = the platform's 60 plus a margin) or was never pulled by this reader; older pulled days come from the cache;
  * the first time it runs it copies the file-derived partitions to warehouse/_file_baseline/ so the Excel side of every later side-by-side
    comparison survives (the ERP partitions overwrite the same file names);
  * a day the ERP now holds no rows for has its stale partition removed - never left to be read as if it were current.
Petty cash (`ingest_ptc_from_erp`): the whole view (about 167,000 rows) in one pass, range taken from the view itself, partitions per date.

Nothing here writes to the ERP; every read is a plain SELECT through the read-only connection (mop_reco/erp.py).
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import erp, erp_source, ingest
from .config import Config
from .ingest import FileRead, IngestError, LoadReport

logger = logging.getLogger("mop_reco")

POS_SOURCE_LABEL = "erp:gold_mop.pos_bill_lines"
PTC_SOURCE_LABEL = "erp:gold_mop.ptc_vouchers"
DEFAULT_REREAD_DAYS = 65


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def reread_days(config: Config) -> int:
    return int(((config.raw or {}).get("erp") or {}).get("reread_days", DEFAULT_REREAD_DAYS))


def _manifest_path(part_dir: Path) -> Path:
    return part_dir / "_erp_manifest.json"


def _load_manifest(part_dir: Path) -> dict:
    p = _manifest_path(part_dir)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("days"), dict):
                return data
        except (ValueError, OSError):
            logger.warning("erp ingest: the ERP manifest is unreadable - every day will be pulled again")
    return {"days": {}}


def _save_manifest(part_dir: Path, manifest: dict):
    tmp = _manifest_path(part_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(_manifest_path(part_dir))


def _protect_file_baseline(part_dir: Path, source: str):
    """Before the ERP's partitions overwrite the file-derived ones, keep a copy of the latter (once)."""
    baseline = part_dir.parent / "_file_baseline" / source
    if part_dir.is_dir() and not _manifest_path(part_dir).is_file() and not baseline.exists():
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(part_dir, baseline)
        logger.info(f"erp ingest[{source}]: copied the file-derived partitions to {baseline} before the first ERP pull")


def days_to_pull(days: list, manifest: dict, today: date, rereads: int, part_dir: Path) -> list:
    """Which dates must be read from the ERP now: inside the platform's re-read window, never pulled by this reader, or whose cached
    partition has gone missing. Everything else comes from the cache."""
    cutoff = today - timedelta(days=rereads)
    out = []
    for d in days:
        entry = manifest["days"].get(d.isoformat())
        if d >= cutoff or entry is None:
            out.append(d)
        elif int(entry.get("rows", 0)) > 0 and not (part_dir / f"{d.isoformat()}.parquet").is_file():
            out.append(d)
    return out


def ingest_pos_from_erp(config: Config, session, date_from, date_to, on_progress=None, today: date = None) -> tuple:
    if date_from is None or date_to is None:
        raise IngestError("Reading POS from the ERP needs the run's date range (from and to): the ERP is read one date at a time, "
                          "never as an open-ended pull.")
    d_from, d_to = _as_date(date_from), _as_date(date_to)
    if d_to < d_from:
        raise IngestError("the run's end date is before its start date")
    today = today or date.today()
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
    part_dir = ingest.WAREHOUSE_ROOT / "pos_bill_wise"
    part_dir.mkdir(parents=True, exist_ok=True)
    _protect_file_baseline(part_dir, "pos_bill_wise")
    manifest = _load_manifest(part_dir)
    need = days_to_pull(days, manifest, today, reread_days(config), part_dir)
    logger.info(f"erp ingest[pos]: {len(days)} dates in range; reading {len(need)} from the ERP, {len(days) - len(need)} from the warehouse cache")

    pulled_rows = 0
    if need:
        conn = erp.connect()
        try:
            erp_source.check_pos_schema(conn)
            for i, d in enumerate(need, 1):
                raw = next(erp_source.iter_pos_raw(conn, d, d, chunk_days=1))
                df_day = erp_source.normalise_pos(config, raw)
                target = part_dir / f"{d.isoformat()}.parquet"
                if len(df_day):
                    df_day.to_parquet(target, index=False)
                else:
                    target.unlink(missing_ok=True)       # the ERP holds nothing for this day now: no stale partition may stand in for it
                manifest["days"][d.isoformat()] = {"pulled_at": datetime.now().isoformat(timespec="seconds"), "rows": int(len(df_day))}
                pulled_rows += len(df_day)
                if i % 10 == 0 or i == len(need):
                    _save_manifest(part_dir, manifest)
                    if on_progress:
                        on_progress(f"read {i} of {len(need)} POS dates from the ERP ({d.isoformat()})")
        finally:
            conn.close()
            _save_manifest(part_dir, manifest)

    parts, warehouse_paths = [], {}
    for d in days:
        f = part_dir / f"{d.isoformat()}.parquet"
        if f.is_file():
            parts.append(pd.read_parquet(f))
            warehouse_paths[d.isoformat()] = str(f)
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if df.empty:
        raise IngestError(f"The ERP holds no POS bill lines for {d_from} to {d_to}.")
    stamp = max((datetime.fromisoformat(manifest["days"][d.isoformat()]["pulled_at"]) for d in days if d.isoformat() in manifest["days"]),
                default=datetime.now())
    files = [(Path(POS_SOURCE_LABEL), stamp)]
    meta = {str(files[0][0]): {"sheet_row_counts": {}, "rows_without_date": 0, "from_warehouse": not need, "missing_optional_columns": []}}
    report = ingest._pos_report_from_df(df, files, meta, sheet_row_counts={}, warehouse_paths=warehouse_paths, rows_without_date=0,
                                        from_warehouse=not need, supersede_report=None, files_missing_optional=None)
    report.notes.append(f"Source: the ERP (gold_mop.pos_bill_lines), read-only. {len(need)} date(s) read from the ERP now "
                        f"({pulled_rows:,} lines), {len(days) - len(need)} from the warehouse cache of earlier ERP reads.")
    return df, report


def ingest_ptc_from_erp(config: Config, session) -> tuple:
    """The petty-cash vouchers: the whole ERP view in one pass (it is small), range taken from the view itself."""
    conn = erp.connect()
    try:
        erp_source.check_ptc_schema(conn)
        lo, hi = erp.run_select(conn, f"select min(business_date), max(business_date) from {erp_source.PTC_VIEW}")[0]
        if lo is None:
            raise IngestError("The ERP holds no petty-cash vouchers.")
        d_lo, d_hi = _as_date(lo), _as_date(hi)
        df = erp_source.read_ptc(conn, d_lo, d_hi)
    finally:
        conn.close()
    part_dir = ingest.WAREHOUSE_ROOT / "ptc"
    part_dir.mkdir(parents=True, exist_ok=True)
    _protect_file_baseline(part_dir, "ptc")
    # Partitions inside the ERP's own range that the ERP no longer holds are removed; earlier partitions (from the files) are left alone.
    present = {pd.Timestamp(v).strftime("%Y-%m-%d") for v in df["business_date"].dropna().unique()}
    for f in part_dir.glob("*.parquet"):
        if d_lo.isoformat() <= f.stem <= d_hi.isoformat() and f.stem not in present:
            f.unlink()
    layout = ingest._ptc_columns(list(erp_source.PTC_COLUMNS.values()), "ERP gold_mop.ptc_vouchers")
    files = [(Path(PTC_SOURCE_LABEL), datetime.now())]
    df, report = ingest._finish_ptc(df, files, "n/a (database)", layout, None)
    report.notes.append(f"Source: the ERP (gold_mop.ptc_vouchers), read-only; range {d_lo} to {d_hi}.")
    return df, report
