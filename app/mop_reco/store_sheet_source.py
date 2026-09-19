"""Assembling the store-wise sheet from the warehouse, and the drill-down.

Separate from `store_sheet.py` so the engine there stays pure (§8). This module
does the I/O: it reads the per-date Parquet the run already wrote, and for a
single store-day it can produce the source records behind every column.

§6.14 judgment, the same one `manual_match.gateway_pool_for_date` makes: the
drill-down reads ONE date's already-normalised Parquet, not the month. That is
the "precomputed rows only" side of the rule rather than the raw-file side, and
it is bounded because a drill-down is always about one store on one day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import func

from . import mop_matrix as mm
from . import store_sheet as ss
from .db import DateTotalAgg, DigitalModeAgg, ManualMatch, MopDayAgg, R1BillLine, StoreDayAgg

logger = logging.getLogger("mop_reco")

WAREHOUSE = Path(__file__).resolve().parents[2] / "warehouse"

POS_COLUMNS = ["business_date", "store", "bill_no", "mop", "amount"]


def _read(source: str, columns=None, dates=None) -> pd.DataFrame:
    folder = WAREHOUSE / source
    if not folder.exists():
        return pd.DataFrame()
    files = [p for p in sorted(folder.glob("*.parquet"))
             if not p.stem.startswith("_")
             and (dates is None or p.stem in dates)]
    if not files:
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(p, columns=columns) for p in files],
                      ignore_index=True)
    if "business_date" in frame.columns:
        frame["business_date"] = frame["business_date"].astype(str)
    return frame


@dataclass
class SheetBundle:
    sheet: pd.DataFrame = None
    summary: pd.DataFrame = None            # block 1 — by date, all stores
    summary_by_store: pd.DataFrame = None   # block 2 — by store, whole range
    grand_total: pd.DataFrame = None        # block 3 — one row
    excluded: pd.DataFrame = None           # TYPE = ZPO only, kept visible
    contribution: pd.DataFrame = None       # how J was built, by TYPE and column
    other_types: pd.DataFrame = None        # ADJ.CR./ADJ.DB./ZPO — the export's own tab
    checks: ss.SheetChecks = None
    cms_type_totals: pd.DataFrame = None
    unresolved_pickups: pd.DataFrame = None
    data_quality: pd.DataFrame = None        # Defect B candidates — credit > deposit
    notes: list = field(default_factory=list)


def ptc_frame(session=None):
    """Every PTC row, freshly ingested — never the warehouse alone.

    Found 2026-09-24: `_read("ptc", dates=dates)` reads ONLY whatever is
    already sitting in `warehouse/ptc/`, and nothing in `run_reconciliation()`
    ever calls `ingest_ptc()` — R1 doesn't need PTC at all, only R3a does, via
    this module. The warehouse there was last written from the OLD
    July-only PTC sample; after the 2026-09-24 reload replaced it with one
    consolidated file spanning April-August, a carry-forward walk that had
    to reach back past July (e.g. building July alone, which must start
    from April 1's opening balance) silently found ZERO PTC rows for
    April-June — no deposits, no expense/income — so 92 days of real cash
    sale accumulated with NOTHING ever offsetting it. Measured: July 1's
    opening balance came out at Rs 205.46 Cr instead of a real figure in
    the tens of millions. `cms_frame()` already avoided this by calling
    `ingest_axis_cms()` fresh every time (CMS is a small file, cheap to
    re-read); this gives PTC the identical treatment — its own file is a
    32 MB CSV, a few seconds to parse, not the 250+ MB POS workbook that
    CLAUDE.md section 6.14 exists to keep off a page."""
    try:
        from .config import load_config
        from .ingest import ingest_ptc
        everything, _report = ingest_ptc(load_config(), session)
        return everything
    except Exception as exc:  # noqa: BLE001 — fall back, but say why
        logger.warning(
            f"store sheet: could not read the PTC source file ({exc}); falling "
            "back to the warehouse, which may be stale for any date the "
            "current run did not itself just ingest")
        return _read("ptc")


def cms_frame(session=None, with_notes: bool = False):
    """Every CMS row, INCLUDING the ones with no deposit date.

    The warehouse is partitioned by DEPDATE, so the 106 ZPO rows — which carry
    no DEPDATE at all — never reach it. Reading only the warehouse made ZPO
    invisible on the page, which is exactly the failure the operator warned
    about: a type sitting outside the sheet with nobody able to see its size.

    So this reads the warehouse for the dated rows and the CMS source file for
    the rest. That file is small — 4,593 rows in a 68 KB zip, about 1.5s — so
    it is a bounded read, not the 222 MB POS workbook that §6.14 exists to keep
    off a page. It is named here rather than done silently.

    `with_notes=True` also returns the ingest report's notes (e.g. the
    pickup-code-needs-a-location pre-flight warning, §6.11) — the drill-down
    caller doesn't need them, so it stays the plain single-DataFrame return by
    default and only `build()` opts in.
    """
    dated = _read("axis_cms")
    try:
        from .config import load_config
        from .ingest import ingest_axis_cms
        everything, report = ingest_axis_cms(load_config(), session)
        return (everything, report.notes) if with_notes else everything
    except Exception as exc:  # noqa: BLE001 — fall back, but say why
        logger.warning(
            f"store sheet: could not read the CMS source file ({exc}); falling "
            "back to the warehouse, which carries no undated rows, so ZPO will "
            "not be visible")
        return (dated, []) if with_notes else dated


def data_quality_rows(sheet: pd.DataFrame, cms: pd.DataFrame) -> pd.DataFrame:
    """Store-dates where the CMS credit exceeds the PTC deposit that day —
    Defect B candidates (a credit posted against the wrong date, or a
    pickup code shared with another store), never a real receivable.

    Columns: store, business_date, I, J, J_minus_I, pickup_code, slip_no.
    `I` is column I as stored — already a positive deposit magnitude since
    2026-09-17 — and `J_minus_I` is `J - I`, the plain rupees the credit
    exceeds the deposit by (K = I - J is negative for these rows; J_minus_I
    is its magnitude, `-K`, kept positive for a reader who should never have
    to know the sign convention behind it).
    `pickup_code` / `slip_no` are every distinct value from the CMS rows
    (TYPE in COL/ADJ.CR./ADJ.DB.) whose DEPDATE is that store-date — joined
    with a comma when more than one slip fed the same day.
    """
    bad = ss.credit_exceeds_deposit_rows(sheet)
    columns = ["store", "business_date", "I", "J", "J_minus_I",
              "pickup_code", "slip_no"]
    if bad.empty:
        return pd.DataFrame(columns=columns)

    feeding_types = tuple(ss.CREDIT_TYPE_SOURCE)
    feeding = pd.DataFrame(columns=["store", "DEPDATE_s", "PICKUPPOINTCODE", "SLIPNO"])
    if cms is not None and not cms.empty:
        feeding = cms[cms["TYPE"].astype(str).str.strip().isin(feeding_types)].copy()
        feeding["DEPDATE_s"] = pd.to_datetime(
            feeding["DEPDATE"], errors="coerce").dt.strftime("%Y-%m-%d")

    def codes_and_slips(store, day):
        if feeding.empty:
            return "", ""
        rows = feeding[(feeding["store"] == store) & (feeding["DEPDATE_s"] == day)]
        codes = sorted(set(rows["PICKUPPOINTCODE"].dropna().astype(str)))
        slips = sorted(set(rows["SLIPNO"].dropna().astype(str))) if "SLIPNO" in rows.columns else []
        return ", ".join(codes), ", ".join(slips)

    records = []
    for row in bad.itertuples():
        codes, slips = codes_and_slips(row.store, row.date)
        records.append({
            "store": row.store, "business_date": row.date,
            "I": float(row.cash_deposit), "J": float(row.cash_received),
            "J_minus_I": -float(row.cash_receivable),
            "pickup_code": codes, "slip_no": slips,
        })
    return pd.DataFrame(records, columns=columns).sort_values(
        "J_minus_I", ascending=False).reset_index(drop=True)


def build(session, run_id, date_from: date, date_to: date,
          cms: pd.DataFrame = None) -> SheetBundle:
    """The sheet, its summary, the excluded types and every control.

    **The carry-forward always starts where the opening balance is KNOWN**, not
    at `date_from`. The balance file is as at 1 July, so asking for 20 July
    alone must still walk the cash book from 1 July and then show only the
    20 July rows — otherwise H is seeded with a three-week-old balance and
    every figure below it is wrong. Measured: building 20 July directly gave
    **71 store-days with a negative closing**, which is impossible; carrying
    forward properly gives none.
    """
    bundle = SheetBundle()

    from .ingest import ingest_opening_cash  # local: avoids a cycle
    from .config import load_config
    opening, opening_report = ingest_opening_cash(load_config(), session)
    bundle.notes.extend(opening_report.notes)

    if not opening.empty and "as_of" in opening.columns:
        as_of = date.fromisoformat(str(opening["as_of"].iloc[0]))
    else:
        as_of = date_from
    carry_from = min(as_of, date_from)
    if carry_from < date_from:
        bundle.notes.append(
            f"Opening balances are known as at {as_of}, so the cash book is "
            f"walked from {carry_from} and only {date_from} to {date_to} is "
            "shown. Seeding H at the start of the range instead would carry a "
            "stale balance into every row below it."
        )

    dates = {d.isoformat() for d in pd.date_range(carry_from, date_to).date}

    bills = pd.DataFrame([{
        "business_date": r.business_date, "store": r.store,
        "bill_no": r.bill_no, "amount": float(r.amount or 0),
        "matched": bool(r.matched),
    } for r in session.query(
        R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no,
        R1BillLine.amount, R1BillLine.matched).filter(
        R1BillLine.run_id == run_id,
        R1BillLine.business_date >= carry_from.isoformat(),
        R1BillLine.business_date <= date_to.isoformat()).all()])

    # A manual match cascades here too (2026-09-13) — digital receivable (G)
    # must fall the moment a bill is matched manually, not only on a re-run.
    # `bill_no` is globally unique (§5), so this is a lookup against the
    # small set of currently-active manual matches, never a per-row
    # manual_match.apply_overlay() pass over the whole month's bill lines
    # (bounded by manual matches, not by bill_lines — §6.14).
    if not bills.empty:
        manual_bill_nos = {b for (b,) in session.query(ManualMatch.bill_no)
                           .filter(ManualMatch.lifecycle == "active").all() if b}
        if manual_bill_nos:
            bills["matched"] = bills["matched"] | bills["bill_no"].isin(manual_bill_nos)
        bills = bills.drop(columns=["bill_no"])

    pos = _read("pos_bill_wise", POS_COLUMNS, dates)
    ptc = ptc_frame(session)
    if not ptc.empty and "business_date" in ptc.columns:
        ptc = ptc[ptc["business_date"].astype(str).isin(dates)]
    if cms is None:
        cms, cms_notes = cms_frame(session, with_notes=True)
        bundle.notes.extend(cms_notes)

    full = ss.build_sheet(
        opening,
        ss.digital_by_store_date(bills),
        ss.cash_sale_by_store_date(pos),
        ss.ptc_by_store_date(ptc),
        ss.cms_received_by_deposit_date(cms),
        carry_from, date_to)

    # Show only what was asked for. The telescoping sum means the estate
    # identity still holds on a slice: H at the first shown date is a real
    # closing balance, not a seed.
    bundle.sheet = (full[(full["date"] >= date_from.isoformat())
                         & (full["date"] <= date_to.isoformat())]
                    .reset_index(drop=True))
    bundle.summary = ss.summarise_by_date(bundle.sheet)
    bundle.summary_by_store = ss.summarise_by_store(bundle.sheet)
    bundle.grand_total = ss.grand_total_row(bundle.sheet)
    bundle.excluded = ss.excluded_credit_types(cms)
    bundle.contribution = ss.cms_contribution_by_type(cms)
    bundle.other_types = ss.other_type_summary(cms)
    bundle.data_quality = data_quality_rows(bundle.sheet, cms)

    # Control (f) compares column D against the digital sale the dashboard
    # reports FOR THE SAME RANGE. Comparing against the carry span instead
    # made every partial range fail a control that was actually fine.
    if bills.empty:
        shown_digital = 0.0
    else:
        in_range = bills[(bills["business_date"] >= date_from.isoformat())
                         & (bills["business_date"] <= date_to.isoformat())]
        shown_digital = float(in_range["amount"].sum())

    bundle.checks = ss.run_controls(
        bundle.sheet, bundle.summary, opening,
        dashboard_digital=shown_digital)

    if not cms.empty:
        frame = cms.copy()
        frame["value"] = pd.to_numeric(frame["SLIPAMOUNT"],
                                       errors="coerce").fillna(0.0)
        bundle.cms_type_totals = (frame.groupby("TYPE", as_index=False)
                                  .agg(rows=("value", "size"),
                                       value=("value", "sum"))
                                  .sort_values("value", ascending=False)
                                  .reset_index(drop=True))
        unresolved = frame[frame["store"].isna()
                           | frame["store"].astype(str).eq("None")]
        if not unresolved.empty:
            bundle.unresolved_pickups = (
                unresolved.groupby(["PICKUPPOINTCODE", "TYPE"], as_index=False)
                .agg(rows=("value", "size"), value=("value", "sum")))
    return bundle


# ---------------------------------------------------------------------------
# The drill-down: the source records behind one store-day
# ---------------------------------------------------------------------------

@dataclass
class SourceRecords:
    digital_bills: pd.DataFrame = None
    cash_bills: pd.DataFrame = None
    ptc_rows: pd.DataFrame = None
    cms_credits: pd.DataFrame = None


def source_records(session, run_id, business_date: str, store: str,
                   cms: pd.DataFrame = None) -> SourceRecords:
    """Every record behind one store-day, column by column.

    Bounded on purpose: one date's Parquet, one store. This is what makes
    "every figure traceable" true rather than a claim.
    """
    records = SourceRecords()
    business_date = str(business_date)

    rows = session.query(R1BillLine).filter(
        R1BillLine.run_id == run_id,
        R1BillLine.business_date == business_date,
        R1BillLine.store == store).all()
    records.digital_bills = pd.DataFrame([{
        "bill_no": r.bill_no, "mop": r.mop, "amount": float(r.amount or 0),
        "matched": bool(r.matched), "status": r.status,
        "gateway": r.matched_gateway, "matched_txn": r.matched_txn_ref,
    } for r in rows])

    pos = _read("pos_bill_wise", POS_COLUMNS, {business_date})
    if not pos.empty:
        cash = pos[pos["store"].eq(store)
                   & pos["mop"].astype(str).str.strip().eq(ss.CASH_MOP)]
        has_bill = cash["bill_no"].notna() & (
            ~cash["bill_no"].astype(str).str.strip().isin(["", "nan", "None"]))
        records.cash_bills = (cash[has_bill][["bill_no", "mop", "amount"]]
                              .sort_values("amount", ascending=False)
                              .reset_index(drop=True))

    ptc = _read("ptc", dates={business_date})
    if not ptc.empty:
        mine = ptc[ptc["store"].eq(store)].copy()
        records.ptc_rows = (mine[["voucher_no", "mode", "head", "ledger",
                                  "amount", "is_deposit", "remarks"]]
                            .sort_values("amount").reset_index(drop=True))

    if cms is None:
        cms = cms_frame(session)
    if not cms.empty:
        # All three types that feed J (COL, ADJ.CR., ADJ.DB.) — not COL alone
        # — since each can put a row on this store-day. The actual column that
        # contributed is carried through so the drill-down never implies
        # SLIPAMOUNT fed an ADJ.CR./ADJ.DB. row when it did not.
        feeding = cms[cms["TYPE"].astype(str).str.strip().isin(ss.CREDIT_TYPE_SOURCE)
                     & cms["store"].eq(store)].copy()
        if not feeding.empty:
            deposit_date = pd.to_datetime(feeding["DEPDATE"], errors="coerce")
            feeding = feeding[deposit_date.dt.strftime("%Y-%m-%d") == business_date]
        if not feeding.empty:
            def _contribution(row):
                column, sign = ss.CREDIT_TYPE_SOURCE[row["TYPE"]]
                value = float(row.get(column) or 0.0)
                return sign * value

            feeding["contributing_column"] = feeding["TYPE"].map(
                lambda t: ss.CREDIT_TYPE_SOURCE[t][0])
            feeding["contribution_to_J"] = feeding.apply(_contribution, axis=1)
            keep = ["TYPE", "SLIPNO", "PICKUPPOINTCODE", "DEPDATE", "CRDATE",
                   "contributing_column", "contribution_to_J"]
            records.cms_credits = (feeding[[c for c in keep if c in feeding.columns]]
                                   .reset_index(drop=True))
    return records


# ---------------------------------------------------------------------------
# Precomputed aggregates (2026-09-20) — written once at RUN time, read by an
# indexed query at VIEW time, never rebuilt from PTC/CMS/opening-balance/POS
# on a page load. This is what makes a full financial year viewable: the
# expensive part of `build()` above (reading a raw CMS zip, walking every
# per-date Parquet file across the whole range, then a per-store cumulative
# carry-forward) now runs ONCE per run, not once per page render.
#
# F (digital_received) and G (digital_receivable) are deliberately NOT
# stored here — CLAUDE.md 6.9b-ii item 1 requires a manual match to cascade
# into every figure immediately, with no re-run, and F is the one column a
# manual match actually moves. Storing it would go stale the moment someone
# recorded a match. `load_fast()` below reads everything else from these
# tables and joins F on live, from the same small, indexed, already-fast
# read (`r1_bill_lines` + active `manual_matches`) the rest of this app
# already uses for the identical cascade (§6.9b-ii).
# ---------------------------------------------------------------------------

AGG_COLUMNS = ("total_sale", "digital_sale", "cash_sale", "opening_balance",
              "cash_deposit", "cash_received", "cash_receivable",
              "cash_expense_income", "closing_balance", "float_amount")


def persist_aggregates(session, run_id: int, date_from: date, date_to: date,
                       cms: pd.DataFrame = None) -> dict:
    """Run `build()` once, for the run's own full range, and write its
    store-day and date-total rows into `store_day_agg` / `date_total_agg`.

    Called from `runs.run_reconciliation()` as its own stage, right after R1
    is persisted — this is the "Run stage writes persisted aggregate
    tables" half of the performance rework; `load_fast()` below is the
    "Summary page reads them" half. Returns a small report (row counts,
    elapsed) for the run's own timings.
    """
    import time
    t0 = time.perf_counter()

    bundle = build(session, run_id, date_from, date_to, cms=cms)
    sheet = bundle.sheet

    # Replace, never append: a re-run of the same run_id (there isn't one
    # today — a run gets a fresh id — but a backfill script might replay
    # one) must not leave stale duplicate rows behind.
    session.query(StoreDayAgg).filter(StoreDayAgg.run_id == run_id).delete()
    session.query(DateTotalAgg).filter(DateTotalAgg.run_id == run_id).delete()

    if sheet.empty:
        session.commit()
        return {"store_days": 0, "dates": 0,
               "elapsed": round(time.perf_counter() - t0, 2),
               "static": static_bundle_json(bundle)}

    defect_dates_stores = set(
        ss.credit_exceeds_deposit_rows(sheet)[["date", "store"]]
        .itertuples(index=False, name=None))

    records = []
    for row in sheet.itertuples(index=False):
        records.append({
            "run_id": run_id, "business_date": row.date, "store": row.store,
            "total_sale": float(row.total_sale), "digital_sale": float(row.digital_sale),
            "cash_sale": float(row.cash_sale), "opening_balance": float(row.opening_balance),
            "cash_deposit": float(row.cash_deposit), "cash_received": float(row.cash_received),
            "cash_receivable": float(row.cash_receivable),
            "cash_expense_income": float(row.cash_expense_income),
            "closing_balance": float(row.closing_balance),
            "float_amount": float(row.float_amount),
            "defect_b": (row.date, row.store) in defect_dates_stores,
        })
        if len(records) >= 5000:
            session.bulk_insert_mappings(StoreDayAgg, records)
            records = []
    if records:
        session.bulk_insert_mappings(StoreDayAgg, records)

    by_date = bundle.summary[bundle.summary["date"] != "TOTAL"]
    dt_records = [{
        "run_id": run_id, "business_date": row.date,
        "total_sale": float(row.total_sale), "digital_sale": float(row.digital_sale),
        "cash_sale": float(row.cash_sale), "opening_balance": float(row.opening_balance),
        "cash_deposit": float(row.cash_deposit), "cash_received": float(row.cash_received),
        "cash_receivable": float(row.cash_receivable),
        "cash_expense_income": float(row.cash_expense_income),
        "closing_balance": float(row.closing_balance),
        "float_amount": float(row.float_amount),
    } for row in by_date.itertuples(index=False)]
    if dt_records:
        session.bulk_insert_mappings(DateTotalAgg, dt_records)

    session.commit()
    elapsed = round(time.perf_counter() - t0, 2)
    logger.info(f"run {run_id}: persisted {len(sheet)} store_day_agg rows, "
               f"{len(dt_records)} date_total_agg rows in {elapsed}s")
    return {
        "store_days": len(sheet), "dates": len(dt_records), "elapsed": elapsed,
        "static": static_bundle_json(bundle),
    }


def persist_mop_day_agg(session, run_id: int, date_from: date, date_to: date) -> dict:
    """Run's own MOP-bucket sale aggregate (2026-09-18, the Summary
    particulars matrix) — one row per store per business date per bucket
    (card/upi/cash/gift_voucher/credit_note_issued/credit_note_received).

    Reads `pos_bill_wise` ONCE for the run's own range (the same warehouse
    Parquet `build()` already reads for column E — never the raw `.xlsb`,
    §6.14) and buckets every row via `mop_matrix.bucket_sales_by_store_date`,
    which raises rather than drops a MOP config.yaml has not been given a
    ruled bucket for. Called from `runs.run_reconciliation()` as its own
    stage, alongside `persist_aggregates()` — a MOP's own sale total never
    changes with a manual match, so unlike `store_day_agg`'s F column this
    table has nothing that needs recomputing live; only "Collected" is read
    live, from `r1_bill_lines`, at page-load time (see
    `mop_particulars_matrix()` below).
    """
    import time

    from .config import load_config

    t0 = time.perf_counter()
    session.query(MopDayAgg).filter(MopDayAgg.run_id == run_id).delete()

    dates = {d.isoformat() for d in pd.date_range(date_from, date_to).date}
    pos = _read("pos_bill_wise", POS_COLUMNS, dates)
    if pos.empty:
        session.commit()
        return {"rows": 0, "elapsed": round(time.perf_counter() - t0, 2)}

    mop_buckets = mm.load_mop_buckets(load_config())
    bucketed = mm.bucket_sales_by_store_date(pos, mop_buckets)

    records = [{
        "run_id": run_id, "business_date": r.business_date, "store": r.store,
        "bucket": r.bucket, "sale": float(r.sale),
    } for r in bucketed.itertuples(index=False)]
    for i in range(0, len(records), 5000):
        session.bulk_insert_mappings(MopDayAgg, records[i:i + 5000])
    session.commit()

    elapsed = round(time.perf_counter() - t0, 2)
    logger.info(f"run {run_id}: persisted {len(records)} mop_day_agg rows in {elapsed}s")
    return {"rows": len(records), "elapsed": elapsed}


def _mop_bucket_sales(session, run_id: int, date_from: date, date_to: date,
                      store: str = None) -> dict:
    """{bucket: sale} summed over the filtered range/store, from the
    precomputed `mop_day_agg` — an indexed SELECT, never a pandas re-read of
    `pos_bill_wise`. Falls back to a live read for a run from before this
    table existed (mirroring `load_fast()`'s own legacy-run fallback)."""
    query = session.query(MopDayAgg.bucket, func.sum(MopDayAgg.sale)).filter(
        MopDayAgg.run_id == run_id,
        MopDayAgg.business_date >= date_from.isoformat(),
        MopDayAgg.business_date <= date_to.isoformat(),
    )
    if store:
        query = query.filter(MopDayAgg.store == store)
    rows = query.group_by(MopDayAgg.bucket).all()
    sums = {bucket: float(total or 0.0) for bucket, total in rows}

    if not sums and session.query(MopDayAgg.id).filter(
            MopDayAgg.run_id == run_id).first() is None:
        # A run from before mop_day_agg existed — read the warehouse live,
        # once, rather than show an empty matrix for an otherwise-good run.
        from .config import load_config
        dates = {d.isoformat() for d in pd.date_range(date_from, date_to).date}
        pos = _read("pos_bill_wise", POS_COLUMNS, dates)
        if store and not pos.empty:
            pos = pos[pos["store"] == store]
        if not pos.empty:
            mop_buckets = mm.load_mop_buckets(load_config())
            bucketed = mm.bucket_sales_by_store_date(pos, mop_buckets)
            sums = bucketed.groupby("bucket")["sale"].sum().to_dict()

    return {b: sums.get(b, 0.0) for b in mm.BUCKETS}


def persist_digital_mode_agg(session, run_id: int, bill_lines: pd.DataFrame) -> dict:
    """Sale by digital payment MODE — card/upi/unclassified — one row per
    store per business date (2026-09-19, the particulars matrix's Card/UPI
    correctness fix; see docs/MOP_PAYMENT_MODE.md for the data behind it).

    The first version of the matrix split Card vs UPI from the POS MOP
    name via `config.yaml`'s bucket field, which put 74% of digital sale
    under "Card" — not credible for Indian retail, and contradicted by the
    unmapped-transaction pool itself (479 UPI vs 30 card). Root cause:
    "PAYTM EDC" is one shared terminal that accepts BOTH card and UPI (Axis
    TIDs are mapped onto Paytm machines for card acceptance) — the MOP
    cannot say which one a given bill used. Only the matched TRANSACTION's
    own identity can (`mop_matrix.classify_digital_mode()`).

    Takes the run's own `bill_lines` DataFrame DIRECTLY — the same frame
    about to be written to `r1_bill_lines` — rather than re-reading
    anything: payment_mode only exists after R1 matching, so this cannot
    be rebuilt from raw POS data the way `persist_mop_day_agg()` is.

    A bill line the engine did NOT match has no known payment mode and is
    counted as "unclassified", never guessed — its Collected is 0 by
    construction, so it carries the WHOLE of that bill's amount as
    Receivable. A later manual match moves it out of "unclassified" live,
    at page-load time, with no re-run — see `_digital_mode_sales()`'s own
    cascade (CLAUDE.md 6.9b-ii item 1, the same guarantee column F/
    Collected already has).

    An engine-MATCHED line whose gateway/payment_mode/MOP combination
    `classify_digital_mode()` does not recognise ("other") is ALSO folded
    into "unclassified" — never guessed into Card or UPI — and counted
    separately in the returned report so a material amount of it is
    visible rather than silently absorbed (rule 5).
    """
    session.query(DigitalModeAgg).filter(DigitalModeAgg.run_id == run_id).delete()
    if bill_lines.empty:
        session.commit()
        return {"rows": 0, "other_matched_count": 0, "other_matched_value": 0.0}

    modes = []
    other_count = 0
    other_value = 0.0
    for row in bill_lines.itertuples(index=False):
        if bool(row.matched):
            mode = mm.classify_digital_mode(
                getattr(row, "matched_gateway", None),
                getattr(row, "matched_payment_mode", None),
                row.mop,
            )
            if mode == "other":
                other_count += 1
                other_value += float(row.amount)
                mode = "unclassified"
        else:
            mode = "unclassified"
        modes.append(mode)

    df = bill_lines[["business_date", "store", "amount"]].copy()
    df["mode"] = modes
    grouped = df.groupby(["business_date", "store", "mode"], as_index=False)["amount"].sum()

    records = [{
        "run_id": run_id, "business_date": str(r.business_date), "store": str(r.store),
        "mode": r.mode, "sale": float(r.amount),
    } for r in grouped.itertuples(index=False)]
    for i in range(0, len(records), 5000):
        session.bulk_insert_mappings(DigitalModeAgg, records[i:i + 5000])
    session.commit()

    if other_count:
        logger.warning(
            f"run {run_id}: {other_count} matched digital bill line(s) worth "
            f"{other_value:,.2f} carried a gateway/payment_mode combination "
            "this app does not recognise and were folded into Unclassified, "
            "never guessed into Card or UPI — see docs/MOP_PAYMENT_MODE.md"
        )
    return {"rows": len(records), "other_matched_count": other_count,
           "other_matched_value": round(other_value, 2)}


def _digital_mode_sales(session, run_id: int, date_from: date, date_to: date,
                        store: str = None) -> dict:
    """{"card", "upi", "card_collected", "upi_collected", "provisional_upi",
    "provisional_bills"} — from the precomputed `digital_mode_agg`,
    adjusted LIVE for any active manual match that has confirmed a real
    transaction for a bill the engine still shows unmatched.

    **Open bills default to UPI, provisionally (2026-09-21, the operator's
    own explicit business rule — see `mop_matrix.PROVISIONAL_DIGITAL_MODE`,
    the one place that constant is read).** A bill with no matched
    transaction has no known payment_mode; until it is matched, its amount
    counts as UPI *Sale* — but never as UPI *Collected*, since nothing was
    actually confirmed collected. That is why `upi` (Sale) and
    `upi_collected` are returned separately here, unlike `card`, which has
    no provisional population at all (Card only ever holds a bill the
    matched transaction ITSELF said was card) and so is always fully
    collected. The gap between `upi` and `upi_collected` — `provisional_upi`
    — is exactly UPI's own Receivable, and is the figure/count the page's
    footnote shows so the assumption is never silently absorbed into a
    real "Collected" figure. `card + upi = Total digital`, always — there
    is no third bucket.

    A manual match makes the payment mode KNOWN the moment it is recorded
    — the same cascade-with-no-re-run guarantee column F/Collected already
    has (CLAUDE.md 6.9b-ii item 1) — so it moves that bill's amount out of
    the provisional pool into whichever of Card/UPI Collected the
    transaction's own `payment_mode` actually says, live, here too.
    """
    query = session.query(DigitalModeAgg.mode, func.sum(DigitalModeAgg.sale)).filter(
        DigitalModeAgg.run_id == run_id,
        DigitalModeAgg.business_date >= date_from.isoformat(),
        DigitalModeAgg.business_date <= date_to.isoformat(),
    )
    if store:
        query = query.filter(DigitalModeAgg.store == store)
    sums = {mode: float(total or 0.0) for mode, total in
           query.group_by(DigitalModeAgg.mode).all()}
    for m in ("card", "upi", "unclassified"):
        sums.setdefault(m, 0.0)

    if session.query(DigitalModeAgg.id).filter(
            DigitalModeAgg.run_id == run_id).first() is None:
        # A run from before this fix existed — classify live from
        # r1_bill_lines directly, once, rather than show an empty Card/UPI
        # split for an otherwise-good run. `payment_mode` is only populated
        # on a bill line for a run since 2026-09-19 (db.py's lightweight
        # ALTER TABLE), so a genuinely old run reads NULL for every Paytm
        # row here and correctly folds them into "unclassified" rather than
        # guessing — only Axis (gateway alone) and Bajaj (its own MOP) can
        # still be classified for a run this old.
        rows = session.query(
            R1BillLine.matched, R1BillLine.matched_gateway,
            R1BillLine.payment_mode, R1BillLine.mop, R1BillLine.amount,
        ).filter(
            R1BillLine.run_id == run_id,
            R1BillLine.business_date >= date_from.isoformat(),
            R1BillLine.business_date <= date_to.isoformat(),
        )
        if store:
            rows = rows.filter(R1BillLine.store == store)
        sums = {"card": 0.0, "upi": 0.0, "unclassified": 0.0}
        for matched, gateway, payment_mode, mop, amount in rows.all():
            mode = mm.classify_digital_mode(gateway, payment_mode, mop) if matched else "unclassified"
            if mode == "other":
                mode = "unclassified"
            sums[mode] += float(amount or 0.0)

    # engine-matched totals — always fully collected, never provisional.
    card_collected = sums["card"]
    upi_collected = sums["upi"]
    open_pool = sums["unclassified"]  # engine has no matched txn for these

    active = session.query(ManualMatch.bill_no, ManualMatch.gateway, ManualMatch.mop,
                          ManualMatch.amount, ManualMatch.validation_detail).filter(
        ManualMatch.lifecycle == "active",
        ManualMatch.business_date >= date_from.isoformat(),
        ManualMatch.business_date <= date_to.isoformat(),
    )
    if store:
        active = active.filter(ManualMatch.store == store)
    active_rows = active.all()
    reclassified_bill_nos = set()
    if active_rows:
        bill_nos = [b for (b, *_rest) in active_rows if b]
        still_unmatched = set()
        if bill_nos:
            still_unmatched = {b for (b,) in session.query(R1BillLine.bill_no).filter(
                R1BillLine.run_id == run_id, R1BillLine.matched == False,  # noqa: E712
                R1BillLine.bill_no.in_(bill_nos)).all()}
        for bill_no, gateway, mop, amount, detail in active_rows:
            if bill_no not in still_unmatched:
                continue
            payment_mode = None
            if detail:
                try:
                    payment_mode = (json.loads(detail).get("txn") or {}).get("payment_mode")
                except (ValueError, TypeError):
                    payment_mode = None
            mode = mm.classify_digital_mode(gateway, payment_mode, mop)
            if mode in ("card", "upi"):
                amt = float(amount or 0.0)
                open_pool -= amt
                reclassified_bill_nos.add(bill_no)
                if mode == "card":
                    card_collected += amt
                else:
                    upi_collected += amt

    # The provisional default (mop_matrix.PROVISIONAL_DIGITAL_MODE) — every
    # rupee still genuinely open (no engine match, no manual match either)
    # counts as UPI Sale but not UPI Collected.
    provisional_upi = max(0.0, open_pool)

    provisional_bills = 0
    if provisional_upi > 0 or reclassified_bill_nos:
        open_query = session.query(func.count(R1BillLine.id)).filter(
            R1BillLine.run_id == run_id, R1BillLine.matched == False,  # noqa: E712
            R1BillLine.business_date >= date_from.isoformat(),
            R1BillLine.business_date <= date_to.isoformat(),
        )
        if store:
            open_query = open_query.filter(R1BillLine.store == store)
        total_open = int(open_query.scalar() or 0)
        provisional_bills = max(0, total_open - len(reclassified_bill_nos))

    return {
        "card": card_collected,
        "card_collected": card_collected,
        "upi": upi_collected + provisional_upi,
        "upi_collected": upi_collected,
        "provisional_upi": provisional_upi,
        "provisional_bills": provisional_bills,
    }


@dataclass
class MatrixColumn:
    sale: float = 0.0
    collected: float = 0.0
    receivable: float = 0.0
    pct: float = None   # None = blank cell (never shown for Total sale)


@dataclass
class ParticularsMatrix:
    total_sale: MatrixColumn = field(default_factory=MatrixColumn)
    upi: MatrixColumn = field(default_factory=MatrixColumn)
    card: MatrixColumn = field(default_factory=MatrixColumn)
    total_digital: MatrixColumn = field(default_factory=MatrixColumn)
    total_cash: MatrixColumn = field(default_factory=MatrixColumn)
    gift_voucher_redeemed: float = 0.0
    credit_note_issued: float = 0.0
    credit_note_received: float = 0.0
    # The provisional-UPI footnote (2026-09-21) — how much of UPI's own
    # Sale, and how many bill lines, are the PROVISIONAL_DIGITAL_MODE
    # default rather than a confirmed transaction. Zero/0 once every open
    # bill has been matched.
    provisional_upi_amount: float = 0.0
    provisional_upi_bills: int = 0
    controls: list = field(default_factory=list)  # (label, holds, detail)


def mop_particulars_matrix(session, run_id: int, date_from: date, date_to: date,
                           store: str = None) -> ParticularsMatrix:
    """The Summary particulars matrix (2026-09-18), replacing the seven-tile
    ribbon — Sale/%/Collected/Receivable for Total sale, UPI, Card, Total
    digital and Total cash, plus Gift Voucher Redeemed / Credit Note Issued
    / Credit Note Received as their own Total-sale-only lines.

    **"Collected" for Gift Voucher is treated as self-collected — its own
    Receivable is asserted at exactly 0** (the operator's own explicit
    instruction: "Receivable for it is ZERO and must never be anything
    else"), so it contributes nothing net to Total sale's own Receivable.
    Credit Note Issued/Received carry no such instruction and no natural
    "collection" event of their own (config.yaml already calls both
    `non_settling` — they are bill adjustments, not new money) — a
    judgment call, stated here rather than silently made: they are added to
    Total sale's own Sale total but never to Collected, so they DO flow
    into Total sale's own Receivable line (July: their net is a small
    -Rs 4,771, not folded away). If this is wrong, it is wrong in the open,
    not hidden inside an assumption.

    Cash's own "Collected" is defined as its own Sale (the operator's own
    words: "for cash, banked or held at store" — every cash sale is fully
    in hand, either way, the moment it happens), so Cash's own Receivable
    is always exactly 0 — a different concept from the (now-removed) tile
    ribbon's old "Cash receivable" tile, which measured CMS deposit-vs-
    bank-credit lag, not R1 collection.

    **Card vs UPI (2026-09-19 correctness fix, docs/MOP_PAYMENT_MODE.md):**
    read from `_digital_mode_sales()` — the matched TRANSACTION's own
    identity — never from the POS MOP name. **Open bills default to UPI,
    provisionally (2026-09-21) — see `_digital_mode_sales()`'s own
    docstring and `mop_matrix.PROVISIONAL_DIGITAL_MODE`.** There is no
    third "Unclassified" column any more: `card + upi = Total digital`,
    always. An open bill's amount counts as UPI Sale but not UPI Collected
    until it is matched, so UPI (and only UPI) can carry a non-zero
    Receivable — Card cannot, since nothing reaches Card without the
    matched transaction itself saying so.
    """
    sales = _mop_bucket_sales(session, run_id, date_from, date_to, store)

    cash_sale = sales["cash"]
    gv_sale = sales["gift_voucher"]
    cni_sale = sales["credit_note_issued"]
    cnr_sale = sales["credit_note_received"]

    modes = _digital_mode_sales(session, run_id, date_from, date_to, store)
    card_sale = modes["card"]
    card_collected = modes["card_collected"]
    upi_sale = modes["upi"]
    upi_collected = modes["upi_collected"]

    cash_collected = cash_sale
    gv_collected = gv_sale  # self-collected — Receivable forced to 0, on instruction

    total_digital_sale = card_sale + upi_sale
    total_digital_collected = card_collected + upi_collected
    total_cash_sale = cash_sale
    total_cash_collected = cash_collected

    total_sale_value = (total_digital_sale + total_cash_sale + gv_sale
                        + cnr_sale + cni_sale)
    # Credit notes flow straight into Total sale's own Receivable — see the
    # docstring's judgment-call note. Gift Voucher never does (self-collected).
    total_sale_collected = total_digital_collected + total_cash_collected + gv_collected

    def _col(sale, collected, pct_base=None):
        receivable = sale - collected
        pct = round(sale / pct_base * 100, 1) if pct_base else None
        return MatrixColumn(sale=sale, collected=collected, receivable=receivable, pct=pct)

    matrix = ParticularsMatrix(
        total_sale=_col(total_sale_value, total_sale_collected, pct_base=None),
        upi=_col(upi_sale, upi_collected, pct_base=total_sale_value),
        card=_col(card_sale, card_collected, pct_base=total_sale_value),
        total_digital=_col(total_digital_sale, total_digital_collected, pct_base=total_sale_value),
        total_cash=_col(total_cash_sale, total_cash_collected, pct_base=total_sale_value),
        gift_voucher_redeemed=gv_sale,
        credit_note_issued=cni_sale,
        credit_note_received=cnr_sale,
        provisional_upi_amount=modes["provisional_upi"],
        provisional_upi_bills=modes["provisional_bills"],
    )

    # --- controls, asserted and returned, never hidden ----------------
    identity_rhs = (matrix.total_digital.sale + matrix.total_cash.sale
                    + matrix.gift_voucher_redeemed + matrix.credit_note_received
                    + matrix.credit_note_issued)
    matrix.controls.append((
        "Total digital + Total cash + Gift Voucher Redeemed + Credit Note "
        "Received + Credit Note Issued = Total sale",
        abs(identity_rhs - matrix.total_sale.sale) <= ss.TOLERANCE,
        f"got {identity_rhs:,.2f}, Total sale is {matrix.total_sale.sale:,.2f}, "
        f"difference {identity_rhs - matrix.total_sale.sale:,.2f}",
    ))

    digital_split_rhs = matrix.upi.sale + matrix.card.sale
    matrix.controls.append((
        "UPI + Card = Total digital",
        abs(digital_split_rhs - matrix.total_digital.sale) <= ss.TOLERANCE,
        f"got {digital_split_rhs:,.2f}, Total digital is "
        f"{matrix.total_digital.sale:,.2f}, difference "
        f"{digital_split_rhs - matrix.total_digital.sale:,.2f}",
    ))

    for label, col in (("Total sale", matrix.total_sale), ("UPI", matrix.upi),
                      ("Card", matrix.card),
                      ("Total digital", matrix.total_digital),
                      ("Total cash", matrix.total_cash)):
        expected = col.sale - col.collected
        matrix.controls.append((
            f"{label}: Sale − Collected = Receivable",
            abs(expected - col.receivable) <= ss.TOLERANCE,
            f"Sale {col.sale:,.2f} − Collected {col.collected:,.2f} = "
            f"{expected:,.2f}, Receivable shows {col.receivable:,.2f}",
        ))

    gv_receivable = gv_sale - gv_collected
    matrix.controls.append((
        "Gift Voucher Redeemed receivable = 0",
        abs(gv_receivable) <= ss.TOLERANCE,
        f"got {gv_receivable:,.2f}",
    ))

    return matrix


def store_cash_summary(session, run_id: int, date_from: date, date_to: date,
                       store: str = None):
    """Opening balance | Cash sale | Cash deposit | Expense & income |
    Closing at store, for Part 2's card beside "Store-wise detail" —
    columns H, E, I, L, M read straight from the precomputed `store_day_agg`
    (never re-derived), so this can never disagree with the table below it.

    **Opening balance is the FIRST filtered date's own H, summed across
    stores — never summed across dates.** H is a stock, not a flow; summing
    it across many days for the same store would count the same carried-
    forward money repeatedly. Cash sale / Cash deposit / Expense & income
    ARE flows and are summed across every store-day in the filter. Closing
    at store is then computed from those four (H(first) + ΣE − ΣI + ΣL),
    which telescopes — via the same carry-forward chain `store_sheet.py`'s
    own H(d) = M(d−1) already relies on — to exactly the LAST filtered
    date's own M, summed across stores. That LAST-date M sum is what this
    function checks it against, returned as `check_closing` and
    `matches` — "the same filter" in the operator's own instruction means
    the last date in it, the same grain the old Closing-balance tile always
    used, not a sum of M across every date (which would double-count the
    same carried balance).

    **Cash received (J) / Cash receivable (K) are a SEPARATE equation about
    the deposit, never folded into the closing-balance line above** (2026-
    09-22, on instruction — Closing at store is H+E−I+L; mixing J or K into
    it would make that arithmetic wrong). Both are flows, summed across
    every store-day in the filter exactly like cash_sale/cash_deposit/
    expense_income above. `cash_receivable_net` is the SAME netting rule
    already agreed for the tile/table elsewhere on this page
    (`store_sheet.net_cash_receivable()`: sum I and sum J over the filter
    FIRST, subtract, floor at zero LAST — never a sum of per-row K, which
    would let one store's excess-credited day cancel another's genuine
    receivable day, or, floored per-row first, overstate the total by
    dropping every negative day) — reimplemented inline here rather than
    calling that pandas-frame helper, since this function reads via SQL
    aggregates, not a DataFrame; the formula is identical:
    `max(0, sum(I) − sum(J))`. `check_receivable` is sum(K) — the store-day
    aggregate's OWN precomputed I−J per row, summed WITHOUT flooring — an
    independent cross-check that `cash_receivable`'s stored value is still
    exactly `I − J` on every row (an algebraic identity that must hold
    regardless of sign or magnitude); `matches_receivable` compares that
    against `cash_deposit − cash_received` computed here, the same
    two-independently-sourced-figures pattern `matches` above already uses
    for Closing at store.
    """
    bounds = session.query(
        func.min(StoreDayAgg.business_date), func.max(StoreDayAgg.business_date),
    ).filter(
        StoreDayAgg.run_id == run_id,
        StoreDayAgg.business_date >= date_from.isoformat(),
        StoreDayAgg.business_date <= date_to.isoformat(),
    )
    if store:
        bounds = bounds.filter(StoreDayAgg.store == store)
    first_date, last_date = bounds.one()

    if not first_date:
        return {"opening_balance": 0.0, "cash_sale": 0.0, "cash_deposit": 0.0,
               "cash_expense_income": 0.0, "closing_at_store": 0.0,
               "check_closing": 0.0, "matches": True,
               "cash_received": 0.0, "cash_receivable_net": 0.0,
               "check_receivable": 0.0, "matches_receivable": True}

    def _scope(query):
        query = query.filter(
            StoreDayAgg.run_id == run_id,
            StoreDayAgg.business_date >= date_from.isoformat(),
            StoreDayAgg.business_date <= date_to.isoformat())
        if store:
            query = query.filter(StoreDayAgg.store == store)
        return query

    opening = _scope(session.query(func.sum(StoreDayAgg.opening_balance))
                     .filter(StoreDayAgg.business_date == first_date)).scalar() or 0.0
    (cash_sale, cash_deposit, expense_income, cash_received,
     check_receivable) = _scope(session.query(
        func.sum(StoreDayAgg.cash_sale), func.sum(StoreDayAgg.cash_deposit),
        func.sum(StoreDayAgg.cash_expense_income),
        func.sum(StoreDayAgg.cash_received),
        func.sum(StoreDayAgg.cash_receivable))).one()
    cash_sale = float(cash_sale or 0.0)
    cash_deposit = float(cash_deposit or 0.0)
    expense_income = float(expense_income or 0.0)
    cash_received = float(cash_received or 0.0)
    check_receivable = float(check_receivable or 0.0)
    opening = float(opening)

    closing_at_store = opening + cash_sale - cash_deposit + expense_income
    cash_receivable_net = max(0.0, cash_deposit - cash_received)

    check_closing = _scope(session.query(func.sum(StoreDayAgg.closing_balance))
                           .filter(StoreDayAgg.business_date == last_date)).scalar() or 0.0
    check_closing = float(check_closing)

    return {
        "opening_balance": opening, "cash_sale": cash_sale,
        "cash_deposit": cash_deposit, "cash_expense_income": expense_income,
        "closing_at_store": closing_at_store, "check_closing": check_closing,
        "matches": abs(closing_at_store - check_closing) <= ss.TOLERANCE,
        "cash_received": cash_received,
        "cash_receivable_net": cash_receivable_net,
        "check_receivable": check_receivable,
        "matches_receivable": abs(
            (cash_deposit - cash_received) - check_receivable) <= ss.TOLERANCE,
    }


def static_bundle_json(bundle: "SheetBundle") -> dict:
    """Everything in a `build()` bundle that is NOT store-day-shaped and NOT
    overlay-dependent — controls, notes, the CMS excluded/contribution/
    other-types breakdowns, unresolved pickups. None of these change
    between runs of the SAME run_id (a manual match cannot move a control
    result or a ZPO row count), so they are computed once here, at run
    time, and stored as plain JSON on `Run.notes` — `load_static()` below
    reads them back with no computation at all, not even a query.

    Every value is rounded/py-native so `json.dumps` never chokes on a
    numpy or Decimal type.
    """
    def records(df):
        if df is None or df.empty:
            return []
        return json.loads(df.to_json(orient="records"))

    return {
        "controls": [{"key": c.key, "what": c.what, "holds": bool(c.holds),
                     "detail": c.detail} for c in bundle.checks.controls] if bundle.checks else [],
        "notes": list(bundle.notes or []),
        "excluded": records(bundle.excluded),
        "contribution": records(bundle.contribution),
        "other_types": records(bundle.other_types),
        "cms_type_totals": records(bundle.cms_type_totals),
        "unresolved_pickups": records(bundle.unresolved_pickups),
    }


def load_static(run) -> dict:
    """The `static_bundle_json()` payload, read back from `Run.notes` — the
    Data notes expander and the Excel export's "Excluded types" tab use
    this instead of ever calling `build()` again for a run that has been
    through the new pipeline.

    Returns `None` when the run predates this (its `notes` JSON has no
    `"store_day_agg"` key at all) — callers fall back to the slow `build()`
    path in that one case, exactly like `load_fast()` does for the sheet.
    """
    if not run or not run.notes:
        return None
    try:
        payload = json.loads(run.notes)
    except (ValueError, TypeError):
        return None
    agg = payload.get("store_day_agg")
    if not agg or "static" not in agg:
        return None
    static = agg["static"]
    return {
        "controls": [(c["key"], c["what"], c["holds"], c["detail"])
                    for c in static.get("controls", [])],
        "notes": static.get("notes", []),
        "excluded": pd.DataFrame(static.get("excluded", [])),
        "contribution": pd.DataFrame(static.get("contribution", [])),
        "other_types": pd.DataFrame(static.get("other_types", [])),
        "cms_type_totals": pd.DataFrame(static.get("cms_type_totals", [])),
        "unresolved_pickups": pd.DataFrame(static.get("unresolved_pickups", [])),
    }


def _live_digital_received(session, run_id: int, date_from: date, date_to: date,
                           store: str = None) -> pd.DataFrame:
    """F only — columns date, store, digital_received.

    The one figure a manual match moves without a re-run. Deliberately the
    ONLY thing `load_fast()` computes live rather than reading from the
    precomputed aggregate — everything else on the sheet is fixed the
    moment a run completes.

    **Aggregated in SQL, not pandas — measured, not assumed.** An earlier
    version of this function pulled every matching `r1_bill_lines` row
    (business_date, store, bill_no, amount, matched — one row per digital
    bill line) and summed them in pandas via `ss.digital_by_store_date()`,
    the same shape `build()` uses. Measured on the one month loaded on this
    machine (docs/PERF_BASELINE.md): that query alone took **16.3 seconds**
    for 660,150 rows — SQLAlchemy ORM row materialisation, not the index,
    was the cost, and it would only grow with a longer range. Since this
    function only ever needs a SUM per (date, store), `func.sum(case(...))`
    with `GROUP BY` pushes the aggregation into SQLite and returns one row
    per store-day (thousands, not hundreds of thousands) — the same
    `ix_r1_matched_date` index still applies to the WHERE/GROUP BY. This is
    the one place in this module that reads `r1_bill_lines` this way rather
    than through `store_sheet.digital_by_store_date()`'s pure-function
    shape (§8) — that engine still serves `build()`, which is no longer on
    the page's hot path (run-time persistence and the lazy sections only).
    """
    # Two aggregates, not one CASE over every row — the engine-matched sum
    # uses `ix_r1_matched_date` (run_id, matched, business_date, store,
    # amount) leading on `matched`, so it seeks the ~99.97% of rows that are
    # engine-matched directly; the manual-overlay sum only ever scans the
    # engine-UNMATCHED rows (a few hundred, §6.9a) further narrowed to the
    # small active-manual-matches set. A single `CASE WHEN matched OR
    # bill_no IN (...)` over every row (measured, replaced 2026-09-20)
    # cannot use that leading column and fell back to summing all 660,150
    # rows per group — 2 of this function's ~3 seconds on one month.
    def _grouped_sum(query):
        query = (query.filter(
            R1BillLine.run_id == run_id,
            R1BillLine.business_date >= date_from.isoformat(),
            R1BillLine.business_date <= date_to.isoformat())
            .group_by(R1BillLine.business_date, R1BillLine.store))
        if store:
            query = query.filter(R1BillLine.store == store)
        return {(r.business_date, r.store): float(r.total or 0.0) for r in query.all()}

    engine_matched = _grouped_sum(session.query(
        R1BillLine.business_date, R1BillLine.store,
        func.sum(R1BillLine.amount).label("total"),
    ).filter(R1BillLine.matched == True))  # noqa: E712 — SQLAlchemy needs ==

    manual_bill_nos = {b for (b,) in session.query(ManualMatch.bill_no)
                       .filter(ManualMatch.lifecycle == "active").all() if b}
    manual_only = {}
    if manual_bill_nos:
        manual_only = _grouped_sum(session.query(
            R1BillLine.business_date, R1BillLine.store,
            func.sum(R1BillLine.amount).label("total"),
        ).filter(R1BillLine.matched == False,  # noqa: E712
                 R1BillLine.bill_no.in_(manual_bill_nos)))

    keys = set(engine_matched) | set(manual_only)
    if not keys:
        return pd.DataFrame(columns=["date", "store", "digital_received"])
    return pd.DataFrame([{
        "date": business_date, "store": store_code,
        "digital_received": engine_matched.get((business_date, store_code), 0.0)
                            + manual_only.get((business_date, store_code), 0.0),
    } for business_date, store_code in keys])


def defect_b_summary(session, run_id: int, date_from: date, date_to: date,
                     store: str = None) -> tuple:
    """(count, total J-minus-I value) of Defect B store-days — a plain
    COUNT+SUM on the precomputed `defect_b` flag, the same "cheap header,
    expensive body only when opened" shape `queries.exception_counts()`
    already uses for the Exceptions section. This is what lets Data
    quality become a normal collapsed-by-default section: the header never
    needs `data_quality_rows()`'s own CMS pickup-code walk, only when the
    section is actually opened does anything read the CMS file.

    The value returned is `sum(J - I)` — `cash_deposit - cash_receivable`
    per flagged row simplifies to `cash_received - cash_deposit`... no:
    `cash_receivable = cash_deposit - cash_received`, so a NEGATIVE
    receivable's magnitude (`J - I`, i.e. `-cash_receivable`) is
    `cash_received - cash_deposit`. Computed the same way
    `data_quality_rows()` computes `J_minus_I`, so the header total and the
    expanded table's own total can never disagree.
    """
    any_row = session.query(StoreDayAgg.id).filter(
        StoreDayAgg.run_id == run_id).first()
    if not any_row:
        # A run from before store_day_agg existed — fall back rather than
        # silently report zero Defect B rows that are actually there.
        sheet, _ = load_fast(session, run_id, date_from, date_to, store)
        bad = ss.credit_exceeds_deposit_rows(sheet)
        return len(bad), float((-bad["cash_receivable"]).sum()) if not bad.empty else 0.0

    query = session.query(
        func.count(StoreDayAgg.id),
        func.sum(StoreDayAgg.cash_received - StoreDayAgg.cash_deposit),
    ).filter(
        StoreDayAgg.run_id == run_id,
        StoreDayAgg.business_date >= date_from.isoformat(),
        StoreDayAgg.business_date <= date_to.isoformat(),
        StoreDayAgg.defect_b == True,  # noqa: E712
    )
    if store:
        query = query.filter(StoreDayAgg.store == store)
    n, v = query.one()
    return int(n or 0), float(v or 0.0)


def load_fast(session, run_id: int, date_from: date, date_to: date,
              store: str = None):
    """The store-day sheet and its by-date summary, read from the
    precomputed aggregate tables with the filter pushed into SQL — never a
    pandas post-filter over a whole-range pull.

    Falls back to the slow `build()` path (returning its `.sheet` /
    `.summary` unchanged) when `store_day_agg` has nothing for this run —
    a run persisted before this table existed, or `persist_aggregates()`
    has not been backfilled for it. This is the only place that fallback
    happens; a run that HAS been through the new pipeline never touches
    `build()` on a page load.

    Returns (sheet, summary_by_date) in the same shape `build()`'s bundle
    carries, minus everything only the lazy sections (Data quality,
    Excluded types, Data notes, the Excel export) need.
    """
    query = session.query(StoreDayAgg).filter(
        StoreDayAgg.run_id == run_id,
        StoreDayAgg.business_date >= date_from.isoformat(),
        StoreDayAgg.business_date <= date_to.isoformat(),
    )
    if store:
        query = query.filter(StoreDayAgg.store == store)
    rows = query.all()

    if not rows:
        # Nothing precomputed for this run — an old run from before this
        # table existed. Fall back rather than show an empty page.
        bundle = build(session, run_id, date_from, date_to)
        return bundle.sheet, bundle.summary

    sheet = pd.DataFrame([{
        "date": r.business_date, "store": r.store,
        **{c: float(getattr(r, c)) for c in AGG_COLUMNS},
    } for r in rows])

    live_f = _live_digital_received(session, run_id, date_from, date_to, store)
    if live_f.empty:
        sheet["digital_received"] = 0.0
    else:
        sheet = sheet.merge(live_f, on=["date", "store"], how="left")
        sheet["digital_received"] = sheet["digital_received"].fillna(0.0)
    sheet["digital_receivable"] = sheet["digital_sale"] - sheet["digital_received"]
    sheet = sheet[ss.FIELDS].sort_values(["date", "store"]).reset_index(drop=True)

    if store is None:
        dt_rows = session.query(DateTotalAgg).filter(
            DateTotalAgg.run_id == run_id,
            DateTotalAgg.business_date >= date_from.isoformat(),
            DateTotalAgg.business_date <= date_to.isoformat(),
        ).all()
        by_date = pd.DataFrame([{
            "date": r.business_date, "store": "ALL STORES",
            **{c: float(getattr(r, c)) for c in AGG_COLUMNS},
        } for r in dt_rows])
        # F/G for the ALL-STORES row is the same live figure, summed by date.
        f_by_date = (live_f.groupby("date", as_index=False)["digital_received"].sum()
                    if not live_f.empty else pd.DataFrame(columns=["date", "digital_received"]))
        by_date = by_date.merge(f_by_date, on="date", how="left")
        by_date["digital_received"] = by_date["digital_received"].fillna(0.0)
        by_date["digital_receivable"] = by_date["digital_sale"] - by_date["digital_received"]
        by_date = by_date.sort_values("date").reset_index(drop=True)
        # `sheet` here is already the small, filtered, aggregate-sourced
        # frame — no more parquet reads or carry-forward walk behind it —
        # so calling summarise_by_date once for its own TOTAL row is cheap
        # at any range; only its per-date rows are replaced by the
        # precomputed `by_date` above.
        computed = ss.summarise_by_date(sheet)
        total_row = computed[computed["date"] == "TOTAL"]
        summary = (pd.concat([by_date[ss.FIELDS], total_row], ignore_index=True)
                  if not by_date.empty else computed)
    else:
        summary = ss.summarise_by_date(sheet)

    return sheet, summary
