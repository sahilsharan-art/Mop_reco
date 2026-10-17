"""Store Task - items assigned to stores, their replies, tracked to closure (2026-10-14).

THREE task types are generated from data - nobody creates them by hand:

  open_bill     one per open bill (no matched transaction, by the engine or by a person)
  cms_not_done  one per store-day where cash was due for deposit and no deposit was credited
  high_float    one per store-day where float is above the threshold and the day has no
                weekend/holiday auto-remark

A task CLOSES ITSELF the moment its underlying condition clears ("resolved by the data"). A
store's reply never closes a task; only the fact, or an explicit close by a person, does.
A task a person closed is never reopened - if the condition is still true on a later sync a
NEW task (generation + 1) is raised.

Reads only precomputed rows: `store_day_agg` for the cash side, the unmatched slice of
`r1_bill_lines` (a few thousand rows, an index seek - never a scan of the month) for bills.
Never alters a bill, a match or a balance.

Pure of Streamlit (CLAUDE.md section 8). Every function that needs "today" takes it as a
parameter so the tests - and rule 8 - never depend on the wall clock.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from . import cms_reco, float_tracker, masters
from .db import ManualMatch, R1BillLine, StoreDayAgg, StoreTask, StoreTaskEvent

OPEN_BILL, CMS_NOT_DONE, HIGH_FLOAT = "open_bill", "cms_not_done", "high_float"
TYPES = (OPEN_BILL, CMS_NOT_DONE, HIGH_FLOAT)
TYPE_LABEL = {OPEN_BILL: "Open bill", CMS_NOT_DONE: "CMS not done", HIGH_FLOAT: "High float"}
LABEL_TO_TYPE = {v: k for k, v in TYPE_LABEL.items()}

OPEN, REPLIED, UNDER_REVIEW, CLOSED = "open", "replied", "under_review", "closed"
STATUS_LABEL = {OPEN: "Open", REPLIED: "Replied", UNDER_REVIEW: "Under review", CLOSED: "Closed"}
LABEL_TO_STATUS = {v: k for k, v in STATUS_LABEL.items()}
LIVE_STATUSES = (OPEN, REPLIED, UNDER_REVIEW)

RESOLVED_BY_DATA = "resolved by the data"
DEFAULT_SLA_WORKING_DAYS = 3
EPS = 0.005   # nil tolerance on a rupee figure that came through float arithmetic

# What each task asks the store - written for the person who receives it.
QUESTION = {
    OPEN_BILL: "What happened to this payment? The bill has no matching transaction.",
    CMS_NOT_DONE: "Why was the cash deposit not made or not credited?",
    HIGH_FLOAT: "Why is this cash being held at the store?",
}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def is_banking_day(d: date) -> bool:
    """A day the agency collects: not a Sunday, not a 2nd/4th Saturday, not a listed holiday -
    exactly the float page's own weekend/holiday rule, so the two cannot disagree."""
    return float_tracker.auto_remark_for(d) == ""


def next_banking_day(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_banking_day(n):
        n += timedelta(days=1)
    return n


def add_working_days(d: date, n: int) -> date:
    """`n` working days after `d` (CLAUDE.md 6.5: weekends and listed holidays skipped)."""
    cur, left = d, int(n)
    while left > 0:
        cur += timedelta(days=1)
        if masters.is_working_day(cur):
            left -= 1
    return cur


def working_days_between(start: date, end: date) -> int:
    """Working days after `start` up to and including `end` (0 when end <= start)."""
    n, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if masters.is_working_day(cur):
            n += 1
    return n


def sla_days(config_raw: dict, task_type: str) -> int:
    section = ((config_raw or {}).get("store_tasks") or {}).get("sla_working_days") or {}
    return int(section.get(task_type, DEFAULT_SLA_WORKING_DAYS))


def display_id(task_id: int) -> str:
    return f"ST-{int(task_id):06d}"


def parse_id(value) -> int | None:
    """'ST-000123', 'st-123', 123, '123' -> 123; anything else -> None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(?i)(?:st-?)?0*(\d+)(?:\.0)?", text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Candidates - the conditions, read from precomputed rows
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    key: str
    task_type: str
    store: str
    business_date: str
    amount: float
    bill_no: str = None
    mop: str = None
    detail: dict = field(default_factory=dict)


@dataclass
class Generated:
    """What one type's conditions say right now. `cleared` are conditions that arose in the
    loaded history and have ALREADY cleared against current data - they are counted, never
    persisted as tasks (a task born closed would only be clutter)."""
    active: list = field(default_factory=list)
    cleared_n: int = 0
    cleared_value: float = 0.0


def _run_dates(session, run_id: int):
    """First and last business date the run covers: its store-day aggregate, or - for a run
    that predates it - its bill lines."""
    for model in (StoreDayAgg, R1BillLine):
        col = model.business_date
        lo = session.query(col).filter(model.run_id == run_id).order_by(col).first()
        if lo:
            hi = session.query(col).filter(model.run_id == run_id).order_by(col.desc()).first()
            return lo[0], hi[0]
    return None, None


def open_bill_candidates(session, run_id: int) -> Generated:
    """Every bill line the engine left unmatched and no active manual match closes, merged to
    one task per (date, store, bill_no, mop) - the same grain as the bill exception key."""
    manual = {b for (b,) in session.query(ManualMatch.bill_no)
              .filter(ManualMatch.lifecycle == "active", ManualMatch.status == "matched_manual").all() if b}
    rows = (session.query(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no,
                          R1BillLine.mop, R1BillLine.amount, R1BillLine.bill_time,
                          R1BillLine.created_by, R1BillLine.status)
            .filter(R1BillLine.run_id == run_id, R1BillLine.matched.is_(False)).all())
    merged: dict = {}
    for bdate, store, bill_no, mop, amount, bill_time, created_by, status in rows:
        if bill_no in manual or not store or not bill_no:
            continue
        key = f"{bdate}|{store}|{bill_no}|{mop}"
        c = merged.get(key)
        if c is None:
            merged[key] = Candidate(key, OPEN_BILL, store, bdate, float(amount or 0), bill_no, mop,
                                    {"bill_time": bill_time, "created_by": created_by, "status": status})
        else:
            c.amount += float(amount or 0)
    return Generated(active=list(merged.values()))


def _store_day_frame(session, run_id: int) -> pd.DataFrame:
    q = session.query(StoreDayAgg.store, StoreDayAgg.business_date, StoreDayAgg.cash_sale,
                      StoreDayAgg.opening_balance, StoreDayAgg.cash_deposit, StoreDayAgg.cash_received,
                      StoreDayAgg.float_amount).filter(StoreDayAgg.run_id == run_id)
    df = pd.DataFrame(q.all(), columns=["store", "business_date", "cash_sale", "opening_balance",
                                        "cash_deposit", "cash_received", "float_amount"])
    for c in ("cash_sale", "opening_balance", "cash_deposit", "cash_received", "float_amount"):
        df[c] = df[c].astype(float)
    return df.sort_values(["store", "business_date"]).reset_index(drop=True)


def high_float_candidates(sf: pd.DataFrame, config_raw: dict) -> Generated:
    """A store-day whose float is above its threshold and that carries no weekend/holiday
    auto-remark. It is STILL ACTIVE while the store's high-float streak is unbroken to the last
    loaded date; the first later day at or below the threshold clears it (float dropped)."""
    out = Generated()
    if sf.empty:
        return out
    for store, block in sf.groupby("store", sort=False):
        thr = float_tracker.threshold_for(store, config_raw)
        high = (block["float_amount"] > thr).to_numpy()
        dates = block["business_date"].tolist()
        # a later day at/below the threshold anywhere after position i clears position i
        later_low = pd.Series(~high[::-1]).cummax().to_numpy()[::-1]
        later_low = list(later_low[1:]) + [False]
        streak = 0
        for i, is_high in enumerate(high):
            streak = streak + 1 if is_high else 0
            if not is_high:
                continue
            d = date.fromisoformat(dates[i])
            if float_tracker.auto_remark_for(d):
                continue
            amount = float(block["float_amount"].iloc[i])
            if later_low[i]:
                out.cleared_n += 1
                out.cleared_value += amount
                continue
            out.active.append(Candidate(
                f"{store}|{dates[i]}", HIGH_FLOAT, store, dates[i], amount,
                detail={"float": amount, "threshold": thr, "days_held_high": streak,
                        "opening": float(block["opening_balance"].iloc[i]),
                        "deposit": float(block["cash_deposit"].iloc[i])}))
    return out


def cms_candidates(session, run_id: int, sf: pd.DataFrame, config_raw: dict) -> Generated:
    """CMS not done - two ways a store-day can carry it, merged onto one task per
    `store|business_date`:

    NO DEPOSIT   the day's cash sale was due for deposit by the next banking day (the agency's
                 own working pattern - not Sunday, not a 2nd/4th Saturday, not a holiday) and no
                 deposit voucher appears in that window. Expected = that day's cash sale. It
                 clears when the store later makes a deposit that the agency credited.
    NOT CREDITED a deposit that the running CMS ledger (cms_reco) still shows outstanding past
                 its credit due date (T+1 working day - the conservative bound; no per-store
                 cycle is in any master, see config store_tasks). The outstanding balance is
                 allocated to the NEWEST deposits first, because the agency clears oldest
                 first. Expected = the part of that deposit still outstanding.

    Using the running ledger rather than the per-day K (I - J) is deliberate: K shows a phantom
    shortfall wherever a credit was booked on a different date or store than its deposit
    (Defect B), and a task to the store for the agency's attribution error is noise.
    """
    out = Generated()
    if sf.empty:
        return out
    last = date.fromisoformat(sf["business_date"].max())
    credit_lag = int(((config_raw or {}).get("store_tasks") or {}).get("cms_credit_due_working_days", 1))
    merged: dict = {}

    def add(store, bdate, amount, part):
        c = merged.get(f"{store}|{bdate}")
        if c is None:
            merged[f"{store}|{bdate}"] = Candidate(f"{store}|{bdate}", CMS_NOT_DONE, store, bdate, amount,
                                                   detail={"reasons": [part]})
        else:
            c.amount += amount
            c.detail["reasons"].append(part)

    # --- NOT CREDITED: the running ledger's outstanding balance, newest deposit first
    ledger = cms_reco.build_ledger(session, run_id)
    final = (ledger.sort_values("business_date").groupby("store").tail(1).set_index("store")
             ["closing_receivable"].astype(float).to_dict() if not ledger.empty else {})
    for store, block in sf.groupby("store", sort=False):
        left = float(final.get(store, 0.0))
        if left <= EPS:
            continue
        for _, r in block[block["cash_deposit"] > EPS].iloc[::-1].iterrows():
            if left <= EPS:
                break
            take = min(float(r["cash_deposit"]), left)
            left -= take
            d = date.fromisoformat(r["business_date"])
            due = add_working_days(d, credit_lag)
            if last <= due:           # still in transit - not late yet
                continue
            add(store, r["business_date"], take, {
                "kind": "not_credited", "deposit": float(r["cash_deposit"]), "outstanding": take,
                "credit_due": due.isoformat(), "days_outstanding": working_days_between(due, last)})

    # --- NO DEPOSIT
    for store, block in sf.groupby("store", sort=False):
        dates = [date.fromisoformat(x) for x in block["business_date"]]
        dep = dict(zip(dates, block["cash_deposit"].astype(float)))
        cred = dict(zip(dates, block["cash_received"].astype(float)))
        credited_deposit_days = sorted(d for d in dates if dep[d] > EPS and cred[d] + EPS >= dep[d])
        for d, sale in zip(dates, block["cash_sale"].astype(float)):
            if sale <= EPS:
                continue
            due = next_banking_day(d)
            if due > last:
                continue
            window = [x for x in dates if d < x <= due]
            if any(dep[x] > EPS for x in window):
                continue
            # cleared by a later deposit that the agency credited
            clear = next((x for x in credited_deposit_days if x > due), None)
            if clear is not None:
                out.cleared_n += 1
                out.cleared_value += sale
                continue
            add(store, d.isoformat(), sale, {
                "kind": "no_deposit", "cash_sale": sale, "deposit_due": due.isoformat(),
                "days_outstanding": working_days_between(due, last)})
    out.active = list(merged.values())
    for c in out.active:
        c.detail["days_outstanding"] = max(p.get("days_outstanding", 0) for p in c.detail["reasons"])
    return out


def _stored_key(c: Candidate) -> str:
    """The deterministic key as stored: type-prefixed, because CMS not done and high float
    share the `store|business_date` shape and must not collide."""
    return f"{c.task_type}:{c.key}"


def generate(session, run_id: int, config_raw: dict) -> dict:
    """{task_type: Generated} for the run, from precomputed rows."""
    sf = _store_day_frame(session, run_id)
    return {OPEN_BILL: open_bill_candidates(session, run_id),
            CMS_NOT_DONE: cms_candidates(session, run_id, sf, config_raw),
            HIGH_FLOAT: high_float_candidates(sf, config_raw)}


# ---------------------------------------------------------------------------
# Sync - create, revise, auto-close. Idempotent.
# ---------------------------------------------------------------------------

@dataclass
class SyncReport:
    created: dict = field(default_factory=dict)         # type -> n
    auto_closed: dict = field(default_factory=dict)     # type -> n
    still_open: dict = field(default_factory=dict)      # type -> (n, value)
    generated: dict = field(default_factory=dict)       # type -> (n, value)  every condition seen
    already_clear: dict = field(default_factory=dict)   # type -> (n, value)  cleared at first sight
    revised: int = 0

    def as_dict(self) -> dict:
        return {"created": self.created, "auto_closed": self.auto_closed,
                "still_open": {k: list(v) for k, v in self.still_open.items()},
                "generated": {k: list(v) for k, v in self.generated.items()},
                "already_clear": {k: list(v) for k, v in self.already_clear.items()},
                "revised": self.revised}


def _event(session, task_id, event, by, text=None, at=None):
    session.add(StoreTaskEvent(task_id=task_id, at=at or datetime.now(), by=by, event=event, text=text))


def _close_reason_for(session, task: StoreTask) -> str:
    if task.task_type == OPEN_BILL:
        lines = (session.query(R1BillLine.matched).filter(R1BillLine.bill_no == task.bill_no)
                 .order_by(R1BillLine.run_id.desc()).limit(50).all())
        if (session.query(ManualMatch.id).filter(ManualMatch.bill_no == task.bill_no,
                                                 ManualMatch.lifecycle == "active",
                                                 ManualMatch.status == "matched_manual").first()):
            return "bill matched manually"
        if any(m for (m,) in lines):
            return "bill matched to a transaction"
        return "bill is no longer in the sale dump"
    if task.task_type == CMS_NOT_DONE:
        return "deposit made and credited"
    return "float is back at or below the threshold"


def sync_tasks(session, run_id: int, today: date, config_raw: dict = None, operator: str = "system") -> SyncReport:
    """Bring `store_tasks` in line with the data of run `run_id`.

    CREATE a task for every active condition that has no live task (generation + 1 when an
    earlier task on that key was closed); REVISE the amount/detail of a live one (an
    `amount_revised` event when the amount moved); AUTO-CLOSE every live task whose condition
    has cleared - but only tasks whose business date lies inside the dates this run covers, so
    a narrower run can never close a task for a period it knows nothing about."""
    config_raw = config_raw or {}
    report = SyncReport()
    lo, hi = _run_dates(session, run_id)
    if lo is None:
        return report
    gen = generate(session, run_id, config_raw)
    for t, g in gen.items():
        report.generated[t] = (len(g.active) + g.cleared_n, sum(c.amount for c in g.active) + g.cleared_value)
        report.already_clear[t] = (g.cleared_n, g.cleared_value)
    active = {_stored_key(c): c for g in gen.values() for c in g.active}

    live = {t.task_key: t for t in
            session.query(StoreTask).filter(StoreTask.status.in_(LIVE_STATUSES)).all()}
    latest_gen: dict = {}
    for k, g in session.query(StoreTask.task_key, StoreTask.generation).all():
        latest_gen[k] = max(g, latest_gen.get(k, 0))

    now = datetime.now()
    for c in active.values():
        t = live.get(_stored_key(c))
        if t is not None:
            if abs(float(t.amount) - c.amount) > EPS:
                _event(session, t.id, "amount_revised", operator,
                       f"amount {float(t.amount):,.2f} -> {c.amount:,.2f}", now)
                t.amount = c.amount
                report.revised += 1
            t.detail = json.dumps(c.detail)
            t.last_seen_run_id = run_id
            continue
        raised = today
        task = StoreTask(
            task_key=_stored_key(c), generation=latest_gen.get(_stored_key(c), 0) + 1, task_type=c.task_type,
            store=c.store, business_date=c.business_date, bill_no=c.bill_no, mop=c.mop, amount=c.amount,
            detail=json.dumps(c.detail), raised_on=raised.isoformat(),
            due_by=add_working_days(raised, sla_days(config_raw, c.task_type)).isoformat(),
            status=OPEN, first_seen_run_id=run_id, last_seen_run_id=run_id)
        session.add(task)
        session.flush()
        _event(session, task.id, "raised", operator, QUESTION[c.task_type], now)
        report.created[c.task_type] = report.created.get(c.task_type, 0) + 1

    for key, t in live.items():
        ttype = t.task_type
        if key in active or not (lo <= t.business_date <= hi):
            continue
        reason = _close_reason_for(session, t)
        t.status, t.closed_on, t.closed_by, t.close_reason = CLOSED, today.isoformat(), RESOLVED_BY_DATA, reason
        _event(session, t.id, "auto_closed", "system", reason, now)
        report.auto_closed[ttype] = report.auto_closed.get(ttype, 0) + 1
    session.commit()

    for ttype in TYPES:
        q = session.query(StoreTask).filter(StoreTask.task_type == ttype, StoreTask.status.in_(LIVE_STATUSES))
        rows = q.all()
        report.still_open[ttype] = (len(rows), float(sum(float(r.amount) for r in rows)))
    return report


# ---------------------------------------------------------------------------
# Reading - server-side, store-scoped
# ---------------------------------------------------------------------------

GRID_COLUMNS = ["Task id", "Type", "Store", "Business date", "Detail", "Amount", "Raised on", "Due by",
                "Age (days)", "Status", "Store reply", "Replied by", "Replied at", "HO remark",
                "Closed on", "Closed by"]

SORTS = {
    "Overdue first, oldest due": [StoreTask.due_by.asc(), StoreTask.id.asc()],
    "Newest raised": [StoreTask.raised_on.desc(), StoreTask.id.desc()],
    "Largest amount": [StoreTask.amount.desc(), StoreTask.id.asc()],
    "Store, then date": [StoreTask.store.asc(), StoreTask.business_date.asc(), StoreTask.id.asc()],
}


def _filtered(session, date_from=None, date_to=None, store=None, types=None, statuses=None,
              overdue_only=False, today: date = None, search: str = None):
    """The one filter every read shares. `store` is how a store login will be scoped later:
    pass the login's own store and nothing else is reachable."""
    q = session.query(StoreTask)
    if date_from:
        q = q.filter(StoreTask.business_date >= str(date_from))
    if date_to:
        q = q.filter(StoreTask.business_date <= str(date_to))
    if store:
        q = q.filter(StoreTask.store == store)
    if types:
        q = q.filter(StoreTask.task_type.in_(list(types)))
    if statuses:
        q = q.filter(StoreTask.status.in_(list(statuses)))
    if overdue_only:
        q = q.filter(StoreTask.status.in_(LIVE_STATUSES), StoreTask.due_by < (today or date.today()).isoformat())
    if search:
        like = f"%{search.strip()}%"
        q = q.filter((StoreTask.bill_no.like(like)) | (StoreTask.store.like(like))
                     | (StoreTask.store_reply.like(like)) | (StoreTask.ho_remark.like(like)))
    return q


def detail_text(task_type: str, bill_no, mop, detail: dict) -> str:
    d = detail or {}
    if task_type == OPEN_BILL:
        bits = [f"Bill {bill_no}", str(mop or "")]
        if d.get("bill_time"):
            bits.append(f"at {d['bill_time']}")
        if d.get("created_by"):
            bits.append(f"by {d['created_by']}")
        return ", ".join(b for b in bits if b)
    if task_type == HIGH_FLOAT:
        return (f"Float {d.get('float', 0):,.2f} against {d.get('threshold', 0):,.0f}; "
                f"{d.get('days_held_high', 0)} day(s) held high")
    parts = []
    for r in d.get("reasons", []):
        if r.get("kind") == "no_deposit":
            parts.append(f"No deposit by {r['deposit_due']} (cash sale {r['cash_sale']:,.2f})")
        else:
            parts.append(f"Deposit {r['deposit']:,.2f} not credited by {r['credit_due']} "
                         f"({r['outstanding']:,.2f} outstanding)")
    return "; ".join(parts) + (f"; {d['days_outstanding']} working day(s) outstanding"
                               if d.get("days_outstanding") is not None else "")


def _records(rows, today: date) -> pd.DataFrame:
    out = []
    for t in rows:
        try:
            detail = json.loads(t.detail) if t.detail else {}
        except ValueError:
            detail = {}
        end = date.fromisoformat(t.closed_on) if t.closed_on else today
        out.append({
            "Task id": display_id(t.id), "Type": TYPE_LABEL.get(t.task_type, t.task_type),
            "Store": t.store, "Business date": t.business_date,
            "Detail": detail_text(t.task_type, t.bill_no, t.mop, detail), "Amount": float(t.amount),
            "Raised on": t.raised_on, "Due by": t.due_by,
            "Age (days)": (end - date.fromisoformat(t.raised_on)).days,
            "Status": STATUS_LABEL.get(t.status, t.status),
            "Store reply": t.store_reply, "Replied by": t.replied_by,
            "Replied at": t.replied_at.strftime("%Y-%m-%d %H:%M") if t.replied_at else None,
            "HO remark": t.ho_remark, "Closed on": t.closed_on, "Closed by": t.closed_by,
            "_id": t.id, "_overdue": bool(t.status in LIVE_STATUSES and t.due_by < today.isoformat()),
        })
    return pd.DataFrame(out, columns=GRID_COLUMNS + ["_id", "_overdue"])


def tasks_page(session, today: date, sort: str, limit: int, offset: int, **filters) -> pd.DataFrame:
    q = _filtered(session, today=today, **filters).order_by(*SORTS.get(sort, SORTS["Overdue first, oldest due"]))
    return _records(q.limit(limit).offset(offset).all(), today)


def tasks_all(session, today: date, sort: str = "Overdue first, oldest due", chunk: int = 5000,
              progress=None, **filters) -> pd.DataFrame:
    """Every row in the filter, fetched in chunks - the export path (never the page on screen)."""
    total = _filtered(session, today=today, **filters).count()
    frames, done = [], 0
    while done < total:
        part = tasks_page(session, today, sort, chunk, done, **filters)
        if part.empty:
            break
        frames.append(part)
        done += len(part)
        if progress:
            progress(done, total)
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=GRID_COLUMNS))[GRID_COLUMNS]


def tiles(session, today: date, **filters) -> dict:
    """The four status tiles, each (count, value). Definitions (2026-10-14):

      open     raised, no store reply yet                         (status Open)
      pending  the store has replied, awaiting head-office review  (status Replied)
      wip      head office reviewing / action underway             (status Under review)
      closed   resolved - by the data or by a person               (status Closed)

    Every live task is in exactly one of open / pending / wip, so open + pending + wip + closed is
    the whole filtered population. Filters apply to business date and store like every other
    page. `overdue` (not closed and past due) is still computed for callers that want it, but it is
    no longer a tile."""
    base = dict(filters)
    base.pop("statuses", None)
    base.pop("overdue_only", None)

    def agg(q):
        n, v = q.with_entities(_count(), _sum()).one()
        return int(n or 0), float(v or 0)

    one = lambda st: agg(_filtered(session, today=today, statuses=[st], **base))   # noqa: E731
    out = {"open": one(OPEN), "pending": one(REPLIED), "wip": one(UNDER_REVIEW), "closed": one(CLOSED),
           "overdue": agg(_filtered(session, today=today, overdue_only=True, **base))}
    out["replied"] = out["pending"]
    return out


def _count():
    from sqlalchemy import func
    return func.count(StoreTask.id)


def _sum():
    from sqlalchemy import func
    return func.sum(StoreTask.amount)


def type_summary(session, today: date, **filters) -> pd.DataFrame:
    """Live tasks by type: count and value."""
    base = dict(filters)
    base.pop("statuses", None)
    rows = []
    for t in TYPES:
        q = _filtered(session, today=today, statuses=LIVE_STATUSES, types=[t], **{k: v for k, v in base.items() if k != "types"})
        n, v = q.with_entities(_count(), _sum()).one()
        rows.append({"Type": TYPE_LABEL[t], "Live tasks": int(n or 0), "Value": float(v or 0)})
    return pd.DataFrame(rows)


def history(session, task_id: int) -> pd.DataFrame:
    rows = (session.query(StoreTaskEvent).filter(StoreTaskEvent.task_id == task_id)
            .order_by(StoreTaskEvent.at.asc(), StoreTaskEvent.id.asc()).all())
    return pd.DataFrame([{"When": r.at.strftime("%Y-%m-%d %H:%M"), "By": r.by, "Event": r.event,
                          "Text": r.text} for r in rows], columns=["When", "By", "Event", "Text"])


# ---------------------------------------------------------------------------
# Store replies - bulk upload now, validated in full before anything is applied
# ---------------------------------------------------------------------------

TEMPLATE_COLUMNS = ["task_id", "store", "reply", "replied_by"]


def upload_template() -> pd.DataFrame:
    return pd.DataFrame(columns=TEMPLATE_COLUMNS)


@dataclass
class ReplyRow:
    row: int
    task_id: int | None
    store: str
    reply: str
    replied_by: str
    accepted: bool
    reasons: list = field(default_factory=list)


@dataclass
class ReplyPreview:
    rows: list

    @property
    def accepted(self):
        return [r for r in self.rows if r.accepted]

    @property
    def rejected(self):
        return [r for r in self.rows if not r.accepted]

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "Row": r.row, "Task id": display_id(r.task_id) if r.task_id else "", "Store": r.store,
            "Reply": r.reply, "Replied by": r.replied_by,
            "Result": "Accepted" if r.accepted else "Rejected", "Why": "; ".join(r.reasons)}
            for r in self.rows])


def _cell(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def preview_replies(session, frame: pd.DataFrame) -> ReplyPreview:
    """Validate EVERY row; nothing is written. A row is accepted only if the task exists, is not
    closed, the store is the task's own store, and the reply and who gave it are present.
    The same task twice in one file is rejected on the second row - never silently last-wins."""
    frame = frame.rename(columns={c: str(c).strip().lower() for c in frame.columns})
    missing = [c for c in TEMPLATE_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError("The file is missing column(s): " + ", ".join(missing)
                         + ". Expected: " + ", ".join(TEMPLATE_COLUMNS))
    seen: set = set()
    rows = []
    records = frame.to_dict("records")
    wanted = sorted({parse_id(r.get("task_id")) for r in records if parse_id(r.get("task_id"))})
    tasks = {}
    for k in range(0, len(wanted), 500):
        tasks.update({t.id: t for t in session.query(StoreTask).filter(StoreTask.id.in_(wanted[k:k + 500])).all()})
    for i, rec in enumerate(records, start=2):
        tid = parse_id(rec.get("task_id"))
        store, reply, by = _cell(rec.get("store")).upper(), _cell(rec.get("reply")), _cell(rec.get("replied_by"))
        why = []
        task = tasks.get(tid) if tid else None
        if tid is None:
            why.append("task_id is missing or not a task id")
        elif task is None:
            why.append(f"no task {display_id(tid)}")
        else:
            if task.status == CLOSED:
                why.append(f"task is already closed ({task.closed_by}, {task.closed_on})")
            if store and store != task.store.upper():
                why.append(f"store {store} does not match the task's store {task.store}")
            elif not store:
                why.append("store is missing")
            if tid in seen:
                why.append("this task appears more than once in the file")
        if not reply:
            why.append("reply is empty")
        if not by:
            why.append("replied_by is empty")
        if tid:
            seen.add(tid)
        rows.append(ReplyRow(i, tid, store, reply, by, not why, why))
    return ReplyPreview(rows)


def apply_replies(session, preview: ReplyPreview, operator: str, now: datetime = None) -> int:
    """Apply the accepted rows. A reply replaces the task's CURRENT reply, and the previous one
    stays in the history - nothing is overwritten silently. Status becomes Replied; the task is
    NOT closed - only the underlying fact, or a person, closes it."""
    now = now or datetime.now()
    n = 0
    for r in preview.accepted:
        t = session.get(StoreTask, r.task_id)
        if t is None or t.status == CLOSED:
            continue
        t.store_reply, t.replied_by, t.replied_at = r.reply, r.replied_by, now
        t.status = REPLIED
        _event(session, t.id, "reply", r.replied_by, r.reply, now)
        n += 1
    session.commit()
    return n


def store_sheet(session, store: str, today: date, **filters) -> pd.DataFrame:
    """The tasks a store has to answer, ready to email: the question, the facts, and blank
    `reply` / `replied_by` columns that come back through the upload."""
    filters = {k: v for k, v in filters.items() if k not in ("store", "statuses")}
    q = _filtered(session, today=today, store=store, statuses=[OPEN, UNDER_REVIEW], **filters)
    rows = q.order_by(StoreTask.business_date.asc(), StoreTask.id.asc()).all()
    out = []
    for t in rows:
        try:
            d = json.loads(t.detail) if t.detail else {}
        except ValueError:
            d = {}
        out.append({"task_id": display_id(t.id), "store": t.store, "type": TYPE_LABEL[t.task_type],
                    "business_date": t.business_date, "amount": float(t.amount),
                    "detail": detail_text(t.task_type, t.bill_no, t.mop, d), "question": QUESTION[t.task_type],
                    "due_by": t.due_by, "reply": "", "replied_by": ""})
    return pd.DataFrame(out, columns=["task_id", "store", "type", "business_date", "amount", "detail",
                                      "question", "due_by", "reply", "replied_by"])


# ---------------------------------------------------------------------------
# Head-office actions - every one appends history
# ---------------------------------------------------------------------------

def add_remark(session, task_id: int, text: str, operator: str, now: datetime = None):
    text = (text or "").strip()
    if not text:
        raise ValueError("A remark cannot be empty.")
    t = session.get(StoreTask, task_id)
    if t is None:
        raise ValueError(f"No task {display_id(task_id)}.")
    t.ho_remark = text
    _event(session, t.id, "remark", operator, text, now)
    session.commit()


def mark_under_review(session, task_id: int, operator: str, now: datetime = None):
    t = session.get(StoreTask, task_id)
    if t is None or t.status == CLOSED:
        raise ValueError("Only a task that is not closed can be put under review.")
    t.status = UNDER_REVIEW
    _event(session, t.id, "under_review", operator, None, now)
    session.commit()


def close_task(session, task_id: int, reason: str, operator: str, today: date, now: datetime = None):
    """An explicit close by a person, reason required. Never reopened by a sync."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A close needs a reason.")
    t = session.get(StoreTask, task_id)
    if t is None or t.status == CLOSED:
        raise ValueError("That task is already closed.")
    t.status, t.closed_on, t.closed_by, t.close_reason = CLOSED, today.isoformat(), operator, reason
    _event(session, t.id, "closed", operator, reason, now)
    session.commit()


# ---------------------------------------------------------------------------
# Immediate cascade from a manual match or its reversal (2026-10-16)
# ---------------------------------------------------------------------------

def close_bill_tasks(session, bill_no: str, reason: str = "bill matched manually", today: date = None,
                     commit: bool = True) -> int:
    """A bill was closed by a person: its open-bill task closes at once, recorded as resolved by the
    data - exactly what the next sync would have done, without waiting for it."""
    today = today or date.today()
    tasks = (session.query(StoreTask).filter(StoreTask.task_type == OPEN_BILL, StoreTask.bill_no == bill_no,
                                             StoreTask.status.in_(LIVE_STATUSES)).all())
    now = datetime.now()
    for t in tasks:
        t.status, t.closed_on, t.closed_by, t.close_reason = CLOSED, today.isoformat(), RESOLVED_BY_DATA, reason
        _event(session, t.id, "auto_closed", "system", reason, now)
    if commit:
        session.commit()
    return len(tasks)


def reopen_bill_tasks(session, run_id: int, bill_no: str, config_raw: dict = None, today: date = None,
                      operator: str = "system", commit: bool = True) -> int:
    """A manual match was reversed: the bill is open again, so a NEW open-bill task is raised for it
    (generation + 1 - a closed task is never reopened in place, the history stays as it was)."""
    today = today or date.today()
    config_raw = config_raw or {}
    made = 0
    now = datetime.now()
    for c in open_bill_candidates(session, run_id).active:
        if c.bill_no != bill_no:
            continue
        key = _stored_key(c)
        if session.query(StoreTask.id).filter(StoreTask.task_key == key, StoreTask.status.in_(LIVE_STATUSES)).first():
            continue
        gen = max([g for (g,) in session.query(StoreTask.generation).filter(StoreTask.task_key == key).all()] or [0])
        task = StoreTask(
            task_key=key, generation=gen + 1, task_type=c.task_type, store=c.store, business_date=c.business_date,
            bill_no=c.bill_no, mop=c.mop, amount=c.amount, detail=json.dumps(c.detail), raised_on=today.isoformat(),
            due_by=add_working_days(today, sla_days(config_raw, c.task_type)).isoformat(), status=OPEN,
            first_seen_run_id=run_id, last_seen_run_id=run_id)
        session.add(task)
        session.flush()
        _event(session, task.id, "raised", operator, "bill is open again - its manual match was reversed", now)
        made += 1
    if commit:
        session.commit()
    return made
