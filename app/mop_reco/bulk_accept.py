"""Bulk accept of the system's own suggestions (2026-10-17).

The Exception bills table suggests a transaction (Txn 1 / Txn 2) for most open bills. A suggestion never
closes a bill by itself; this module turns the ones that are beyond argument into manual matches in one
confirmed step, and refuses everything else.

Two SEPARATE rules, never mixed:

  EXACT  one candidate (Txn 1 filled, Txn 2 empty), Amount 1 equals the bill amount to the paisa, same store,
         the transaction is still unmapped.
  SPLIT  two candidates whose amounts sum to the bill amount exactly, both same store, both still unmapped,
         the two different. Both transactions are consumed; the bill closes once.

Never bulk-accepted: an ambiguous cluster, an amount mismatch, a candidate already claimed. Those stay in the
table for a person.

Nothing here trusts the cached suggestion: `apply` re-reads the bill and every transaction at the moment of
applying and validates each row exactly as an inline match is validated (`manual_match.validate` /
`validate_split`). A row whose transaction was claimed since the suggestions were built is refused and reported;
the rest apply together in ONE database transaction. Every row is recorded as a manual match with source
"bulk accept", so the cascade (bill and transaction leave the exception tables, the store task closes,
Summary updates) and the reversal (a whole batch, with history) are the ones manual matching already has.

No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from . import exception_pairing as ep
from . import manual_match as mm
from . import upload_prefetch as up
from .db import ManualMatchBatch, StoreTask
from .store_tasks import LIVE_STATUSES

logger = logging.getLogger("mop_reco")

BULK_SOURCE = "bulk accept"
BULK_REASON_CODE = "bulk_accept"
EPS = 0.005


@dataclass
class Leg:
    gateway: str
    txn_id: str
    amount: float
    date: str
    time: str


@dataclass
class BulkRow:
    bill_key: str
    bill_no: str
    date: str
    store: str
    mop: str
    amount: float
    legs: list
    kind: str                       # exact | split

    @property
    def day_gap(self) -> int:
        return max(abs((pd.Timestamp(l.date) - pd.Timestamp(self.date)).days) for l in self.legs)


@dataclass
class BulkSet:
    exact: list = field(default_factory=list)
    split: list = field(default_factory=list)
    left: dict = field(default_factory=dict)      # reason -> {"bills": n, "value": rs}
    bills: int = 0
    value: float = 0.0

    @staticmethod
    def _tot(rows):
        return len(rows), round(sum(r.amount for r in rows), 2)

    @property
    def exact_n(self): return self._tot(self.exact)[0]
    @property
    def exact_value(self): return self._tot(self.exact)[1]
    @property
    def split_n(self): return self._tot(self.split)[0]
    @property
    def split_value(self): return self._tot(self.split)[1]
    @property
    def left_n(self): return sum(v["bills"] for v in self.left.values())
    @property
    def left_value(self): return round(sum(v["value"] for v in self.left.values()), 2)


def build(bills: pd.DataFrame, pool: pd.DataFrame, assignment) -> BulkSet:
    """The rows each button would accept, from the whole-population assignment (never a page or a filter)."""
    out = BulkSet(bills=len(bills), value=round(float(bills["amount"].sum()), 2) if len(bills) else 0.0)
    if bills.empty:
        return out
    b = bills.drop_duplicates("bill_key").set_index("bill_key")
    free = {(r.gateway, r.txn_id): r for r in pool.itertuples(index=False)}
    taken_exact = set()
    for key, kind in assignment.kind.items():
        if key not in b.index or key in assignment.ambiguous:
            continue
        cands = assignment.candidates.get(key, [])
        bill = b.loc[key]
        amount = float(bill["amount"])
        legs = [Leg(c.gateway, c.txn_id, float(c.amount), str(c.date), c.time or "") for c in cands]
        if any((l.gateway, l.txn_id) not in free or free[(l.gateway, l.txn_id)].store != bill["store"] for l in legs):
            continue
        if kind == "exact" and len(legs) == 1 and abs(legs[0].amount - amount) < EPS:
            out.exact.append(BulkRow(key, str(bill["bill_no"]), str(bill["date"]), bill["store"], bill["mop"], amount,
                                     legs, "exact"))
            taken_exact.add(key)
        elif (kind == "split" and len(legs) == 2 and legs[0].txn_id != legs[1].txn_id
              and abs(sum(l.amount for l in legs) - amount) < EPS):
            out.split.append(BulkRow(key, str(bill["bill_no"]), str(bill["date"]), bill["store"], bill["mop"],
                                     amount, legs, "split"))
    accepted = {r.bill_key for r in out.exact} | {r.bill_key for r in out.split}
    rest = b[~b.index.isin(accepted)]
    amb = rest[rest.index.isin(set(assignment.ambiguous))]
    cand_not_ok = rest[rest.index.isin(set(assignment.candidates)) & ~rest.index.isin(set(assignment.ambiguous))]
    none = rest[~rest.index.isin(amb.index) & ~rest.index.isin(cand_not_ok.index)]
    for label, frame in (("ambiguous - several bills and transactions share a store and amount", amb),
                         ("no candidate transaction of that store and amount", none),
                         ("a candidate exists but fails a rule (amount, store or already claimed)", cand_not_ok)):
        if len(frame):
            out.left[label] = {"bills": int(len(frame)), "value": round(float(frame["amount"].sum()), 2)}
    return out


def preview_frame(rows: list) -> pd.DataFrame:
    """The full list the operator confirms - and exports. One row per bill."""
    out = []
    for r in rows:
        l1 = r.legs[0]
        l2 = r.legs[1] if len(r.legs) > 1 else None
        out.append({"date": r.date, "store": r.store, "bill_no": r.bill_no, "mop": r.mop, "amount": r.amount,
                    "Txn 1": l1.txn_id, "Amount 1": l1.amount, "Txn 2": l2.txn_id if l2 else "",
                    "Amount 2": l2.amount if l2 else None, "gateway": l1.gateway,
                    "txn_date": l1.date if not l2 else max(l.date for l in r.legs),
                    "day_gap": r.day_gap, "rule": r.kind})
    return pd.DataFrame(out, columns=["date", "store", "bill_no", "mop", "amount", "Txn 1", "Amount 1", "Txn 2",
                                      "Amount 2", "gateway", "txn_date", "day_gap", "rule"])


@dataclass
class ApplyReport:
    kind: str
    batch_id: int = None
    submitted: int = 0
    applied: int = 0
    refused: list = field(default_factory=list)       # [{"bill_no", "txns", "why"}]
    bills_closed: int = 0
    transactions_consumed: int = 0
    tasks_closed: int = 0
    value: float = 0.0
    open_value_before: float = 0.0
    open_value_after: float = 0.0
    seconds: float = 0.0


def _pool_row(gateway, txn) -> pd.DataFrame:
    return pd.DataFrame([{"gateway": gateway, "txn_ref": txn.txn_id, "business_date": txn.date, "store": txn.store,
                          "amount": txn.amount, "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None}])


def _open_value(session, run_id: int) -> float:
    from sqlalchemy import func, text
    from .db import R1BillLine
    base = session.query(func.coalesce(func.sum(R1BillLine.amount), 0)).filter(
        R1BillLine.run_id == run_id, R1BillLine.matched.is_(False)).scalar() or 0
    manual = session.execute(text(
        "select coalesce(sum(r.amount),0) from r1_bill_lines r where r.run_id=:r and r.matched=0 and r.bill_no in "
        "(select bill_no from manual_matches where lifecycle='active' and status='matched_manual')"),
        {"r": run_id}).scalar() or 0
    return round(float(base) - float(manual), 2)


def apply(session, run_id: int, rows: list, operator: str, kind: str, progress=None) -> ApplyReport:
    """Validate EVERY row now - against the bill and the transactions as they are at this moment, not as the
    suggestion was built - then record the survivors as manual matches in one database transaction."""
    import time as _time
    t0 = _time.perf_counter()
    if not (operator or "").strip():
        raise ValueError("A bulk accept must record who made it")
    if kind not in ("exact", "split"):
        raise ValueError(f"unknown bulk rule {kind!r}")
    rep = ApplyReport(kind=kind, submitted=len(rows))
    rows = [r for r in rows if r.kind == kind]            # a button only ever applies ITS OWN rule's rows
    rep.submitted = len(rows)
    tick = (lambda f, m: progress(f, m)) if progress else (lambda f, m: None)

    fresh = {r.txn_id: r for r in ep.unmapped_pool(session).itertuples(index=False)}
    uses: dict = {}
    for r in rows:
        for l in r.legs:
            uses.setdefault(l.txn_id, []).append(r.bill_no)
    rep.open_value_before = _open_value(session, run_id)
    refs = [l.txn_id for r in rows for l in r.legs]
    good, bills_seen = [], {}

    with up.prefetch(session, run_id, bill_nos=[r.bill_no for r in rows], refs=refs):
        for i, r in enumerate(rows):
            if i % 200 == 0:
                tick(0.4 * i / max(len(rows), 1), f"Checked {i:,} of {len(rows):,} bills")
            why = []
            bill, is_open, why_not = mm.find_bill_for_match(session, run_id, r.bill_no)
            if bill is None or not is_open:
                why.append(why_not or "the bill is no longer open")
            else:
                if bill["store"] != r.store or abs(float(bill["amount"]) - r.amount) > EPS:
                    why.append("the bill no longer matches the suggestion (store or amount changed)")
            for l in r.legs:
                if len(uses[l.txn_id]) > 1:
                    why.append(f"transaction {l.txn_id} is suggested to more than one bill in this set "
                               f"({', '.join(sorted(set(uses[l.txn_id])))})")
                if l.txn_id not in fresh:
                    why.append(f"transaction {l.txn_id} is no longer unmapped (claimed, refunded or closed since "
                               "the suggestions were built)")
            validation = None
            if not why:
                pools = pd.concat([_pool_row(l.gateway, fresh[l.txn_id]) for l in r.legs], ignore_index=True)
                if kind == "exact":
                    validation = mm.validate(session, bill, r.legs[0].gateway, r.legs[0].txn_id, pools)
                    validation.gateway = r.legs[0].gateway
                else:
                    validation = mm.validate_split(session, bill, [(l.gateway, l.txn_id) for l in r.legs], pools)
                if not validation.ok or validation.case != mm.CASE_CLEAN:
                    why.extend(validation.problems or ["the transaction is not a clean match"])
            if why:
                rep.refused.append({"bill_no": r.bill_no, "txns": ", ".join(l.txn_id for l in r.legs),
                                    "why": " | ".join(dict.fromkeys(why))})
            else:
                good.append((r, bill, validation))
                bills_seen[r.bill_no] = r.amount

        tasks_before = session.query(StoreTask).filter(StoreTask.task_type == "open_bill",
                                                       StoreTask.status.in_(LIVE_STATUSES)).count()
        batch = ManualMatchBatch(created_at=datetime.now(), operator=operator, source=BULK_SOURCE,
                                 rows_submitted=len(rows), rows_accepted=len(good),
                                 rows_rejected=len(rep.refused),
                                 note=f"bulk accept - {kind} rule: "
                                      + ("one candidate, exact amount, same store" if kind == "exact"
                                         else "two candidates summing to the bill, same store"))
        try:
            session.add(batch)
            session.flush()
            for n, (r, bill, validation) in enumerate(good):
                if n % 200 == 0:
                    tick(0.4 + 0.6 * n / max(len(good), 1), f"Recorded {n:,} of {len(good):,} matches")
                second = None
                if kind == "split":
                    second = {"gateway": r.legs[1].gateway, "txn_ref": r.legs[1].txn_id, "amount": r.legs[1].amount}
                mm.record(session, bill, r.legs[0].gateway, r.legs[0].txn_id, BULK_REASON_CODE, operator, validation,
                          note=f"bulk accept ({kind})", batch=batch, source=BULK_SOURCE, commit=False, second=second)
            session.commit()
        except Exception:
            session.rollback()
            raise
        tasks_after = session.query(StoreTask).filter(StoreTask.task_type == "open_bill",
                                                      StoreTask.status.in_(LIVE_STATUSES)).count()

    rep.batch_id = batch.id
    rep.applied = len(good)
    rep.bills_closed = len(good)
    rep.transactions_consumed = sum(len(r.legs) for r, _, _ in good)
    rep.tasks_closed = max(tasks_before - tasks_after, 0)
    rep.value = round(sum(r.amount for r, _, _ in good), 2)
    rep.open_value_after = _open_value(session, run_id)
    rep.seconds = _time.perf_counter() - t0
    logger.info("bulk accept (%s): %d of %d applied, %d refused, batch #%s, %.1fs", kind, rep.applied, rep.submitted,
                len(rep.refused), rep.batch_id, rep.seconds)
    return rep
