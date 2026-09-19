"""Phase 1 — the explicit run. CLAUDE.md section 6.14.

Streams each raw source once, normalises it to Parquet, runs R1, and writes
results, exceptions and control totals into SQLite. The dashboard then reads
only what this produced — it never opens a raw file.

No Streamlit imports here: this is callable from a page, a script or a test.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime

import pandas as pd

from . import ingest as ingest_mod
from . import store_sheet_source
from .config import Config
from .db import R1BillLine, ReconciliationResult, Run, RunFile
from . import manual_match
from .exceptions import build_r1_exceptions, sync_exceptions
from .r1_bill_txn import run_r1

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

        with _stage(session, run, timings, "r1_match"):
            result = run_r1(bills, paytm, axis, bajaj if len(bajaj) else None,
                            on_progress=_progress)

        bill_lines = result.bill_lines
        if date_from is not None:
            bill_lines = bill_lines[bill_lines["business_date"] >= date_from]
        if date_to is not None:
            bill_lines = bill_lines[bill_lines["business_date"] <= date_to]

        with _stage(session, run, timings, "persist_results"):
            _persist_r1(session, run.id, bill_lines, result, on_progress=_progress)

        # Every date in the REQUESTED range that produced zero bills is
        # named explicitly (2026-09-24) — "nothing there" and "not loaded"
        # look identical otherwise, which is exactly the confusion that
        # led to a whole diagnostic round-trip over April-August showing
        # zero. A gap here is not necessarily wrong (a store network can
        # genuinely have no digital bills some day), but it must never be
        # silent.
        dates_with_bills = set(str(d) for d in bill_lines["business_date"].dropna().unique())
        no_data_dates = []
        if date_from is not None and date_to is not None:
            no_data_dates = sorted(
                d.isoformat() for d in pd.date_range(date_from, date_to).date
                if d.isoformat() not in dates_with_bills)
            if no_data_dates:
                logger.warning(
                    f"run {run.id}: {len(no_data_dates)} date(s) in the requested "
                    f"range produced ZERO digital bill lines — not an error by "
                    f"itself, but reported rather than silently absorbed: "
                    f"{no_data_dates[:10]}{'...' if len(no_data_dates) > 10 else ''}")

        # The store-day / date-total aggregates (CLAUDE.md's performance
        # rework, 2026-09-20): computed once here, for the run's own full
        # range, so Summary reads an indexed lookup instead of rebuilding
        # the whole cash-book sheet from PTC/CMS/opening-balance/POS on
        # every page load. Resolved to concrete dates from the bill lines
        # themselves when the run itself carried no explicit range (rule 8
        # — never a hardcoded fallback date).
        def _as_date(value):
            if value is None:
                return None
            return value if isinstance(value, date) else date.fromisoformat(str(value))

        with _stage(session, run, timings, "persist_store_day_agg"):
            if not bill_lines.empty:
                agg_from = _as_date(date_from) or date.fromisoformat(
                    str(bill_lines["business_date"].min()))
                agg_to = _as_date(date_to) or date.fromisoformat(
                    str(bill_lines["business_date"].max()))
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
            if not bill_lines.empty:
                mop_agg_report = store_sheet_source.persist_mop_day_agg(
                    session, run.id, agg_from, agg_to)
            else:
                mop_agg_report = {"rows": 0, "elapsed": 0.0}

        # The Card/UPI correctness fix (2026-09-19, docs/MOP_PAYMENT_MODE.md):
        # built from the run's OWN `bill_lines` frame directly, in memory,
        # because payment_mode only exists after R1 matching — it cannot be
        # rebuilt from raw POS the way persist_mop_day_agg() is.
        with _stage(session, run, timings, "persist_digital_mode_agg"):
            digital_mode_report = store_sheet_source.persist_digital_mode_agg(
                session, run.id, bill_lines)

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


def _persist_r1(session, run_id: int, bill_lines: pd.DataFrame, result, on_progress=None):
    """One results row per store x business_date, plus the per-line detail the
    transaction view pages through — committed ONE BUSINESS DATE AT A TIME
    (2026-09-24), not once for the whole range.

    Before this, both the store-day summary and the bill-line detail were
    built entirely in memory across the WHOLE requested range and committed
    in exactly two `session.commit()` calls total, right at the end. That
    meant Summary showed nothing — not even the first day — until every
    date in the run had been matched AND persisted, made an hour of
    genuine work vanish on any late failure, and left no way to tell a
    slow run from a hung one. R1 MATCHING itself still has to see the
    whole loaded range at once (a gateway reference is deliberately
    date-agnostic — CLAUDE.md's own R1 design), so that part is unchanged;
    this only changes when the ALREADY-MATCHED result for one date becomes
    visible. `bill_lines` is grouped by date, oldest first, and each
    date's rows are added and committed before the next date starts — a
    business date is visible on Summary the moment its own commit lands,
    and a mid-run failure only loses whatever date was mid-flight, not the
    whole run."""
    now = datetime.now()
    dates = sorted(bill_lines["business_date"].dropna().unique())
    total_rows_written = 0

    for i, business_date in enumerate(dates, 1):
        day = bill_lines[bill_lines["business_date"] == business_date]

        for store, g in day.groupby("store", dropna=False):
            payload = {
                "digital_bill_lines": int(len(g)),
                "matched": int(g["matched"].sum()),
                "unmatched": int((~g["matched"]).sum()),
                "tier1": int((g["match_tier"] == "Tier 1").sum()),
                "tier2": int((g["match_tier"] == "Matched (EDC OTHER)").sum()),
                "ambiguous": int((g["match_tier"] == "Ambiguous").sum()),
                "digital_value": float(g["amount"].sum()),
                "matched_value": float(g.loc[g["matched"], "amount"].sum()),
                "unmatched_value": float(g.loc[~g["matched"], "amount"].sum()),
            }
            session.add(ReconciliationResult(
                run_id=run_id, reconciliation="R1",
                business_date=str(business_date), store=str(store),
                payload=json.dumps(payload), created_at=now,
            ))

        records = [_bill_line_record(run_id, row) for row in day.itertuples(index=False)]
        if records:
            session.bulk_insert_mappings(R1BillLine, records)

        # Committed HERE, per date — this is the actual fix. Everything
        # above this line for `business_date` is now durable and visible
        # to any other reader (the Summary page, mid-run) the instant this
        # returns; a crash on the NEXT date loses only that next date.
        session.commit()
        total_rows_written += len(records)
        if on_progress:
            on_progress(f"persisted {i:,} of {len(dates):,} dates "
                       f"({business_date}, {total_rows_written:,} rows so far)")

    logger.info(f"run {run_id}: persisted {len(dates)} date(s), "
               f"{total_rows_written} R1 bill-line detail rows, committed per date")
