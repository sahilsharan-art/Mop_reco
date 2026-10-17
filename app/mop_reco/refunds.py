"""Customer refunds — a THIRD way an unmapped transaction closes (2026-09-20).

Not a bill match. A refund is money we received and then paid back: a dual
payment, or a payment with no bill raised against it. The store team raises a
Zoho ticket; once the customer is paid, the refund data is uploaded here.

Same shape as `manual_match.py`'s bulk upload — validate every row before
applying any, preview accepted/rejected with reasons, apply all-or-nothing
(unlike a manual-match upload, which applies row-by-row: a refund batch is
"all or nothing, never partially", per the operator's own instruction, so a
single failure during apply rolls back the whole batch rather than leaving a
partial set committed).

No Streamlit imports here (§8). The page supplies the operator and the input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from . import masters
from .db import TransactionRefund
from .manual_match import _clean, _clean_ref, gateway_pool_for_date, identify_gateway, targeted_pool_many

logger = logging.getLogger("mop_reco")

UPLOAD_COLUMNS = ["date", "store_code", "transaction_no", "zoho_no",
                 "customer_name", "status"]

STATUS_PENDING = "pending"
STATUS_RELEASED = "released"
_STATUS_ALIASES = {
    "pending": STATUS_PENDING,
    "paid": STATUS_RELEASED,
    "released": STATUS_RELEASED,
    "refunded": STATUS_RELEASED,
    "completed": STATUS_RELEASED,
}


def upload_template() -> pd.DataFrame:
    return pd.DataFrame([{
        "date": "2026-07-20",
        "store_code": "ABD",
        "transaction_no": "20260720111212800110168...",
        "zoho_no": "ZOHO-12345",
        "customer_name": "Ramesh Kumar",
        "status": "pending",
    }], columns=UPLOAD_COLUMNS)


@dataclass
class RefundRowOutcome:
    row_number: int
    business_date: str
    store_code: str
    transaction_ref: str
    zoho_no: str
    customer_name: str
    refund_status: str
    gateway: str = None
    amount: float = None
    verdict: str = "reject"       # "accept" | "reject"
    why: list = field(default_factory=list)


@dataclass
class RefundUploadPreview:
    rows: list = field(default_factory=list)

    @property
    def accepted(self) -> list:
        return [r for r in self.rows if r.verdict == "accept"]

    @property
    def rejected(self) -> list:
        return [r for r in self.rows if r.verdict == "reject"]

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "row": r.row_number,
            "date": r.business_date,
            "store_code": r.store_code,
            "transaction_no": r.transaction_ref,
            "zoho_no": r.zoho_no,
            "customer_name": r.customer_name,
            "status": r.refund_status,
            "gateway": r.gateway,
            "amount": r.amount,
            "verdict": r.verdict,
            "why": " | ".join(r.why),
        } for r in self.rows])


def preview_upload(session, frame: pd.DataFrame, warehouse=None) -> RefundUploadPreview:
    """Validate a refund file with every lookup done ONCE (see upload_prefetch), not once per row."""
    from . import upload_prefetch as up
    from .manual_match import latest_completed_run_id

    refs = ([_clean_ref(r) for r in frame["transaction_no"]] if "transaction_no" in frame.columns else [])
    with up.prefetch(session, latest_completed_run_id(session), refs=refs):
        return _preview_upload(session, frame, warehouse)


def _preview_upload(session, frame: pd.DataFrame, warehouse=None) -> RefundUploadPreview:
    """Validate EVERY row before anything is applied. Rules, verbatim from
    the operator's own instruction — every one is a rejection, not a warning,
    since a refund upload has no "accept with an override" path at all:

      - transaction exists in the loaded gateway data
      - transaction is currently UNMAPPED (refuse one already matched to a
        bill, naming that bill)
      - store_code exists in the store master
      - store_code matches the transaction's own store; if it differs, flag
        it and show both, never silently accept either
      - zoho_no is present and not already used on another transaction
    """
    from . import gateway_txns as gt
    from .db import ManualMatch, R1BillLine

    preview = RefundUploadPreview()
    missing = [c for c in UPLOAD_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"The upload is missing column(s): {', '.join(missing)}. "
                         f"Expected exactly: {', '.join(UPLOAD_COLUMNS)}")

    # Store master, as of today — a refund upload is a today's-date action,
    # not a re-run of a past business date's own master version.
    try:
        _, store_rows = masters.get_rows_asof(session, "store", datetime.now())
        known_stores = {_clean(r.get("store")).upper() for r in store_rows}
    except Exception:
        known_stores = set()

    already_refunded = {
        (str(r.gateway).lower(), str(r.transaction_ref).strip())
        for r in session.query(TransactionRefund).all()
    }
    zoho_in_use = {
        _clean(r.zoho_no).upper()
        for r in session.query(TransactionRefund).all() if r.zoho_no
    }

    # only the ids in the file are looked up - ONE pass over the warehouse, never the whole 4.4M-row pool
    file_refs = [_clean_ref(r.get("transaction_no")) for r in frame.to_dict("records")]
    pool = targeted_pool_many(file_refs, warehouse=warehouse)
    # a Zoho number or a transaction named by two rows is wrong for BOTH rows - neither is more right
    zoho_rows, txn_rows = {}, {}
    for position, raw in enumerate(frame.to_dict("records"), start=2):
        z, r_ = _clean(raw.get("zoho_no")).upper(), _clean_ref(raw.get("transaction_no"))
        if z:
            zoho_rows.setdefault(z, []).append(position)
        if r_:
            txn_rows.setdefault(r_, []).append(position)

    for position, raw in enumerate(frame.to_dict("records"), start=2):
        typed_date = _clean(raw.get("date"))
        store_code = _clean(raw.get("store_code")).upper()
        ref = _clean_ref(raw.get("transaction_no"))
        zoho_no = _clean(raw.get("zoho_no"))
        customer_name = _clean(raw.get("customer_name"))
        typed_status = _clean(raw.get("status")).lower()
        why = []

        outcome = RefundRowOutcome(position, typed_date, store_code, ref,
                                   zoho_no, customer_name, None)

        if not ref:
            why.append("no transaction id was given")
        if not zoho_no:
            why.append("zoho_no is required and was blank")
        if store_code and store_code not in known_stores:
            why.append(f"store_code {store_code!r} is not in the store master")

        refund_status = _STATUS_ALIASES.get(typed_status)
        if typed_status and refund_status is None:
            why.append(f"status {typed_status!r} is not recognised — expected "
                       f"one of {', '.join(sorted(set(_STATUS_ALIASES)))}")
        outcome.refund_status = refund_status or STATUS_PENDING

        if zoho_no:
            if zoho_no.upper() in zoho_in_use:
                why.append(f"zoho_no {zoho_no!r} is already used on another, "
                          "previously uploaded refund")
            elif len(zoho_rows.get(zoho_no.upper(), [])) > 1:
                others = [n for n in zoho_rows[zoho_no.upper()] if n != position]
                why.append(f"zoho_no {zoho_no!r} is also used on row "
                          f"{', '.join(map(str, others))} of this same file")

        if why:
            preview.rows.append(outcome)
            outcome.why = why
            continue

        hit = identify_gateway(session, typed_date or None, ref, pool)
        if hit.problem and not hit.found:
            why.append(hit.problem)
            preview.rows.append(outcome)
            outcome.why = why
            continue
        if not hit.found:
            why.append(f"transaction {ref!r} is not in the Paytm, Axis or Bajaj "
                      "data loaded, on any date")
            preview.rows.append(outcome)
            outcome.why = why
            continue

        outcome.gateway = hit.gateway
        outcome.amount = float(hit.txn.get("amount") or 0)
        txn_store = _clean(hit.txn.get("store")).upper()

        if (hit.gateway, ref) in already_refunded:
            why.append(f"transaction {ref!r} is already recorded as a refund")
        if len(txn_rows.get(ref, [])) > 1:
            others = [n for n in txn_rows[ref] if n != position]
            why.append(f"transaction {ref!r} is also used on row "
                      f"{', '.join(map(str, others))} of this same file")

        # Is it already matched to a bill (system or manual)? Naming the bill,
        # never silently refusing.
        existing_bill = _owner_of(session, hit.gateway, ref)
        if existing_bill is not None:
            why.append(
                f"transaction {ref!r} is already matched to bill "
                f"{existing_bill.bill_no} ({existing_bill.store}, "
                f"{existing_bill.business_date}) — a refund cannot be recorded "
                "against a transaction that already closed a bill."
            )

        if txn_store and store_code and txn_store != store_code:
            why.append(
                f"store_code mismatch: the upload says {store_code!r}, the "
                f"transaction itself belongs to {txn_store!r} — both shown, "
                "neither assumed."
            )

        outcome.why = why
        outcome.verdict = "accept" if not why else "reject"
        preview.rows.append(outcome)

    return preview


def apply_upload(session, preview: RefundUploadPreview, operator: str,
                 file_name: str = None) -> tuple:
    """Apply every accepted row as ONE all-or-nothing batch — a failure on any
    row rolls the whole batch back, per the operator's own instruction that
    this upload is never applied partially."""
    if not _clean(operator):
        raise ValueError("A refund upload must record who applied it")

    chosen = preview.accepted
    if not chosen:
        return [], []

    now = datetime.now()
    try:
        entries = []
        for row in chosen:
            entry = TransactionRefund(
                gateway=row.gateway,
                transaction_ref=row.transaction_ref,
                business_date=row.business_date,
                store=row.store_code,
                amount=row.amount,
                zoho_no=row.zoho_no,
                customer_name=row.customer_name or None,
                refund_date=row.business_date,
                refund_status=row.refund_status,
                uploaded_by=operator,
                uploaded_at=now,
            )
            session.add(entry)
            entries.append(entry)
        session.flush()
        session.commit()
    except Exception:
        session.rollback()
        raise

    logger.info(f"refunds: applied {len(entries)} row(s) by {operator} "
               f"(file={file_name})")
    return entries, preview.rejected


def refund_records(session) -> pd.DataFrame:
    """Every refund ever recorded — the Refund report. Newest upload first."""
    rows = (session.query(TransactionRefund)
            .order_by(TransactionRefund.uploaded_at.desc()).all())
    return pd.DataFrame([{
        "refund_date": r.refund_date,
        "store": r.store,
        "gateway": r.gateway,
        "transaction_id": r.transaction_ref,
        "amount": float(r.amount or 0),
        "zoho_no": r.zoho_no,
        "customer_name": r.customer_name,
        "status": r.refund_status,
        "uploaded_by": r.uploaded_by,
        "uploaded_at": r.uploaded_at,
    } for r in rows])


# ---------------------------------------------------------------------------
# The inline / table-upload path (2026-10-16): customer_name, zoho_no, refund_date - all three required
# ---------------------------------------------------------------------------

def parse_refund_date(value):
    """DD/MM/YYYY or YYYY-MM-DD (also an Excel date) -> ISO text, or None."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none"):
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _owner_of(session, gateway: str, ref: str):
    """The bill (engine or person) that already holds this transaction, else None."""
    from . import upload_prefetch as _up
    pf = _up.current()
    if pf is not None and ref in pf.requested_refs:
        from types import SimpleNamespace
        owner = pf.engine_owner.get(ref)
        manual = pf.manual_by_txn.get((gateway, ref))
        if owner:
            return SimpleNamespace(**owner)
        return manual if manual is not None and manual.status == "matched_manual" else None
    from .db import ManualMatch, R1BillLine
    engine_bill = (session.query(R1BillLine).filter(R1BillLine.matched_txn_ref == ref,
                                                    R1BillLine.matched.is_(True)).first())
    manual_bill = (session.query(ManualMatch).filter(ManualMatch.gateway == gateway, ManualMatch.transaction_ref == ref,
                                                     ManualMatch.lifecycle == "active",
                                                     ManualMatch.status == "matched_manual").first())
    return engine_bill or manual_bill


def validate_refund(session, txn: dict, customer_name, zoho_no, refund_date) -> list:
    """Problems that stop a customer refund; empty = acceptable. `txn` is the gateway row
    (gateway, txn_ref, ...) or None when the id was found nowhere."""
    problems = []
    name, zoho = _clean(customer_name), _clean(zoho_no)
    if not name:
        problems.append("customer_name is required")
    if not zoho:
        problems.append("zoho_no is required")
    if parse_refund_date(refund_date) is None:
        problems.append("refund_date is required (DD/MM/YYYY)")
    if txn is None:
        problems.append("the transaction does not exist in the loaded gateway data")
        return problems
    gateway, ref = str(txn["gateway"]).lower(), _clean_ref(txn["txn_ref"])
    owner = _owner_of(session, gateway, ref)
    if owner is not None:
        problems.append(f"transaction {ref} is already matched to bill {owner.bill_no} "
                        f"({owner.store}, {owner.business_date}) - it is not unmapped")
    from . import upload_prefetch as _up
    pf = _up.current()
    if pf is not None:
        already_txn = (gateway, ref) in pf.refund_txns
        already_zoho = bool(zoho) and zoho in pf.refund_zoho
    else:
        already_txn = bool(session.query(TransactionRefund.id).filter(
            TransactionRefund.gateway == gateway, TransactionRefund.transaction_ref == ref).first())
        already_zoho = bool(zoho) and bool(
            session.query(TransactionRefund.id).filter(TransactionRefund.zoho_no == zoho).first())
    if already_txn:
        problems.append(f"transaction {ref} is already recorded as a refund")
    if already_zoho:
        problems.append(f"zoho_no {zoho} is already used on another refund")
    return problems


def record_refund(session, txn: dict, customer_name, zoho_no, refund_date, operator: str,
                  source: str = "inline", commit: bool = True) -> TransactionRefund:
    """Resolve an unmapped transaction as REFUNDED: it never closes a bill and is never revenue. The
    transaction leaves Exception transactions; the live row and an append-only event both record it."""
    from .db import RefundEvent
    from .manual_match import close_txn_exception

    if not _clean(operator):
        raise ValueError("A refund must record who made it")
    problems = validate_refund(session, txn, customer_name, zoho_no, refund_date)
    if problems:
        raise ValueError("; ".join(problems))
    now = datetime.now()
    gateway, ref = str(txn["gateway"]).lower(), _clean_ref(txn["txn_ref"])
    entry = TransactionRefund(
        gateway=gateway, transaction_ref=ref, business_date=str(txn.get("business_date")),
        store=_clean(txn.get("store")) or None, amount=float(txn.get("amount") or 0), zoho_no=_clean(zoho_no),
        customer_name=_clean(customer_name), refund_date=parse_refund_date(refund_date),
        refund_status=STATUS_RELEASED, uploaded_by=operator, uploaded_at=now, source=source)
    session.add(entry)
    session.flush()
    session.add(RefundEvent(action="recorded", gateway=gateway, transaction_ref=ref,
                            business_date=entry.business_date, store=entry.store, amount=entry.amount,
                            zoho_no=entry.zoho_no, customer_name=entry.customer_name, refund_date=entry.refund_date,
                            source=source, reason="customer refund", by=operator, at=now))
    close_txn_exception(session, gateway, ref, operator, "customer refund")
    if commit:
        session.commit()
    return entry


def reverse_refund(session, refund_id: int, operator: str, reason: str, commit: bool = True):
    """Undo a refund: the transaction returns to Exception transactions and its Zoho number is free
    again. The record is not lost - the event log keeps every field, who reversed it and when."""
    from .db import RefundEvent
    from .manual_match import reopen_txn_exception

    row = session.get(TransactionRefund, refund_id)
    if row is None:
        raise LookupError(f"No refund #{refund_id}")
    if not _clean(reason):
        raise ValueError("A reversal must carry a reason")
    if not _clean(operator):
        raise ValueError("A reversal must record who made it")
    now = datetime.now()
    session.add(RefundEvent(action="reversed", gateway=row.gateway, transaction_ref=row.transaction_ref,
                            business_date=row.business_date, store=row.store, amount=row.amount,
                            zoho_no=row.zoho_no, customer_name=row.customer_name, refund_date=row.refund_date,
                            source=row.source, reason=_clean(reason), by=operator, at=now))
    gateway, ref = row.gateway, row.transaction_ref
    session.delete(row)
    session.flush()
    reopen_txn_exception(session, gateway, ref, operator, "refund reversed")
    if commit:
        session.commit()


def refund_register(session) -> pd.DataFrame:
    """Every refund event ever (recorded and reversed) - the audit record."""
    from .db import RefundEvent
    rows = session.query(RefundEvent).order_by(RefundEvent.id.desc()).all()
    return pd.DataFrame([{"at": r.at, "action": r.action, "by": r.by, "gateway": r.gateway,
                          "transaction_id": r.transaction_ref, "store": r.store, "amount": float(r.amount or 0),
                          "zoho_no": r.zoho_no, "customer_name": r.customer_name, "refund_date": r.refund_date,
                          "source": r.source, "reason": r.reason} for r in rows])
