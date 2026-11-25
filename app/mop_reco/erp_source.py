"""Readers that turn the ERP's POS bill lines and petty-cash vouchers into the SAME normalised frames the Excel readers produce
(2026-11-23). Pure engine - no Streamlit. Every query goes through `mop_reco.erp`, which is read-only.

How it fits (docs/ERP_INTEGRATION_BRIEF.md section 8): the reader here only RENAMES the ERP's columns to the headers the Excel files
carry (BILLNO, MOPDESC, BASEAMT, TPEDCRESPONSE ... and the PTC layout-B headers) and hands the frame to the shared normalisers in
`ingest` (`_normalise_pos_frame`, `_normalise_ptc_frame`). The reference extraction, line sequencing, MOP classes, store aliases and
every other rule therefore run through identical code for both sources - which is what makes a side-by-side comparison meaningful.

NOTHING in a reconciliation run reads from here yet: the sources stay on `file` until the shadow comparison (scripts/erp_shadow.py)
has agreed, month by month, with every difference explained.

The ERP views (DBA guide, 23 Nov 2026):
  gold_mop.pos_bill_lines  one row per bill x payment line. ALWAYS filter business_date with a half-open range (never a function
                           on the column). A day takes seconds, a month 10-20 s; the login's statement limit is 120 s, so the
                           reader pulls `chunk_days` at a time (default 1).
  gold_mop.ptc_vouchers    one row per petty-cash voucher (exists from the night of 23/24 Nov 2026).
Columns the final POS view carries are listed in POS_COLUMNS. `edc_response` (the terminal reply, JSON, may hold masked card numbers)
is parsed into references by the shared normaliser and DROPPED; it is never logged, printed or stored.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Iterator

import pandas as pd

from . import erp

logger = logging.getLogger(__name__)

POS_VIEW = "gold_mop.pos_bill_lines"
PTC_VIEW = "gold_mop.ptc_vouchers"

# ERP column -> the header the Excel export carries. The first six are REQUIRED: without `edc_response` a bill cannot be matched to a
# payment (CLAUDE.md 6.2), so a view that lacks it is refused rather than read as if every bill had no reference.
POS_COLUMNS = {
    "bill_no": "BILLNO",
    "store_name": "STORE_NAME",
    "business_date": "BILLDATE",
    "mop": "MOPDESC",
    "amount": "BASEAMT",
    "edc_response": "TPEDCRESPONSE",
    "site_ref": "PSITE_POSBILL_CODE",
    "bill_time": "BILL_TIME",
    "created_by": "CREATEBY",
}
POS_REQUIRED = ("bill_no", "store_name", "business_date", "mop", "amount", "edc_response")

# PTC: ERP column -> the layout-B header (ingest.PTC_ALIASES knows both layouts). All eight are required.
PTC_COLUMNS = {
    "store": "Source Short Name",
    "business_date": "Billdate",
    "voucher_no": "Billno",
    "mode": "Ptcmode",
    "head": "PTC Head",
    "ledger": "Ledger",
    "amount": "PTC Bill Amount SUM",
    "remarks": "Remarks",
}


class ErpSchemaError(RuntimeError):
    """The ERP view lacks something the application needs (names it - never guesses a substitute)."""


def view_columns(conn, relation: str) -> list:
    rows = erp.run_select(
        conn,
        "select a.attname from pg_attribute a where a.attrelid = to_regclass(%s) and a.attnum > 0 and not a.attisdropped "
        "order by a.attnum", [relation])
    return [r[0] for r in rows]


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min)


def _chunks(d_from: date, d_to: date, chunk_days: int) -> Iterator[tuple]:
    if d_to < d_from:
        raise ValueError("the end date is before the start date")
    step = max(1, int(chunk_days))
    cur = d_from
    while cur <= d_to:
        end = min(cur + timedelta(days=step - 1), d_to)
        yield cur, end
        cur = end + timedelta(days=1)


def check_pos_schema(conn, view: str = POS_VIEW) -> list:
    """The view's columns, or ErpSchemaError naming what is missing. Call before reading."""
    have = view_columns(conn, view)
    if not have:
        raise ErpSchemaError(f"{view} does not exist or is not readable by this login.")
    missing = [c for c in POS_REQUIRED if c not in have]
    if missing:
        raise ErpSchemaError(
            f"{view} has no column(s) {', '.join(missing)} yet - the application cannot match bills to payments without them. "
            "They are expected after the DBA's night load of 23/24 Nov 2026; nothing is read until they exist.")
    return have


def check_ptc_schema(conn, view: str = PTC_VIEW) -> list:
    have = view_columns(conn, view)
    if not have:
        raise ErpSchemaError(f"{view} does not exist yet or is not readable by this login (expected after the night load of 23/24 Nov 2026).")
    missing = [c for c in PTC_COLUMNS if c not in have]
    if missing:
        raise ErpSchemaError(f"{view} has no column(s) {', '.join(missing)}.")
    return have


def iter_pos_raw(conn, d_from: date, d_to: date, chunk_days: int = 1, view: str = POS_VIEW) -> Iterator[pd.DataFrame]:
    """Yield the POS bill lines for each chunk of dates as a frame with the EXCEL headers (BILLNO, STORE_NAME, BILLDATE, MOPDESC,
    BASEAMT, TPEDCRESPONSE, and PSITE_POSBILL_CODE / BILL_TIME / CREATEBY when the view has them). Memory stays bounded to one chunk.
    Lines are ordered bill_no, mop, amount, bill_time so `line_seq` is deterministic (it can differ from the Excel file's own row
    order for a bill with several lines; bill-level results are identical)."""
    have = check_pos_schema(conn, view)
    cols = [c for c in POS_COLUMNS if c in have]
    select = ", ".join(cols)
    order = "bill_no, mop, amount" + (", bill_time" if "bill_time" in have else "")
    sql = (f"select {select} from {view} where business_date >= %s and business_date < %s order by {order}")
    for a, b in _chunks(d_from, d_to, chunk_days):
        rows = erp.run_select(conn, sql, [_day_start(a), _day_start(b + timedelta(days=1))])
        df = pd.DataFrame(rows, columns=cols)
        df["amount"] = df["amount"].astype(float)
        yield df.rename(columns={c: POS_COLUMNS[c] for c in cols})


def iter_ptc_raw(conn, d_from: date, d_to: date, chunk_days: int = 31, view: str = PTC_VIEW) -> Iterator[pd.DataFrame]:
    """Yield petty-cash vouchers per chunk with the layout-B headers the PTC file carries."""
    check_ptc_schema(conn, view)
    cols = list(PTC_COLUMNS)
    sql = (f"select {', '.join(cols)} from {view} where business_date >= %s and business_date < %s order by business_date, store, voucher_no")
    for a, b in _chunks(d_from, d_to, chunk_days):
        rows = erp.run_select(conn, sql, [_day_start(a), _day_start(b + timedelta(days=1))])
        df = pd.DataFrame(rows, columns=cols)
        df["amount"] = df["amount"].astype(float)
        yield df.rename(columns=PTC_COLUMNS)


def merge_reference_less_lines(df: pd.DataFrame) -> tuple:
    """One line per bill x reference-less digital MOP, as the Excel export always carried it. Returns (frame, lines_removed).

    The ERP keeps one row per TENDER, the Excel export one row per bill x MOP for a method that has no terminal reference. Measured
    2026-11-25 on every April-October date (5.08M bill x MOP groups): the two sources agree on every group except 68 `EDC OTHER` groups
    that the ERP splits into 2-4 lines (e.g. 99 + 527) where the file holds one (626); the summed amount is identical in every one, no
    other MOP differs, and no merged line carries a reference. R1's Tier 2 matches one line to one transaction by exact amount, so a
    split line changes the match (CLAUDE.md 6.3) - merging restores the audited file result.

    Only lines of the same bill and MOP are merged, only when NONE of them carries a Paytm / Axis / Bajaj reference (a line with a
    reference is a payment of its own and is never merged), and only for the `digital_noref` class. The amount is the exact sum; the first
    line keeps its time, operator and site reference; `line_seq` is renumbered."""
    if df.empty or "mop_class" not in df.columns:
        return df, 0
    keys = ["bill_no", "mop"]
    cand = (df["mop_class"] == "digital_noref") & df["in_sale_universe"] & df["paytm_ref"].isna() & df["axis_ref"].isna() & df["bajaj_ref"].isna()
    sub = df.loc[cand]
    if sub.empty:
        return df, 0
    multi = sub[sub.groupby(keys)["amount"].transform("size") > 1]
    if multi.empty:
        return df, 0
    by_group = multi.groupby(keys)
    first = by_group.head(1).index
    df = df.copy()
    df.loc[first, "amount"] = by_group["amount"].transform("sum").loc[first].round(2)
    if "tpedc_parse_failed" in df.columns:
        df.loc[first, "tpedc_parse_failed"] = by_group["tpedc_parse_failed"].transform("max").loc[first]
    drop = multi.index.difference(first)
    df = df.drop(index=drop).reset_index(drop=True)
    df["line_seq"] = df.groupby("bill_no").cumcount() + 1
    return df, int(len(drop))


def merge_enabled(config) -> bool:
    return bool((((getattr(config, "raw", None) or {}).get("erp") or {}).get("merge_reference_less_lines", True)))


def normalise_pos(config, raw: pd.DataFrame, label: str = "ERP gold_mop.pos_bill_lines") -> pd.DataFrame:
    """A raw ERP chunk -> the normalised POS frame, through the SAME function the Excel reader uses."""
    from . import ingest
    from .masters import store_aliases
    df, _rows_without_date = ingest._normalise_pos_frame(config, raw, label)
    if store_aliases():                                    # config store_code_aliases (the Excel path applies it after the read)
        df["store"] = df["store"].replace(store_aliases())
    if merge_enabled(config):
        df, removed = merge_reference_less_lines(df)
        if removed:
            logger.info("%s: merged %d reference-less same-MOP line(s) into their bill's single line (as the Excel export carries them)", label, removed)
    return df


def normalise_ptc(raw: pd.DataFrame) -> pd.DataFrame:
    """A raw ERP voucher chunk -> the normalised PTC frame, through the SAME function the file reader uses, plus the deposit flags
    `ingest_ptc` adds (is_deposit = head OR ledger; deposit_amount = the positive deposit)."""
    from . import ingest
    header = list(raw.columns)
    cols = ingest._ptc_columns(header, "ERP gold_mop.ptc_vouchers")
    df = ingest._normalise_ptc_frame(raw.copy(), cols)
    by_head = df["head"].eq(ingest.PTC_DEPOSIT_HEAD)
    by_ledger = df["ledger"].eq(ingest.PTC_DEPOSIT_LEDGER)
    df["is_deposit"] = by_head | by_ledger
    df["deposit_amount"] = 0.0
    df.loc[df["is_deposit"], "deposit_amount"] = -df.loc[df["is_deposit"], "amount"]
    return df.reset_index(drop=True)


def read_pos(config, conn, d_from: date, d_to: date, chunk_days: int = 1) -> pd.DataFrame:
    """The normalised POS frame for a (small) date range. For a month or more, iterate `iter_pos_raw` instead and normalise per chunk."""
    parts = [normalise_pos(config, raw) for raw in iter_pos_raw(conn, d_from, d_to, chunk_days)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def read_ptc(conn, d_from: date, d_to: date) -> pd.DataFrame:
    parts = [normalise_ptc(raw) for raw in iter_ptc_raw(conn, d_from, d_to)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
