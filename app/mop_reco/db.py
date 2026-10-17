"""Database schema — Stage 0.

Tables only. No reconciliation logic writes to these yet; that starts at
Stage 4+ (R3a) per CLAUDE.md section 15 / the build sequence.

Covers, per CLAUDE.md section 4 ("db/ ... results, exceptions, remarks,
runs") plus the master-versioning requirement from section 6.12:
  - runs               one row per reconciliation run
  - run_files          every file a run read, with its mtime and row count
                        (needed for the Paytm multi-part read/dedupe logging
                        and the general "log every run" rule in section 8)
  - exceptions         the working exception register (section 6.9)
  - remarks            free-text remarks against an exception, never lost
  - reconciliation_results  generic per-row output slot; the real column
                        shape per reconciliation (R1/R2/R3a/R3b) is designed
                        when that engine is built, not guessed now
  - master_versions    one row per snapshot of a master on load (section 6.12)
  - master_rows        the rows belonging to one master_versions snapshot
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Boolean,
    Index,
    text,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from .config import PROJECT_ROOT

# MOP_RECO_DB points the whole application at another database file - a COPY, to rehearse a workflow
# end to end (a second dashboard on another port, a script) without touching db/reco.sqlite.
import os as _os

DEFAULT_DB_PATH = Path(_os.environ["MOP_RECO_DB"]) if _os.environ.get("MOP_RECO_DB") else PROJECT_ROOT / "db" / "reco.sqlite"

Base = declarative_base()


class Run(Base):
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    business_dates = Column(Text, nullable=False)  # JSON list, e.g. '["2026-07-20"]'
    status = Column(String, nullable=False, default="running")  # running|completed|failed
    triggered_by = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    # Progress visibility (2026-09-24, after a 5-month run sat silent for 14+
    # minutes mid-flight with no way to tell working from hung). Updated
    # throughout the run, not just at stage boundaries — see runs.heartbeat().
    # A `running` row whose heartbeat_at is stale is shown as STALLED by the
    # Run page rather than trusted at face value.
    current_stage = Column(String, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    progress_detail = Column(Text, nullable=True)  # e.g. "file 3 of 5" / "62,000 of 91,000 bills"


class RunFile(Base):
    __tablename__ = "run_files"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    source = Column(String, nullable=False)       # logical source name, e.g. "paytm_settlement"
    file_path = Column(Text, nullable=False)
    file_mtime = Column(DateTime, nullable=True)
    row_count = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)             # e.g. "part 2 of 3", "before dedupe 17099 / after 17098"


class Exception_(Base):
    # Named Exception_ to avoid shadowing the Python builtin; table name is clean.
    __tablename__ = "exceptions"

    id = Column(Integer, primary_key=True)
    exception_key = Column(String, nullable=False, unique=True)  # deterministic, section 6.9
    reconciliation = Column(String, nullable=False)  # R1 | R2 | R3a | R3b | data
    type = Column(String, nullable=False)            # e.g. suspected_duplicate_bill, store_ambiguous
    business_date = Column(String, nullable=False)   # ISO date string
    store = Column(String, nullable=True)
    cluster = Column(String, nullable=True)
    amount = Column(Numeric, nullable=True)
    detail = Column(Text, nullable=True)             # JSON blob: bill_no, transaction_id, UTR, ...
    status = Column(String, nullable=False, default="open")  # open|under_investigation|closed|auto_resolved
    reason_code = Column(String, nullable=True)
    external_reference = Column(String, nullable=True)
    first_seen_run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    last_seen_run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    closed_on = Column(DateTime, nullable=True)
    closed_by = Column(String, nullable=True)

    # The gateway transaction's own time, 'HH:MM:SS' (2026-09-29) — this row
    # IS the stored record for an unmapped transaction (there is no separate
    # gateway-transaction table), keyed exactly on (gateway, transaction_id)
    # via exception_key already. Populated only for type
    # "unmapped_gateway_transaction"; null for every bill-side exception
    # type and null for Axis (that gateway's own TRAN_TIME field is a
    # constant '000000' placeholder, never a real time — confirmed against
    # all 134,887 rows of the real April-August file, no exceptions).
    transaction_time = Column(String, nullable=True)


class Remark(Base):
    __tablename__ = "remarks"

    id = Column(Integer, primary_key=True)
    exception_id = Column(Integer, ForeignKey("exceptions.id"), nullable=False)
    remark_text = Column(Text, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False)


class ReconciliationResult(Base):
    __tablename__ = "reconciliation_results"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    reconciliation = Column(String, nullable=False)  # R1 | R2 | R3a | R3b
    business_date = Column(String, nullable=False)
    store = Column(String, nullable=True)
    payload = Column(Text, nullable=False)  # JSON blob; shape fixed when each engine is built
    created_at = Column(DateTime, nullable=False)


class R1BillLine(Base):
    """Per-bill-line R1 detail — what the transaction view pages through.

    Indexed on business_date / store / status because the view filters and
    paginates on exactly those; a full month is ~660k rows and must never be
    loaded into memory at once (CLAUDE.md 6.14).
    """
    __tablename__ = "r1_bill_lines"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    store = Column(String, nullable=True, index=True)
    bill_no = Column(String, nullable=True, index=True)
    line_seq = Column(Integer, nullable=True)
    mop = Column(String, nullable=True)
    mop_class = Column(String, nullable=True)
    amount = Column(Numeric, nullable=True)
    paytm_ref = Column(String, nullable=True)
    axis_ref = Column(String, nullable=True)
    bajaj_ref = Column(String, nullable=True)
    matched = Column(Boolean, nullable=False, default=False)
    match_tier = Column(String, nullable=True)
    matched_gateway = Column(String, nullable=True)
    matched_txn_ref = Column(String, nullable=True)
    # The matched transaction's own payment_mode, captured at match time
    # (2026-09-19) — the Card/UPI correctness fix. Kept here for
    # traceability/drill-down only; `digital_mode_agg` (built from the same
    # in-memory bill_lines frame at run time, not from this column) is what
    # the particulars matrix actually reads. Null for an unmatched line and
    # for a Bajaj/Axis match, whose mode is decided without this field —
    # see mop_matrix.classify_digital_mode().
    payment_mode = Column(String, nullable=True)
    status = Column(String, nullable=False, index=True)

    # A gateway reference is globally unique (2026-09-13) — Tier 1 no longer
    # restricts the search to the bill's own business date, so a match can
    # land on a transaction dated a different day. Never hidden: date_diff_days
    # is txn_date - bill_date (0 when they agree), and match_note names it in
    # words ("reference matched, txn dated N day(s) after bill") whenever it
    # is non-zero. Revenue recognition still stays on the bill's own date —
    # this is a matching-search change only, never a date change on the bill.
    date_diff_days = Column(Integer, nullable=True)
    match_note = Column(String, nullable=True)

    # BILL_TIME / CREATEBY (2026-09-29) — carried straight from the POS
    # bill-wise ingest, real schema columns (not a JSON detail blob), so an
    # open bill can be paired against an open transaction by eye: same
    # store, same amount, times a few seconds apart. 'HH:MM:SS' text,
    # never shifted for timezone, never derived from anything else.
    bill_time = Column(String, nullable=True)
    created_by = Column(String, nullable=True)

    # The transaction view orders by (business_date, store, bill_no) within a
    # run and pages with LIMIT/OFFSET. Without a composite index covering that
    # exact order, SQLite sorts the whole month for every page: a measured
    # 25.1s to reach OFFSET 500,000 on 660,150 rows. With it, the same page is
    # an index walk. Single-column indexes do not help an ORDER BY on three.
    __table_args__ = (
        Index("ix_r1_page", "run_id", "business_date", "store", "bill_no"),
        # The EDC OTHER summary seeks by MOP then ranges over dates. Without a
        # covering index for that shape the aggregate scans the month: measured
        # 18.2s as four counts, 3.7s as one aggregate, 0.03s with this index.
        Index("ix_r1_mop_date", "run_id", "mop", "business_date"),
        # Every landing-page figure is "matched vs not, over a date range":
        # the verdict aggregate and the by-reason breakdown both filter on
        # (run_id, matched) and range over business_date. Column order puts
        # matched before business_date so the 205 unmatched rows are an index
        # seek rather than a scan of the month, and amount is carried so the
        # SUM is answered from the index without touching the table.
        # Measured on 660,150 lines: verdict 3,092ms -> 225ms, and the
        # by-reason breakdown 5,496ms -> 435ms. Do not drop this index.
        Index("ix_r1_matched_date", "run_id", "matched", "business_date",
              "store", "amount"),
    )


# ---------------------------------------------------------------------------
# Manual matching — an OVERLAY, never an edit to source data.
#
# The engine runs untouched and writes r1_bill_lines as it always did; the
# overlay is applied afterwards, keyed by the bill's deterministic key
# (business_date | store | bill_no | mop — §6.9). That is what makes rule 2
# ("every date must be re-runnable") safe here: a re-run rebuilds the engine's
# own rows from scratch and the overlay survives because it lives in its own
# table and is never written by the engine.
# ---------------------------------------------------------------------------

# One upload = one batch = one id, so it can be reversed as a unit.
class ManualMatchBatch(Base):
    __tablename__ = "manual_match_batches"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, nullable=False)
    operator = Column(String, nullable=False)
    source = Column(String, nullable=False)      # "single" | "upload"
    file_name = Column(String, nullable=True)
    rows_submitted = Column(Integer, nullable=False, default=0)
    rows_accepted = Column(Integer, nullable=False, default=0)
    rows_rejected = Column(Integer, nullable=False, default=0)
    note = Column(Text, nullable=True)


class ManualMatch(Base):
    """One human assertion that a bill was paid by a named transaction.

    `bill_key` is the deterministic exception key, so a remark or status set
    against the same bill before the manual match still points at the same
    thing after it.

    `validation_case` is the letter from the validation the operator actually
    saw — 'a' clean, 'b' overridden, 'c' asserted with no gateway row found.
    It is stored rather than recomputed because it records what was true and
    what was shown at the moment of the decision.
    """
    __tablename__ = "manual_matches"

    id = Column(Integer, primary_key=True)
    bill_key = Column(String, nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    store = Column(String, nullable=True, index=True)
    bill_no = Column(String, nullable=True, index=True)
    mop = Column(String, nullable=True)
    amount = Column(Numeric, nullable=True)

    gateway = Column(String, nullable=False)          # paytm | axis | bajaj
    transaction_ref = Column(String, nullable=False, index=True)

    # "matched_manual" or "asserted_unverified" — never blended with the
    # engine's own "Matched". §4 of the brief: a rate that mixes them flatters
    # itself.
    status = Column(String, nullable=False, index=True)
    validation_case = Column(String, nullable=False)  # "a" | "b" | "c"
    override_used = Column(Boolean, nullable=False, default=False)
    override_reason = Column(Text, nullable=True)
    reason_code = Column(String, nullable=False)
    note = Column(Text, nullable=True)

    # What the validator found at decision time, kept verbatim for audit.
    validation_detail = Column(Text, nullable=True)   # JSON

    operator = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)
    batch_id = Column(Integer, ForeignKey("manual_match_batches.id"), nullable=True,
                      index=True)

    # Lifecycle. "active" is the only state that affects a figure; everything
    # else is kept and reported, never deleted (rule 4).
    #   active     — in force
    #   reversed   — undone by a person; the bill returns to unmatched
    #   superseded — the engine later matched the same bill to the SAME txn
    #   conflicted — the engine later matched that bill to a DIFFERENT txn
    #   orphaned   — the bill no longer exists in the dump (IT deleted it)
    # How it was recorded (2026-10-16): "inline" (typed or chosen on the table) or "upload" (a file).
    # NULL on a match recorded before this existed - read as inline, or as upload for a bulk batch.
    source = Column(String, nullable=True)
    lifecycle = Column(String, nullable=False, default="active", index=True)
    lifecycle_note = Column(Text, nullable=True)
    lifecycle_at = Column(DateTime, nullable=True)
    reversed_by = Column(String, nullable=True)

    __table_args__ = (
        # A transaction may back at most ONE active manual match. Assigning one
        # transaction to two bills double-counts revenue and hides a genuine
        # shortfall, so it is refused in the validator AND unique here — the
        # database is the backstop if a code path ever forgets to check.
        Index("ux_manual_txn_active", "gateway", "transaction_ref",
              unique=True, sqlite_where=text("lifecycle = 'active'")),
        Index("ux_manual_bill_active", "bill_key",
              unique=True, sqlite_where=text("lifecycle = 'active'")),
        Index("ix_manual_operator_date", "operator", "business_date"),
    )


class ManualMatchEvent(Base):
    """Every state change to a manual match. Append-only: a reversal is a new
    row, never an update that erases what was there (rule 4)."""
    __tablename__ = "manual_match_events"

    id = Column(Integer, primary_key=True)
    manual_match_id = Column(Integer, ForeignKey("manual_matches.id"),
                             nullable=False, index=True)
    event = Column(String, nullable=False)     # created | reversed | superseded | ...
    at = Column(DateTime, nullable=False)
    operator = Column(String, nullable=True)
    detail = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Store float remarks (2026-09-13) — the Float page's own overlay, on the
# identical shape manual_matches uses: a mutable "current" row, keyed
# deterministically so it survives every re-run, plus an append-only event
# log so a remark is never silently replaced without a trace (rule 4).
#
# Auto-remarks ("Weekend"/"Holiday") are NOT rows here — they are computed
# fresh on every read from the calendar alone (see mop_reco.float_tracker),
# never persisted, so there is nothing for the system to silently overwrite.
# A row only exists here once an OPERATOR has written something, at which
# point it is exactly what displays, forever, until they change it again.
# ---------------------------------------------------------------------------

class FloatRemark(Base):
    __tablename__ = "float_remarks"

    id = Column(Integer, primary_key=True)
    store = Column(String, nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    remark = Column(Text, nullable=False)
    remark_by = Column(String, nullable=False)
    remark_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("ux_float_remark_key", "store", "business_date", unique=True),
    )


class FloatRemarkEvent(Base):
    """Append-only: every remark ever set against a store-date, in order.
    Never an update that erases what was there (rule 4)."""
    __tablename__ = "float_remark_events"

    id = Column(Integer, primary_key=True)
    store = Column(String, nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    remark = Column(Text, nullable=False)
    remark_by = Column(String, nullable=False)
    remark_at = Column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# Store Task (2026-10-14) - items assigned to stores, their replies, tracked to closure.
# Tasks are GENERATED from data (open bill / CMS not done / high float) and closed by the
# data; they belong to no run, so a re-run never duplicates or loses one. Every task
# carries its store so a future store login is a WHERE clause, not a rebuild.
# ---------------------------------------------------------------------------

class StoreTask(Base):
    __tablename__ = "store_tasks"

    id = Column(Integer, primary_key=True)
    # Deterministic key, prefixed with the type because CMS not done and high float share
    # the `store|date` shape (open_bill:date|store|bill_no|mop, cms_not_done:store|date,
    # high_float:store|date). `generation` is 1 for the first task on a key; a task the operator closed
    # by hand is never reopened - if the condition is still true on a later sync a NEW task
    # (generation + 1) is raised.
    task_key = Column(String, nullable=False)
    generation = Column(Integer, nullable=False, default=1)
    task_type = Column(String, nullable=False, index=True)   # open_bill | cms_not_done | high_float
    store = Column(String, nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    bill_no = Column(String, nullable=True)
    mop = Column(String, nullable=True)
    amount = Column(Numeric, nullable=False, default=0)
    detail = Column(Text, nullable=True)                      # JSON: the type's own facts
    raised_on = Column(String, nullable=False)                # ISO date
    due_by = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open", index=True)  # open|replied|under_review|closed
    store_reply = Column(Text, nullable=True)
    replied_by = Column(String, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    ho_remark = Column(Text, nullable=True)
    closed_on = Column(String, nullable=True)
    closed_by = Column(String, nullable=True)                 # "resolved by the data" | a name
    close_reason = Column(Text, nullable=True)
    first_seen_run_id = Column(Integer, nullable=True)
    last_seen_run_id = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ux_store_task_key_gen", "task_key", "generation", unique=True),
        Index("ix_store_task_page", "store", "business_date", "task_type"),
    )


class StoreTaskEvent(Base):
    """Append-only history: raise, every reply, every remark, every status change, every
    closure. A reply or a remark is never overwritten silently - the task row shows the
    latest, this table keeps them all (rule 4)."""
    __tablename__ = "store_task_events"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("store_tasks.id"), nullable=False, index=True)
    at = Column(DateTime, nullable=False)
    by = Column(String, nullable=False)
    event = Column(String, nullable=False)   # raised|reply|remark|under_review|closed|auto_closed|amount_revised
    text = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# CMS Reco (2026-10-16) - one block per DEPOSIT DATE, chained per store. The page reads these
# precomputed rows only: the CMS slips (every Axis CMS row of the five types, with its store
# resolved through the pickup-code master) and the PTC cash deposits, per run.
# ---------------------------------------------------------------------------

class CmsSlip(Base):
    __tablename__ = "cms_slips"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    row_type = Column(String, nullable=False)                 # COL | ADJ.CR. | ADJ.DB. | ZPO
    dep_date = Column(String, nullable=True, index=True)      # ISO; NULL for ZPO (the file gives none)
    cr_date = Column(String, nullable=True)                   # CRDATE, ISO
    store = Column(String, nullable=False)                    # a store code, 'UNRESOLVED', or 'BNK' (its own pseudo-store)
    pickup_code = Column(String, nullable=True)
    location = Column(String, nullable=True)
    slip_no = Column(String, nullable=True)
    amount = Column(Numeric, nullable=False)                  # SLIPAMOUNT
    adj = Column(Numeric, nullable=True)                      # ADJ (column U)
    recovered = Column(Numeric, nullable=True)                # RECOVEREDAMOUNT (column X)
    source_file = Column(String, nullable=True)

    __table_args__ = (Index("ix_cms_slips_run_dep", "run_id", "row_type", "dep_date"),)


class CmsPtcDeposit(Base):
    """PTC 'Cash/CMS Deposit' per store per business date, carried POSITIVE (the source is negative
    and multiplied by -1)."""
    __tablename__ = "cms_ptc_deposits"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    dep_date = Column(String, nullable=False)
    store = Column(String, nullable=False)
    amount = Column(Numeric, nullable=False)

    __table_args__ = (Index("ix_cms_ptc_run_date", "run_id", "dep_date", "store"),)


class CmsRecoRemark(Base):
    """The current remark for a store over a period (from..to). History is `CmsRecoRemarkEvent`."""
    __tablename__ = "cms_reco_remarks"

    id = Column(Integer, primary_key=True)
    store = Column(String, nullable=False)
    period_from = Column(String, nullable=False)
    period_to = Column(String, nullable=False)
    remark = Column(Text, nullable=False)
    remark_by = Column(String, nullable=False)
    remark_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("ux_cms_reco_remark", "store", "period_from", "period_to", unique=True),)


class CmsRecoRemarkEvent(Base):
    """Append-only: every remark ever set (rule 4)."""
    __tablename__ = "cms_reco_remark_events"

    id = Column(Integer, primary_key=True)
    store = Column(String, nullable=False, index=True)
    period_from = Column(String, nullable=False)
    period_to = Column(String, nullable=False)
    remark = Column(Text, nullable=False)
    remark_by = Column(String, nullable=False)
    remark_at = Column(DateTime, nullable=False)


class CmsOverride(Base):
    """A hand correction of one CMS Report cell - an OVERLAY, never an edit of the bank's figure.

    Keyed on store | deposit date | column (same_day / next_day). Keeps the computed value, the override,
    the reason and who/when; a revert marks the row 'reverted' (never deletes it). Survives every re-run."""
    __tablename__ = "cms_overrides"

    id = Column(Integer, primary_key=True)
    store = Column(String, nullable=False)
    dep_date = Column(String, nullable=False)
    col = Column(String, nullable=False)                   # same_day | next_day
    computed_amount = Column(Numeric(18, 2), nullable=False)
    override_amount = Column(Numeric(18, 2), nullable=False)
    reason = Column(Text, nullable=False)
    entered_by = Column(String, nullable=False)
    entered_at = Column(DateTime, nullable=False)
    status = Column(String, nullable=False, default="active")     # active | reverted
    reverted_by = Column(String)
    reverted_at = Column(DateTime)
    batch_id = Column(String)                               # one bulk upload = one batch

    __table_args__ = (Index("ux_cms_override_active", "store", "dep_date", "col", unique=True,
                            sqlite_where=text("status = 'active'")),)


class CmsOverrideEvent(Base):
    """Append-only history of every override, change and revert (rule 4)."""
    __tablename__ = "cms_override_events"

    id = Column(Integer, primary_key=True)
    store = Column(String, nullable=False, index=True)
    dep_date = Column(String, nullable=False)
    col = Column(String, nullable=False)
    action = Column(String, nullable=False)                 # set | revert
    computed_amount = Column(Numeric(18, 2))
    override_amount = Column(Numeric(18, 2))
    reason = Column(Text)
    by = Column(String, nullable=False)
    at = Column(DateTime, nullable=False)


class MasterGap(Base):
    """An identifier the run could not resolve to a store (a Paytm MID, an Axis MID, a CMS pickup code - and the BNK
    pseudo-store's slips), with the run's own facts about it (2026-10-17). One row per run x source x identifier x
    location. Rows and value are the run's range only; first/last seen are over that range too. Whether the identifier
    is in the CURRENT master is read live by the Masters page, not stored here - a master is updated after a run."""
    __tablename__ = "master_gaps"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    source = Column(String, nullable=False)          # paytm_mid | axis_mid | pickup_code | cms_bnk
    identifier = Column(String, nullable=False)
    location = Column(String, nullable=True)
    rows = Column(Integer, nullable=False)
    value = Column(Numeric, nullable=False)
    first_seen = Column(String, nullable=True)
    last_seen = Column(String, nullable=True)


# ---------------------------------------------------------------------------
# Store-day and date-total aggregates (2026-09-20) — precomputed at RUN time,
# not view time (CLAUDE.md's performance rework: "the Run stage must write
# persisted aggregate tables ... the Summary page READS these"). These carry
# columns A..N EXCEPT F (digital_received) and G (digital_receivable): F
# depends on which bills are matched, and a manual match must cascade into G
# the instant it is recorded, with no re-run (CLAUDE.md 6.9b-ii item 1) — so
# F/G stay a small LIVE read over r1_bill_lines + active manual_matches (the
# same overlay-lookup shape store_sheet_source.build() already used), joined
# onto these stable, precomputed columns at view time. Every other column
# here (C, D as raw bill amount, E, H, I, J, K, L, M, N) is fixed the moment
# a run completes — no manual match or remark ever changes them — so
# recomputing them from PTC/CMS/opening-balance/POS on every page load, for
# every date in the range, was pure waste.
# ---------------------------------------------------------------------------

class StoreDayAgg(Base):
    """One row per store per business date per run — columns A..N minus F/G.
    `digital_sale` (D) is the raw bill amount total, stable across a manual
    match (only which bills are MATCHED changes, not what they total)."""
    __tablename__ = "store_day_agg"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    store = Column(String, nullable=False, index=True)
    total_sale = Column(Numeric, nullable=False)             # C = D + E
    digital_sale = Column(Numeric, nullable=False)            # D
    cash_sale = Column(Numeric, nullable=False)                # E
    opening_balance = Column(Numeric, nullable=False)          # H
    cash_deposit = Column(Numeric, nullable=False)              # I
    cash_received = Column(Numeric, nullable=False)              # J
    cash_receivable = Column(Numeric, nullable=False)             # K = I - J
    cash_expense_income = Column(Numeric, nullable=False)          # L
    closing_balance = Column(Numeric, nullable=False)                # M
    float_amount = Column(Numeric, nullable=False)                    # N = H - I
    # A cheap, precomputed flag for the Data quality section's header count
    # (K < 0 — Defect B) — so that count needs no per-view query over CMS,
    # never mind a re-derivation of K itself.
    defect_b = Column(Boolean, nullable=False, default=False, index=True)

    __table_args__ = (
        Index("ux_store_day_agg", "run_id", "business_date", "store",
              unique=True),
        Index("ix_store_day_agg_date_store", "run_id", "business_date", "store"),
    )


class TxnDayAgg(Base):
    """Per run, per business date, store and gateway (2026-10-13): how many transactions the
    gateway file holds and how many of those the ENGINE matched to a bill.

    This is what the Summary's "Mapped / Refunded / Unmapped" strip reads. Building that strip
    live meant loading the whole 3.7M-row gateway pool into pandas on every page load (144 s and
    6 GB for five months). The engine's own answer is fixed once the run is done; what a person
    does afterwards - a manual match, a customer refund - moves a handful of transactions, so
    those are applied live on top of this (queries.transaction_resolution_summary), keeping the
    no-re-run cascade (CLAUDE.md 6.9b-ii) without the pool read.

    `total_*` covers EVERY row of the gateway files (refunds, chargebacks and failures too -
    the strip's total always has); `mapped_*` is the matching-pool rows a bill consumed. `store`
    is NULL for a transaction whose MID resolves to no store."""
    __tablename__ = "txn_day_agg"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    business_date = Column(String, nullable=False)
    store = Column(String, nullable=True)
    gateway = Column(String, nullable=False)
    total_n = Column(Integer, nullable=False)
    total_v = Column(Numeric, nullable=False)
    mapped_n = Column(Integer, nullable=False)
    mapped_v = Column(Numeric, nullable=False)
    # 2026-10-16: the SALE population (SUCCESS / ACQUIRING rows a bill can map to) and how many of
    # those rows the engine physically consumed. NULL on a run aggregated before this existed.
    pool_n = Column(Integer, nullable=True)
    pool_v = Column(Numeric, nullable=True)
    consumed_n = Column(Integer, nullable=True)
    consumed_v = Column(Numeric, nullable=True)

    __table_args__ = (Index("ix_txn_day_agg_run_date_store", "run_id", "business_date", "store"),)


class MopDayAgg(Base):
    """One row per store per business date per run per MOP BUCKET (2026-09-18,
    the Summary particulars matrix) — card / upi / cash / gift_voucher /
    credit_note_issued / credit_note_received. `sale` is the gross MOP total
    for that bucket; nothing here changes with a manual match (a MOP's own
    sale amount is fixed the moment the bill is posted) — only "Collected"
    does, and that is read live from `r1_bill_lines` at page-load time, the
    same cascade-immediately shape `_live_digital_received()` already uses
    for column F, never stored here.

    The bucket a MOP belongs to is config-driven (`config.yaml`'s
    `mop_classification.*.bucket`), not hardcoded — see
    `mop_matrix.MOP_BUCKET` for the loader.
    """
    __tablename__ = "mop_day_agg"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    store = Column(String, nullable=False, index=True)
    bucket = Column(String, nullable=False, index=True)
    sale = Column(Numeric, nullable=False)

    __table_args__ = (
        Index("ux_mop_day_agg", "run_id", "business_date", "store", "bucket",
              unique=True),
        Index("ix_mop_day_agg_date_store", "run_id", "business_date", "store"),
    )


class DigitalModeAgg(Base):
    """One row per store per business date per run per digital payment
    MODE — card / upi / unclassified (2026-09-19, the particulars matrix's
    Card/UPI correctness fix, replacing a MOP-name-based split that put 74%
    of digital sale under "Card", not credible for Indian retail and
    contradicted by the transaction data itself — see
    docs/MOP_PAYMENT_MODE.md).

    Unlike `MopDayAgg`, this is NOT config-driven by MOP name — the mode is
    decided per bill line from the matched TRANSACTION's own identity
    (`mop_matrix.classify_digital_mode()`), because a MOP like "PAYTM EDC"
    is one shared terminal that accepts both card and UPI and cannot
    itself say which. A bill line the engine did not match has no known
    payment mode and is counted here as "unclassified", never guessed —
    `sale` for that bucket is the whole of the digital receivable (G) by
    construction, since only a matched line can ever be "card" or "upi".

    Built from the run's own `bill_lines` DataFrame directly (the same
    frame written to `r1_bill_lines`), not re-read from raw POS — see
    `store_sheet_source.persist_digital_mode_agg()`.
    """
    __tablename__ = "digital_mode_agg"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    store = Column(String, nullable=False, index=True)
    mode = Column(String, nullable=False, index=True)  # card | upi | unclassified
    sale = Column(Numeric, nullable=False)

    __table_args__ = (
        Index("ux_digital_mode_agg", "run_id", "business_date", "store", "mode",
              unique=True),
        Index("ix_digital_mode_agg_date_store", "run_id", "business_date", "store"),
    )


class DateTotalAgg(Base):
    """One row per business date per run — the consolidated ALL STORES row.
    Same column shape and the same F/G exclusion as StoreDayAgg, for the
    identical reason."""
    __tablename__ = "date_total_agg"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    business_date = Column(String, nullable=False, index=True)
    total_sale = Column(Numeric, nullable=False)
    digital_sale = Column(Numeric, nullable=False)
    cash_sale = Column(Numeric, nullable=False)
    opening_balance = Column(Numeric, nullable=False)
    cash_deposit = Column(Numeric, nullable=False)
    cash_received = Column(Numeric, nullable=False)
    cash_receivable = Column(Numeric, nullable=False)
    cash_expense_income = Column(Numeric, nullable=False)
    closing_balance = Column(Numeric, nullable=False)
    float_amount = Column(Numeric, nullable=False)

    __table_args__ = (
        Index("ux_date_total_agg", "run_id", "business_date", unique=True),
    )


# ---------------------------------------------------------------------------
# Customer refunds (2026-09-20) — a THIRD way an unmapped transaction closes,
# alongside "matched to a bill" and "still open". A refund is never a bill
# match: the store team raises a Zoho request, the customer is paid back,
# and only then is the refund data supplied here. The transaction moves out
# of "Unmapped" into its own resolution, but it is NOT counted as mapped to
# a bill and NEVER closes a bill (rule 3: no write-offs by another name).
#
# One row per transaction — `gateway`/`transaction_ref` is the natural key
# (rule: "refunds survive every re-run, keyed on gateway | transaction_id"),
# a plain unique index rather than the manual_matches lifecycle machinery:
# a refund has no engine to reconcile against on a re-run (a gateway
# transaction's own identity does not change between runs), so there is no
# "superseded/conflicted" case to handle the way manual matches need one.
# ---------------------------------------------------------------------------

class TransactionRefund(Base):
    __tablename__ = "transaction_refunds"

    id = Column(Integer, primary_key=True)
    gateway = Column(String, nullable=False)             # paytm | axis | bajaj
    transaction_ref = Column(String, nullable=False)
    business_date = Column(String, nullable=False)        # the transaction's own date
    store = Column(String, nullable=True)
    amount = Column(Numeric, nullable=True)

    zoho_no = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=True)
    refund_date = Column(String, nullable=True)           # from the upload's own "date" column
    # "pending" (refundable, not yet paid) or "released" (paid back) — the
    # two counts the operator's own instruction says must stay separate,
    # never merged into one "refunded" figure.
    refund_status = Column(String, nullable=False, default="pending")

    uploaded_by = Column(String, nullable=False)
    uploaded_at = Column(DateTime, nullable=False)
    batch_id = Column(Integer, nullable=True, index=True)
    source = Column(String, nullable=True)                # "inline" | "upload" (2026-10-16)

    __table_args__ = (
        Index("ux_refund_txn", "gateway", "transaction_ref", unique=True),
        Index("ux_refund_zoho", "zoho_no", unique=True),
    )


class RefundEvent(Base):
    """Append-only history of every refund recorded and every refund reversed (rule 4). A reversal
    removes the live row from `transaction_refunds` (so the transaction and its Zoho number are free
    again) but the full record survives here."""
    __tablename__ = "refund_events"

    id = Column(Integer, primary_key=True)
    action = Column(String, nullable=False)               # recorded | reversed
    gateway = Column(String, nullable=False)
    transaction_ref = Column(String, nullable=False, index=True)
    business_date = Column(String, nullable=True)
    store = Column(String, nullable=True)
    amount = Column(Numeric, nullable=True)
    zoho_no = Column(String, nullable=True)
    customer_name = Column(String, nullable=True)
    refund_date = Column(String, nullable=True)
    source = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    by = Column(String, nullable=False)
    at = Column(DateTime, nullable=False)


class UploadedFile(Base):
    """The audit trail for the Upload page (2026-09-30) — never deleted, only
    marked. A withdrawn upload's row survives so the record of what was
    supplied and later removed is never lost (rule 4); only the physical
    file on disk is removed on delete."""
    __tablename__ = "uploaded_files"

    id = Column(Integer, primary_key=True)
    uploaded_at = Column(DateTime, nullable=False, index=True)
    source_key = Column(String, nullable=False, index=True)   # config.yaml sources: key
    report_name = Column(String, nullable=False)              # display label at upload time
    month = Column(String, nullable=True)                     # 'YYYY-MM', null for a master
    original_file_name = Column(String, nullable=False)
    stored_path = Column(Text, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    row_count = Column(Integer, nullable=True)
    date_range_start = Column(String, nullable=True)
    date_range_end = Column(String, nullable=True)
    uploaded_by = Column(String, nullable=False)
    status = Column(String, nullable=False, default="stored")  # stored | deleted
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(String, nullable=True)


class MasterVersion(Base):
    __tablename__ = "master_versions"

    id = Column(Integer, primary_key=True)
    master_name = Column(String, nullable=False)  # e.g. mid_axis, mid_paytm, pickup_code
    source_file_path = Column(Text, nullable=False)
    source_file_mtime = Column(DateTime, nullable=True)
    loaded_at = Column(DateTime, nullable=False)      # effective_from for this snapshot
    effective_to = Column(DateTime, nullable=True)    # null = still current
    row_count = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    content_hash = Column(String, nullable=False)     # sha256 of the row content — lets
                                                       # a reload detect "unchanged" and
                                                       # skip creating a duplicate version


class MasterRow(Base):
    __tablename__ = "master_rows"

    id = Column(Integer, primary_key=True)
    master_version_id = Column(Integer, ForeignKey("master_versions.id"), nullable=False)
    row_json = Column(Text, nullable=False)  # the raw row, e.g. {"MID": "...", "Store Code": "..."}


def get_engine(db_path: Path = DEFAULT_DB_PATH):
    """Every caller — scripts, tests, and the dashboard alike — gets a
    thread-safe engine: `check_same_thread=False` plus `NullPool` (no
    connection is ever held open and handed to a different thread than the
    one that opened it) is what makes it SAFE for the dashboard to cache
    this engine object across Streamlit reruns, which each run in their own
    thread. A live SESSION must never be cached the same way — see
    `dashboard_common.db_session()` — this only makes the ENGINE itself
    shareable.

    A short-lived script or test still works identically: NullPool simply
    means each checkout opens a fresh sqlite3 connection rather than
    reusing one from a pool, which costs nothing worth noticing against a
    local SQLite file.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        # WAL matters because the Run stage writes while the UI reads
        # (CLAUDE.md 6.14's two-phase design) — WAL lets a reader and a
        # writer proceed without blocking each other. busy_timeout gives a
        # writer time to finish rather than the reader raising "database is
        # locked" immediately.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine=None, db_path: Path = DEFAULT_DB_PATH):
    """Create all tables if they don't exist. Never drops or alters existing data."""
    engine = engine or get_engine(db_path)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


# New nullable columns added to an existing table after it was first created.
# `create_all` only creates missing TABLES, never adds a column to one that
# already exists — an existing db/reco.sqlite from before 2026-09-13 would
# otherwise lack date_diff_days/match_note and every read of them would raise
# `OperationalError: no such column`. Each entry is (table, column, DDL type);
# applied with a plain ALTER TABLE, which SQLite supports for adding a
# nullable column, and skipped if the column is already there (a fresh db
# created via create_all already has it, so this is a no-op there).
_ADDED_COLUMNS = [
    ("r1_bill_lines", "date_diff_days", "INTEGER"),
    ("r1_bill_lines", "match_note", "TEXT"),
    ("r1_bill_lines", "payment_mode", "TEXT"),
    ("r1_bill_lines", "bill_time", "TEXT"),
    ("r1_bill_lines", "created_by", "TEXT"),
    ("exceptions", "transaction_time", "TEXT"),
    ("runs", "current_stage", "TEXT"),
    ("runs", "heartbeat_at", "TIMESTAMP"),
    ("runs", "progress_detail", "TEXT"),
    ("txn_day_agg", "pool_n", "INTEGER"),
    ("txn_day_agg", "pool_v", "NUMERIC"),
    ("txn_day_agg", "consumed_n", "INTEGER"),
    ("txn_day_agg", "consumed_v", "NUMERIC"),
    ("manual_matches", "source", "TEXT"),
    ("transaction_refunds", "source", "TEXT"),
]


# Indexes added to an existing table after it was first created (`create_all` never adds an index to a table
# that already exists). `IF NOT EXISTS`, so a fresh database and an upgraded one end up the same.
#   ix_r1_matched_txn (2026-10-16): "which bill already holds this transaction?" is asked for every pasted or
#   uploaded match and every refund. With no index it scanned the 3.7M-row table (seconds PER ROW - an upload
#   of a few thousand rows did not finish); with it, it is a seek.
_ADDED_INDEXES = [
    ("ix_r1_matched_txn", "CREATE INDEX IF NOT EXISTS ix_r1_matched_txn ON r1_bill_lines (matched_txn_ref)"),
]


def _add_missing_columns(engine):
    with engine.connect() as conn:
        for _name, ddl in _ADDED_INDEXES:
            conn.exec_driver_sql(ddl)
            conn.commit()
        for table, column, ddl_type in _ADDED_COLUMNS:
            existing = {row[1] for row in
                       conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
                conn.commit()


def get_session(engine=None, db_path: Path = DEFAULT_DB_PATH):
    engine = engine or get_engine(db_path)
    Session = sessionmaker(bind=engine)
    return Session()
