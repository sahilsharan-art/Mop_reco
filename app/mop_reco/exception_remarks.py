"""Editable remarks on Exception bills/transactions — inline edit and bulk
upload, both writing through the same underlying primitive,
`exceptions.add_remark()` (2026-09-30).

A remark is annotation only — it never changes a figure, a status or a
match (CLAUDE.md non-negotiable rule 4: nothing here is ever a silent
overwrite). Every remark is a NEW `Remark` row, append-only; the exception
register's own `exception_key` (business_date|store|bill_no|mop for a
bill, gateway|transaction_id for a transaction — §6.9) is what makes a
remark survive a full re-run of a restated date.

Bulk upload follows the same validate-everything-then-apply-all-or-nothing
shape `manual_match.py`'s and `refunds.py`'s own bulk uploads already use:
every row is checked before anything is written, the operator sees exactly
what will happen and why, and only THEN does anything touch the database —
in one all-or-nothing batch, per the operator's own instruction ("applies —
all or nothing, never partially").
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from . import io_
from .db import Exception_, Remark
from .exceptions import OPEN_STATUSES, bill_exception_key, txn_exception_key

BILL_TEMPLATE_COLUMNS = ["business_date", "store", "bill_no", "mop", "remark"]
TXN_TEMPLATE_COLUMNS = ["gateway", "transaction_id", "remark"]


@dataclass
class RemarkRow:
    """One row of a bulk-upload file, validated. Never written until the
    operator confirms — `ok` says whether it WOULD be accepted."""
    row_index: int
    exception_key: str
    remark: str
    ok: bool
    reason: str = ""
    fields: dict = field(default_factory=dict)  # the row's own typed values,
                                                # for the preview table


def _lookup(session, exception_key: str):
    return session.query(Exception_).filter_by(exception_key=exception_key).one_or_none()


def _validate_one(session, exception_key: str, remark: str):
    """Returns (ok, reason) — the three checks the operator specified:
    the row exists, it is currently an open exception, the remark is not
    empty. Never guessed, never partially accepted (rule 5)."""
    row = _lookup(session, exception_key)
    if row is None:
        return False, "no exception with this key"
    if row.status not in OPEN_STATUSES:
        return False, f"not an open exception (status is '{row.status}')"
    if not remark:
        return False, "remark is empty"
    return True, ""


def validate_bill_remarks(session, df: pd.DataFrame) -> list:
    """`df` columns: business_date, store, bill_no, mop, remark (any case/
    order — see `_read_upload`). One RemarkRow per input row, in order."""
    results = []
    for i, r in df.iterrows():
        business_date = str(r.get("business_date", "")).strip()
        store = str(r.get("store", "")).strip()
        bill_no = str(r.get("bill_no", "")).strip()
        mop = str(r.get("mop", "")).strip()
        remark = str(r.get("remark", "") or "").strip()
        key = bill_exception_key(business_date, store, bill_no, mop)
        ok, reason = _validate_one(session, key, remark)
        results.append(RemarkRow(
            i, key, remark, ok, reason,
            fields={"business_date": business_date, "store": store,
                   "bill_no": bill_no, "mop": mop}))
    return results


def validate_txn_remarks(session, df: pd.DataFrame) -> list:
    """`df` columns: gateway, transaction_id, remark. A leading apostrophe
    from Excel is stripped and the id matched on its full digit string as
    text, exactly like every other reference id in this app
    (`io_.clean_ref()`, already used for this same reason elsewhere)."""
    results = []
    for i, r in df.iterrows():
        gateway = str(r.get("gateway", "")).strip()
        transaction_id = io_.clean_ref(r.get("transaction_id")) or ""
        remark = str(r.get("remark", "") or "").strip()
        key = txn_exception_key(gateway, transaction_id)
        ok, reason = _validate_one(session, key, remark)
        results.append(RemarkRow(
            i, key, remark, ok, reason,
            fields={"gateway": gateway, "transaction_id": transaction_id}))
    return results


def apply_bulk_remarks(session, rows: list, operator: str) -> int:
    """All-or-nothing — called only with rows the caller has already
    confirmed are every one `ok`. Builds every `Remark` row and commits
    ONCE at the end, so a mid-batch failure leaves nothing partially
    written — deliberately NOT a loop of the single-row `add_remark()`
    (which commits per row, the right behaviour for one inline edit but
    not for a batch that must apply as a unit, on the operator's own
    instruction: "applies — all or nothing, never partially"). Returns the
    count written."""
    now = datetime.now()
    remark_objs = []
    for r in rows:
        exc_row = _lookup(session, r.exception_key)
        if exc_row is None:
            raise ValueError(f"no exception with key {r.exception_key!r}")
        remark_objs.append(Remark(exception_id=exc_row.id, remark_text=r.remark,
                                  created_by=operator, created_at=now))
    session.add_all(remark_objs)
    session.commit()
    return len(remark_objs)


def preview_frame(rows: list) -> pd.DataFrame:
    """The accepted/rejected preview table — every row, its own key fields,
    the remark, and why it was rejected (blank when accepted)."""
    out = []
    for r in rows:
        record = dict(r.fields)
        record["remark"] = r.remark
        record["result"] = "Accepted" if r.ok else "Rejected"
        record["reason"] = r.reason
        out.append(record)
    return pd.DataFrame(out)


def read_upload(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Reads the operator's uploaded file (csv or xlsx) into a plain
    DataFrame, header names lower-cased/stripped so a template filled in
    Excel (which may title-case or pad a header) still matches."""
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, keep_default_na=False)
    else:
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
        df = df.fillna("")
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return df


def bill_template_csv() -> bytes:
    return pd.DataFrame(columns=BILL_TEMPLATE_COLUMNS).to_csv(index=False).encode("utf-8")


def txn_template_csv() -> bytes:
    return pd.DataFrame(columns=TXN_TEMPLATE_COLUMNS).to_csv(index=False).encode("utf-8")
