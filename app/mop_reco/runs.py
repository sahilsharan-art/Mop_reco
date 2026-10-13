"""Phase 1 — the explicit run. CLAUDE.md section 6.14.

Streams each raw source once, normalises it to Parquet, runs R1, and writes
results, exceptions and control totals into SQLite. The dashboard then reads
only what this produced — it never opens a raw file.

No Streamlit imports here: this is callable from a page, a script or a test.
"""

from __future__ import annotations

import gc
import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import ingest as ingest_mod
from . import store_sheet_source
from .config import Config
from .db import R1BillLine, ReconciliationResult, Run, RunFile
from . import manual_match
from . import masters as masters_mod
from .exceptions import build_r1_exceptions, sync_exceptions
from .r1_chunked import run_r1_chunked

logger = logging.getLogger("mop_reco")

# A `running` row whose heartbeat is older than this is STALLED, never
# trusted as genuinely in-progress (CLAUDE.md 6.14 — this is also what
# queries.STALLED_AFTER_SECONDS must match; the two are kept as separate
# constants only because they live in modules that must not import each
# other, not because they are allowed to disagree).
STALE_RUN_SECONDS = 300


class RunAlreadyInProgress(Exception):
    """Refused to start: another run is already `running`. Found 2026-09-24
    the hard way — a script-triggered run, an operator's own click on the
    Run page, and a THIRD attempt all ended up running at once, each
    holding a five-month dataset in memory, and the machine ran out of RAM
    (one run was OS-OOM-killed outright; another failed to allocate 1.72
    MiB, proof there was nothing left to give). Only one reconciliation may
    be in progress at a time, system-wide, regardless of what triggers it —
    checked HERE, inside `run_reconciliation()` itself, so an ad hoc script
    is refused exactly the same way the Run page's own button is; the lock
    cannot be bypassed by calling this function from somewhere else.

    `stalled=True` means the blocking run's heartbeat is stale (>=5 min) —
    it is not silently overridden even then; the caller (the Run page) must
    show it as STALLED and let the operator explicitly mark it failed
    before a new run's own call to this function will pass the check."""
    def __init__(self, run_id: int, heartbeat_age_seconds, stalled: bool):
        self.run_id = run_id
        self.heartbeat_age_seconds = heartbeat_age_seconds
        self.stalled = stalled
        age_desc = (f"{heartbeat_age_seconds:.0f}s" if heartbeat_age_seconds is not None
                   else "never (no heartbeat recorded)")
        super().__init__(
            f"Run #{run_id} is already in progress (last heartbeat {age_desc} ago)"
            + (" — STALLED, mark it failed before starting a new run" if stalled else "")
        )


def heartbeat(session, run, detail: str = None):
    """Record that the run is still alive, right now, doing `detail` — called
    both at stage boundaries and from INSIDE a long-running loop (the POS
    per-file read, the R1 matching loop), because a stage boundary alone is
    not enough: the incident that forced this (2026-09-24) was ONE stage
    silent for 14+ minutes while genuinely still working. The Run page
    treats a `running` row whose heartbeat_at is more than a couple of
    minutes old as STALLED rather than trusting `status` at face value —
    this is what makes that distinction possible. Commits immediately so a
    concurrent reader (the Run page, mid-run) sees it without waiting for
    the whole run to finish."""
    run.heartbeat_at = datetime.now()
    if detail is not None:
        run.progress_detail = detail
    session.commit()


@contextmanager
def _stage(session, run, timings: dict, name: str):
    """Times one stage of the run so the breakdown is measured, not
    estimated, and marks it as the run's current stage for the duration —
    the Run page's own progress display reads `current_stage` directly."""
    run.current_stage = name
    heartbeat(session, run, detail=None)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - t0, 2)
        logger.info(f"stage '{name}': {timings[name]}s")
        heartbeat(session, run, detail=f"finished '{name}' in {timings[name]}s")


def _record_files(session, run_id: int, report):
    for f in report.files:
        session.add(RunFile(
            run_id=run_id,
            source=report.source,
            file_path=f.path,
            file_mtime=f.mtime,
            row_count=report.rows_read,
            note=json.dumps({
                "encoding": f.encoding,
                "business_dates": report.business_dates,
                "control_total": report.control_total,
                "status": report.status,
                "notes": report.notes,
            }, default=str)[:4000],
        ))


def run_reconciliation(config: Config, session, date_from=None, date_to=None,
                       triggered_by: str = None, source_file_paths: dict = None) -> dict:
    """Ingest every available source, run R1, persist everything.

    `date_from` / `date_to` filter the RESULT to a business-date range; the
    linkage check in R1 still sees the whole loaded set first, because two
    chargeback-reversals carry dates outside the range they belong to
    (CLAUDE.md 6.3).

    `source_file_paths` (2026-09-24, after a 5-month run took over an hour):
    an optional {source_name: file_path} override for pos_bill_wise/
    axis_edc/bajaj (a single path) and paytm_settlement (a list of zip
    paths, matching `ingest_paytm`'s own `file_paths` argument) — bypasses
    multi-file discovery entirely for that source, reading exactly the
    file(s) named. For a targeted single-month run this also sidesteps a
    real, separate cost: a source folder can hold an old, permanently
    superseded duplicate (e.g. July's retired daily sample sitting beside
    the current monthly export) that discovery would otherwise read in
    full before discarding it in the date/key-overlap resolution — naming
    the file explicitly skips that read altogether. Omit it (or omit a
    key) for the normal, full auto-discovery behaviour.
    """
    source_file_paths = source_file_paths or {}

    # The hard lock (2026-09-24) — checked before anything else, including
    # before the new Run row is even created, so a refused attempt leaves
    # no trace in the run history at all. See RunAlreadyInProgress's own
    # docstring for why this exists.
    existing = (session.query(Run).filter(Run.status == "running")
                .order_by(Run.started_at.desc()).first())
    if existing is not None:
        age = ((datetime.now() - existing.heartbeat_at).total_seconds()
               if existing.heartbeat_at else None)
        stalled = age is None or age >= STALE_RUN_SECONDS
        raise RunAlreadyInProgress(existing.id, age, stalled=stalled)

    started = datetime.now()
    run = Run(
        started_at=started,
        business_dates=json.dumps([str(date_from) if date_from else None,
                                   str(date_to) if date_to else None]),
        status="running",
        triggered_by=triggered_by,
    )
    session.add(run)
    session.commit()
    logger.info(f"run {run.id}: started ({date_from} .. {date_to}) by {triggered_by}")

    load_reports = {}
    timings = {}

    def _progress(detail: str):
        heartbeat(session, run, detail=detail)

    try:
        # The masters' schema guard (2026-10-13): every master file must still be
        # present with every column the code reads, BEFORE any source is read.
        # A master losing a column used to surface only when the CMS stage first
        # loaded it - after R1 had run - or in the test suite days later.
        with _stage(session, run, timings, "masters_preflight"):
            master_checks = masters_mod.preflight_masters(config, session)
        master_preflight = masters_mod.preflight_summary(master_checks)
        for check in master_checks:
            for warning in check.warnings:
                logger.warning(f"run {run.id}: master pre-flight: {warning}")

        with _stage(session, run, timings, "ingest_pos_bill_wise"):
            bills, bills_report = ingest_mod.ingest_pos_bill_wise(
                config, session, file_path=source_file_paths.get("pos_bill_wise"),
                on_progress=_progress)
            load_reports["pos_bill_wise"] = bills_report
            _record_files(session, run.id, bills_report)

        with _stage(session, run, timings, "ingest_paytm"):
            paytm, paytm_report = ingest_mod.ingest_paytm(
                config, session, file_paths=source_file_paths.get("paytm_settlement"))
            load_reports["paytm_settlement"] = paytm_report
            _record_files(session, run.id, paytm_report)

        with _stage(session, run, timings, "ingest_axis_edc"):
            axis, axis_report = ingest_mod.ingest_axis_edc(
                config, session, file_path=source_file_paths.get("axis_edc"))
            load_reports["axis_edc"] = axis_report
            _record_files(session, run.id, axis_report)

        with _stage(session, run, timings, "ingest_bajaj"):
            bajaj, bajaj_report = ingest_mod.ingest_bajaj(
                config, session, file_path=source_file_paths.get("bajaj"))
            load_reports["bajaj"] = bajaj_report
            _record_files(session, run.id, bajaj_report)

        session.commit()

        # R1 reads the warehouse one business date at a time (2026-10-13), so
        # the ingest frames are no longer needed: release them before matching
        # starts. What R1 needs is already on disk - ingest wrote one Parquet
        # per business date per source and named them in its report.
        bill_files = dict(bills_report.warehouse_paths)
        pool_files = {"paytm": dict(paytm_report.warehouse_paths),
                      "axis": dict(axis_report.warehouse_paths),
                      "bajaj": dict(bajaj_report.warehouse_paths) if len(bajaj) else {}}
        undated = {name: rep.rows_without_date for name, rep in
                   (("pos_bill_wise", bills_report), ("paytm_settlement", paytm_report),
                    ("axis_edc", axis_report), ("bajaj", bajaj_report)) if rep.rows_without_date}
        if undated:
            # Never silent: a row with no business date has no per-date Parquet
            # to be matched from.
            logger.warning(f"run {run.id}: rows with NO business date are not matched "
                           f"(no per-date file exists for them): {undated}")
        del bills, paytm, axis, bajaj
        gc.collect()

        def _as_date(value):
            if value is None:
                return None
            return value if isinstance(value, date) else date.fromisoformat(str(value))

        d_from, d_to = _as_date(date_from), _as_date(date_to)
        persist_secs = 0.0
        mode_secs = 0.0
        unmatched_in_range = []
        dates_in_range = []
        mode_rows = mode_other_count = 0
        mode_other_value = 0.0
        persisted_rows = 0
        persisted_at = datetime.now()
        store_sheet_source.digital_mode_begin(session, run.id)
        session.commit()

        def _on_day(day_str, day_frame):
            """Called by the chunked matcher, oldest date first, with ONE date's
            final lines: persist and commit that date, keep only what the
            run-level stages still need (the unmatched lines), free the rest."""
            nonlocal persist_secs, mode_secs, mode_rows, mode_other_count
            nonlocal mode_other_value, persisted_rows
            bd = date.fromisoformat(day_str)
            if (d_from is not None and bd < d_from) or (d_to is not None and bd > d_to):
                return      # took part in matching, outside the requested range
            t0 = time.perf_counter()
            persisted_rows += _persist_day(session, run.id, day_str, day_frame, persisted_at)
            t1 = time.perf_counter()
            rows, oc, ov = store_sheet_source.digital_mode_add_day(session, run.id, day_frame)
            mode_rows += rows
            mode_other_count += oc
            mode_other_value += ov
            session.commit()      # durable and visible on Summary the moment this returns
            t2 = time.perf_counter()
            persist_secs += t1 - t0
            mode_secs += t2 - t1
            unmatched_in_range.append(day_frame.loc[~day_frame["matched"]])
            dates_in_range.append(day_str)

        with _stage(session, run, timings, "r1_match"):
            result = run_r1_chunked(bill_files, pool_files, on_day=_on_day,
                                    on_progress=_progress)
        # Persistence happens inside the matcher's per-date loop; report it as
        # its own stage so the stage table stays comparable with earlier runs.
        timings["r1_match"] = round(timings["r1_match"] - persist_secs - mode_secs, 2)
        timings["persist_results"] = round(persist_secs, 2)
        timings["persist_digital_mode_agg"] = round(mode_secs, 2)

        if mode_other_count:
            logger.warning(
                f"run {run.id}: {mode_other_count} matched digital bill line(s) worth "
                f"{mode_other_value:,.2f} carried a gateway/payment_mode combination "
                "this app does not recognise and were folded into Unclassified, "
                "never guessed into Card or UPI - see docs/MOP_PAYMENT_MODE.md")
        digital_mode_report = {"rows": mode_rows, "other_matched_count": mode_other_count,
                               "other_matched_value": round(mode_other_value, 2)}
        logger.info(f"run {run.id}: persisted {len(dates_in_range)} date(s), "
                    f"{persisted_rows} R1 bill-line detail rows, committed per date")

        bill_lines = (pd.concat(unmatched_in_range, ignore_index=True)
                      if unmatched_in_range else pd.DataFrame({"matched": pd.Series(dtype=bool)}))
        has_lines = bool(dates_in_range)

        # Every date in the REQUESTED range that produced zero bills is
        # named explicitly (2026-09-24) - "nothing there" and "not loaded"
        # look identical otherwise, which is exactly the confusion that
        # led to a whole diagnostic round-trip over April-August showing
        # zero. A gap here is not necessarily wrong (a store network can
        # genuinely have no digital bills some day), but it must never be
        # silent.
        dates_with_bills = set(dates_in_range)
        no_data_dates = []
        if date_from is not None and date_to is not None:
            no_data_dates = sorted(
                d.isoformat() for d in pd.date_range(date_from, date_to).date
                if d.isoformat() not in dates_with_bills)
            if no_data_dates:
                logger.warning(
                    f"run {run.id}: {len(no_data_dates)} date(s) in the requested "
                    f"range produced ZERO digital bill lines - not an error by "
                    f"itself, but reported rather than silently absorbed: "
                    f"{no_data_dates[:10]}{'...' if len(no_data_dates) > 10 else ''}")

        # The store-day / date-total aggregates (CLAUDE.md's performance
        # rework, 2026-09-20): computed once here, for the run's own full
        # range, so Summary reads an indexed lookup instead of rebuilding
        # the whole cash-book sheet from PTC/CMS/opening-balance/POS on
        # every page load. Resolved to concrete dates from the bill lines
        # themselves when the run itself carried no explicit range (rule 8
        # - never a hardcoded fallback date).
        with _stage(session, run, timings, "persist_store_day_agg"):
            if has_lines:
                agg_from = d_from or date.fromisoformat(min(dates_in_range))
                agg_to = d_to or date.fromisoformat(max(dates_in_range))
                agg_report = store_sheet_source.persist_aggregates(
                    session, run.id, agg_from, agg_to)
            else:
                agg_report = {"store_days": 0, "dates": 0, "elapsed": 0.0}

        # The MOP-bucket sale aggregate (2026-09-18, the Summary particulars
        # matrix) — a separate stage from persist_store_day_agg above since
        # it reads pos_bill_wise on its own bucket taxonomy (card/upi/cash/
        # gift_voucher/credit_note_issued/credit_note_received), not the E
        # column's plain cash-only split. Same range as the store-day agg,
        # so the two can never disagree about what period they cover.
        with _stage(session, run, timings, "persist_mop_day_agg"):
            if has_lines:
                mop_agg_report = store_sheet_source.persist_mop_day_agg(
                    session, run.id, agg_from, agg_to)
            else:
                mop_agg_report = {"rows": 0, "elapsed": 0.0}

        # The Card/UPI aggregate (2026-09-19, docs/MOP_PAYMENT_MODE.md) is
        # written date by date inside the matcher's loop above - payment_mode
        # only exists after matching, so it cannot be rebuilt later from raw POS.

        with _stage(session, run, timings, "exception_register"):
            exceptions = build_r1_exceptions(bill_lines, result)
            sync_result = sync_exceptions(session, run.id, exceptions)

        # The manual overlay is re-checked against the fresh run, never
        # discarded by it (rule 2). Three outcomes, all reported: redundant
        # (the engine now matches the same way), CONFLICTED (it matches a
        # different transaction — both kept, a person resolves), or orphaned
        # (the bill has left the dump). See manual_match.reconcile_after_run.
        with _stage(session, run, timings, "manual_overlay"):
            overlay = manual_match.reconcile_after_run(session, run.id)
            manual_result = {
                "superseded": [m.id for m in overlay.superseded],
                "conflicted": [m.id for m in overlay.conflicted],
                "orphaned": [m.id for m in overlay.orphaned],
                "unchanged": overlay.unchanged,
            }
        if overlay.conflicted:
            logger.warning(
                f"run {run.id}: {len(overlay.conflicted)} manual match(es) now "
                "CONFLICT with the engine — neither was overwritten, both are "
                "on the Manual matches page for a person to resolve"
            )

        run.finished_at = datetime.now()
        run.status = "completed"
        run.notes = json.dumps({
            "r1": result.summary,
            "exceptions": sync_result,
            "manual_overlay": manual_result,
            "store_day_agg": agg_report,
            "mop_day_agg": mop_agg_report,
            "digital_mode_agg": digital_mode_report,
            "timings": timings,
            "masters_preflight": master_preflight,
            "load": {k: {"rows_read": v.rows_read, "status": v.status,
                         "control_total": v.control_total, "notes": v.notes}
                    for k, v in load_reports.items()},
            "no_data_dates": no_data_dates,
        }, default=str)
        session.commit()

        elapsed = (run.finished_at - started).total_seconds()
        logger.info(f"run {run.id}: completed in {elapsed:.1f}s — {result.summary}")

        return {
            "run_id": run.id,
            "elapsed_seconds": elapsed,
            "r1": result.summary,
            "exceptions": sync_result,
            "manual_overlay": manual_result,
            "timings": timings,
            "load_reports": load_reports,
            "no_data_dates": no_data_dates,
        }

    except Exception as exc:
        # Found 2026-09-24, the hard way: a DB-level failure (e.g. the
        # exception_register IntegrityError below) leaves the Session in a
        # "pending rollback" state — SQLAlchemy refuses ANY further use,
        # including this handler's own attempt to set run.status="failed"
        # and commit, until `rollback()` is called first. Without this, the
        # handler's own commit raised a SECOND exception (PendingRollbackError)
        # that masked the first and crashed the process with the run still
        # sitting at status="running" forever — precisely the "stuck, not
        # marked failed" failure mode this handler exists to prevent.
        # `rollback()` discards only this stage's own uncommitted work; every
        # EARLIER stage's per-date commits (ingestion, R1 matching's
        # persist_results) are already durable and are untouched by it.
        failed_stage = run.current_stage
        session.rollback()
        run.finished_at = datetime.now()
        run.status = "failed"
        # JSON, like every other successful-path write to `notes` — a plain
        # string here (as this used to be) breaks every reader that assumes
        # `notes` is JSON, including run_history() on the Run page itself,
        # the exact page an operator opens to see WHY a run failed.
        run.notes = json.dumps({
            "failure_reason": f"{type(exc).__name__}: {exc}",
            "timings": timings,
            "current_stage": failed_stage,
        })
        session.commit()
        logger.error(f"run {run.id}: FAILED at stage '{failed_stage}' — {type(exc).__name__}: {exc}")
        raise


def _s(v):
    return None if pd.isna(v) else str(v)


def _bill_line_record(run_id: int, row) -> dict:
    return {
        "run_id": run_id,
        "business_date": str(row.business_date),
        "store": _s(row.store),
        "bill_no": _s(row.bill_no),
        "line_seq": int(getattr(row, "line_seq", 1) or 1),
        "mop": _s(row.mop),
        "mop_class": _s(row.mop_class),
        "amount": float(row.amount),
        "paytm_ref": _s(row.paytm_ref),
        "axis_ref": _s(row.axis_ref),
        "bajaj_ref": _s(row.bajaj_ref),
        "matched": bool(row.matched),
        "match_tier": _s(row.match_tier),
        "matched_gateway": _s(row.matched_gateway),
        "matched_txn_ref": _s(row.matched_txn_ref),
        "payment_mode": _s(getattr(row, "matched_payment_mode", None)),
        "status": _s(row.status),
        "date_diff_days": int(getattr(row, "date_diff_days", 0) or 0),
        "match_note": _s(getattr(row, "match_note", None)),
        # BILL_TIME / CREATEBY (2026-09-29) — carried from the POS ingest
        # straight through matching, unchanged; real columns, not a JSON
        # detail blob. Null on the rare row a source file predates these
        # two columns for.
        "bill_time": _s(getattr(row, "bill_time", None)),
        "created_by": _s(getattr(row, "created_by", None)),
    }


def _text_column(series: pd.Series) -> pd.Series:
    """`_s()` over a whole column: None for a missing value, str(value)
    otherwise - without a Python call per cell when the column is already text."""
    if pd.api.types.is_string_dtype(series.dtype) and not pd.api.types.is_object_dtype(series.dtype):
        return series.astype(object).where(series.notna(), None)
    return series.map(_s).astype(object)


def _bill_line_records(run_id: int, day: pd.DataFrame) -> list:
    """The list `[_bill_line_record(run_id, row) for row in day.itertuples()]`,
    built column-wise. `tests/test_runs_persist.py` asserts the two are
    identical row for row; `_bill_line_record` stays as the specification."""
    n = len(day)

    def col(name, default=None):
        return day[name] if name in day.columns else pd.Series([default] * n, index=day.index)

    line_seq = pd.to_numeric(col("line_seq", 1), errors="coerce").fillna(1)
    line_seq = line_seq.where(line_seq != 0, 1).astype("int64")
    diff = pd.to_numeric(col("date_diff_days", 0), errors="coerce").fillna(0).astype("int64")
    out = pd.DataFrame({
        "run_id": run_id,
        "business_date": day["business_date"].map(str),
        "store": _text_column(day["store"]),
        "bill_no": _text_column(day["bill_no"]),
        "line_seq": line_seq,
        "mop": _text_column(day["mop"]),
        "mop_class": _text_column(day["mop_class"]),
        "amount": day["amount"].astype("float64"),
        "paytm_ref": _text_column(day["paytm_ref"]),
        "axis_ref": _text_column(day["axis_ref"]),
        "bajaj_ref": _text_column(day["bajaj_ref"]),
        "matched": day["matched"].astype(bool),
        "match_tier": _text_column(day["match_tier"]),
        "matched_gateway": _text_column(day["matched_gateway"]),
        "matched_txn_ref": _text_column(day["matched_txn_ref"]),
        "payment_mode": _text_column(col("matched_payment_mode")),
        "status": _text_column(day["status"]),
        "date_diff_days": diff,
        "match_note": _text_column(col("match_note")),
        "bill_time": _text_column(col("bill_time")),
        "created_by": _text_column(col("created_by")),
    })
    # Column by column, straight to Python scalars: `DataFrame.to_dict("records")`
    # over an all-object frame was ~30% of a day's persistence.
    cols = list(out.columns)
    lists = []
    for c in cols:
        column = out[c]
        if column.isna().any():
            column = column.astype(object).where(column.notna(), None)
        lists.append(column.tolist())
    return [dict(zip(cols, row)) for row in zip(*lists)]


def _store_payloads(day: pd.DataFrame):
    """[(store, payload dict)] for one date, oldest-key-first exactly like
    `day.groupby("store", dropna=False)`: the per-store counts and sums the
    reconciliation_results rows carry. Each store's amounts are summed with the
    same numpy routine `Series.sum` uses (np.nansum over that store's own
    contiguous values), so every figure is bit-identical to the per-group loop
    this replaces (`tests/test_runs_persist.py` holds that loop as the spec)."""
    amount = day["amount"].to_numpy(dtype="float64")
    matched = day["matched"].to_numpy(dtype=bool)
    tier = day["match_tier"]
    is_t1 = (tier == "Tier 1").fillna(False).to_numpy(dtype=bool)
    is_t2 = (tier == "Matched (EDC OTHER)").fillna(False).to_numpy(dtype=bool)
    is_amb = (tier == "Ambiguous").fillna(False).to_numpy(dtype=bool)
    groups = day.groupby("store", dropna=False).indices
    out = []
    for store in sorted(groups, key=lambda k: (pd.isna(k), "" if pd.isna(k) else k)):
        idx = groups[store]
        a, m = amount[idx], matched[idx]
        out.append((store, {
            "digital_bill_lines": int(len(idx)),
            "matched": int(m.sum()),
            "unmatched": int((~m).sum()),
            "tier1": int(is_t1[idx].sum()),
            "tier2": int(is_t2[idx].sum()),
            "ambiguous": int(is_amb[idx].sum()),
            "digital_value": float(np.nansum(a)),
            "matched_value": float(np.nansum(a[m])),
            "unmatched_value": float(np.nansum(a[~m])),
        }))
    return out


def _persist_day(session, run_id: int, business_date, day: pd.DataFrame, now) -> int:
    """One business date: its per-store result rows and its bill-line detail.
    Added to the session only - the caller commits (once per date)."""
    for store, payload in _store_payloads(day):
        session.add(ReconciliationResult(
            run_id=run_id, reconciliation="R1",
            business_date=str(business_date), store=str(store),
            payload=json.dumps(payload), created_at=now,
        ))
    records = _bill_line_records(run_id, day)
    if records:
        # Core executemany: ~30% cheaper than the ORM's bulk_insert_mappings for the
        # same rows (the detail table is the largest thing a run writes).
        session.execute(R1BillLine.__table__.insert(), records)
    return len(records)


def _persist_r1(session, run_id: int, bill_lines: pd.DataFrame, result, on_progress=None):
    """One results row per store x business_date, plus the per-line detail the
    transaction view pages through — committed ONE BUSINESS DATE AT A TIME
    (2026-09-24), not once for the whole range.

    Kept for callers that already hold a whole range of matched lines; the
    run itself now persists each date as the chunked matcher yields it
    (`run_reconciliation`, 2026-10-13) through the same `_persist_day`."""
    now = datetime.now()
    dates = sorted(bill_lines["business_date"].dropna().unique())
    total_rows_written = 0

    for i, business_date in enumerate(dates, 1):
        day = bill_lines[bill_lines["business_date"] == business_date]
        total_rows_written += _persist_day(session, run_id, business_date, day, now)
        # Committed HERE, per date: durable and visible to any other reader
        # the instant this returns; a crash on the NEXT date loses only it.
        session.commit()
        if on_progress:
            on_progress(f"persisted {i:,} of {len(dates):,} dates "
                       f"({business_date}, {total_rows_written:,} rows so far)")

    logger.info(f"run {run_id}: persisted {len(dates)} date(s), "
               f"{total_rows_written} R1 bill-line detail rows, committed per date")
