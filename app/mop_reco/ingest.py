"""Raw report normalisers — Stage 2.

One function per source. Each reads the real file(s), stamps a store code on
every row via masters.resolve_store, writes Parquet to
warehouse/<source>/<business_date>.parquet, and returns a LoadReport.

Business dates are read only from data columns (CLAUDE.md section 6.10) —
never from a filename or sheet name. A source file can span more than one
business date (the CMS file does); output is partitioned accordingly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import io_
from .config import Config
from .masters import (
    UnmappedEntry,
    load_master,
    pickup_codes_needing_location_registration,
    resolve_pickup_store,
    resolve_store,
)

logger = logging.getLogger("mop_reco")

WAREHOUSE_ROOT = Path(__file__).resolve().parents[2] / "warehouse"


class IngestError(ValueError):
    """A raw file doesn't match what this normaliser expects — never guessed past."""


@dataclass
class FileRead:
    path: str
    mtime: datetime
    encoding: str = None


@dataclass
class LoadReport:
    source: str
    status: str  # "ok" | "no_source_report"
    files: list = field(default_factory=list)          # list[FileRead]
    business_dates: list = field(default_factory=list)  # sorted ISO date strings
    rows_read: int = 0
    rows_before_dedupe: int = None
    rows_after_dedupe: int = None
    rows_with_store: int = 0
    rows_without_date: int = 0
    unmapped: list = field(default_factory=list)        # list[UnmappedEntry]
    control_total: float = 0.0
    breakdown: dict = field(default_factory=dict)        # e.g. {"transaction_type": {...}}
    warehouse_paths: dict = field(default_factory=dict)  # business_date (str) -> parquet path
    notes: list = field(default_factory=list)


def _accumulate_unmapped(agg: dict, source: str, identifier, amount, status: str):
    key = str(identifier)
    entry = agg.setdefault(key, {"row_count": 0, "value": 0.0, "status": status})
    entry["row_count"] += 1
    entry["value"] += float(amount or 0)
    entry["status"] = status


def _unmapped_list(agg: dict, source: str) -> list:
    return [
        UnmappedEntry(source=source, identifier=k, row_count=v["row_count"], value=v["value"], status=v["status"])
        for k, v in sorted(agg.items())
    ]


def _resolve_store_column(session, source: str, identifiers, amounts, business_dates):
    """Runs resolve_store per row. A row with no business date (NaT — e.g. the
    CMS file's ZPO rows with a blank DEPDATE) is never passed to resolve_store
    — it would crash the DB query with a NaN/NaT bind parameter — and is
    counted separately as rows_without_date rather than silently skipped.
    Returns (stores: list, rows_with_store: int, rows_without_date: int, unmapped_agg: dict)."""
    stores = []
    rows_with_store = 0
    rows_without_date = 0
    unmapped_agg: dict = {}
    # resolve_store is a pure function of (source, identifier, business date) - the master
    # version is chosen by the date - so ask it once per distinct pair (~15k for a month of
    # Paytm) instead of once per row (642k). Every row is still counted and accumulated.
    memo: dict = {}
    for identifier, amount, business_date in zip(identifiers, amounts, business_dates):
        if pd.isna(business_date):
            stores.append(None)
            rows_without_date += 1
            continue
        key = (identifier, business_date)
        result = memo.get(key)
        if result is None:
            result = memo[key] = resolve_store(session, source, identifier, business_date)
        stores.append(result.store)
        if result.status == "resolved":
            rows_with_store += 1
        else:
            _accumulate_unmapped(unmapped_agg, source, identifier, amount, result.status)
    return stores, rows_with_store, rows_without_date, unmapped_agg


def _resolve_cms_store_column(session, pickup_codes, locations, amounts, business_dates):
    """Like _resolve_store_column, but for the CMS pickup code — location
    decides only for a pickup code registered in the pickup_location master
    (2026-09-12, defect A); every other code resolves on the code alone,
    exactly as _resolve_store_column already does. Kept as its own function
    rather than adding an optional location column there, so nothing about
    the POS/MID resolution paths at the other two call sites can be affected
    by this change."""
    stores = []
    rows_with_store = 0
    rows_without_date = 0
    unmapped_agg: dict = {}
    for pickup_code, location, amount, business_date in zip(
            pickup_codes, locations, amounts, business_dates):
        if pd.isna(business_date):
            stores.append(None)
            rows_without_date += 1
            continue
        result = resolve_pickup_store(session, pickup_code, location, business_date)
        stores.append(result.store)
        if result.status == "resolved":
            rows_with_store += 1
        else:
            _accumulate_unmapped(unmapped_agg, "pickup_code", pickup_code, amount, result.status)
    return stores, rows_with_store, rows_without_date, unmapped_agg


def _write_parquet_by_date(df: pd.DataFrame, source: str, date_col: str):
    """Partitions df by the business date column and writes one Parquet file
    per distinct date to warehouse/<source>/<date>.parquet. Returns
    (written: {date_str: path_str}, rows_without_date: int).

    pandas' groupby silently drops rows whose group key is NaN — which would
    otherwise mean a row with no business date just vanishes from the
    warehouse with nothing said about it. Counted and returned explicitly
    instead, so the caller can put it in the load report rather than lose it."""
    out_dir = WAREHOUSE_ROOT / source
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_without_date = int(df[date_col].isna().sum())
    written = {}
    for business_date, group in df.groupby(date_col, dropna=True):
        date_str = pd.Timestamp(business_date).strftime("%Y-%m-%d")
        out_path = out_dir / f"{date_str}.parquet"
        group.to_parquet(out_path, index=False)
        written[date_str] = str(out_path)
        logger.info(f"ingest[{source}]: wrote {len(group)} rows to {out_path}")
    if rows_without_date:
        logger.warning(f"ingest[{source}]: {rows_without_date} row(s) have no {date_col} — excluded from Parquet output, kept in the returned DataFrame")
    return written, rows_without_date


# ---------------------------------------------------------------------------
# Multi-file sources (2026-09-24) — a source folder can now hold several
# genuinely distinct files at once (one per month: April, May, June, July,
# August), not always one dominant dump. CLAUDE.md section 4's own rule
# ("where more than one discovered file covers the same business date, take
# the most recently modified") was written for — and `io_.latest_file()`
# still only implements — ONE winning file for the WHOLE source. That is
# silently wrong the moment a source holds disjoint monthly files plus an
# overlapping duplicate at the same time (found 2026-09-24 while preparing
# to load the reloaded April-August data): picking the single
# newest-mtime file across the whole tree would pick only August's POS/Axis
# EDC file and drop April/May/June/July entirely, with nothing said about
# it. Generalised here to "one winning file PER BUSINESS DATE" — every
# ingest function that can see more than one file for its source now reads
# every one of them and resolves overlaps at the date level, never the
# whole-file level.
# ---------------------------------------------------------------------------

def _discover_files_for_source(config: Config, source: str, file_path_override):
    """(file_path, mtime) pairs to read for `source`. An explicit
    `file_path_override` (tests, or a caller pinning to one known dump)
    still means exactly that one file, unchanged from before. Otherwise
    every file discovered for the source (CLAUDE.md section 4, depth-1) —
    plural, not just the single most-recently-modified one."""
    if file_path_override is not None:
        fp = Path(file_path_override)
        return [(fp, datetime.fromtimestamp(fp.stat().st_mtime))]
    status = config.source_status(source)
    if not status.files:
        raise io_.IOError_(f"No usable files found for source '{source}' (path: {status.configured_path})")
    return [(p, datetime.fromtimestamp(p.stat().st_mtime)) for p in status.files]


def _resolve_key_overlaps(df: pd.DataFrame, key_col: str, source: str, key_label: str = None):
    """CLAUDE.md section 4's file-selection rule ("most recently modified
    wins"), generalised from "one winning file for the whole source" down to
    "one winning file per NATURAL KEY" — e.g. Axis EDC's RRN, Bajaj's
    Transaction Id, or a composite pickup/date/slip key for CMS. `df` must
    carry the temporary '_src_file' / '_src_mtime' columns (one file's
    identity/mtime stamped onto every row it contributed) — added by the
    caller before concatenating every discovered file's rows, and stripped
    again before returning here.

    Deliberately per-key, NOT per-date: an earlier version of this resolved
    per business date, which broke on Bajaj's June'26 file — 12 rows in it
    are genuinely dated in July, and a whole-date rule dropped July's OWN
    file's real 2026-07-01 rows in favour of that small, unrelated leak,
    just because June's file happened to have a marginally newer mtime.
    Two DIFFERENT transactions sharing a business date must never compete;
    only two rows that are actually the SAME transaction — proven by
    sharing the natural key, e.g. the same RRN re-exported in two Axis
    files for July (confirmed 2026-09-24: all 453 RRNs in the old sample
    file exist in the new monthly file) — should ever supersede each other.

    For any key claimed by rows from more than one file, only the rows from
    the most-recently-modified contributing file for THAT key survive; a
    row whose key is blank cannot be compared to anything and is always
    kept (CLAUDE.md rule 6 — never silently dropped for a missing key).

    Returns (kept_df, supersede_report) — the report lists every superseded
    key, which file won it and which file(s) lost, with row counts, so this
    is reported, never silent (CLAUDE.md rule 4)."""
    key_label = key_label or key_col
    if df.empty or df["_src_file"].nunique() <= 1:
        return df.drop(columns=["_src_file", "_src_mtime"]), []

    with_key = df[df[key_col].notna() & (df[key_col].astype(str).str.strip() != "")]
    without_key = df[~df.index.isin(with_key.index)]

    report = []
    if not with_key.empty:
        winner_mtime = with_key.groupby(key_col)["_src_mtime"].transform("max")
        is_winner = with_key["_src_mtime"] == winner_mtime
        dropped = with_key.loc[~is_winner]
        kept_with_key = with_key.loc[is_winner]
        if len(dropped):
            for k, grp in dropped.groupby(key_col):
                winning_files = sorted(str(p) for p in kept_with_key.loc[
                    kept_with_key[key_col] == k, "_src_file"].unique())
                losing_files = sorted(str(p) for p in grp["_src_file"].unique())
                report.append({
                    key_label: str(k),
                    "winning_file(s)": winning_files,
                    "superseded_file(s)": losing_files,
                    "rows_dropped": int(len(grp)),
                })
            logger.warning(
                f"ingest[{source}]: {len(dropped)} row(s) across {len(report)} {key_label}(s) "
                f"dropped as superseded — a more-recently-modified file carries the same "
                f"{key_label}. Detail: {report}"
            )
    else:
        kept_with_key = with_key

    kept = pd.concat([kept_with_key, without_key], ignore_index=False).sort_index()
    return kept.drop(columns=["_src_file", "_src_mtime"]), report


# ---------------------------------------------------------------------------
# Warehouse-first reading — CLAUDE.md 6.14
#
# A re-run of an already-ingested date must NOT re-open the raw file. The POS
# .xlsb costs ~46s just to open before a single row is readable, plus ~150s to
# stream 1.49M rows and re-parse every TPEDCRESPONSE JSON blob. The Parquet in
# warehouse/ already holds every derived column, so it is the read path unless
# the raw file is new or its mtime/size has changed.
# ---------------------------------------------------------------------------

def _manifest_path(source: str) -> Path:
    return WAREHOUSE_ROOT / source / "_manifest.json"


def _load_manifest_file(source: str) -> dict:
    """The manifest JSON as a {file_path_str: entry} dict — keyed per raw
    file, not per source, since a source can now hold several genuinely
    distinct files at once (one per month) rather than always one
    dominant dump (2026-09-24 five-month reload). A manifest written before
    this change was a single flat entry (source_file/mtime/size/...); it is
    treated as stale here rather than migrated — the one raw file it named
    is simply re-read once and the manifest is rewritten in the new shape."""
    path = _manifest_path(source)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if "source_file" in raw:  # old flat single-file shape — not migrated, just superseded
        return {}
    return raw.get("files", {})


def _write_manifest(source: str, file_path, warehouse_paths: dict, extra: dict = None):
    st = Path(file_path).stat()
    files = _load_manifest_file(source)
    files[str(file_path)] = {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "parquet": warehouse_paths,
        "extra": extra or {},
    }
    path = _manifest_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"files": files}, indent=2, default=str), encoding="utf-8")


def warehouse_manifest(source: str, file_path):
    """This file's own entry if warehouse Parquet is current for it, else
    None. 'Current' means: same mtime, same size, and every Parquet part
    still on disk. Each raw file for a source carries its own entry, so
    re-reading one file's freshness never depends on which OTHER file for
    this source was read most recently."""
    files = _load_manifest_file(source)
    entry = files.get(str(file_path))
    if entry is None:
        return None
    st = Path(file_path).stat()
    if entry.get("mtime") != st.st_mtime or entry.get("size") != st.st_size:
        return None
    parts = [Path(v) for v in (entry.get("parquet") or {}).values()]
    if not parts or not all(part.exists() for part in parts):
        return None
    return entry


def read_warehouse(manifest: dict) -> pd.DataFrame:
    frames = [pd.read_parquet(Path(v)) for v in manifest["parquet"].values()]
    return pd.concat(frames, ignore_index=True)


def _write_parquet_single_file_cache(df: pd.DataFrame, source: str, file_path: Path) -> dict:
    """The PER-RAW-FILE read cache for a multi-file source (2026-09-24) —
    distinct from `_write_parquet_by_date`'s per-BUSINESS-DATE output. Two
    files for the same source can cover the same business date (a genuine
    duplicate dump, e.g. July's two competing exports); if each file's own
    cache write landed at the date-partitioned path, the second file
    processed would silently overwrite the first's cache entry at that path,
    and a later cache-hit for either file could read back the WRONG file's
    rows. Keyed on a hash of the file's own path so two different months'
    files (or two files sharing a filename in different folders) can never
    collide, and so a re-run of the SAME file always lands at the same
    cache path. This cache is never read by anything except
    `read_warehouse()` for THIS one file's own manifest entry — the
    authoritative per-business-date Parquet the rest of the app would read
    from is written separately, once, from the post-merge winning rows
    (`ingest_pos_bill_wise`), never from here."""
    import hashlib
    out_dir = WAREHOUSE_ROOT / source / "_by_file"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()[:16]
    out_path = out_dir / f"{slug}.parquet"
    df.to_parquet(out_path, index=False)
    return {"single": str(out_path)}


def _to_float(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    if s == "" or s.lower() == "nan":
        return 0.0
    return float(s)


def _to_float_series(values: pd.Series) -> pd.Series:
    """`values.map(_to_float)` as one Polars pass: trim, drop surrounding single quotes,
    blank / "nan" / missing -> 0.0, otherwise the decimal value. Whatever Polars cannot
    parse (Python's float() is more permissive: "1_000", "infinity") goes through the
    original `_to_float` for exactly those cells, so the result is the same number -
    and a genuinely non-numeric cell raises the same ValueError - as before."""
    import polars as pl

    raw = values.tolist()
    text = pl.Series("v", raw, dtype=pl.String)
    trimmed = text.str.strip_chars().str.replace(r"(?s)^'(.*)'$", "${1}")
    zero = text.is_null() | (trimmed == "") | (trimmed.str.to_lowercase() == "nan")
    parsed = trimmed.cast(pl.Float64, strict=False)
    out = pl.select(pl.when(zero).then(0.0).otherwise(parsed)).to_series().to_numpy().copy()
    unparsed = np.flatnonzero((~zero & parsed.is_null()).to_numpy())
    for i in unparsed:
        out[i] = _to_float(raw[i])
    return pd.Series(out, index=values.index, dtype="float64")


def _strip_quotes(value):
    """THE SPECIFICATION of quote stripping (one value). The live code strips whole
    columns with `_strip_quotes_columns`; `tests/test_paytm_ingest_vectorized.py`
    asserts the two agree on awkward values."""
    if isinstance(value, str) and value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    return value


def _strip_quotes_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """`_strip_quotes` over every cell of an all-text frame (str or None cells), as one
    Polars regex per column - the per-cell Python call was ~36 s of a month's Paytm
    ingest (9M cells). Same rule: a value that starts AND ends with a single quote and
    has at least two characters loses exactly its first and last character; anything
    else, including None, is untouched. Columns come back as object dtype holding str or
    None, as before."""
    import polars as pl

    table = pl.from_pandas(frame, include_index=False)
    table = table.with_columns(
        pl.col(c).str.replace(r"(?s)^'(.*)'$", "${1}") for c in table.columns)
    out = table.to_pandas()
    out.index = frame.index
    for c in out.columns:
        col = out[c]
        out[c] = col.astype(object).where(col.notna(), None)
    return out


# ---------------------------------------------------------------------------
# Paytm settlement — 3 zip parts, concatenate, dedupe on transaction_id
# ---------------------------------------------------------------------------

PAYTM_REQUIRED_COLUMNS = [
    "transaction_id", "transaction_date", "status", "transaction_type",
    "original_mid", "amount", "commission", "gst", "payout_id", "utr_no",
    "settled_amount", "settled_date", "payment_mode",
    # Needed to link a REFUND/CHARGEBACK/CHARGEBACK_REVERSAL/REVERSAL row back
    # to the original sale it reverses (CLAUDE.md 6.3). Without it those rows
    # cannot be linked and would look like unexplained gateway noise.
    "reference_transaction_id",
]


def _normalise_row_width(rows: list, width: int):
    """Force every row to the header's width. Returns (rows, wide, narrow).

    The July Paytm export is genuinely ragged: 47,276 of 642,165 rows carry 105
    fields against a 100-column header. The extra fields are all at the TAIL and
    every named column still lands in its correct position (verified:
    payment_mode at index 22, settled_amount at index 26), so the first `width`
    fields are the real record and the surplus is a wider export schema.

    Rows are trimmed or padded rather than passed to pandas, which raises
    "N columns passed, passed data had M columns" and takes the whole run down.
    Both directions are counted so the condition is reported, never absorbed.
    """
    wide = narrow = 0
    out = []
    for row in rows:
        n = len(row)
        if n == width:
            out.append(row)
        elif n > width:
            wide += 1
            out.append(row[:width])
        else:
            narrow += 1
            out.append(list(row) + [None] * (width - n))
    return out, wide, narrow


def ingest_paytm(config: Config, session, file_paths: list = None) -> tuple:
    """`file_paths` scopes the read to specific zip parts; omit it to read every
    part discovered for the source (CLAUDE.md section 5 — multi-part export)."""
    load_master(config, session, "mid_paytm")

    if file_paths is not None:
        zip_files = [Path(p) for p in file_paths]
    else:
        status = config.source_status("paytm_settlement")
        zip_files = [p for p in status.files if p.suffix.lower() == ".zip"]
    if not zip_files:
        return pd.DataFrame(), LoadReport(
            source="paytm_settlement", status="no_source_report",
            notes=["No Paytm settlement zip files found — source not configured or empty."],
        )

    encoding = config.encoding_for("paytm_settlement")
    files_meta = []
    parts = []
    rows_before = 0
    ragged_wide = 0
    ragged_narrow = 0
    for zpath in sorted(zip_files):
        mtime = datetime.fromtimestamp(zpath.stat().st_mtime)
        result = io_.read_any(zpath, encoding=encoding)
        header_idx = io_.find_header_row(result.grid, PAYTM_REQUIRED_COLUMNS)
        header = [str(c).strip() for c in result.grid[header_idx]]
        part_rows = result.grid[header_idx + 1:]

        part_rows, wide, narrow = _normalise_row_width(part_rows, len(header))
        if wide:
            ragged_wide += wide
            logger.warning(
                f"ingest_paytm: part '{zpath.name}' — {wide} row(s) carry MORE fields than the "
                f"{len(header)}-column header. The named columns align correctly, so the first "
                f"{len(header)} fields are kept and the extra trailing fields are dropped. "
                "Counted and reported, not silently absorbed."
            )
        if narrow:
            ragged_narrow += narrow
            logger.warning(
                f"ingest_paytm: part '{zpath.name}' — {narrow} row(s) carry FEWER fields than the "
                f"header and were padded. Reported: a short row may mean a truncated export."
            )

        # Only the columns the ingest keeps are turned into a frame (13 of the file's 100 - building
        # all 100 as Arrow strings was ~15 s a month). Same columns, same order, same values.
        position = {name: i for i, name in reversed(list(enumerate(header)))}   # first wins, as df[col] did
        kept = [c for c in PAYTM_REQUIRED_COLUMNS if c in position]
        part_df = pd.DataFrame({c: [row[position[c]] for row in part_rows] for c in kept}, columns=kept)
        rows_before += len(part_df)
        parts.append(part_df)
        files_meta.append(FileRead(path=str(zpath), mtime=mtime, encoding=result.encoding_used))
        logger.info(f"ingest_paytm: part '{zpath.name}' — {len(part_df)} rows")

    combined = pd.concat(parts, ignore_index=True)

    # Values arrive wrapped in single quotes ('SUCCESS') — strip them everywhere.
    combined = _strip_quotes_columns(combined)

    combined["transaction_id"] = combined["transaction_id"].map(io_.clean_ref)
    combined["utr_no"] = combined["utr_no"].map(io_.clean_ref)

    rows_after = combined["transaction_id"].nunique()
    combined = combined.drop_duplicates(subset="transaction_id", keep="first")
    logger.info(f"ingest_paytm: {rows_before} rows before dedupe, {len(combined)} after")

    for col in ("amount", "commission", "gst", "settled_amount"):
        combined[col] = _to_float_series(combined[col])

    # transaction_date carries the time embedded, e.g. '20-07-2026 19:21:19'.
    parsed = pd.to_datetime(combined["transaction_date"], format="%d-%m-%Y %H:%M:%S")
    combined["business_date"] = parsed.dt.date
    combined["transaction_time"] = parsed.dt.time
    combined["settled_date"] = pd.to_datetime(combined["settled_date"], format="%d-%m-%Y", errors="coerce").dt.date

    type_breakdown = combined["transaction_type"].value_counts().to_dict()

    stores, rows_with_store, rows_without_date, unmapped_agg = _resolve_store_column(
        session, "paytm_mid", combined["original_mid"], combined["amount"], combined["business_date"]
    )
    combined["store"] = stores

    warehouse_paths, _ = _write_parquet_by_date(combined, "paytm_settlement", "business_date")

    report = LoadReport(
        source="paytm_settlement",
        status="ok",
        files=files_meta,
        business_dates=sorted(str(d) for d in combined["business_date"].dropna().unique()),
        rows_read=rows_before,
        rows_before_dedupe=rows_before,
        rows_after_dedupe=len(combined),
        rows_with_store=rows_with_store,
        rows_without_date=rows_without_date,
        unmapped=_unmapped_list(unmapped_agg, "paytm_mid"),
        control_total=float(combined["amount"].sum()),
        breakdown={
            "transaction_type": type_breakdown,
            "payment_mode": combined["payment_mode"].value_counts().to_dict(),
            "ragged_rows": {
                "more_fields_than_header": ragged_wide,
                "fewer_fields_than_header": ragged_narrow,
            },
        },
        warehouse_paths=warehouse_paths,
    )
    return combined, report


# ---------------------------------------------------------------------------
# Axis EDC — single file, MID-only store resolution
# ---------------------------------------------------------------------------

AXIS_EDC_REQUIRED_COLUMNS = [
    "RRN", "TRAN_DATE", "TRAN_TIME", "MID", "TERM_ID", "CARD_TYPE",
    "GROSS_AMT", "MDR", "GST", "NET_AMT", "UTR", "PROCESS_DATE",
]


def _read_axis_edc_file(file_path: Path, mtime) -> pd.DataFrame:
    result = io_.read_any(file_path)
    header_idx = io_.find_header_row(result.grid, AXIS_EDC_REQUIRED_COLUMNS)
    header = [str(c).strip() for c in result.grid[header_idx]]
    all_rows = result.grid[header_idx + 1:]
    # A wholly blank row (every cell None) is Excel's own trailing padding
    # beyond the real data range, never a transaction — confirmed on the
    # 2026-09-24 reload: June's file carries 406,627 such rows against only
    # 21,421 real ones. Filtering them out here matches the pattern already
    # used for Bajaj/CMS row reading; left in, they would inflate rows_read
    # by 19x and flood the unmapped-MID report with hundreds of thousands of
    # phantom blanks.
    blank_rows = sum(1 for r in all_rows if not r or all(v is None for v in r))
    data_rows = [r for r in all_rows if r and any(v is not None for v in r)]
    if blank_rows:
        logger.warning(
            f"ingest_axis_edc: '{file_path.name}' — {blank_rows} wholly blank row(s) "
            "(Excel padding beyond the real data range) excluded, not counted as rows"
        )
    df = pd.DataFrame(data_rows, columns=header)
    df = df[AXIS_EDC_REQUIRED_COLUMNS]

    df["RRN"] = df["RRN"].map(io_.clean_ref)
    for col in ("GROSS_AMT", "MDR", "GST", "NET_AMT"):
        df[col] = df[col].map(_to_float)

    # Parsed BEFORE the raw PROCESS_DATE column is stringified below —
    # parse_business_date's Excel-serial and datetime branches need the
    # column's original type; stringifying first would turn a genuine
    # serial number into unparseable text.
    # The daily file carries '20-Jul-2026' strings; the monthly file carries real
    # datetimes. A hardcoded format works on one and throws on the other.
    df["business_date"] = io_.parse_business_date_series(df["TRAN_DATE"])
    df["process_date"] = io_.parse_business_date_series(df["PROCESS_DATE"])

    # TERM_ID, PROCESS_DATE, TRAN_TIME, MID, CARD_TYPE and UTR are read and
    # stored for traceability (or store resolution, for MID) but arrive with
    # a genuinely mixed Python type per cell — confirmed on the 2026-09-24
    # reload: August's PROCESS_DATE mixes str and datetime, June's carried
    # float (NaN, from the padding rows above) alongside str before the
    # blank-row filter. Left as-is, concatenating several files' worth of
    # rows into one column for the Parquet write trips pyarrow's type
    # inference the moment it meets a mixed-type column. Normalised to a
    # plain string AFTER the clean business_date/process_date columns are
    # already derived above; TRAN_DATE itself stays a real Timestamp/NaT
    # since every file agrees on that type and it is never fed to this
    # cleanup.
    for col in ("TERM_ID", "PROCESS_DATE", "TRAN_TIME", "MID", "CARD_TYPE", "UTR"):
        df[col] = df[col].map(lambda v: "" if v is None else str(v).strip())

    # TRAN_TIME is NOT a real per-transaction time (confirmed 2026-09-29,
    # against the real April/July/August files): every single row carries
    # the literal string '000000', never varying — a placeholder Axis's own
    # export always fills, not a captured time. Reading it as 00:00:00
    # would claim every Axis transaction happened at midnight, which rule 5
    # ("nothing is guessed") forbids. `transaction_time` is left genuinely
    # None for every Axis row; TRAN_TIME itself is still kept, unrenamed,
    # for traceability only, exactly as before.
    df["transaction_time"] = None

    df["_src_file"] = str(file_path)
    df["_src_mtime"] = mtime.timestamp()
    return df


def ingest_axis_edc(config: Config, session, file_path: Path = None) -> tuple:
    load_master(config, session, "mid_axis")

    # Every discovered file is read (2026-09-24) — a source folder can now
    # hold one file per month rather than always one dominant dump; picking
    # only the single most-recently-modified one across the whole tree
    # would silently drop every other month (see the comment above
    # _discover_files_for_source).
    files = _discover_files_for_source(config, "axis_edc", file_path)
    parts = [_read_axis_edc_file(fp, mt) for fp, mt in files]
    combined = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    # RRN is the natural key — two files sharing an RRN are the same
    # transaction re-exported, never two different transactions sharing a
    # date (see _resolve_key_overlaps' own docstring for why date was wrong).
    df, supersede_report = _resolve_key_overlaps(combined, "RRN", "axis_edc")

    stores, rows_with_store, rows_without_date, unmapped_agg = _resolve_store_column(
        session, "axis_mid", df["MID"], df["GROSS_AMT"], df["business_date"]
    )
    df["store"] = stores

    warehouse_paths, _ = _write_parquet_by_date(df, "axis_edc", "business_date")

    report = LoadReport(
        source="axis_edc",
        status="ok",
        files=[FileRead(path=str(fp), mtime=mt) for fp, mt in files],
        business_dates=sorted(str(d) for d in df["business_date"].dropna().unique()),
        rows_read=len(df),
        rows_with_store=rows_with_store,
        rows_without_date=rows_without_date,
        unmapped=_unmapped_list(unmapped_agg, "axis_mid"),
        control_total=float(df["GROSS_AMT"].sum()),
        breakdown={"CARD_TYPE": df["CARD_TYPE"].value_counts().to_dict(), "net_amt_total": float(df["NET_AMT"].sum())},
        warehouse_paths=warehouse_paths,
        notes=[f"superseded_by_more_recent_file: {supersede_report}"] if supersede_report else [],
    )
    return df, report


# ---------------------------------------------------------------------------
# Axis CMS — unreliable header names (two duplicate pairs). Read by column
# POSITION and assert the name found matches what's expected at that position.
# ---------------------------------------------------------------------------

# 0-based column index -> expected header name (A=0, D=3, E=4, H=7, J=9,
# K=10, P=15, W=22, X=23, BO=66).
CMS_COLUMN_POSITIONS = {
    0: "TYPE",
    # Column D — the pickup point's own location code, e.g. ARRAH, MADHB.
    # Added 2026-09-12 (defect A): a pickup code shared between more than one
    # store (today: AX31918, APM/MMR) can only be told apart by this column —
    # LOCATIONNAME (column E) carries the same information in longer form but
    # LOCAT is what the pickup_location master matches on.
    3: "LOCAT",
    4: "LOCATIONNAME",
    7: "DEPDATE",
    8: "ADJDATE",
    9: "CLGDATE",
    10: "CRDATE",
    15: "SLIPNO.",
    # Column U — the adjustment amount on an ADJ.CR. row. Confirmed on the
    # July sample: the 3 ADJ.CR. rows carry ADJ=103/7/8, distinct from their
    # SLIPAMOUNT of 47400/127722/95760 (the ORIGINAL slip being adjusted, not
    # the size of the adjustment). J must add ADJ here, never SLIPAMOUNT, or
    # an adjustment of a few rupees would be booked as the whole original
    # slip amount again.
    20: "ADJ",
    22: "SLIPAMOUNT",
    23: "RECOVEREDAMOUNT",
    66: "PICKUPPOINTCODE",
}

CMS_HEADER_ANCHORS = ["TYPE", "DEPDATE", "CRDATE", "PICKUPPOINTCODE"]  # for locating the header row only


def _col_letter(idx: int) -> str:
    """0-based column index -> Excel column letter (0 -> A, 25 -> Z, 26 -> AA, ...)."""
    letters = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _assert_column_positions(header_row: list, expected: dict, file_label: str):
    for idx, expected_name in expected.items():
        col = _col_letter(idx)
        if idx >= len(header_row):
            raise IngestError(
                f"{file_label}: expected column '{expected_name}' at position {col} ({idx}), "
                f"but the header row only has {len(header_row)} columns"
            )
        actual_name = str(header_row[idx]).strip() if header_row[idx] is not None else ""
        if actual_name != expected_name:
            raise IngestError(
                f"{file_label}: header mismatch at position {col} ({idx}) — "
                f"expected '{expected_name}', found '{actual_name}'"
            )


def _read_axis_cms_file(config: Config, file_path: Path, mtime) -> pd.DataFrame:
    # The monthly file is a zip of CSV and needs a declared encoding; the daily
    # file is .xlsx and carries its own. Passing None for the .xlsx is correct —
    # read_any only demands an encoding for text formats, and never guesses one.
    result = io_.read_any(file_path, encoding=config.encoding_for("axis_cms"))
    header_idx = io_.find_header_row(result.grid, CMS_HEADER_ANCHORS)
    header_row = result.grid[header_idx]
    _assert_column_positions(header_row, CMS_COLUMN_POSITIONS, file_path.name)

    data_rows = result.grid[header_idx + 1:]
    records = []
    for row in data_rows:
        if row is None or all(v is None for v in row):
            continue
        records.append({
            "TYPE": row[0],
            "LOCAT": row[3],
            "LOCATIONNAME": row[4],
            "DEPDATE": row[7],
            "ADJDATE": row[8],
            "CLGDATE": row[9],
            "CRDATE": row[10],
            "SLIPNO": row[15],
            "ADJ": row[20],
            "SLIPAMOUNT": row[22],
            "RECOVEREDAMOUNT": row[23],
            "PICKUPPOINTCODE": row[66],
        })
    df = pd.DataFrame(records)

    df["SLIPAMOUNT"] = df["SLIPAMOUNT"].map(lambda v: float(v) if v is not None else 0.0)
    df["ADJ"] = df["ADJ"].map(lambda v: float(v) if v is not None else 0.0)
    df["RECOVEREDAMOUNT"] = df["RECOVEREDAMOUNT"].map(
        lambda v: float(v) if v is not None else 0.0)
    # The xlsx export carries real datetimes; the newer CSV-in-zip export carries
    # day-first 'DD-MM-YYYY' strings. An unqualified parse turns 01-07-2026 into
    # 7 January and silently NaTs the 13th and 31st — verified. Never guess.
    df["DEPDATE"] = pd.to_datetime(io_.parse_business_date_series(df["DEPDATE"]))
    df["CRDATE"] = pd.to_datetime(io_.parse_business_date_series(df["CRDATE"]))
    df["business_date"] = df["DEPDATE"].dt.date
    df["_src_file"] = str(file_path)
    df["_src_mtime"] = mtime.timestamp()
    return df


def ingest_axis_cms(config: Config, session, file_path: Path = None) -> tuple:
    load_master(config, session, "pickup_code")
    load_master(config, session, "pickup_location")

    # Every discovered file is read (2026-09-24) — see the comment above
    # _discover_files_for_source. CMS now arrives as one file per month.
    files = _discover_files_for_source(config, "axis_cms", file_path)
    parts = [_read_axis_cms_file(config, fp, mt) for fp, mt in files]
    combined = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    # The natural key for a CMS pickup is (pickup_code, deposit_date, slip_no)
    # — CLAUDE.md section 6.9's own exception key for this source — never
    # the deposit date alone, for the identical reason business_date alone
    # is wrong for Bajaj (see _resolve_key_overlaps' docstring): two
    # DIFFERENT pickups sharing a deposit date must never compete with each
    # other just because one file happens to be marginally newer. A row
    # missing any one component (e.g. a ZPO row with no DEPDATE) gets a
    # blank composite key and is always kept, never compared.
    has_all_parts = (combined["PICKUPPOINTCODE"].notna() & combined["DEPDATE"].notna()
                      & combined["SLIPNO"].notna())
    combined["_cms_key"] = None
    combined.loc[has_all_parts, "_cms_key"] = (
        combined.loc[has_all_parts, "PICKUPPOINTCODE"].astype(str).str.strip() + "|"
        + combined.loc[has_all_parts, "DEPDATE"].astype(str) + "|"
        + combined.loc[has_all_parts, "SLIPNO"].astype(str).str.strip()
    )
    df, supersede_report = _resolve_key_overlaps(combined, "_cms_key", "axis_cms", key_label="pickup_key")
    df = df.drop(columns=["_cms_key"])

    # CLAUDE.md section 6.7 TYPE handling.
    def _credit_amount(row):
        if row["TYPE"] == "COL" or row["TYPE"] == "ADJ.CR.":
            return row["SLIPAMOUNT"]
        if row["TYPE"] == "ADJ.DB.":
            return -row["SLIPAMOUNT"]
        return 0.0  # ZPO — excluded from the credit total, but the row is kept and counted

    df["credit_amount"] = df.apply(_credit_amount, axis=1)
    type_breakdown = df["TYPE"].value_counts().to_dict()

    stores, rows_with_store, rows_without_date, unmapped_agg = _resolve_cms_store_column(
        session, df["PICKUPPOINTCODE"], df["LOCAT"], df["SLIPAMOUNT"], df["business_date"]
    )
    df["store"] = stores
    if rows_without_date:
        logger.warning(
            f"ingest_axis_cms: {rows_without_date} row(s) have no DEPDATE and were not "
            "store-resolved (all TYPE=ZPO in this file) — kept in the DataFrame, "
            "counted, and reported, not silently skipped"
        )

    # Pre-flight (CLAUDE.md 6.11, added 2026-09-12 after defect A): a pickup
    # code seen against more than one location that the pickup_location
    # master does not yet know about. This is exactly the check that would
    # have caught APM's credits being posted under MMR's code a month
    # earlier than the arithmetic on one row happened to look odd — reported
    # by name, never silently resolved by picking a store (rule 5).
    unregistered_codes = pickup_codes_needing_location_registration(
        df, session, datetime.now())
    if unregistered_codes:
        note = (
            f"Pickup code(s) {', '.join(unregistered_codes)} appear against more "
            "than one location in this file and are NOT in the pickup_location "
            "master — every row under them is being resolved on the code alone, "
            "which is exactly how defect A (APM's credits under MMR's code) went "
            "undetected for a month. Add a location entry for each before "
            "trusting store attribution here."
        )
        logger.warning(f"ingest_axis_cms: {note}")

    # Credit lag — reported, not asserted (DEPDATE to CRDATE, in days).
    lag_mask = df["DEPDATE"].notna() & df["CRDATE"].notna()
    lag_days = (df.loc[lag_mask, "CRDATE"] - df.loc[lag_mask, "DEPDATE"]).dt.days
    credit_lag_distribution = lag_days.value_counts().sort_index().to_dict()

    warehouse_paths, _ = _write_parquet_by_date(df, "axis_cms", "business_date")

    notes = []
    if unregistered_codes:
        notes.append(note)
    if supersede_report:
        notes.append(f"superseded_by_more_recent_file: {supersede_report}")

    report = LoadReport(
        source="axis_cms",
        status="ok",
        files=[FileRead(path=str(fp), mtime=mt) for fp, mt in files],
        business_dates=sorted(str(d) for d in df["business_date"].dropna().unique()),
        rows_read=len(df),
        rows_with_store=rows_with_store,
        rows_without_date=rows_without_date,
        unmapped=_unmapped_list(unmapped_agg, "pickup_code"),
        control_total=float(df["credit_amount"].sum()),
        breakdown={"TYPE": type_breakdown, "credit_lag_days": {int(k): int(v) for k, v in credit_lag_distribution.items()}},
        warehouse_paths=warehouse_paths,
        notes=notes,
    )
    return df, report


# ---------------------------------------------------------------------------
# Bajaj — real loader (data arrived 2026-09-02). Bank statement — still a stub.
# ---------------------------------------------------------------------------

# "Transaction Id" is the join key — matches the bill's flat `rrn` 192/192.
# Never the file's own RRN column, which is the literal string 'NA' throughout.
BAJAJ_REQUIRED_COLUMNS = [
    "Transaction Id", "Transaction Date", "Transaction Amount", "Transaction Status",
    "TID", "MID", "Card Scheme", "Net Amount", "Total MDR",
]


def _read_bajaj_file(file_path: Path, mtime) -> pd.DataFrame:
    result = io_.read_any(file_path)
    data_rows = [r for r in result.grid[1:] if r and any(v is not None for v in r)]
    if not data_rows:
        return None
    header = [str(c).strip() for c in result.grid[0]]
    df = pd.DataFrame(data_rows, columns=header)

    missing = [c for c in BAJAJ_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise IngestError(f"{file_path.name}: Bajaj settlement file is missing column(s) {missing}")
    df = df[BAJAJ_REQUIRED_COLUMNS].copy()

    df["Transaction Id"] = df["Transaction Id"].map(io_.clean_ref)
    df["Transaction Amount"] = df["Transaction Amount"].map(_to_float)
    df["Net Amount"] = df["Net Amount"].map(_to_float)
    # Date comes from the data column, never the containing folder's name —
    # a file can (and does) carry rows dated outside its own nominal month,
    # e.g. the June'26 folder's file holding 12 rows actually dated in July.
    df["business_date"] = io_.parse_business_date_series(df["Transaction Date"])
    # Transaction Date embeds a real, per-row TIME too ('01-08-2026 22:32:38')
    # — confirmed varying across rows on the real July file, unlike Axis's
    # own TRAN_TIME (a constant '000000' placeholder, never a real time —
    # see ingest_axis_edc). `errors="coerce"` so one oddly-formatted row
    # loses only its own time (NaT -> None), never the whole file's read.
    df["transaction_time"] = pd.to_datetime(
        df["Transaction Date"], format="%d-%m-%Y %H:%M:%S", errors="coerce").dt.time

    # Store for a MATCHED Bajaj bill comes from the bill side; the MID here
    # ('BFL…') does not match the bill's mid, and no Bajaj TID→store master
    # exists yet. Left unmapped rather than guessed (CLAUDE.md 5).
    df["store"] = None
    df["_src_file"] = str(file_path)
    df["_src_mtime"] = mtime.timestamp()
    return df


def ingest_bajaj(config: Config, session, file_path: Path = None) -> tuple:
    status = config.source_status("bajaj")
    if file_path is not None:
        status = None
    if status is not None and not status.files:
        return pd.DataFrame(), LoadReport(
            source="bajaj", status="no_source_report",
            notes=["Source not configured or no file present — no Bajaj settlement data has been supplied."],
        )

    # Every discovered file is read (2026-09-24) — see the comment above
    # _discover_files_for_source. Bajaj now arrives as one file per month
    # (today: June, July, August — April and May genuinely have no file at
    # all, which the caller reports as NOT SUPPLIED for those two months,
    # distinct from a file that exists but holds zero transactions).
    files = _discover_files_for_source(config, "bajaj", file_path)
    parts = [p for p in (_read_bajaj_file(fp, mt) for fp, mt in files) if p is not None]
    if not parts:
        return pd.DataFrame(), LoadReport(
            source="bajaj", status="no_source_report",
            files=[FileRead(path=str(fp), mtime=mt) for fp, mt in files],
            notes=[f"Every discovered Bajaj file contains zero data rows — no Bajaj settlement data has been supplied for {[fp.name for fp, _ in files]}."],
        )
    combined = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    # Transaction Id is the natural key — CLAUDE.md documents it as the join
    # key back to the bill's own rrn, so a shared Transaction Id genuinely is
    # the same transaction re-exported, never a coincidence.
    df, supersede_report = _resolve_key_overlaps(combined, "Transaction Id", "bajaj")

    status_counts = df["Transaction Status"].value_counts().to_dict()
    warehouse_paths, rows_without_date = _write_parquet_by_date(df, "bajaj", "business_date")

    notes = [
        "Store is unmapped for every Bajaj row: the settlement MID ('BFL…') does not "
        "match the bill's mid and no Bajaj TID→store master exists. A matched bill "
        "takes its store from the bill side; only bill-less Bajaj rows need that master.",
        "Transaction Status is mixed case in this file — compare case-insensitively.",
    ]
    if supersede_report:
        notes.append(f"superseded_by_more_recent_file: {supersede_report}")

    report = LoadReport(
        source="bajaj",
        status="ok",
        files=[FileRead(path=str(fp), mtime=mt) for fp, mt in files],
        business_dates=sorted(str(d) for d in df["business_date"].dropna().unique()),
        rows_read=len(df),
        rows_with_store=0,
        rows_without_date=rows_without_date,
        control_total=float(df["Transaction Amount"].sum()),
        breakdown={
            "Transaction Status": status_counts,
            "rows_per_business_date": {
                str(d): int(n) for d, n in df["business_date"].value_counts().sort_index().items()
            },
        },
        warehouse_paths=warehouse_paths,
        notes=notes,
    )
    return df, report


def ingest_bank_statement(config: Config, session) -> tuple:
    status = config.source_status("bank_statement")
    if status.resolved_path is None or not status.files:
        return pd.DataFrame(), LoadReport(
            source="bank_statement", status="no_source_report",
            notes=["Source not configured — no bank statement file has been supplied yet."],
        )

    file_path, mtime = io_.latest_file(config, "bank_statement")
    result = io_.read_any(file_path)
    data_rows = [r for r in result.grid[1:] if r and any(v is not None for v in r)]
    header = [str(c).strip() for c in result.grid[0]]
    df = pd.DataFrame(data_rows, columns=header)
    return df, LoadReport(source="bank_statement", status="ok", files=[FileRead(path=str(file_path), mtime=mtime)], rows_read=len(df))


# ---------------------------------------------------------------------------
# POS bill-wise — Stage 3
# ---------------------------------------------------------------------------

POS_BILL_WISE_REQUIRED_COLUMNS = [
    "BILLNO", "PSITE_POSBILL_CODE", "STORE_NAME", "BILLDATE", "MOPDESC", "BASEAMT", "TPEDCRESPONSE",
]

# CREATEBY (the till operator) and BILL_TIME (2026-09-29) — present on every
# monthly .xlsb export seen (April-August), absent from the retired daily
# .xlsx sample and July's superseded MOP SALE DATA.xlsb duplicate. Kept
# OUT of POS_BILL_WISE_REQUIRED_COLUMNS on purpose: that list also anchors
# header-row detection (io_.find_header_row), and requiring these two would
# make the older, still-discovered-but-superseded files fail to parse at
# all instead of simply losing to the newer file in overlap resolution as
# they already do. Carried through when present, blank when not — never
# guessed (rule 5).
POS_BILL_WISE_OPTIONAL_COLUMNS = ["CREATEBY", "BILL_TIME"]


def _extract_pos_refs(raw_json):
    """CLAUDE.md section 6.2. Returns (paytm_ref, axis_ref, bajaj_ref, parse_failed).

    Three gateways, three payload shapes:
      - Paytm/Axis: nested {"head":..., "body":{...}}
      - Bajaj:      FLAT {"rrn":..., "tid":...} with no head/body at all

    `axis_ref` is CARD-CONDITIONAL: retrievalReferenceNo is taken only when the
    response also carries issuerMaskCardNo. Without that condition, 11,603 of
    12,424 PAYTM EDC rows on the 20-Jul file would get an Axis reference for
    UPI/wallet transactions Axis never processed.

    A blank/None TPEDCRESPONSE (e.g. every Cash row) is normal, not a parse
    failure. A non-blank value that json.loads chokes on IS a parse failure —
    reported explicitly, never silently turned into a blank reference.
    """
    # A blank Excel cell surfaces here as Python None from openpyxl directly,
    # but pandas silently normalises a mixed None/str column to float('nan')
    # once it's inside a DataFrame — both mean "no TPEDCRESPONSE", neither is
    # a parse failure. pd.isna() catches both; a plain `is None` check alone
    # does not, and would misreport every blank row as a parse failure.
    if raw_json is None or (isinstance(raw_json, float) and pd.isna(raw_json)):
        return None, None, None, False
    s = str(raw_json).strip()
    if s == "" or s.lower() == "none":
        return None, None, None, False
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, None, None, True
    if not isinstance(obj, dict):
        return None, None, None, False

    body = obj.get("body")
    if isinstance(body, dict):
        # Paytm / Axis — nested envelope.
        paytm_ref = body.get("acquirementId") or body.get("txnId")
        # Card-conditional: only a card transaction carries an Axis reference.
        axis_ref = body.get("retrievalReferenceNo") if body.get("issuerMaskCardNo") else None
        return io_.clean_ref(paytm_ref), io_.clean_ref(axis_ref), None, False

    # Bajaj — flat payload, no head/body. Bill-side key is lowercase `rrn`,
    # which joins to the Bajaj settlement column "Transaction Id" (192/192).
    if "rrn" in obj:
        return None, None, io_.clean_ref(obj.get("rrn")), False

    # Valid JSON, recognised as neither shape — no reference, not a parse error.
    return None, None, None, False


def _assert_known_mops(df: pd.DataFrame, config: Config):
    """An unknown MOP stops that store-day. CLAUDE.md section 6.1."""
    known = set(config.mop_classification.keys())
    unknown_mask = ~df["MOPDESC"].isin(known)
    if unknown_mask.any():
        row = df[unknown_mask].iloc[0]
        affected_stores = sorted(df.loc[unknown_mask, "STORE_NAME"].unique())
        raise IngestError(
            f"Unknown MOP '{row['MOPDESC']}' at store '{row['STORE_NAME']}' on "
            f"{row['business_date']} — not in config.yaml mop_classification. "
            f"Stopping ingestion: {unknown_mask.sum()} row(s) affected across "
            f"store(s) {affected_stores}."
        )


# The columns the POS bill-wise ingest keeps, by how calamine must type them. Every
# one is DECLARED (see io_.read_xlsb_table) so nothing is guessed from a sample.
_POS_TEXT_COLUMNS = ["BILLNO", "PSITE_POSBILL_CODE", "STORE_NAME", "MOPDESC",
                     "TPEDCRESPONSE", "CREATEBY"]
_POS_FLOAT_COLUMNS = ["BASEAMT"]
_POS_DATE_COLUMNS = ["BILLDATE"]
_POS_TIME_COLUMNS = ["BILL_TIME"]


def _pos_extract_refs_polars(frame):
    """Per sheet, inside the reader: derive the four reference columns from the
    TPEDCRESPONSE JSON with the one authoritative parser (`_extract_pos_refs`) and
    drop the JSON before the sheet is concatenated or converted to pandas. The JSON
    is ~65% of the frame's memory (CLAUDE.md 6.14); a month of it as Python str
    objects was most of the ingest's peak."""
    import polars as pl

    if "TPEDCRESPONSE" not in frame.columns:
        return frame
    # In batches: all ~800k JSON strings of a sheet as Python objects at once was ~2 GB
    # of transient memory on top of the sheet itself.
    refs = []
    json_col = frame["TPEDCRESPONSE"]
    for start in range(0, frame.height, 100_000):
        refs.extend(_extract_pos_refs(v) for v in json_col.slice(start, 100_000).to_list())
    return frame.drop("TPEDCRESPONSE").with_columns(
        pl.Series("paytm_ref", [r[0] for r in refs], dtype=pl.String),
        pl.Series("axis_ref", [r[1] for r in refs], dtype=pl.String),
        pl.Series("bajaj_ref", [r[2] for r in refs], dtype=pl.String),
        pl.Series("tpedc_parse_failed", [r[3] for r in refs], dtype=pl.Boolean),
    )


def _read_pos_raw(file_path: Path):
    """The raw POS bill-wise table: (DataFrame, {sheet: data_rows}).

    An `.xlsb` goes through calamine (Polars' fastexcel): ~15x faster than streaming
    pyxlsb cell by cell, with the same cells (`tests/test_pos_ingest_calamine.py`
    asserts the ingested frame identical, cell for cell). Anything calamine cannot
    serve - another format, a header that is not on row 1, fastexcel not installed,
    a required column missing - takes the original pyxlsb/grid path, unchanged."""
    if Path(file_path).suffix.lower() == ".xlsb":
        try:
            df, per_sheet = io_.read_xlsb_table(
                file_path, _POS_TEXT_COLUMNS, _POS_FLOAT_COLUMNS, _POS_DATE_COLUMNS, _POS_TIME_COLUMNS,
                sheet_transform=_pos_extract_refs_polars)
        except ImportError:
            df = None
        except Exception as exc:
            # Never lossy: anything calamine cannot read exactly (a column that mixes types,
            # a header that moves between sheets, a type it did not infer as expected) goes
            # to the original reader - named, not swallowed.
            logger.warning(f"ingest_pos_bill_wise: calamine could not read '{Path(file_path).name}' "
                           f"exactly ({type(exc).__name__}: {str(exc)[:200]}) - using the original "
                           "pyxlsb reader")
            df = None
        else:
            # (TPEDCRESPONSE was consumed per sheet into the reference columns.)
            needed = [c for c in POS_BILL_WISE_REQUIRED_COLUMNS if c != "TPEDCRESPONSE"]
            if all(c in df.columns for c in needed) and "paytm_ref" in df.columns:
                return df, per_sheet
            logger.info(f"ingest_pos_bill_wise: '{Path(file_path).name}' has no row-1 header "
                        "calamine can use - reading it with the original pyxlsb path")
    result = io_.read_any(file_path)
    header_idx = io_.find_header_row(result.grid, POS_BILL_WISE_REQUIRED_COLUMNS)
    header = [str(c).strip() for c in result.grid[header_idx]]
    data_rows = result.grid[header_idx + 1:]
    return pd.DataFrame(data_rows, columns=header), result.sheet_row_counts


def _ingest_pos_bill_wise_one_file(config: Config, file_path: Path, mtime,
                                   use_warehouse: bool = True) -> tuple:
    """Reads (or warehouse-caches) exactly ONE POS bill-wise file, fully
    enriched. Split out from `ingest_pos_bill_wise` (2026-09-24) so the
    caller can discover and read every file a source now holds — a folder
    can hold one file per month rather than always one dominant dump — and
    resolve any genuinely overlapping dates across them afterwards, while
    each individual file still gets its own warehouse-Parquet cache hit/miss
    exactly as before (the manifest is keyed per file, not per source)."""
    manifest = warehouse_manifest("pos_bill_wise", file_path) if use_warehouse else None
    if manifest is not None:
        df = read_warehouse(manifest)
        logger.info(
            f"ingest_pos_bill_wise: read {len(df)} rows from warehouse Parquet "
            f"({len(manifest['parquet'])} part(s)) for '{file_path.name}' — raw file untouched"
        )
        cached_missing = [c for c in ("bill_time", "created_by") if c not in df.columns
                         or df[c].isna().all()]
        return df, {
            "sheet_row_counts": (manifest.get("extra") or {}).get("sheet_row_counts") or {},
            "rows_without_date": (manifest.get("extra") or {}).get("rows_without_date", 0),
            "from_warehouse": True,
            "missing_optional_columns": cached_missing,
        }

    df, sheet_row_counts = _read_pos_raw(file_path)

    # The schema guard for bill_time/created_by (2026-09-29) — loud, not a
    # hard raise: these two are kept OUT of POS_BILL_WISE_REQUIRED_COLUMNS
    # on purpose (that list also anchors header-row detection, and this
    # source folder still holds older, permanently-superseded duplicate
    # files — MOP SALE DATA.xlsb, the retired daily .xlsx sample — that
    # never carried these columns and never will; raising here would
    # crash discovery on a file that is about to lose in overlap
    # resolution anyway). A file that SHOULD have them (every real monthly
    # export, April-August, confirmed) losing them is still a real
    # regression worth knowing about the day it happens, so it is a named,
    # visible WARNING here, threaded into the load report below, never
    # silently absorbed into a blank column.
    missing_optional = [c for c in POS_BILL_WISE_OPTIONAL_COLUMNS if c not in df.columns]
    if missing_optional:
        logger.warning(
            f"ingest_pos_bill_wise: '{file_path.name}' has no {missing_optional} "
            "column(s) — bill_time/created_by will be blank for every row of "
            "this file. Expected on the known-retired duplicate exports; a "
            "real monthly file losing these is a regression."
        )

    keep_cols = POS_BILL_WISE_REQUIRED_COLUMNS + [
        c for c in POS_BILL_WISE_OPTIONAL_COLUMNS if c in df.columns] + [
        c for c in ("paytm_ref", "axis_ref", "bajaj_ref", "tpedc_parse_failed") if c in df.columns]
    df = df[[c for c in keep_cols if c in df.columns]]

    df = df.rename(columns={
        "BILLNO": "bill_no",
        "PSITE_POSBILL_CODE": "site_ref",
        "STORE_NAME": "store",
        "MOPDESC": "mop",
        "BASEAMT": "amount",
        "CREATEBY": "created_by",
    })
    df["amount"] = df["amount"].astype(float)
    # BILLDATE arrives as an Excel serial float from .xlsb and a real datetime
    # from .xlsx — parse_business_date handles both and refuses to guess.
    df["business_date"] = io_.parse_business_date_series(df["BILLDATE"])
    # BILL_TIME — an Excel time serial on every file that carries it at all
    # (io_.parse_bill_time). Absent entirely on the older, superseded files
    # (§ above) — those rows get a genuinely blank bill_time, never a guess.
    if "BILL_TIME" in df.columns:
        df["bill_time"] = io_.parse_bill_time_series(df["BILL_TIME"])
    else:
        df["bill_time"] = None
    if "created_by" not in df.columns:
        df["created_by"] = None

    _assert_known_mops(df.rename(columns={"mop": "MOPDESC", "store": "STORE_NAME"}), config)

    # CLAUDE.md 6.13 — a row with no bill number is not a bill. It is a cash
    # in/out movement, excluded from the sale universe whatever its MOP, but
    # kept in the frame, counted, and reported per MOP so a sudden change in
    # them is noticed rather than absorbed.
    bill_no_blank = df["bill_no"].isna() | (df["bill_no"].astype(str).str.strip() == "")
    df["in_sale_universe"] = ~bill_no_blank
    excluded = df.loc[bill_no_blank]
    excluded_by_mop = {
        str(mop): {"count": int(len(g)), "total_amount": float(g["amount"].sum())}
        for mop, g in excluded.groupby("mop")
    }
    if len(excluded):
        logger.warning(
            f"ingest_pos_bill_wise: {len(excluded)} row(s) in '{file_path.name}' have no "
            f"BILLNO — excluded from the sale universe (CLAUDE.md 6.13), kept and reported. "
            f"Per MOP: {excluded_by_mop}"
        )

    if "TPEDCRESPONSE" in df.columns:
        refs = df["TPEDCRESPONSE"].map(_extract_pos_refs)
        df["paytm_ref"] = [r[0] for r in refs]
        df["axis_ref"] = [r[1] for r in refs]
        df["bajaj_ref"] = [r[2] for r in refs]
        df["tpedc_parse_failed"] = [r[3] for r in refs]
    else:
        # The calamine reader already derived them per sheet and dropped the JSON. Put the
        # four columns where the pyxlsb path leaves them - after everything computed so
        # far - so the two paths return the same frame, column for column.
        derived = ["paytm_ref", "axis_ref", "bajaj_ref", "tpedc_parse_failed"]
        df = df[[c for c in df.columns if c not in derived] + derived]

    parse_failed_count = int(df["tpedc_parse_failed"].sum())
    if parse_failed_count:
        failed_bills = df.loc[df["tpedc_parse_failed"], "bill_no"].tolist()
        logger.warning(
            f"ingest_pos_bill_wise: {parse_failed_count} row(s) in '{file_path.name}' had a "
            f"TPEDCRESPONSE value that failed to parse as JSON — bill_no(s): {failed_bills[:20]}"
            + (" ..." if parse_failed_count > 20 else "")
        )

    # Dropped now, not carried any further (2026-09-24) — every reference this
    # column can yield is already in paytm_ref/axis_ref/bajaj_ref above, and
    # nothing downstream (R1 matching, persistence, the warehouse cache, this
    # function's own duplicate-detection report below) ever reads the raw
    # JSON again. CLAUDE.md's own performance notes already named this "65%
    # of the frame's memory" as a known, not-yet-taken win; at five months'
    # scale it stopped being optional — carrying it through the multi-file
    # concat and the per-date Parquet write is what turned a routine
    # duplicate-bill check into `pyarrow.lib.ArrowMemoryError: realloc of
    # size 2566667392 failed`, reproduced and confirmed as the actual crash.
    df = df.drop(columns=["TPEDCRESPONSE"], errors="ignore")

    # line_seq — a bill can carry up to 4 MOP lines.
    df = df.sort_index()
    df["line_seq"] = df.groupby("bill_no").cumcount() + 1

    mop_class = {mop: entry["class"] for mop, entry in config.mop_classification.items()}
    df["mop_class"] = df["mop"].map(mop_class)

    rows_without_date = int(df["business_date"].isna().sum())
    if rows_without_date:
        logger.warning(
            f"ingest_pos_bill_wise: {rows_without_date} row(s) in '{file_path.name}' have no "
            "business_date — kept in the returned DataFrame, excluded from the per-date "
            "Parquet written after cross-file merge"
        )

    cache_paths = _write_parquet_single_file_cache(df, "pos_bill_wise", file_path)
    _write_manifest("pos_bill_wise", file_path, cache_paths, extra={
        "sheet_row_counts": sheet_row_counts or {},
        "rows_without_date": rows_without_date,
    })

    return df, {
        "sheet_row_counts": sheet_row_counts or {},
        "rows_without_date": rows_without_date,
        "from_warehouse": False,
        "missing_optional_columns": missing_optional,
    }


def ingest_pos_bill_wise(config: Config, session, file_path: Path = None,
                         use_warehouse: bool = True, on_progress=None) -> tuple:
    """`file_path` targets one specific dump; omit it to read every file
    discovered for the source (CLAUDE.md section 4) — a folder can hold one
    file per month (April, May, June, July, August) as well as competing
    duplicate dumps of the same date, so picking only the single
    most-recently-modified file across the whole tree would silently drop
    every other month. Any two files that genuinely cover the SAME business
    date still resolve to the most-recently-modified of the two, per row
    (`_resolve_date_overlaps`) — this is what makes July's two competing
    exports (`2026-07.xlsb` superseding `MOP SALE DATA.xlsb`) still resolve
    to exactly one winner, unchanged from before.

    With `use_warehouse` (the default), an already-ingested file is read back
    from Parquet instead of re-opening the raw workbook — per file, so a
    fresh month's file is read raw while an unchanged month's file is not
    re-opened. Pass False to force a raw re-read of every file."""
    files = _discover_files_for_source(config, "pos_bill_wise", file_path)

    per_file_meta = {}
    parts = []
    for i, (fp, mt) in enumerate(files, 1):
        # The POS raw read is the single most expensive stage in a run (a
        # 250-300MB .xlsb can take several minutes) and reading several
        # months' worth of them used to be entirely silent until the whole
        # stage finished — the thing that made a genuinely slow run
        # indistinguishable from a hung one (2026-09-24). Reported both
        # before and after each file so "reading" and "stuck between files"
        # are never confused.
        if on_progress:
            on_progress(f"reading POS file {i} of {len(files)}: {fp.name}")
        df_one, meta = _ingest_pos_bill_wise_one_file(config, fp, mt, use_warehouse)
        df_one["_src_file"] = str(fp)
        df_one["_src_mtime"] = mt.timestamp()
        parts.append(df_one)
        per_file_meta[str(fp)] = meta
        if on_progress:
            on_progress(f"read {len(df_one):,} rows from POS file {i} of {len(files)}: {fp.name}")

    combined = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    # business_date, not a per-row natural key, deliberately for POS: its one
    # real duplicate (July's two competing exports) is a full re-export of
    # the exact same bills for the exact same dates with no incidental-leak
    # case like Bajaj's, so resolving whole dates is correct and simpler
    # than reconstructing a per-line key across files.
    df, supersede_report = _resolve_key_overlaps(combined, "business_date", "pos_bill_wise")

    # Merged across every file read this call — keyed by filename so two
    # files' sheets sharing a name (e.g. both called "Sheet1") can never
    # collide.
    sheet_row_counts = {
        f"{Path(fp).name}::{sheet}": count
        for fp, meta in per_file_meta.items()
        for sheet, count in (meta["sheet_row_counts"] or {}).items()
    }
    rows_without_date = sum(meta["rows_without_date"] for meta in per_file_meta.values())

    # The authoritative per-BUSINESS-DATE Parquet is written once, here, from
    # the post-merge winning rows only — never per file (see the comment on
    # _write_parquet_single_file_cache for why writing it per file would let
    # a losing file's rows silently overwrite a winning file's rows on disk
    # for a date they both claim).
    warehouse_paths, _ = _write_parquet_by_date(df, "pos_bill_wise", "business_date")
    from_warehouse_all = all(meta["from_warehouse"] for meta in per_file_meta.values())

    # bill_time/created_by missing on a file that ought to have them
    # (2026-09-29) — named per file, never silently absorbed. Expected on
    # the known-retired duplicate exports (which lose in overlap
    # resolution anyway); unexpected on anything else.
    files_missing_optional = {
        Path(fp).name: meta["missing_optional_columns"]
        for fp, meta in per_file_meta.items() if meta.get("missing_optional_columns")
    }

    return df, _pos_report_from_df(
        df, files, per_file_meta,
        sheet_row_counts=sheet_row_counts,
        warehouse_paths=warehouse_paths,
        rows_without_date=rows_without_date,
        from_warehouse=from_warehouse_all,
        supersede_report=supersede_report,
        files_missing_optional=files_missing_optional,
    )


def _pos_report_from_df(df, files, per_file_meta: dict, sheet_row_counts: dict,
                        warehouse_paths: dict, rows_without_date: int,
                        from_warehouse: bool, supersede_report: list = None,
                        files_missing_optional: dict = None) -> LoadReport:
    """Builds the POS load report from the frame, across every file read this
    call (2026-09-24 — a source can now hold several genuinely distinct
    files, not just one). `files` is the full (file_path, mtime) list;
    `per_file_meta` carries each file's own from_warehouse/sheet_row_counts/
    rows_without_date, keyed by str(file_path).
    """
    sale = df.loc[df["in_sale_universe"]]
    excluded = df.loc[~df["in_sale_universe"]]

    class_breakdown = {}
    for cls, group in sale.groupby("mop_class"):
        class_breakdown[cls] = {"count": int(len(group)), "total_amount": float(group["amount"].sum())}

    excluded_by_mop = {
        str(mop): {"count": int(len(g)), "total_amount": float(g["amount"].sum())}
        for mop, g in excluded.groupby("mop")
    } if len(excluded) else {}

    dup_groups = detect_duplicate_candidates(sale)
    dup_bill_count = sum(len(g["bill_nos"]) for g in dup_groups)

    notes = [
        "No store master exists — 'store' is STORE_NAME as supplied, not independently "
        "validated against a master (see CLAUDE.md section 5).",
        "Class totals and all bill counts cover the SALE UNIVERSE only "
        "(rows carrying a BILLNO) — CLAUDE.md 6.13.",
    ]
    if sheet_row_counts:
        notes.append(f"Read across {len(sheet_row_counts)} sheet(s): {sheet_row_counts}")
    if files_missing_optional:
        notes.append(
            "bill_time/created_by missing (blank for every row) in: "
            f"{files_missing_optional} — expected only for a known-retired, "
            "already-superseded duplicate export; a regression on any other file."
        )
    warehouse_files = [Path(fp).name for fp, mt in files if per_file_meta.get(str(fp), {}).get("from_warehouse")]
    raw_read_files = [Path(fp).name for fp, mt in files if not per_file_meta.get(str(fp), {}).get("from_warehouse")]
    if warehouse_files:
        notes.append(
            f"Read from warehouse Parquet (raw file not opened, unchanged since ingested): {warehouse_files}"
        )
    if raw_read_files:
        notes.append(f"Read from the raw workbook (new or changed since last ingested): {raw_read_files}")
    if supersede_report:
        notes.append(f"superseded_by_more_recent_file: {supersede_report}")

    return LoadReport(
        source="pos_bill_wise",
        status="ok_from_warehouse" if from_warehouse else "ok",
        files=[FileRead(path=str(fp), mtime=mt) for fp, mt in files],
        business_dates=sorted(str(d) for d in sale["business_date"].dropna().unique()),
        rows_read=len(df),
        rows_with_store=int(df["store"].notna().sum()),
        rows_without_date=rows_without_date,
        control_total=float(sale["amount"].sum()),
        breakdown={
            "mop": sale["mop"].value_counts().to_dict(),
            "mop_class": class_breakdown,
            "sale_universe": {
                "rows_in_sale_universe": int(len(sale)),
                "rows_without_bill_no": int(len(excluded)),
                "rows_without_bill_no_per_mop": excluded_by_mop,
                "excluded_total_amount": float(excluded["amount"].sum()) if len(excluded) else 0.0,
            },
            "sheet_row_counts": sheet_row_counts or {},
            "reference_extraction": {
                "paytm_ref_found": int(df["paytm_ref"].notna().sum()),
                "axis_ref_found": int(df["axis_ref"].notna().sum()),
                "bajaj_ref_found": int(df["bajaj_ref"].notna().sum()),
                "neither": int((df["paytm_ref"].isna() & df["axis_ref"].isna()
                                & df["bajaj_ref"].isna() & (~df["tpedc_parse_failed"])).sum()),
                "parse_failed": int(df["tpedc_parse_failed"].sum()),
            },
            "rows_per_business_date": {
                str(d): int(n) for d, n in sale["business_date"].value_counts().sort_index().items()
            },
            "distinct_bill_no": int(sale["bill_no"].nunique()),
            "multi_line_bills": int((sale.groupby("bill_no").size() > 1).sum()),
            "duplicate_candidates": {"groups": len(dup_groups), "bills_covered": dup_bill_count},
        },
        warehouse_paths=warehouse_paths,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# POS store-wise — not supplied; derived from bill-wise, never authoritative
# ---------------------------------------------------------------------------

STORE_WISE_REQUIRED_COLUMNS = ["STORE_NAME", "BILLDATE", "MOP", "COLLECTION", "MOP TYPE"]


_STORE_WISE_SHAPE_CACHE: dict = {}


def preflight_bill_vs_store_wise(config: Config) -> dict:
    """CLAUDE.md section 6.11. Must never report as passed while no real,
    independent store-wise file exists — there is nothing to compare against.

    Cached per (path, mtime, size): this only needs the header row, but
    `pyxlsb` must parse the whole 222MB workbook structure before yielding a
    single row (~46s even for a 5-row peek), so the answer is computed once
    per file version rather than on every call."""
    status = config.source_status("pos_store_wise")
    if not status.files:
        return {"status": "store-wise dump not supplied", "detail": "no file present for source 'pos_store_wise'"}

    file_path = max(status.files, key=lambda p: p.stat().st_mtime)
    st = file_path.stat()
    cache_key = (str(file_path), st.st_mtime, st.st_size)
    if cache_key in _STORE_WISE_SHAPE_CACHE:
        return _STORE_WISE_SHAPE_CACHE[cache_key]

    result = io_.read_any(file_path, max_rows=30)
    try:
        io_.find_header_row(result.grid, STORE_WISE_REQUIRED_COLUMNS)
    except io_.IOError_:
        outcome = {
            "status": "store-wise dump not supplied",
            "detail": (
                f"'{file_path.name}' does not have the store-wise column shape "
                f"(expected {STORE_WISE_REQUIRED_COLUMNS} on one header row) — "
                "this is the bill-wise file; no separate store-wise dump exists."
            ),
        }
    else:
        outcome = {
            "status": "store-wise file found but comparison not yet implemented",
            "detail": f"'{file_path.name}' has the expected columns — this has never happened before; the comparison logic needs building and testing against it before it can report pass/fail.",
        }
    _STORE_WISE_SHAPE_CACHE[cache_key] = outcome
    return outcome


def derive_store_wise_totals(bill_wise_df: pd.DataFrame) -> pd.DataFrame:
    """BASEAMT (amount) grouped by store, business_date and mop_class — a
    fallback for the missing store-wise dump. Never authoritative.

    Restricted to the sale universe (CLAUDE.md 6.13). Without this filter the
    non-bill cash in/out rows land straight back in the store-level cash total
    and re-break the very figure the exclusion rule exists to fix."""
    if "in_sale_universe" in bill_wise_df.columns:
        bill_wise_df = bill_wise_df.loc[bill_wise_df["in_sale_universe"]]
    totals = (
        bill_wise_df.groupby(["store", "business_date", "mop_class"], dropna=False)["amount"]
        .sum()
        .reset_index()
        .rename(columns={"amount": "total_amount"})
    )
    totals["source"] = "derived_from_bill_wise_not_authoritative"
    return totals


def ingest_pos_store_wise(config: Config, session, bill_wise_df: pd.DataFrame = None) -> tuple:
    if bill_wise_df is None:
        bill_wise_df, _ = ingest_pos_bill_wise(config, session)

    totals = derive_store_wise_totals(bill_wise_df)
    preflight = preflight_bill_vs_store_wise(config)

    out_dir = WAREHOUSE_ROOT / "pos_store_wise"
    out_dir.mkdir(parents=True, exist_ok=True)
    warehouse_paths = {}
    for business_date, group in totals.groupby("business_date", dropna=True):
        date_str = pd.Timestamp(business_date).strftime("%Y-%m-%d")
        out_path = out_dir / f"{date_str}.parquet"
        group.to_parquet(out_path, index=False)
        warehouse_paths[date_str] = str(out_path)

    report = LoadReport(
        source="pos_store_wise",
        status="derived_not_authoritative",
        rows_read=len(totals),
        business_dates=sorted(str(d) for d in totals["business_date"].dropna().unique()),
        control_total=float(totals["total_amount"].sum()),
        breakdown={"preflight_bill_vs_store_wise": preflight},
        warehouse_paths=warehouse_paths,
        notes=[
            "DERIVED from the bill-wise dump — NOT authoritative. No real store-wise file exists.",
            f"Pre-flight check result: {preflight['status']}",
        ],
    )
    return totals, report


# ---------------------------------------------------------------------------
# Duplicate-bill candidate detection — CLAUDE.md section 6.8
# ---------------------------------------------------------------------------

def detect_duplicate_candidates(bill_wise_df: pd.DataFrame) -> list:
    """Digital-MOP bill lines (class 'digital' or 'digital_noref') sharing
    store + business_date + amount with another bill line - CLAUDE.md 6.8. Returns
    the bill numbers in each group, in row order, so a group can be investigated.
    A group qualifies on row count, not distinct-bill count.

    Polars group-by (2026-10-13): the pandas `groupby(...).apply(list)` over ~566k
    groups took 33 s a month. Same groups, same order (sorted by store, date,
    amount), same bill_nos order, rows with a missing store/date/amount excluded as
    before - `tests/test_pos_ingest_calamine.py` asserts it against
    `detect_duplicate_candidates_pandas` on randomised and on real data."""
    import polars as pl

    needed = bill_wise_df[["store", "business_date", "amount", "bill_no", "mop_class"]]
    digital = needed[needed["mop_class"].isin(["digital", "digital_noref"])]
    frame = pl.from_pandas(digital[["store", "business_date", "amount", "bill_no"]].reset_index(drop=True))
    groups = (
        frame.drop_nulls(["store", "business_date", "amount"])
        .filter(~pl.col("amount").is_nan())
        .group_by(["store", "business_date", "amount"], maintain_order=True)
        .agg(pl.col("bill_no"))
        .filter(pl.col("bill_no").list.len() > 1)
        .sort(["store", "business_date", "amount"])
    )
    return [
        {"store": store, "business_date": str(day), "amount": float(amount), "bill_nos": bill_nos}
        for store, day, amount, bill_nos in groups.iter_rows()
    ]


def detect_duplicate_candidates_pandas(bill_wise_df: pd.DataFrame) -> list:
    """THE SPECIFICATION (pandas) of `detect_duplicate_candidates`, kept for the equivalence
    test - the live function is the Polars one below. Digital-MOP bill lines (class 'digital' or 'digital_noref') sharing
    store + business_date + amount with another bill line. Returns the bill
    numbers in each group, not just a count, so a group can be investigated.

    A group qualifies on row count, not distinct-bill count, and bill_nos is
    the raw list (a bill_no can repeat within a group — e.g. one bill with
    two same-amount lines). This matches how the reference figures were
    computed: deduping bill_no first before counting gives 1,663 groups /
    4,142 bills, one and sixteen short of the 1,664 / 4,158 the raw-row count
    reproduces exactly.

    Only the 5 columns actually used are sliced out BEFORE filtering
    (2026-09-24) — `bill_wise_df` carries TPEDCRESPONSE and its derived
    paytm_ref/axis_ref/bajaj_ref columns, which CLAUDE.md's own performance
    notes call "65% of the frame's memory"; filtering the FULL frame first
    means the boolean-index copy carries all of that along for no reason.
    At one month's scale this never mattered; at five months (~8.6M rows)
    it was the difference between this function working and a genuine
    `pyarrow.lib.ArrowMemoryError: realloc of size 2566667392 failed` —
    reproduced and confirmed as the actual crash, not a hypothetical."""
    needed = bill_wise_df[["store", "business_date", "amount", "bill_no", "mop_class"]]
    digital = needed[needed["mop_class"].isin(["digital", "digital_noref"])]

    groups = (
        digital.groupby(["store", "business_date", "amount"])["bill_no"]
        .apply(list)
        .reset_index()
    )
    groups = groups[groups["bill_no"].map(len) > 1]

    return [
        {
            "store": row["store"],
            "business_date": str(row["business_date"]),
            "amount": float(row["amount"]),
            "bill_nos": row["bill_no"],
        }
        for _, row in groups.iterrows()
    ]


# ---------------------------------------------------------------------------
# PTC vouchers — CLAUDE.md section 5. Layout is detected by which columns are
# present, NEVER by the file's date (an earlier revision assumed a "July
# layout" and an "August layout"; the 21-Jul sample uses the August columns).
# ---------------------------------------------------------------------------

# logical field -> acceptable column names, layout A first then layout B.
PTC_ALIASES = {
    "store": ["STORE_NAME", "Source Short Name"],
    "date": ["BILLDATE", "Billdate"],
    "voucher_no": ["BILLNO", "Billno"],
    "mode": ["PTCMODE", "Ptcmode"],
    "head": ["PTCHEAD", "PTC Head"],
    "ledger": ["LEDGER", "Ledger"],
    "amount": ["PTC Bill Amount SUM", "POSTED_AMOUNT"],
    "remarks": ["REMARKS", "Remarks"],
}

# A deposit voucher is identified by EITHER co-identifier (§5). Both are
# accepted; a file where one appears without the other is a data-quality
# exception, not a reason to trust either alone.
PTC_DEPOSIT_HEAD = "Cash/CMS Deposit"
PTC_DEPOSIT_LEDGER = "CMS-RECD."


def _ptc_columns(header: list, file_label: str) -> dict:
    """logical field -> actual column name, by presence not by date."""
    resolved = {}
    for field_name, candidates in PTC_ALIASES.items():
        for candidate in candidates:
            if candidate in header:
                resolved[field_name] = candidate
                break
        else:
            raise IngestError(
                f"{file_label}: no acceptable column found for PTC field "
                f"{field_name!r} — tried {candidates}, header has {header}"
            )
    return resolved


def ingest_ptc(config: Config, session, file_path: Path = None) -> tuple:
    """PTC vouchers: the income/expense movements and the cash deposits.

    net_ptc is ONE netted column by decision (§6.6): PAYMENT rows arrive
    negative and RECEIVE rows positive, so the net is simply the sum. The
    income/expense split is reported for information, never emitted as two
    output columns.
    """
    if file_path is None:
        file_path, mtime = io_.latest_file(config, "ptc")
    else:
        file_path = Path(file_path)
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)

    encoding = config.encoding_for("ptc") or "cp1252"
    result = io_.read_any(file_path, encoding=encoding)

    # Header is on row 3 of the supplied CSV (row 1 a Cube Name title, row 2
    # blank) — found, not assumed, so a file without the preamble still works.
    header_idx = _ptc_header_row(result.grid, file_path.name)
    header = [str(c).strip() for c in result.grid[header_idx]]
    cols = _ptc_columns(header, file_path.name)

    df = pd.DataFrame(result.grid[header_idx + 1:], columns=header)
    df = df.rename(columns={actual: logical for logical, actual in cols.items()})
    df = df[list(PTC_ALIASES)]

    df["store"] = df["store"].astype(str).str.strip()
    df["amount"] = df["amount"].map(_to_float)
    df["business_date"] = io_.parse_business_date_series(df["date"])
    df["mode"] = df["mode"].astype(str).str.strip().str.upper()
    df["head"] = df["head"].astype(str).str.strip()
    df["ledger"] = df["ledger"].astype(str).str.strip()

    # Drop wholly blank trailing rows the CSV pads with, without touching a row
    # that merely has a blank optional field.
    df = df[df["store"].ne("") & df["store"].ne("nan")].reset_index(drop=True)

    by_head = df["head"].eq(PTC_DEPOSIT_HEAD)
    by_ledger = df["ledger"].eq(PTC_DEPOSIT_LEDGER)
    df["is_deposit"] = by_head | by_ledger
    disagreements = int((by_head ^ by_ledger).sum())

    # Deposits are PAYMENT rows (negative). Sign-flip to a positive deposit.
    df["deposit_amount"] = 0.0
    df.loc[df["is_deposit"], "deposit_amount"] = -df.loc[df["is_deposit"], "amount"]

    warehouse_paths, rows_without_date = _write_parquet_by_date(df, "ptc", "business_date")

    income = float(df.loc[df["amount"] > 0, "amount"].sum())
    expense = float(df.loc[df["amount"] < 0, "amount"].sum())

    notes = []
    if disagreements:
        notes.append(
            f"{disagreements} row(s) carry one deposit co-identifier without the "
            "other — accepted as deposits but flagged: section 5 says a "
            "disagreement is a data-quality exception, not a reason to trust "
            "either identifier alone."
        )

    report = LoadReport(
        source="ptc",
        status="ok",
        files=[FileRead(path=str(file_path), mtime=mtime, encoding=encoding)],
        business_dates=sorted(str(d) for d in df["business_date"].dropna().unique()),
        rows_read=len(df),
        rows_with_store=int(df["store"].ne("").sum()),
        rows_without_date=rows_without_date,
        control_total=float(df["amount"].sum()),
        breakdown={
            "mode": df["mode"].value_counts().to_dict(),
            "deposit_vouchers": int(df["is_deposit"].sum()),
            "deposit_total": float(df["deposit_amount"].sum()),
            "income_total": income,
            "expense_total": expense,
            "co_identifier_disagreements": disagreements,
            "layout_columns": dict(cols),
        },
        warehouse_paths=warehouse_paths,
        notes=notes,
    )
    return df, report


def _ptc_header_row(grid: list, file_label: str) -> int:
    """The row carrying the PTC mode column, under either layout."""
    for idx, row in enumerate(grid[:20]):
        cells = {str(c).strip() for c in row if c is not None}
        if cells & {"Ptcmode", "PTCMODE"}:
            return idx
    raise IngestError(
        f"{file_label}: no PTC header row found in the first 20 rows "
        "(looked for a Ptcmode / PTCMODE column)"
    )


# ---------------------------------------------------------------------------
# Opening cash balance — a Tally "Ledger Summary - Cost Centre" export, keyed
# by Cost Centre, which is the store's FULL name. The store master is
# therefore a prerequisite. Header is on row 5; rows 1-4 are title/period/
# filter lines, and the file ends with a Grand Total plus print footers.
# ---------------------------------------------------------------------------

OPENING_CASH_REQUIRED_COLUMNS = ["Cost Centre", "Opening"]


def ingest_opening_cash(config: Config, session, file_path: Path = None,
                        as_of=None) -> tuple:
    """Opening cash per store as at the file's own period start.

    A cost centre that does not resolve to a store code is reported by name and
    excluded — never guessed at, never fuzzy-matched (rule 5).
    """
    load_master(config, session, "store")

    if file_path is None:
        file_path, mtime = io_.latest_file(config, "opening_cash")
    else:
        file_path = Path(file_path)
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)

    result = io_.read_any(file_path)
    header_idx = io_.find_header_row(result.grid, OPENING_CASH_REQUIRED_COLUMNS)
    header = [str(c).strip() if c is not None else "" for c in result.grid[header_idx]]
    df = pd.DataFrame(result.grid[header_idx + 1:], columns=header)

    # Grand Total sits in Sub-Ledger with no Cost Centre, and the print footers
    # have no Cost Centre either — so requiring one drops all three without
    # matching on their text.
    grand_total = None
    if "Sub-Ledger" in df.columns:
        totals = df[df["Sub-Ledger"].astype(str).str.strip().eq("Grand Total :")]
        if not totals.empty:
            grand_total = _to_float(totals.iloc[0]["Opening"])

    df = df[df["Cost Centre"].notna()].copy()
    df["cost_centre"] = df["Cost Centre"].astype(str).str.strip()
    df = df[df["cost_centre"].ne("") & df["cost_centre"].ne("nan")]
    for column in ("Opening", "Debit", "Credit", "Closing"):
        if column in df.columns:
            df[column.lower()] = df[column].map(_to_float)

    as_of = as_of or _opening_period_start(result.grid)
    if as_of is None:
        raise IngestError(
            f"{file_path.name}: no 'Period:' line found, so the as-at date cannot "
            "be read from the file. Pass as_of explicitly — it is never guessed."
        )

    stores, unmapped = [], []
    for centre, opening in zip(df["cost_centre"], df["opening"]):
        outcome = resolve_store(session, "store_name", centre, as_of)
        stores.append(outcome.store)
        if outcome.store is None:
            unmapped.append({"cost_centre": centre, "opening": float(opening),
                             "status": outcome.status})
    df["store"] = stores

    mapped = df[df["store"].notna()].copy()
    # Two cost centres can legitimately carry the same store code (the master
    # lists MADHEPURA-2 and MADHEPURA-2-MCR, both MCR), so sum per store rather
    # than assuming one row each.
    per_store = (mapped.groupby("store", as_index=False)["opening"].sum()
                 .rename(columns={"opening": "opening_cash"}))
    per_store["as_of"] = str(as_of)

    notes = [f"opening cash as at {as_of}, read from the file's own Period line"]
    if unmapped:
        notes.append(
            f"{len(unmapped)} cost centre(s) did not resolve to a store code and are "
            f"excluded; their combined opening is "
            f"{sum(u['opening'] for u in unmapped):,.2f}. Listed by name in "
            "breakdown.unmapped_cost_centres — reported, never guessed."
        )
    if grand_total is not None:
        difference = round(grand_total - float(mapped["opening"].sum()), 2)
        notes.append(
            f"file Grand Total opening {grand_total:,.2f} vs mapped "
            f"{mapped['opening'].sum():,.2f} — difference {difference:,.2f}"
        )

    report = LoadReport(
        source="opening_cash",
        status="ok",
        files=[FileRead(path=str(file_path), mtime=mtime)],
        business_dates=[str(as_of)],
        rows_read=len(df),
        rows_with_store=len(mapped),
        control_total=float(per_store["opening_cash"].sum()),
        breakdown={
            "cost_centres_read": len(df),
            "cost_centres_mapped": len(mapped),
            "cost_centres_unmapped": len(unmapped),
            "unmapped_cost_centres": unmapped,
            "stores": len(per_store),
            "file_grand_total_opening": grand_total,
        },
        notes=notes,
    )
    return per_store, report


def _opening_period_start(grid: list):
    """The 'Period: 01-07-2026 To ...' line, so the as-at date comes from the
    file rather than being hardcoded (rule 8: no hardcoded dates)."""
    for row in grid[:8]:
        for cell in row:
            text = str(cell or "")
            if text.strip().startswith("Period:"):
                token = text.split(":", 1)[1].split("To")[0].strip()
                try:
                    return io_.parse_business_date(token)
                except Exception:
                    return None
    return None
