"""Set-based lookups for validating a whole uploaded file (2026-10-17).

Validating an upload row by row issued ~4 queries per row (find the bill, its manual match, who owns the
transaction, ...) and, for a transaction that is not in the open pool, a scan of every gateway parquet file
(~1.5-2 s EACH). Here everything a file needs is loaded ONCE - a handful of `IN (...)` queries and one
batched parquet pass - into dictionaries, and the existing validators (`manual_match.validate`,
`find_bill_for_match`, `refunds.validate_refund`) read those instead of the database while a `prefetch()`
context is active. Outside a context they behave exactly as before, so the inline paste is untouched.

The context is a ContextVar, so two Streamlit sessions validating at once never see each other's data.
No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import text

logger = logging.getLogger("mop_reco")

_ACTIVE: ContextVar = ContextVar("upload_prefetch", default=None)
CHUNK = 500


@dataclass
class Prefetch:
    run_id: int
    bills: dict = field(default_factory=dict)             # bill_no -> first line of the bill (dict)
    manual_active: list = field(default_factory=list)      # every ACTIVE ManualMatch row (a small table)
    manual_by_txn: dict = field(default_factory=dict)      # (gateway, ref) -> ManualMatch
    manual_by_key: dict = field(default_factory=dict)      # bill_key -> ManualMatch
    engine_owner: dict = field(default_factory=dict)       # txn ref -> first matched bill line (dict)
    refund_txns: set = field(default_factory=set)          # (gateway, ref)
    refund_zoho: set = field(default_factory=set)
    requested_bills: set = field(default_factory=set)
    requested_refs: set = field(default_factory=set)
    timings: dict = field(default_factory=dict)
    queries: int = 0


def current():
    return _ACTIVE.get()


def _chunks(items, n=CHUNK):
    items = sorted({i for i in items if i})
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _in(prefix: str, values: list) -> tuple:
    names = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return ", ".join(f":{k}" for k in names), names


@contextmanager
def prefetch(session, run_id: int, bill_nos=(), refs=(), zohos=()):
    from .db import ManualMatch, TransactionRefund
    from .manual_match import bill_key_of

    pf = Prefetch(run_id=run_id)
    pf.requested_bills = {b for b in bill_nos if b}
    pf.requested_refs = {r for r in refs if r}
    t0 = time.perf_counter()

    def timed(name, t):
        pf.timings[name] = pf.timings.get(name, 0.0) + (time.perf_counter() - t)

    t = time.perf_counter()
    for chunk in _chunks(bill_nos):
        marks, params = _in("b", chunk)
        rows = session.execute(text(
            "select id, business_date, store, bill_no, mop, amount, matched, matched_txn_ref, matched_gateway "
            f"from r1_bill_lines where run_id=:r and bill_no in ({marks}) order by id"), {"r": run_id, **params})
        pf.queries += 1
        for r in rows:
            pf.bills.setdefault(r.bill_no, {
                "business_date": r.business_date, "store": r.store, "bill_no": r.bill_no, "mop": r.mop,
                "amount": float(r.amount or 0), "matched": bool(r.matched), "matched_txn_ref": r.matched_txn_ref,
                "matched_gateway": r.matched_gateway})
    timed("bills", t)

    t = time.perf_counter()
    pf.manual_active = session.query(ManualMatch).filter(ManualMatch.lifecycle == "active").all()
    pf.queries += 1
    for m in pf.manual_active:
        pf.manual_by_txn.setdefault((str(m.gateway).lower(), m.transaction_ref), m)
        pf.manual_by_key.setdefault(m.bill_key, m)
    timed("manual_matches", t)

    t = time.perf_counter()
    for chunk in _chunks(refs):
        marks, params = _in("t", chunk)
        rows = session.execute(text(
            "select matched_txn_ref, bill_no, store, business_date from r1_bill_lines "
            f"where run_id=:r and matched=1 and matched_txn_ref in ({marks})"), {"r": run_id, **params})
        pf.queries += 1
        for r in rows:
            pf.engine_owner.setdefault(r.matched_txn_ref, {"bill_no": r.bill_no, "store": r.store,
                                                           "business_date": r.business_date})
    timed("engine_owner", t)

    t = time.perf_counter()
    for g, ref, zoho in session.query(TransactionRefund.gateway, TransactionRefund.transaction_ref,
                                      TransactionRefund.zoho_no).all():
        pf.refund_txns.add((str(g).lower(), ref))
        pf.refund_zoho.add(zoho)
    pf.queries += 1
    timed("refunds", t)

    pf.timings["prefetch_total"] = time.perf_counter() - t0
    token = _ACTIVE.set(pf)
    try:
        yield pf
    finally:
        _ACTIVE.reset(token)
        logger.info("upload prefetch: %d queries, %s", pf.queries,
                    {k: round(v, 3) for k, v in pf.timings.items()})
