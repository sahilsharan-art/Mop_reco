"""Upload — supplying or replacing a single report for a single month
without touching the numbered source folders directly (2026-09-30).

The numbered folders (CLAUDE.md section 4) stay the PRIMARY way data
arrives; this module is the validated, audited side door. Every upload:

  1. is validated BEFORE anything is written to disk — extension, required
     columns (by header name, case/whitespace insensitive), and the dates
     actually inside the file against the month the operator selected;
  2. is previewed (row count, date range, columns found, first 5 rows) so
     the operator confirms before anything is stored — nothing is stored
     on the strength of passing validation alone;
  3. lands in the SAME folder a run would already discover it from, under
     its own original name (never overwriting — a colliding name gets a
     timestamp suffix), so CLAUDE.md's own "most recently modified file
     wins" rule (section 4) picks it up with no code change;
  4. is recorded in `uploaded_files`, an audit trail that is never deleted
     — a withdrawn file's row is marked deleted, not removed (rule 4).

Pure functions plus a handful of session-backed helpers, same shape as
manual_match.py / refunds.py: DataFrames/dataclasses in and out, no
Streamlit import (CLAUDE.md section 8).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from . import io_
from .config import Config
from .db import RunFile, UploadedFile

MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# The source registry — one entry per report the operator can supply.
# Driven from here, not hardcoded in the page, so the dropdown can never
# drift from what the app actually knows how to validate (the operator's
# own instruction).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceSpec:
    key: str                      # config.yaml `sources:` key this feeds
    display_name: str              # exactly what the dropdown shows
    extensions: tuple              # allowed suffixes, lowercase, with the dot
    month_specific: bool
    # Column check: a plain list of header names to look for (case/
    # whitespace insensitive), or None when no schema is documented yet
    # (bank statement, holiday calendar — CLAUDE.md's own open items) —
    # never guessed, reported as "not checked" rather than invented.
    required_columns: Optional[tuple] = None
    # Which sheet(s) to look in for a multi-sheet workbook, or None for
    # "the file's own sheet(s), whichever they are". The MID master alone
    # needs two named sheets checked independently.
    sheet_names: Optional[tuple] = None
    note: str = ""


SOURCE_REGISTRY = [
    SourceSpec("pos_bill_wise", "POS bill-wise", (".xlsb", ".xlsx"), True,
              ("BILLNO", "PSITE_POSBILL_CODE", "STORE_NAME", "BILLDATE",
               "MOPDESC", "BASEAMT")),
    SourceSpec("paytm_settlement", "Paytm settlement", (".zip",), True,
              ("transaction_id", "transaction_date", "status", "transaction_type",
               "original_mid", "amount", "settled_amount", "settled_date",
               "payment_mode")),
    SourceSpec("axis_edc", "Axis EDC settlement", (".xlsx",), True,
              ("RRN", "TRAN_DATE", "TRAN_TIME", "MID", "GROSS_AMT", "NET_AMT")),
    SourceSpec("bajaj", "Bajaj Pay settlement", (".xlsx",), True,
              ("Transaction Id", "Transaction Date", "Transaction Amount",
               "Transaction Status", "TID", "MID")),
    SourceSpec("axis_cms", "CMS received (Axis)", (".zip", ".xlsx"), True,
              ("TYPE", "DEPDATE", "CRDATE", "PICKUPPOINTCODE"),
              note="Production reads this file by column POSITION, not name "
                   "(CLAUDE.md section 5 — two duplicate header names exist). "
                   "This check confirms the expected names appear somewhere in "
                   "the header row, as a sanity check, not a guarantee of column "
                   "order."),
    SourceSpec("ptc", "Petty cash ledger (PTC)", (".csv",), True,
              None,  # layout A/B, checked specially — see _validate_ptc
              note="Layout A or B, detected by which columns are present, "
                   "never by month (CLAUDE.md section 5)."),
    SourceSpec("pickup_code_master", "CMS deposit master", (".xlsx",), False,
              ("PICKCODE", "CODE")),
    SourceSpec("pickup_location_master", "Pickup location master", (".xlsx",), False,
              ("PICKCODE", "LOCATION", "CODE")),
    SourceSpec("mid_master", "MID wise store code master", (".xlsx",), False,
              ("MID", "Store Code"),
              sheet_names=("Axis mid wise store", "Paytm Mid wise store code")),
    SourceSpec("store_master", "Store master (active site vs store code)",
              (".xlsx",), False, ("SITES NAMES", "Store Code")),
    SourceSpec("opening_cash", "Opening cash balance", (".xlsx",), True,
              ("Cost Centre", "Opening"),
              note="The as-at date comes from the file's own 'Period:' line, "
                   "never the filename (rule 8)."),
    SourceSpec("bank_statement", "Bank statement", (".xlsx", ".csv"), True,
              None, note="No column schema documented yet (CLAUDE.md open "
                         "item) — column and date-range checks are skipped "
                         "and reported as skipped, never guessed."),
    SourceSpec("holiday_calendar", "Public holiday calendar", (".xlsx", ".csv"),
              False, None, note="No column schema documented yet (CLAUDE.md "
                                "open item) — column check is skipped and "
                                "reported as skipped, never guessed."),
]

SOURCE_BY_KEY = {s.key: s for s in SOURCE_REGISTRY}


# ---------------------------------------------------------------------------
# Validation + preview
# ---------------------------------------------------------------------------

@dataclass
class UploadPreview:
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    row_count: int = 0
    date_range: Optional[tuple] = None       # (min, max) as ISO strings, or None
    columns_found: list = field(default_factory=list)
    first_rows: Optional[pd.DataFrame] = None
    encoding_used: Optional[str] = None


def _normalise(name: str) -> str:
    return " ".join(str(name).split()).strip().lower()


def _find_header_row_loose(grid: list, required: tuple, scan_limit: int = 30) -> Optional[int]:
    """Like io_.find_header_row, but case/whitespace insensitive and never
    raises — returns None (reported by the caller) instead of an IOError,
    since a preview must be able to say "not found" rather than crash the
    page an operator is trying to fix a mistake on."""
    wanted = {_normalise(r) for r in required}
    for idx, row in enumerate(grid[:scan_limit]):
        cells = {_normalise(c) for c in row if c is not None}
        if wanted.issubset(cells):
            return idx
    return None


def _month_bounds(month: str) -> tuple:
    """'YYYY-MM' -> (first day, last day) as date objects."""
    year, mon = int(month[:4]), int(month[5:7])
    from calendar import monthrange
    last_day = monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last_day)


def _extract_dates(df: pd.DataFrame, date_col: str) -> pd.Series:
    return pd.Series(io_.parse_business_date_series(df[date_col])).dropna()


def validate_upload(spec: SourceSpec, temp_path: Path, original_name: str,
                    month: Optional[str], config: Config) -> UploadPreview:
    """Validates a file already saved to a temp path. Nothing is stored yet
    — this only ever reads. `month` is None for a master (the Month control
    is disabled on the page for those)."""
    prev = UploadPreview(ok=True)

    suffix = temp_path.suffix.lower()
    if suffix not in spec.extensions:
        prev.ok = False
        prev.errors.append(
            f"'{original_name}' has extension '{suffix}', but {spec.display_name} "
            f"accepts {', '.join(spec.extensions)} only.")
        return prev

    if spec.key == "ptc":
        return _validate_ptc(temp_path, original_name, month, config)

    encoding = None
    if suffix in (".csv", ".zip"):
        encoding = config.encoding_for(spec.key) or "utf-8"
    prev.encoding_used = encoding

    try:
        if spec.sheet_names:
            return _validate_multi_sheet(spec, temp_path, original_name, encoding)
        result = io_.read_any(temp_path, encoding=encoding)
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator verbatim
        prev.ok = False
        prev.errors.append(f"Could not read '{original_name}': {type(exc).__name__}: {exc}")
        return prev

    grid = result.grid
    if spec.required_columns:
        header_idx = _find_header_row_loose(grid, spec.required_columns)
        if header_idx is None:
            prev.ok = False
            prev.errors.append(
                "Could not find a header row containing all of "
                f"{list(spec.required_columns)} in the first 30 rows.")
            return prev
    else:
        header_idx = 0
        prev.warnings.append(
            f"No column schema documented for {spec.display_name} yet — "
            "column check skipped (reported, not guessed)." + (f" {spec.note}" if spec.note else ""))

    header = [str(c).strip() for c in grid[header_idx]]
    data_rows = grid[header_idx + 1:]
    df = pd.DataFrame(data_rows, columns=header)
    df = df[[c for c in df.columns if c and str(c).strip() and not str(c).startswith("Unnamed")]]
    prev.columns_found = list(df.columns)
    prev.row_count = len(df)
    prev.first_rows = df.head(5)

    date_col = _guess_date_column(spec, df.columns)
    if spec.month_specific and month:
        if date_col is None:
            prev.warnings.append(
                "Could not identify a date column to check against the selected "
                "month — the file will still be stored, but the month/date-range "
                "check could not run.")
        else:
            dates = _extract_dates(df, date_col)
            if dates.empty:
                prev.warnings.append(f"No parseable dates found in column '{date_col}'.")
            else:
                found_months = sorted({f"{d.year:04d}-{d.month:02d}" for d in dates})
                prev.date_range = (str(min(dates)), str(max(dates)))
                if month not in found_months:
                    prev.ok = False
                    prev.errors.append(
                        f"Selected month is {month}, but the file's own dates fall in "
                        f"{', '.join(found_months)}. A file dropped into the wrong "
                        "month is the most likely mistake — rejected rather than "
                        "guessed which one is right."
                    )
    return prev


def _guess_date_column(spec: SourceSpec, columns) -> Optional[str]:
    by_source = {
        "pos_bill_wise": "BILLDATE",
        "paytm_settlement": "transaction_date",
        "axis_edc": "TRAN_DATE",
        "bajaj": "Transaction Date",
        "axis_cms": "DEPDATE",
        "opening_cash": None,  # date comes from the file's own "Period:" line
        "bank_statement": None,
    }
    wanted = by_source.get(spec.key)
    if wanted and wanted in columns:
        return wanted
    return None


def _validate_multi_sheet(spec: SourceSpec, temp_path: Path, original_name: str,
                          encoding) -> UploadPreview:
    """The MID master — two named sheets, each checked independently."""
    prev = UploadPreview(ok=True)
    total_rows = 0
    all_columns = []
    first_rows = None
    for sheet in spec.sheet_names:
        try:
            result = io_.read_any(temp_path, sheet_name=sheet)
        except Exception as exc:  # noqa: BLE001
            prev.ok = False
            prev.errors.append(f"Sheet '{sheet}' — could not read: {type(exc).__name__}: {exc}")
            continue
        header_idx = _find_header_row_loose(result.grid, spec.required_columns)
        if header_idx is None:
            prev.ok = False
            prev.errors.append(
                f"Sheet '{sheet}': could not find a header row containing "
                f"{list(spec.required_columns)}.")
            continue
        header = [str(c).strip() for c in result.grid[header_idx]]
        df = pd.DataFrame(result.grid[header_idx + 1:], columns=header)
        total_rows += len(df)
        all_columns.append(f"{sheet}: {list(df.columns)}")
        if first_rows is None:
            first_rows = df.head(5)
    prev.row_count = total_rows
    prev.columns_found = all_columns
    prev.first_rows = first_rows
    return prev


def _validate_ptc(temp_path: Path, original_name: str, month: Optional[str],
                  config: Config) -> UploadPreview:
    from . import ingest as ingest_mod
    prev = UploadPreview(ok=True)
    encoding = config.encoding_for("ptc") or "cp1252"
    prev.encoding_used = encoding
    try:
        result = io_.read_any(temp_path, encoding=encoding)
        header_idx = ingest_mod._ptc_header_row(result.grid, original_name)
    except Exception as exc:  # noqa: BLE001
        prev.ok = False
        prev.errors.append(f"Could not read '{original_name}': {type(exc).__name__}: {exc}")
        return prev

    header = [str(c).strip() for c in result.grid[header_idx]]
    cols = ingest_mod._ptc_columns(header, original_name)
    missing = [f for f in ingest_mod.PTC_ALIASES if f not in cols]
    if missing:
        prev.ok = False
        prev.errors.append(f"Missing PTC field(s) under either known layout: {missing}")
        return prev

    df = pd.DataFrame(result.grid[header_idx + 1:], columns=header)
    df = df.rename(columns={actual: logical for logical, actual in cols.items()})
    prev.columns_found = [f"{logical} <- {actual}" for logical, actual in cols.items()]
    prev.row_count = len(df)
    prev.first_rows = df[list(ingest_mod.PTC_ALIASES)].head(5)

    dates = _extract_dates(df.rename(columns={"date": "date"}), "date")
    if dates.empty:
        prev.warnings.append("No parseable dates found in the date column.")
    else:
        found_months = sorted({f"{d.year:04d}-{d.month:02d}" for d in dates})
        prev.date_range = (str(min(dates)), str(max(dates)))
        if month and month not in found_months:
            prev.ok = False
            prev.errors.append(
                f"Selected month is {month}, but the file's own dates fall in "
                f"{', '.join(found_months)}."
            )
    return prev


# ---------------------------------------------------------------------------
# Where a file lands — same folder a run would already discover it from
# ---------------------------------------------------------------------------

def _month_folder_name(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{MONTH_LABELS[mon - 1]}'{year % 100:02d}"


def resolve_destination_dir(spec: SourceSpec, month: Optional[str], config: Config) -> tuple:
    """Returns (dir_path, config_gap_note). `config_gap_note` is non-None
    when the source has no configured path at all (bank_statement /
    holiday_calendar) — a new folder is created, but config.yaml itself is
    NOT edited automatically (rule 5: nothing this consequential is
    silently wired in) — the operator must add the `sources:` entry by
    hand before a run will discover it, and the note says so plainly."""
    base = config.sources.get(spec.key)
    gap_note = None
    if base is None:
        base = Path(config.base_dir) / f"{len(SOURCE_REGISTRY) + 1}. {spec.display_name}"
        gap_note = (
            f"config.yaml has no path configured for '{spec.key}' yet. Created "
            f"{base} to hold this upload, but a RUN will not discover it until "
            f"you add `sources.{spec.key}: \"{base.relative_to(Path(config.base_dir))}\"` "
            "to config.yaml by hand — never done automatically."
        )
    elif spec.key == "opening_cash":
        # Scoped to "April Opening Balance" specifically (CLAUDE.md section 5)
        # so latest_file() can't cross-pick a sibling month — the upload
        # target is the PARENT folder, with a month subfolder under it.
        base = base.parent

    if not spec.month_specific:
        return base, gap_note

    folder_name = _month_folder_name(month)
    # Reuse an existing month folder if one already exists under a
    # differently-spelled name (CLAUDE.md section 4: naming is never
    # interpreted) — match on the month's 3-letter name and 2-digit year
    # both appearing in the existing folder's own name.
    year, mon = int(month[:4]), int(month[5:7])
    mon_abbr = MONTH_LABELS[mon - 1].lower()
    yy = f"{year % 100:02d}"
    if base.exists():
        for child in base.iterdir():
            if not child.is_dir():
                continue
            lowered = child.name.lower()
            if mon_abbr in lowered and yy in lowered:
                return child, gap_note
    return base / folder_name, gap_note


def store_file(spec: SourceSpec, temp_path: Path, original_name: str,
              month: Optional[str], operator: str, session, config: Config,
              row_count: int, date_range: Optional[tuple]) -> UploadedFile:
    """Moves the validated temp file into its real destination — never
    overwriting (a colliding name gets a timestamp suffix, and that is
    reported back to the caller via the returned row's own file name)."""
    dest_dir, gap_note = resolve_destination_dir(spec, month, config)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / original_name
    renamed = False
    if dest_path.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        stem, suffix = Path(original_name).stem, Path(original_name).suffix
        dest_path = dest_dir / f"{stem}__uploaded-{stamp}{suffix}"
        renamed = True

    shutil.move(str(temp_path), str(dest_path))

    row = UploadedFile(
        uploaded_at=datetime.now(),
        source_key=spec.key,
        report_name=spec.display_name,
        month=month,
        original_file_name=original_name,
        stored_path=str(dest_path),
        size_bytes=dest_path.stat().st_size,
        row_count=row_count,
        date_range_start=date_range[0] if date_range else None,
        date_range_end=date_range[1] if date_range else None,
        uploaded_by=operator,
        status="stored",
    )
    session.add(row)
    session.commit()
    row._renamed = renamed
    row._dest_path = dest_path
    row._config_gap_note = gap_note
    return row


def used_by_run(session, stored_path: str) -> Optional[int]:
    """The most recent run that consumed this exact file, or None."""
    rf = (session.query(RunFile).filter(RunFile.file_path == stored_path)
          .order_by(RunFile.run_id.desc()).first())
    return rf.run_id if rf else None


def history(session) -> list:
    return session.query(UploadedFile).order_by(UploadedFile.uploaded_at.desc()).all()


def delete_upload(session, upload_id: int, operator: str, run_in_progress: bool) -> tuple:
    """Removes the file on disk, marks the history row deleted — the row
    itself is never removed (rule 4). Refused outright while a run is in
    progress. Returns (ok, message)."""
    if run_in_progress:
        return False, "A run is currently in progress — deletion is refused until it finishes."

    row = session.query(UploadedFile).get(upload_id)
    if row is None:
        return False, "No such upload."
    if row.status == "deleted":
        return False, "Already deleted."

    path = Path(row.stored_path)
    if path.exists():
        path.unlink()

    row.status = "deleted"
    row.deleted_at = datetime.now()
    row.deleted_by = operator
    session.commit()
    return True, f"Deleted '{row.original_file_name}'."
