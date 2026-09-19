"""Store float, and whether it is sitting too high (2026-09-13, formula and
threshold corrected 2026-09-15).

**Float = opening balance LESS the cash deposited that business date. It is
NOT the closing balance.** The deposit made on day D is the PRIOR day's
cash, so float measures what the store held back instead of banking, not
what it holds at the end of the day (which includes that same day's cash
sale, not yet even eligible for deposit). The original build read the
sheet's closing balance (column M) and called it float — wrong, caught and
corrected on the operator's explicit instruction, with four real rows
(ABD/AGT/AJL/ALC, 2026-07-31) verified by hand against the corrected
formula before anything else in this module changed.

The store sheet already computes exactly this figure, as column N
(`store_sheet.COLUMNS`: `("N", "float_amount", "Float amount")` =
`opening_balance - cash_deposit`; `cash_deposit` is carried POSITIVE on the
sheet since 2026-09-17 — see CLAUDE.md §6.9d — so `float_amount` there
already equals opening minus the deposit's magnitude). This module reads
that column directly rather than recomputing it, consistent with never
recomputing cash movement itself — the earlier float-vs-closing-balance bug
was reading the wrong ALREADY-COMPUTED column (M instead of N), not an
arithmetic error of this module's own, and is unaffected by column I's own
later sign correction.

Pure engine functions (§8): DataFrames in, DataFrames out, no Streamlit, no
database session held open across a computation. `set_float_remark` and the
history readers are the one place this module touches the database, and
only for the OPERATOR's own remarks — see the module docstring on
`db.FloatRemark` for why the auto-remark itself is never a database row.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from . import masters
from .db import FloatRemark, FloatRemarkEvent

logger = logging.getLogger("mop_reco")

# Rs 10,000 — the operator's own figure, corrected 2026-09-15 from a much
# higher earlier default. That earlier number must not reappear anywhere in
# this module or the Float page.
DEFAULT_FLOAT_THRESHOLD = 10_000.0

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]

UPLOAD_COLUMNS = ["store", "business_date", "remark"]


# ---------------------------------------------------------------------------
# The threshold — config-driven, with an optional per-store override
# ---------------------------------------------------------------------------

def threshold_for(store: str, config_raw: dict) -> float:
    """Rs 10,000 by default (`float_tracker.float_threshold` in
    config.yaml — corrected 2026-09-15 from an earlier, much higher
    default), with an optional per-store override in
    `float_tracker.threshold_overrides`.

    The supplied store master (`SR.No · CURRENTLY ACTICVE · SITES NAMES ·
    Store Code`, §5) carries no threshold column of its own — this is NOT
    read from that file. The override lives in config, held there rather
    than guessed into a master file column that does not exist, per rule 5.
    """
    section = (config_raw or {}).get("float_tracker", {}) or {}
    overrides = section.get("threshold_overrides", {}) or {}
    if store in overrides:
        return float(overrides[store])
    return float(section.get("float_threshold", DEFAULT_FLOAT_THRESHOLD))


# ---------------------------------------------------------------------------
# The weekend/holiday auto-remark — computed, never stored (see db.py)
# ---------------------------------------------------------------------------

def second_and_fourth_saturdays(year: int, month: int) -> set:
    """The 2nd and 4th Saturday of the calendar month — counted by which
    Saturday-of-the-month each one is, never by a fixed date range (a month
    starting on a Saturday would otherwise be counted wrong)."""
    days_in_month = monthrange(year, month)[1]
    saturdays = [date(year, month, d) for d in range(1, days_in_month + 1)
                if date(year, month, d).weekday() == 5]
    picked = set()
    if len(saturdays) >= 2:
        picked.add(saturdays[1])
    if len(saturdays) >= 4:
        picked.add(saturdays[3])
    return picked


def auto_remark_for(d: date) -> str:
    """"Weekend", "Holiday", or "" (none). A high float is EXPECTED — no
    bank pickup — on a Sunday, the 2nd/4th Saturday of the month, or a
    listed public holiday.

    **No holiday calendar has been supplied** (`config.yaml`:
    `holiday_calendar: null`, the same gap CLAUDE.md §6.5's due-date working
    day logic already lives with) — `masters._HOLIDAY_SET` stays empty until
    one is, so "Holiday" cannot fire today; this is the SAME calendar
    `masters.is_working_day()` already uses, not a second one, so the day a
    real calendar is loaded this starts working with no change here.
    """
    if d.weekday() == 6:  # Sunday
        return "Weekend"
    if d.weekday() == 5 and d in second_and_fourth_saturdays(d.year, d.month):
        return "Weekend"
    if d in masters._HOLIDAY_SET:
        return "Holiday"
    return ""


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

# Column order matches the operator's own spec exactly: store, date, day,
# opening, cash sale, deposit, FLOAT, threshold, high-float?, days held high,
# cash received, closing balance — closing balance is kept so the arithmetic
# stays checkable by eye, but it is no longer the figure the threshold is
# tested against. `negative_float` is not one of the operator's named
# columns; it is carried alongside so the page can flag a negative-float row
# without a caller having to recompute `float_amount < 0` itself.
FIELDS = ["store", "business_date", "day_name", "opening_balance", "cash_sale",
         "cash_deposit", "float_amount", "float_threshold", "high_float",
         "days_held_high", "cash_received", "closing_balance", "negative_float"]


def build_float_grid(sheet: pd.DataFrame, config_raw: dict) -> pd.DataFrame:
    """One row per store per business date.

    Float = opening - cash deposit for the business date. It is NOT the
    closing balance. The deposit on day D is the prior day's cash, so float
    measures cash withheld from banking. Threshold defaults to Rs 10,000 per
    store, per-store override in the store master.

    Read directly from the store sheet's own `float_amount` column
    (= H - I, §6.9d) — never recomputed here — so this can never drift from
    the sheet that operators already reconcile against. `cash_deposit` is
    already stored positive on the sheet (2026-09-17) and needs no display
    correction here any more.

    **A negative float is never floored or dropped.** `deposit > opening`
    means either a catch-up deposit (more than one day's cash banked at
    once) or a deposit attributed to the wrong date — either way it is a
    real number that must stay visible, not zero. See
    `negative_float_rows()` for the drill-down.

    `days_held_high` is consecutive business days (calendar days in the
    loaded range, in order — the store sheet has no gaps) this store's
    FLOAT — not closing balance — has stood above its threshold, UP TO AND
    INCLUDING this date; it resets to 0 the first day float drops back at or
    below threshold.
    """
    if sheet.empty:
        return pd.DataFrame(columns=FIELDS)

    grid = sheet[["date", "store", "opening_balance", "cash_sale",
                 "cash_deposit", "float_amount", "cash_received",
                 "closing_balance"]].copy()
    grid = grid.rename(columns={"date": "business_date"})
    grid["day_name"] = grid["business_date"].map(
        lambda d: DAY_NAMES[date.fromisoformat(d).weekday()])
    grid["float_threshold"] = grid["store"].map(
        lambda s: threshold_for(s, config_raw))
    grid["high_float"] = grid["float_amount"] > grid["float_threshold"]
    grid["negative_float"] = grid["float_amount"] < 0

    grid = grid.sort_values(["store", "business_date"]).reset_index(drop=True)
    grid["days_held_high"] = 0
    for store, block in grid.groupby("store", sort=False):
        streak = 0
        streaks = []
        for is_high in block["high_float"]:
            streak = streak + 1 if is_high else 0
            streaks.append(streak)
        grid.loc[block.index, "days_held_high"] = streaks

    return grid[FIELDS].reset_index(drop=True)


def negative_float_rows(grid: pd.DataFrame) -> pd.DataFrame:
    """Every store-date where float is negative — deposit exceeded opening,
    a catch-up deposit or a misdated one. Sorted most-negative first so the
    largest anomaly leads. Never floored, never excluded from the grid
    itself (rule 4) — this is the drill-down alongside it."""
    columns = ["store", "business_date", "opening_balance", "cash_deposit",
              "float_amount"]
    if grid is None or grid.empty:
        return pd.DataFrame(columns=columns)
    rows = grid.loc[grid["negative_float"], columns]
    return rows.sort_values("float_amount").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Operator remarks — persisted, keyed on (store, business_date), history kept
# ---------------------------------------------------------------------------

def set_float_remark(session, store: str, business_date: str, remark: str,
                     remark_by: str) -> FloatRemark:
    """Record an operator's remark. Upserts the CURRENT row (so a read is one
    lookup, not a scan of history) and always appends to the event log
    (rule 4: never a silent overwrite — the prior text is kept, not just the
    prior existence)."""
    remark = (remark or "").strip()
    if not remark:
        raise ValueError("A remark cannot be blank.")
    if not remark_by:
        raise ValueError("A remark must record who wrote it.")

    now = datetime.now()
    current = (session.query(FloatRemark)
              .filter(FloatRemark.store == store,
                      FloatRemark.business_date == str(business_date))
              .first())
    if current is None:
        current = FloatRemark(store=store, business_date=str(business_date),
                              remark=remark, remark_by=remark_by, remark_at=now)
        session.add(current)
    else:
        current.remark = remark
        current.remark_by = remark_by
        current.remark_at = now

    session.add(FloatRemarkEvent(store=store, business_date=str(business_date),
                                 remark=remark, remark_by=remark_by,
                                 remark_at=now))
    session.commit()
    logger.info(f"float_tracker: remark set for {store}/{business_date} by {remark_by}")
    return current


def float_remarks_for_range(session, date_from=None, date_to=None,
                            store: str = None) -> dict:
    """{(store, business_date): FloatRemark} for every OPERATOR remark on
    record in range — never the computed auto-remark, which is not a row."""
    query = session.query(FloatRemark)
    if date_from is not None:
        query = query.filter(FloatRemark.business_date >= str(date_from))
    if date_to is not None:
        query = query.filter(FloatRemark.business_date <= str(date_to))
    if store:
        query = query.filter(FloatRemark.store == store)
    return {(r.store, r.business_date): r for r in query.all()}


def float_remark_history(session, store: str, business_date: str) -> pd.DataFrame:
    """Every remark ever recorded for this store-date, oldest first — the
    current one is simply the last row."""
    rows = (session.query(FloatRemarkEvent)
           .filter(FloatRemarkEvent.store == store,
                   FloatRemarkEvent.business_date == str(business_date))
           .order_by(FloatRemarkEvent.remark_at.asc()).all())
    return pd.DataFrame([{
        "remark_at": r.remark_at, "remark_by": r.remark_by, "remark": r.remark,
    } for r in rows])


def with_remarks(grid: pd.DataFrame, remarks: dict) -> pd.DataFrame:
    """Attach the effective remark/remark_by/remark_at to every row: the
    operator's own remark if one is on record, otherwise the computed
    auto-remark (never stored — see module docstring), otherwise blank. Adds
    `remark`, `remark_by`, `remark_at` (None for a computed auto-remark, since
    it is never a database row), `is_auto` (True only for a computed,
    never-yet-overwritten auto-remark, so the UI can grey it out rather than
    invite editing over it silently)."""
    out = grid.copy()
    remark_col, by_col, at_col, auto_col = [], [], [], []
    for row in out.itertuples():
        recorded = remarks.get((row.store, row.business_date))
        if recorded is not None:
            remark_col.append(recorded.remark)
            by_col.append(recorded.remark_by)
            at_col.append(recorded.remark_at)
            auto_col.append(False)
            continue
        auto = auto_remark_for(date.fromisoformat(row.business_date))
        remark_col.append(auto)
        by_col.append("system" if auto else "")
        at_col.append(None)
        auto_col.append(bool(auto))
    out["remark"] = remark_col
    out["remark_by"] = by_col
    out["remark_at"] = at_col
    out["is_auto"] = auto_col
    return out


# ---------------------------------------------------------------------------
# Bulk upload — template: store, business_date, remark
# ---------------------------------------------------------------------------

@dataclass
class FloatRowOutcome:
    row_number: int
    store: str
    business_date: str
    remark: str
    verdict: str            # "accept" | "reject"
    why: list = field(default_factory=list)


@dataclass
class FloatUploadPreview:
    rows: list = field(default_factory=list)

    @property
    def accepted(self) -> list:
        return [r for r in self.rows if r.verdict == "accept"]

    @property
    def rejected(self) -> list:
        return [r for r in self.rows if r.verdict == "reject"]

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "row": r.row_number, "store": r.store,
            "business_date": r.business_date, "remark": r.remark,
            "verdict": r.verdict, "why": " | ".join(r.why),
        } for r in self.rows])


def upload_template() -> pd.DataFrame:
    return pd.DataFrame([{
        "store": "ABD", "business_date": "2026-07-20",
        "remark": "Bank pickup delayed, agency confirmed collection tomorrow",
    }], columns=UPLOAD_COLUMNS)


def preview_upload(frame: pd.DataFrame, grid: pd.DataFrame) -> FloatUploadPreview:
    """Validate EVERY row and apply nothing. A row is rejected when the
    store+date is not in the currently loaded/filtered grid at all, or is
    not flagged high float — rule 5, nothing here is guessed onto a
    store-date that was never actually high."""
    preview = FloatUploadPreview()
    missing = [c for c in UPLOAD_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"The upload is missing column(s): {', '.join(missing)}. "
                         f"Expected exactly: {', '.join(UPLOAD_COLUMNS)}")

    high = {(r.store, r.business_date) for r in
           grid.loc[grid["high_float"]].itertuples()}
    known_stores = set(grid["store"].unique())
    known_dates = set(grid["business_date"].unique())

    for position, raw in enumerate(frame.to_dict("records"), start=2):
        store = str(raw.get("store") or "").strip()
        business_date = str(raw.get("business_date") or "").strip()
        remark = str(raw.get("remark") or "").strip()
        why = []

        if store not in known_stores:
            why.append(f"store {store!r} is not in the loaded range")
        if business_date not in known_dates:
            why.append(f"business_date {business_date!r} is not in the loaded range")
        if not why and (store, business_date) not in high:
            why.append("this store-date is not flagged high float")
        if not remark:
            why.append("remark is blank")

        verdict = "reject" if why else "accept"
        preview.rows.append(FloatRowOutcome(position, store, business_date,
                                            remark, verdict, why))
    return preview


def apply_upload(session, preview: FloatUploadPreview, operator: str) -> tuple:
    """Apply every accepted row. Nothing partially applies to a single row —
    each row is its own `set_float_remark` call, atomic per row; a failure
    on one row does not touch the others (each is independent, unlike a
    manual-match batch there is no cross-row consumption to protect)."""
    applied, failed = [], []
    for row in preview.accepted:
        try:
            entry = set_float_remark(session, row.store, row.business_date,
                                     row.remark, operator)
            applied.append(entry)
        except ValueError as exc:
            failed.append((row, str(exc)))
    return applied, failed
