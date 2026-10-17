"""The lifecycle of a closed exception - one place for the rules that keep every view in step (2026-10-16).

A bill and a transaction close TOGETHER, by a manual match (inline or upload) or - for a transaction -
by a customer refund. This module holds what is not tied to one table:

  * `sync_closures()`  - brings the exception register in line with the manual matches and refunds in
    force: a transaction a person matched or refunded is `closed`, never listed as open. Idempotent;
    run after every re-run and by the closure script, so a record made before this rule existed is
    brought forward and a re-run can never re-open what a person closed.

No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import logging

from .db import ManualMatch, TransactionRefund
from .manual_match import close_txn_exception

logger = logging.getLogger("mop_reco")


def sync_closures(session, operator: str = "system") -> dict:
    """Close the transaction exception of every active accepted manual match and every refund.
    Returns {"matched": n closed, "refunded": n closed}."""
    matched = refunded = 0
    for m in (session.query(ManualMatch).filter(ManualMatch.lifecycle == "active",
                                               ManualMatch.status == "matched_manual").all()):
        if close_txn_exception(session, m.gateway, m.transaction_ref, m.operator or operator, "matched manually"):
            matched += 1
    for r in session.query(TransactionRefund).all():
        if close_txn_exception(session, r.gateway, r.transaction_ref, r.uploaded_by or operator, "customer refund"):
            refunded += 1
    session.commit()
    logger.info(f"lifecycle: closed {matched} matched and {refunded} refunded transaction exception(s)")
    return {"matched": matched, "refunded": refunded}
