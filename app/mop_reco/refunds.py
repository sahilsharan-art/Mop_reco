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
from pathlib import Path

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


def _norm_name(name) -> str:
    return " ".join(_clean(name).lower().split())


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
    notes: list = field(default_factory=list)   # facts that are recorded but do not stop the refund (folder flow)
    store_used: str = None                       # the store the refund is recorded under


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


def preview_upload(session, frame: pd.DataFrame, warehouse=None, folder_mode: bool = False) -> RefundUploadPreview:
    """Validate a refund file with every lookup done ONCE (see upload_prefetch), not once per row."""
    from . import upload_prefetch as up
    from .manual_match import latest_completed_run_id

    refs = ([_clean_ref(r) for r in frame["transaction_no"]] if "transaction_no" in frame.columns else [])
    with up.prefetch(session, latest_completed_run_id(session), refs=refs):
        return _preview_upload(session, frame, warehouse, folder_mode)


def _preview_upload(session, frame: pd.DataFrame, warehouse=None, folder_mode: bool = False) -> RefundUploadPreview:
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
    except masters.MasterLoadError as exc:
        # No version of the store master has ever been loaded: truthfully, no store is known. Said in the log, not hidden.
        logger.warning("refund validation: %s - no store is known", exc)
        known_stores = set()
    except Exception as exc:
        # Any other failure must not become an empty master: every refund would be rejected as "store not in master" (misleading), and
        # in folder mode a refund could be recorded under the file's store instead of the transaction's (audit C-002).
        logger.exception("refund validation: the store master could not be read")
        raise RuntimeError(f"The store master could not be read ({type(exc).__name__}: {exc}); no refund was checked or recorded.") from exc

    already_refunded = {
        (str(r.gateway).lower(), str(r.transaction_ref).strip())
        for r in session.query(TransactionRefund).all()
    }
    # Zoho number -> the customers already holding it. A ticket may cover several payments of ONE customer (2026-10-30).
    zoho_in_use = {}
    for r in session.query(TransactionRefund).all():
        if r.zoho_no:
            zoho_in_use.setdefault(_clean(r.zoho_no).upper(), set()).add(_norm_name(r.customer_name))

    # only the ids in the file are looked up - ONE pass over the warehouse, never the whole 4.4M-row pool
    file_refs = [_clean_ref(r.get("transaction_no")) for r in frame.to_dict("records")]
    pool = targeted_pool_many(file_refs, warehouse=warehouse)
    # a Zoho number or a transaction named by two rows is wrong for BOTH rows - neither is more right
    zoho_rows, txn_rows = {}, {}
    for position, raw in enumerate(frame.to_dict("records"), start=2):
        z, r_ = _clean(raw.get("zoho_no")).upper(), _clean_ref(raw.get("transaction_no"))
        if z:
            zoho_rows.setdefault(z, []).append((position, _norm_name(raw.get("customer_name"))))
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
        if store_code and store_code not in known_stores and not folder_mode:
            why.append(f"store_code {store_code!r} is not in the store master")

        refund_status = _STATUS_ALIASES.get(typed_status)
        if typed_status and refund_status is None:
            why.append(f"status {typed_status!r} is not recognised — expected "
                       f"one of {', '.join(sorted(set(_STATUS_ALIASES)))}")
        outcome.refund_status = refund_status or STATUS_PENDING

        if zoho_no:
            mine = _norm_name(customer_name)
            if zoho_no.upper() in zoho_in_use and mine not in zoho_in_use[zoho_no.upper()]:
                why.append(f"zoho_no {zoho_no!r} is already used on another, "
                          "previously uploaded refund (of a different customer)")
            else:
                clash = [n for n, who in zoho_rows.get(zoho_no.upper(), []) if n != position and who != mine]
                if clash:
                    why.append(f"zoho_no {zoho_no!r} is also used on row "
                              f"{', '.join(map(str, clash))} of this same file for a different customer")

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
        outcome.store_used = store_code
        if folder_mode and store_code and store_code not in known_stores:
            if store_code == txn_store:
                outcome.notes.append(f"store {store_code} is not in the store master (it is the transaction's own store)")
            else:
                why.append(f"store_code {store_code!r} is not in the store master")

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

        if folder_mode and txn_store and store_code and txn_store != store_code:
            # Recorded, never silent: the refund file names one store, the gateway's MID master another. The exception
            # lives under the transaction's store, so that is the store the refund is recorded under (unless it is not a store).
            outcome.store_used = txn_store if txn_store in known_stores else store_code
            outcome.notes.append(f"store in the refund file is {store_code}, the transaction belongs to {txn_store}"
                                 + ("" if txn_store in known_stores else " (not a valid store code)"))
        elif txn_store and store_code and txn_store != store_code:
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


def refund_table(session, date_from=None, date_to=None, store=None) -> pd.DataFrame:
    """The Customer refunds table of the Exceptions page: every refund whose transaction falls in the date range / store,
    newest first, with the id the reversal needs. Dates are the transaction's own business date (rule 6)."""
    q = session.query(TransactionRefund)
    if date_from:
        q = q.filter(TransactionRefund.business_date >= str(date_from))
    if date_to:
        q = q.filter(TransactionRefund.business_date <= str(date_to))
    if store:
        q = q.filter(TransactionRefund.store == store)
    rows = q.order_by(TransactionRefund.business_date.desc(), TransactionRefund.id.desc()).all()
    cols = ["id", "business_date", "store", "gateway", "transaction_id", "amount", "zoho_no", "customer_name",
            "refund_date", "status", "uploaded_by", "uploaded_at"]
    return pd.DataFrame([{
        "id": r.id, "business_date": r.business_date, "store": r.store, "gateway": r.gateway,
        "transaction_id": r.transaction_ref, "amount": float(r.amount or 0), "zoho_no": r.zoho_no,
        "customer_name": r.customer_name, "refund_date": r.refund_date, "status": r.refund_status,
        "uploaded_by": r.uploaded_by, "uploaded_at": r.uploaded_at} for r in rows], columns=cols)


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
        already_zoho = bool(zoho) and zoho in pf.refund_zoho and _norm_name(name) not in pf.refund_zoho[zoho]
    else:
        already_txn = bool(session.query(TransactionRefund.id).filter(
            TransactionRefund.gateway == gateway, TransactionRefund.transaction_ref == ref).first())
        already_zoho = bool(zoho) and any(
            _norm_name(n) != _norm_name(name) for (n,) in
            session.query(TransactionRefund.customer_name).filter(TransactionRefund.zoho_no == zoho).all())
    if already_txn:
        problems.append(f"transaction {ref} is already recorded as a refund")
    if already_zoho:
        problems.append(f"zoho_no {zoho} is already used on another refund (of a different customer)")
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


def split_problem(session, bill_no: str, gateway: str, ref: str, refund_amount: float, txn_amount: float, store: str):
    """None when ONE transaction can be split into a bill and a customer refund; otherwise why not. Rules: the bill is open,
    in the transaction's store, and bill + refund equals the transaction EXACTLY (nothing is written off)."""
    from .manual_match import find_bill_for_match, latest_completed_run_id
    bill, is_open, why = find_bill_for_match(session, latest_completed_run_id(session), bill_no)
    if bill is None or not is_open:
        return f"bill {bill_no}: {why or 'is not open'}"
    if _clean(bill["store"]).upper() != _clean(store).upper():
        return f"bill {bill_no} is in store {bill['store']}, the transaction is in {store}"
    if abs(float(bill["amount"]) + refund_amount - txn_amount) > 0.005:
        return (f"bill {bill_no} Rs {float(bill['amount']):,.2f} + refund Rs {refund_amount:,.2f} = "
                f"Rs {float(bill['amount']) + refund_amount:,.2f}, not the transaction's Rs {txn_amount:,.2f}")
    return None


def record_refund_with_bill(session, bill_no: str, gateway: str, ref: str, refund_amount: float, customer_name, zoho_no,
                            refund_date, operator: str, source: str = "inline", commit: bool = True):
    """ONE transaction that paid a bill AND overpaid it: the bill is matched to the transaction (a manual match recorded
    with the amount difference as its written override) and the REST goes back to the customer as a refund of
    `refund_amount`. Both close the transaction's exception; one database transaction, so either both are recorded or
    neither. The transaction counts once (as refunded) in the strips, its value split bill / refund."""
    from . import manual_match as mm

    ref = _clean_ref(ref)
    gateway = str(gateway).lower()
    pool = targeted_pool_many([ref])
    hits = pool[pool["txn_ref"].astype(str) == ref] if len(pool) else pool
    if hits.empty:
        raise ValueError(f"transaction {ref} was not found in the loaded gateway data")
    txn = hits.iloc[0].to_dict()
    txn_amount = float(txn.get("amount") or 0)
    problem = split_problem(session, bill_no, gateway, ref, float(refund_amount), txn_amount, txn.get("store"))
    if problem:
        raise ValueError(problem)
    run_id = mm.latest_completed_run_id(session)
    bill, _, _ = mm.find_bill_for_match(session, run_id, bill_no)
    try:
        record_refund(session, dict(txn, gateway=gateway, txn_ref=ref, amount=float(refund_amount)), customer_name, zoho_no,
                      refund_date, operator, source=source, commit=False)
        v = mm.validate(session, bill, gateway, ref, pool)
        note = (f"transaction Rs {txn_amount:,.2f} = bill Rs {float(bill['amount']):,.2f} + customer refund "
                f"Rs {float(refund_amount):,.2f} (Zoho {_clean(zoho_no)})")
        mm.record(session, bill, gateway, ref, "manual_match_by_bill", operator, v, note=note, override_reason=note,
                  source=source, commit=False)
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise


def refund_register(session) -> pd.DataFrame:
    """Every refund event ever (recorded and reversed) - the audit record."""
    from .db import RefundEvent
    rows = session.query(RefundEvent).order_by(RefundEvent.id.desc()).all()
    return pd.DataFrame([{"at": r.at, "action": r.action, "by": r.by, "gateway": r.gateway,
                          "transaction_id": r.transaction_ref, "store": r.store, "amount": float(r.amount or 0),
                          "zoho_no": r.zoho_no, "customer_name": r.customer_name, "refund_date": r.refund_date,
                          "source": r.source, "reason": r.reason} for r in rows])


# ---------------------------------------------------------------------------
# The "Customer Refund" folder (2026-10-29): the finance team's own workbook, read as it is.
# ---------------------------------------------------------------------------
# Columns are found by NAME (case, spacing and underscores ignored), never by position. Only what the reconciliation needs is read:
# the transaction number, Zoho id, customer name, store, transaction date, process date and amount. The bank details in the
# workbook (IFSC, account number) and the approver are never read into the application or stored.
FOLDER_COLUMNS = {"bill no": "bill_no", "transaction no": "transaction_no", "zoho id": "zoho_no", "name": "customer_name", "store code": "store_code",
                  "tran date": "date", "process date": "process_date", "amt": "amount_file", "remarks": "remark",
                  "transaction source": "source"}
DEFAULT_FOLDER = Path(__file__).resolve().parents[2] / "Customer Refund"


def _norm_header(h) -> str:
    return " ".join(str(h).replace("_", " ").split()).lower() if h is not None else ""


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _as_iso(v) -> str:
    if v is None or v == "":
        return ""
    if hasattr(v, "date"):
        return v.date().isoformat() if hasattr(v, "hour") else v.isoformat()
    return str(v).strip()


def read_refund_folder(folder=None):
    """(frame, files): every .xlsx in the folder (an Excel lock file `~$...` is skipped), one row per refund line, in the shape
    `preview_upload` reads plus `process_date`, `amount_file`, `remark`, `file`, `file_row`. `status` is `released` when the
    workbook carries a process date and `pending` when it does not (approved, not yet paid)."""
    import openpyxl

    folder = Path(folder) if folder else DEFAULT_FOLDER
    files, rows = [], []
    for path in sorted(folder.glob("*.xlsx")) if folder.exists() else []:
        if path.name.startswith("~$"):
            continue
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        n = 0
        for ws in wb.worksheets:
            grid = list(ws.iter_rows(values_only=True))
            if not grid:
                continue
            header = [FOLDER_COLUMNS.get(_norm_header(h)) for h in grid[0]]
            if "transaction_no" not in header:
                continue
            for pos, raw in enumerate(grid[1:], start=2):
                rec = {name: raw[i] for i, name in enumerate(header) if name and i < len(raw)}
                if not any(v not in (None, "") for v in rec.values()):
                    continue
                process = _as_iso(rec.get("process_date"))
                rows.append({
                    "date": _as_iso(rec.get("date")), "store_code": masters.canonical_store(_as_text(rec.get("store_code")).upper()),
                    "transaction_no": _as_text(rec.get("transaction_no")), "zoho_no": _as_text(rec.get("zoho_no")),
                    "customer_name": _as_text(rec.get("customer_name")), "status": "released" if process else "pending",
                    "process_date": process, "amount_file": float(rec["amount_file"]) if rec.get("amount_file") not in (None, "") else None,
                    "remark": _as_text(rec.get("remark")), "file": path.name, "file_row": pos,
                    "bill_no": _as_text(rec.get("bill_no"))})
                n += 1
        files.append({"file": path.name, "modified": datetime.fromtimestamp(path.stat().st_mtime), "rows": n})
    cols = UPLOAD_COLUMNS + ["process_date", "amount_file", "remark", "file", "file_row", "bill_no"]
    return pd.DataFrame(rows, columns=cols), files


_PREVIEW_COLUMNS = ["file", "file_row", "bill_no", "transaction_no", "store_code", "zoho_no", "customer_name", "amount_file",
                    "txn_amount", "gateway", "process_date", "business_date", "store_used", "verdict", "why", "notes",
                    "extra_remark"]


def _open_candidates(session, store: str, amount) -> list:
    """Open unmapped transactions of this store and EXACT amount - offered to a person when the workbook gives no
    transaction number. Never applied: a refund needs the transaction named."""
    from .db import Exception_
    if not store or amount is None:
        return []
    rows = session.query(Exception_).filter(
        Exception_.type == "unmapped_gateway_transaction", Exception_.store == store,
        Exception_.status.in_(("open", "under_investigation"))).all()
    return [e.exception_key.split("|", 1)[1] for e in rows if abs(float(e.amount or 0) - float(amount)) < 0.005]


def preview_folder(session, frame: pd.DataFrame) -> pd.DataFrame:
    """Every refund line judged. A line is `apply` only when the ordinary refund rules pass (`preview_upload`, folder mode) and
    it carries a process date (approved but unpaid is held) and its amount equals the transaction (a difference is carried,
    never written off). Folder-specific handling, each one visible in `notes` / `extra_remark`, never silent:
      * ONE Zoho ticket may cover several payments of the same customer (each payment is its own refund);
      * ONE transaction named by two lines is refunded ONCE - the line with the earlier process date (then the earlier row)
        is recorded and the other ticket is written into the exception's remark as a possible second payout, not recorded;
      * a store the master lacks but which is the transaction's own store is accepted, and a store in the file that differs
        from the transaction's store is recorded with both named.
    Returns one row per line, in file order, with `verdict` (apply / hold) and `why`."""
    if frame.empty:
        return pd.DataFrame(columns=_PREVIEW_COLUMNS)
    frame = frame.reset_index(drop=True)
    refs = frame["transaction_no"].map(lambda v: _clean_ref(v) if v else "")
    # a transaction named by more than one line: the earliest process date (blank last), then the earliest file row, is primary
    order = sorted(range(len(frame)), key=lambda i: (frame.at[i, "process_date"] or "9999", int(frame.at[i, "file_row"])))
    primary, secondary_of = {}, {}
    for i in order:
        r = refs[i]
        if not r or r.lower() == "not found":
            continue
        if r in primary:
            secondary_of.setdefault(r, []).append(i)
        else:
            primary[r] = i
    secondary = {i for idxs in secondary_of.values() for i in idxs}
    keep = [i for i in range(len(frame)) if i not in secondary]
    base = preview_upload(session, frame.loc[keep, UPLOAD_COLUMNS].reset_index(drop=True), folder_mode=True)
    outcome_of = dict(zip(keep, base.rows))

    out = []
    for i, rec in enumerate(frame.to_dict("records")):
        ref = refs[i]
        row = {"file": rec["file"], "file_row": rec["file_row"], "bill_no": rec.get("bill_no") or "",
               "transaction_no": rec["transaction_no"],
               "store_code": rec["store_code"], "zoho_no": rec["zoho_no"], "customer_name": rec["customer_name"],
               "amount_file": rec["amount_file"], "txn_amount": None, "gateway": None, "process_date": rec["process_date"],
               "business_date": None, "store_used": rec["store_code"], "verdict": "hold", "why": "", "notes": "",
               "extra_remark": ""}
        if i in secondary:
            first = frame.loc[primary[ref]]
            row["why"] = (f"transaction is already refunded by row {first['file_row']} (Zoho {first['zoho_no']}, "
                          f"processed {first['process_date'] or 'no date'}) - a transaction is refunded once; this second ticket "
                          f"(Zoho {rec['zoho_no']}, Rs {rec['amount_file']:,.2f}) is a possible duplicate payout and is noted "
                          "on the exception, not recorded")
            out.append(row)
            continue
        oc = outcome_of[i]
        why = list(oc.why)
        row.update(txn_amount=oc.amount, gateway=oc.gateway, business_date=oc.business_date,
                   store_used=oc.store_used or rec["store_code"], notes=" | ".join(oc.notes))
        if not ref or ref.lower() == "not found":
            cands = _open_candidates(session, rec["store_code"], rec["amount_file"])
            why = ["no transaction number in the workbook" + (
                f" - {len(cands)} open transaction(s) of {rec['store_code']} for exactly Rs {rec['amount_file']:,.2f}: "
                + ", ".join(cands[:5]) if cands else f" - and no open transaction of {rec['store_code']} for exactly "
                f"Rs {rec['amount_file']:,.2f}")]
        elif oc.verdict == "accept":
            if not rec["process_date"]:
                why.append("approved but no process date yet - not paid, so not recorded")
            amt, txn_amt = rec.get("amount_file"), oc.amount
            if amt is not None and txn_amt is not None and abs(float(amt) - float(txn_amt)) > 0.005:
                if rec.get("bill_no"):
                    # The rest of the transaction paid a named bill: bill + refund must equal the transaction exactly.
                    problem = split_problem(session, rec["bill_no"], oc.gateway, ref, float(amt), float(txn_amt),
                                            oc.store_used or rec["store_code"])
                    if problem:
                        why.append(problem)
                    else:
                        row["notes"] = (row["notes"] + " | " if row["notes"] else "") + (
                            f"transaction Rs {float(txn_amt):,.2f} = bill {rec['bill_no']} + refund Rs {float(amt):,.2f}")
                else:
                    why.append(f"refund amount in the file (Rs {float(amt):,.2f}) differs from the transaction "
                               f"(Rs {float(txn_amt):,.2f}) - a difference is carried, never written off")
        if ref in secondary_of:
            row["extra_remark"] = " | ".join(
                f"Second refund ticket in the workbook for this transaction: Zoho {frame.at[j, 'zoho_no']}, "
                f"{frame.at[j, 'customer_name']}, Rs {float(frame.at[j, 'amount_file']):,.2f}, processed "
                f"{frame.at[j, 'process_date'] or 'no date'} (row {frame.at[j, 'file_row']}) - possible duplicate payout, "
                "not recorded" for j in secondary_of[ref])
        row["verdict"] = "apply" if oc.verdict == "accept" and not why else "hold"
        row["why"] = " | ".join(why)
        if row["verdict"] == "hold" and any("already recorded as a refund" in w for w in why):
            row["verdict"], row["why"] = "recorded", "already recorded as a customer refund"
        out.append(row)
    return pd.DataFrame(out, columns=_PREVIEW_COLUMNS)


def apply_folder(session, judged: pd.DataFrame, operator: str) -> int:
    """Record every `apply` row as a customer refund (released, with its process date): the transaction leaves Exception
    transactions, appears under Customer refunds, and carries a remark naming the customer, the Zoho id and the process date
    (plus any note: a store difference, a second ticket for the same transaction). One database transaction: a failure
    records nothing."""
    if not _clean(operator):
        raise ValueError("A refund must record who applied it")
    from .db import Exception_, Remark

    n = 0
    try:
        for r in judged[judged["verdict"] == "apply"].to_dict("records"):
            txn = {"gateway": r["gateway"], "txn_ref": r["transaction_no"], "business_date": r["business_date"],
                   "store": r["store_used"] or r["store_code"], "amount": r["txn_amount"]}
            if r.get("bill_no") and r["txn_amount"] is not None and abs(float(r["amount_file"]) - float(r["txn_amount"])) > 0.005:
                record_refund_with_bill(session, r["bill_no"], r["gateway"], r["transaction_no"], float(r["amount_file"]),
                                        r["customer_name"], r["zoho_no"], r["process_date"], operator, source="folder",
                                        commit=False)
            else:
                record_refund(session, txn, r["customer_name"], r["zoho_no"], r["process_date"], operator,
                              source="folder", commit=False)
            exc = session.query(Exception_).filter_by(
                exception_key=f"{str(r['gateway']).lower()}|{_clean_ref(r['transaction_no'])}").one_or_none()
            if exc is not None:
                text = f"Customer refund - {r['customer_name']}, Zoho {r['zoho_no']}, processed {r['process_date']}"
                for extra in (r.get("notes"), r.get("extra_remark")):
                    if isinstance(extra, str) and extra.strip():
                        text += f". {extra.strip()}"
                session.add(Remark(exception_id=exc.id, created_by=operator, created_at=datetime.now(), remark_text=text))
            n += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(f"refunds: {n} refund(s) recorded from the Customer Refund folder by {operator}")
    return n
