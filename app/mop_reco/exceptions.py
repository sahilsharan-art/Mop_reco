"""The exception register. CLAUDE.md sections 6.9 / non-negotiable rules 3 & 4.

Deterministic keys, so a re-run finds the same row rather than creating a new
one. Remarks and status survive a full re-run of a restated date. An exception
that disappears is marked auto_resolved with a reason — never deleted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .db import Exception_, Remark

logger = logging.getLogger("mop_reco")

EXCEPTION_TYPES = (
    "bill_not_matched",
    "ambiguous_edc_other",
    "unmapped_gateway_transaction",
    "original_txn_not_found",
    "store_ambiguous",
    "no_source_report",
)

OPEN_STATUSES = ("open", "under_investigation")


@dataclass
class CandidateException:
    """One exception as this run sees it, before it meets the register."""
    exception_key: str
    reconciliation: str
    type: str
    business_date: str
    store: str = None
    amount: float = None
    detail: dict = field(default_factory=dict)
    # The gateway transaction's own time, 'HH:MM:SS' or None — a real
    # column on Exception_ (2026-09-29), not inside `detail`. Only ever
    # set for type "unmapped_gateway_transaction"; None for Axis (that
    # gateway supplies no real time at all — see r1_bill_txn.build_
    # gateway_pool) and for every other exception type.
    transaction_time: str = None


# ---------------------------------------------------------------------------
# Keys — deterministic, per section 6.9
# ---------------------------------------------------------------------------

def bill_exception_key(business_date, store, bill_no, mop) -> str:
    return f"{business_date}|{store}|{bill_no}|{mop}"


def txn_exception_key(gateway, transaction_id) -> str:
    return f"{gateway}|{transaction_id}"


# ---------------------------------------------------------------------------
# Building this run's candidates
# ---------------------------------------------------------------------------

def _clean(value):
    """pd.NA / NaN -> None, so the detail JSON holds nulls rather than the
    string 'NA' or a value that raises when tested for truth."""
    return None if pd.isna(value) else value


def _linkage_detail(row) -> dict:
    """A refund/reversal carries TWO dates and both must be visible.

    Without this, a July run shows an exception dated 2026-05-21 and it looks
    like May data leaked in. It didn't: these are July-file rows reversing much
    older sales. So we record the reversal's own date, the date encoded in its
    own transaction id, and the date of the missing original (also from its id,
    which is the only way to date a row that was never loaded).

    Two July chargeback-reversals disagree with themselves — their
    transaction_date says May while their transaction_id encodes July. That
    disagreement is surfaced, not smoothed over.
    """
    own_date = _clean(row.get("business_date"))
    own_id = row["transaction_id"]
    original_id = row["reference_transaction_id"]

    id_date = date_from_paytm_txn_id(own_id)
    original_date = date_from_paytm_txn_id(original_id)

    detail = {
        "transaction_type": row["transaction_type"],
        "transaction_id": own_id,
        "reversal_date": str(own_date) if own_date is not None else None,
        "reversal_date_from_id": str(id_date) if id_date else None,
        "missing_original_id": original_id,
        "original_transaction_date": str(original_date) if original_date else None,
        "note": (
            "This is a reversal booked in the loaded period against an earlier sale. "
            "The original sale is outside the loaded range, so it could not be linked."
        ),
    }

    if own_date is not None and id_date is not None and str(own_date) != str(id_date):
        detail["date_disagreement"] = (
            f"the row's transaction_date says {own_date} but its transaction_id encodes "
            f"{id_date} — the file disagrees with itself; treat the id-encoded date as "
            "when the reversal was raised"
        )

    if original_date is not None:
        detail["age_of_original_days"] = None
        try:
            from datetime import date as _date
            ref = id_date or (own_date if isinstance(own_date, _date) else None)
            if ref is not None:
                detail["age_of_original_days"] = (ref - original_date).days
        except (TypeError, ValueError):
            pass

    return detail


def build_r1_exceptions(bill_lines: pd.DataFrame, result) -> list:
    """Every R1 exception this run found. A bill with no resolving reference is
    one exception — never one per unresolved reference (6.3)."""
    candidates = []

    unmatched = bill_lines[~bill_lines["matched"]]
    for _, row in unmatched.iterrows():
        # match_tier is pd.NA on an unmatched bill; `NA or ""` raises rather
        # than falling through, so test for missing explicitly.
        tier = row.get("match_tier")
        is_ambiguous = (not pd.isna(tier)) and str(tier) == "Ambiguous"
        candidates.append(CandidateException(
            exception_key=bill_exception_key(
                row["business_date"], row["store"], row["bill_no"], row["mop"]),
            reconciliation="R1",
            type="ambiguous_edc_other" if is_ambiguous else "bill_not_matched",
            business_date=str(row["business_date"]),
            store=str(row["store"]),
            amount=float(row["amount"]),
            detail={
                "bill_no": row["bill_no"],
                "mop": row["mop"],
                "line_seq": int(1 if pd.isna(row.get("line_seq")) else row.get("line_seq")),
                "status": _clean(row.get("status")),
                "paytm_ref": _clean(row.get("paytm_ref")),
                "axis_ref": _clean(row.get("axis_ref")),
                "bajaj_ref": _clean(row.get("bajaj_ref")),
                "refs_tried": int(0 if pd.isna(row.get("refs_tried")) else row.get("refs_tried")),
                "ambiguous_candidates": _clean(row.get("ambiguous_candidates")),
            },
        ))

    for _, row in result.unmapped_txns.iterrows():
        txn_time = row.get("txn_time")
        # Genuinely None for Axis (see r1_bill_txn.build_gateway_pool's own
        # note — Axis's own TRAN_TIME is a constant placeholder, never a
        # real time) and for any Bajaj/Paytm row whose own time failed to
        # parse. A real datetime.time formats to plain 'HH:MM:SS'.
        transaction_time = (txn_time.strftime("%H:%M:%S")
                            if txn_time is not None and not pd.isna(txn_time) else None)
        candidates.append(CandidateException(
            exception_key=txn_exception_key(row["gateway"], row["txn_ref"]),
            reconciliation="R1",
            type="unmapped_gateway_transaction",
            business_date=str(row["business_date"]),
            store=str(row["store"]) if pd.notna(row.get("store")) else None,
            amount=float(row["amount"]),
            detail={"gateway": row["gateway"], "transaction_id": row["txn_ref"],
                    "payment_mode": row.get("payment_mode")},
            transaction_time=transaction_time,
        ))

    for _, row in result.linkage_exceptions.iterrows():
        candidates.append(CandidateException(
            exception_key=txn_exception_key("paytm", row["transaction_id"]),
            reconciliation="R1",
            type="original_txn_not_found",
            business_date=str(row["business_date"]),
            store=str(row["store"]) if pd.notna(row.get("store")) else None,
            amount=float(row["amount"]),
            detail=_linkage_detail(row),
        ))

    return candidates


# ---------------------------------------------------------------------------
# Meeting the register — the part that must never lose a remark
# ---------------------------------------------------------------------------

def sync_exceptions(session, run_id: int, candidates: list) -> dict:
    """Reconcile this run's candidates against the register.

    - already there  -> keep its status, remarks and ticket reference; advance
                        last_seen. If the amount moved, flag amount_revised.
    - new            -> create as `open`.
    - gone           -> auto_resolved with a reason. NEVER deleted (rule 4).
    """
    now = datetime.now()
    seen_keys = set()
    created = updated = amount_revised = 0

    # A key (business_date|store|bill_no|mop) is deliberately coarser than
    # one bill LINE (CLAUDE.md 6.9) — a bill with two unmatched lines that
    # happen to share the same MOP collapses into ONE exception by design.
    # Found 2026-09-24, the hard way: this collapsing was never actually
    # enforced before hitting the database — two same-key candidates in one
    # batch both took the "not in `existing`" branch below (built once, from
    # the DB, never updated as this loop adds new rows) and both tried to
    # INSERT, and the second violated sqlite's own UNIQUE constraint on
    # exception_key, crashing the entire run. Merged here, before touching
    # the database at all: amounts SUM — money at risk is never silently
    # dropped by a merge (rule 4's "no write-offs" applies just as much to
    # an accidental one as a deliberate one) — and `detail` keeps every
    # contributing line's own line_seq, so the merge itself is visible, not
    # a silent loss of one line's identity.
    merged: dict = {}
    merge_notes = []
    for cand in candidates:
        prior = merged.get(cand.exception_key)
        if prior is None:
            merged[cand.exception_key] = cand
            continue
        prior_lines = prior.detail.get("merged_line_seqs") or [prior.detail.get("line_seq")]
        combined_detail = dict(cand.detail)
        combined_detail["merged_line_seqs"] = list(prior_lines) + [cand.detail.get("line_seq")]
        merged[cand.exception_key] = CandidateException(
            exception_key=cand.exception_key, reconciliation=cand.reconciliation,
            type=cand.type, business_date=cand.business_date, store=cand.store,
            amount=float(prior.amount or 0) + float(cand.amount or 0),
            detail=combined_detail,
            transaction_time=cand.transaction_time or prior.transaction_time,
        )
        merge_notes.append(cand.exception_key)
    if merge_notes:
        logger.warning(
            f"run {run_id}: {len(merge_notes)} exception key(s) had more than one "
            f"candidate in this batch — a bill with 2+ unmatched lines sharing the "
            f"same MOP, which the exception key deliberately collapses to one row "
            f"— merged with amounts summed, never dropped: {merge_notes[:10]}"
            + (" ..." if len(merge_notes) > 10 else ""))
    candidates = list(merged.values())

    existing = {e.exception_key: e for e in session.query(Exception_).all()}

    for cand in candidates:
        seen_keys.add(cand.exception_key)
        row = existing.get(cand.exception_key)
        detail_json = json.dumps(cand.detail, default=str)

        if row is None:
            session.add(Exception_(
                exception_key=cand.exception_key,
                reconciliation=cand.reconciliation,
                type=cand.type,
                business_date=cand.business_date,
                store=cand.store,
                amount=cand.amount,
                detail=detail_json,
                status="open",
                first_seen_run_id=run_id,
                last_seen_run_id=run_id,
                transaction_time=cand.transaction_time,
            ))
            created += 1
            continue

        # Seen before: the remark and status are the operator's, not ours.
        if row.amount is not None and cand.amount is not None and float(row.amount) != float(cand.amount):
            detail = dict(cand.detail)
            detail["amount_revised"] = {"was": float(row.amount), "now": float(cand.amount)}
            detail_json = json.dumps(detail, default=str)
            amount_revised += 1

        row.amount = cand.amount
        row.detail = detail_json
        row.type = cand.type
        row.last_seen_run_id = run_id
        row.transaction_time = cand.transaction_time
        if row.status == "auto_resolved":
            # It came back. Reopen rather than leaving a resolved row that is
            # demonstrably still a live difference.
            row.status = "open"
            row.closed_on = None
        updated += 1

    auto_resolved = 0
    for key, row in existing.items():
        if key in seen_keys:
            continue
        if row.status in OPEN_STATUSES:
            row.status = "auto_resolved"
            row.closed_on = now
            row.closed_by = f"run {run_id}"
            detail = json.loads(row.detail) if row.detail else {}
            detail["auto_resolved_reason"] = (
                "no longer present in the source data as at this run — "
                "the underlying bill or transaction is gone (e.g. a duplicate bill deleted by IT)"
            )
            row.detail = json.dumps(detail, default=str)
            auto_resolved += 1

    session.commit()
    outcome = {"created": created, "updated": updated,
               "auto_resolved": auto_resolved, "amount_revised": amount_revised,
               "total_candidates": len(candidates)}
    logger.info(f"run {run_id}: exception register — {outcome}")
    return outcome


def add_remark(session, exception_key: str, text: str, operator: str) -> Remark:
    """A remark is never overwritten; each is stamped with who and when."""
    row = session.query(Exception_).filter_by(exception_key=exception_key).one_or_none()
    if row is None:
        raise ValueError(f"no exception with key {exception_key!r}")
    remark = Remark(exception_id=row.id, remark_text=text,
                    created_by=operator, created_at=datetime.now())
    session.add(remark)
    session.commit()
    return remark


def set_status(session, exception_key: str, status: str, operator: str,
               reason_code: str = None) -> Exception_:
    """`reason_code` records WHY the status changed — e.g. 'matched manually'.
    The remark history is never touched by a status change (rule 4)."""
    valid = ("open", "under_investigation", "closed")
    if status not in valid:
        raise ValueError(f"status must be one of {valid}, got {status!r}")
    row = session.query(Exception_).filter_by(exception_key=exception_key).one_or_none()
    if row is None:
        raise ValueError(f"no exception with key {exception_key!r}")
    row.status = status
    if reason_code:
        row.reason_code = reason_code
    if status == "closed":
        row.closed_on = datetime.now()
        row.closed_by = operator
    else:
        row.closed_on = None
        row.closed_by = None
    session.commit()
    return row


def remarks_for(session, exception_key: str) -> list:
    row = session.query(Exception_).filter_by(exception_key=exception_key).one_or_none()
    if row is None:
        return []
    return (session.query(Remark).filter_by(exception_id=row.id)
            .order_by(Remark.created_at.asc()).all())


# ---------------------------------------------------------------------------
# Severity — drives the row stripe AND a text label, so it never depends on
# colour alone. Type first (what kind of problem), then value (how much).
# ---------------------------------------------------------------------------

# Money at the gateway with no bill behind it is the most serious class: it is
# collected-but-not-booked. An unmatched bill is money we may never have taken.
SEVERITY_BY_TYPE = {
    "unmapped_gateway_transaction": "action",
    "bill_not_matched": "attention",
    "original_txn_not_found": "attention",
    "ambiguous_edc_other": "review",
    "store_ambiguous": "attention",
    "no_source_report": "attention",
}

# Value escalation thresholds (rupees).
SEVERITY_VALUE_ACTION = 100_000
SEVERITY_VALUE_ATTENTION = 10_000

SEVERITY_ORDER = {"action": 0, "attention": 1, "review": 2}

SEVERITY_LABEL = {
    "action": "Action",
    "attention": "Attention",
    "review": "Review",
}

SEVERITY_COLOUR = {
    "action": "#c0392b",      # red
    "attention": "#d68910",   # amber
    "review": "#7f8c8d",      # grey
}


def severity_for(exception_type: str, amount) -> str:
    """'action' | 'attention' | 'review'.

    Value can only escalate, never de-escalate: a large 'review' item becomes
    'attention', but a tiny 'action' item stays 'action' because the *kind* of
    problem is what makes it serious.
    """
    base = SEVERITY_BY_TYPE.get(exception_type, "attention")
    try:
        value = abs(float(amount or 0))
    except (TypeError, ValueError):
        value = 0.0

    if value >= SEVERITY_VALUE_ACTION:
        escalated = "action"
    elif value >= SEVERITY_VALUE_ATTENTION:
        escalated = "attention"
    else:
        escalated = base

    return escalated if SEVERITY_ORDER[escalated] < SEVERITY_ORDER[base] else base


def date_from_paytm_txn_id(transaction_id):
    """Paytm transaction ids lead with YYYYMMDD — '20260523010890...' is
    2026-05-23. This is the only way to date a referenced original that is
    outside the loaded range, since its own row was never read.

    Returns a date, or None if the id doesn't start with a plausible date.
    """
    from datetime import datetime

    if not transaction_id:
        return None
    head = str(transaction_id).strip()[:8]
    if not head.isdigit():
        return None
    try:
        parsed = datetime.strptime(head, "%Y%m%d").date()
    except ValueError:
        return None
    if not (2000 <= parsed.year <= 2100):
        return None
    return parsed


# Readable names for the register and the summary. The raw type stays the
# stable value in the database; this is only how it is written on screen.
TYPE_LABEL = {
    "unmapped_gateway_transaction": "Gateway payment with no bill",
    "bill_not_matched": "Bill with no gateway payment",
    "original_txn_not_found": "Reversal whose original is out of range",
    "ambiguous_edc_other": "EDC OTHER — more than one candidate",
    "store_ambiguous": "Terminal maps to more than one store",
    "no_source_report": "No gateway report for this MOP",
}


def type_label(exception_type: str) -> str:
    return TYPE_LABEL.get(exception_type, str(exception_type).replace("_", " ").capitalize())
