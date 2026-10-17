"""The Exceptions tables - ONE definition of their columns, shared by the screen and the export.

The screen and the export used to build their frames separately: the screen gained the candidate
transaction columns, the export did not, and the export then selected the screen's column list
against a frame that did not have them (a raw KeyError at the write step). Everything that
decides what a row of these tables contains now lives here:

  * the declared column schema - data columns, candidate columns, remark columns, and the two
    UI-only affordances (More, Match) that are never data;
  * the frame builders (`bill_frame`, `txn_frame`, `add_pairing`) the page AND the export call;
  * `select()`, which fails with a message naming a declared column that is missing instead of a
    KeyError, and `export_view()`, which shapes a frame for a file (dates DD/MM/YYYY, no UI-only
    columns, ids as text - `export.build_export` stringifies them).

No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import pandas as pd

from . import exception_pairing as ep
from . import queries as q
from .formatting import blank, display_date

# --- the declared schema ----------------------------------------------------------------

BILL_DATA_COLUMNS = ["date", "time", "store", "bill_no", "mop", "amount", "created_by", "status"]
BILL_PAIR_COLUMNS = ["Txn 1", "Amount 1", "Txn 2", "Amount 2"]       # real data: the candidates found
BILL_UI_ONLY = ["More", "Match"]                                     # affordances, never exported
REMARK_COLUMNS = ["remark", "remark_by", "remark_at"]

# The columns a person FILLS IN on an exception transaction to close it: the bill it paid (a match), or the
# three refund fields (all required). Part of the table, the export and the template - one definition.
TXN_ACTION_COLUMNS = ["matched_bill", "customer_name", "zoho_no", "refund_date"]

TXN_COLUMNS = ["gateway", "transaction_id", "date", "time", "store", "amount", "payment_mode", "status"] \
    + TXN_ACTION_COLUMNS + REMARK_COLUMNS

# The two closed-by-a-person tables carry EXACTLY the columns of the exception table they came from, then
# who/when/how it was closed.
MATCHED_BILL_EXTRA = ["matched_txn", "matched_by", "matched_at", "source"]
MATCHED_TXN_EXTRA = ["resolution", "matched_by", "matched_at", "source"]

# An open bill is shown with its candidates; a closed / auto-resolved bill has none to find.
BILL_BASE_COLUMNS = BILL_DATA_COLUMNS + REMARK_COLUMNS
BILL_SCREEN_COLUMNS = BILL_DATA_COLUMNS + BILL_PAIR_COLUMNS + BILL_UI_ONLY + REMARK_COLUMNS
BILL_EXPORT_COLUMNS = [c for c in BILL_SCREEN_COLUMNS if c not in BILL_UI_ONLY]

KEY = "exception_key"


class ExportSchemaError(ValueError):
    """A declared export column is missing from the frame - named, never a raw KeyError."""


def select(frame: pd.DataFrame, columns: list, what: str) -> pd.DataFrame:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ExportSchemaError(
            f"{what}: declared column(s) {missing} are not in the data this export built "
            f"(it has {list(frame.columns)}). Nothing was written.")
    return frame[columns]


def bill_columns(pairing: bool, screen: bool) -> list:
    """The one place that says which columns a bill table has. `screen=True` includes the UI-only
    More/Match; an export never does."""
    if not pairing:
        return list(BILL_BASE_COLUMNS)
    return list(BILL_SCREEN_COLUMNS if screen else BILL_EXPORT_COLUMNS)


# --- frame builders ----------------------------------------------------------------------

def with_remarks(session, df: pd.DataFrame, key_col: str = KEY) -> pd.DataFrame:
    if df.empty:
        return df
    remarks = q.latest_remarks_full(session, df[key_col].tolist())
    df = df.copy()
    none = ("", "", None)
    df["remark"] = df[key_col].map(lambda k: remarks.get(k, none)[0])
    df["remark_by"] = df[key_col].map(lambda k: remarks.get(k, none)[1] or "")
    df["remark_at"] = df[key_col].map(
        lambda k: remarks.get(k, none)[2].strftime("%d/%m/%Y %H:%M") if remarks.get(k, none)[2] else "")
    return df


def bill_frame(session, run_id: int, page_df: pd.DataFrame) -> pd.DataFrame:
    """One page (or export chunk) of bill exceptions, shaped: remarks and the bill's time and till
    operator looked up for THESE rows only."""
    if page_df.empty:
        return pd.DataFrame(columns=BILL_BASE_COLUMNS + [KEY])
    df = with_remarks(session, page_df)
    df["bill_no"] = df["detail"].map(lambda d: d.get("bill_no") or "—")
    df["mop"] = df["detail"].map(lambda d: d.get("mop") or "—")
    facts = q.unmatched_bill_facts(session, run_id, bill_nos=df["bill_no"].tolist())
    hit = df.apply(lambda r: facts.get((str(r["date"]), str(r["store"]), str(r["bill_no"]), str(r["mop"]))), axis=1)
    df["time"] = hit.map(lambda f: blank(f[0]) if f else "")
    df["created_by"] = hit.map(lambda f: blank(f[1]) if f else "")
    return df[BILL_BASE_COLUMNS + [KEY]].reset_index(drop=True)


def txn_frame(session, page_df: pd.DataFrame) -> pd.DataFrame:
    if page_df.empty:
        return pd.DataFrame(columns=TXN_COLUMNS + [KEY])
    df = with_remarks(session, page_df)
    for c in TXN_ACTION_COLUMNS:          # filled in by a person; empty until then
        df[c] = ""
    df["gateway"] = df["detail"].map(lambda d: d.get("gateway") or "—")
    df["transaction_id"] = df["detail"].map(lambda d: d.get("transaction_id") or d.get("bill_no") or "—")
    df["payment_mode"] = df["detail"].map(lambda d: d.get("payment_mode") or "—")
    df["time"] = df["transaction_time"].map(blank)
    return df[TXN_COLUMNS + [KEY]].reset_index(drop=True)


def add_pairing(frame: pd.DataFrame, assignment: "ep.Assignment") -> tuple:
    """Add Txn 1 / Amount 1 / Txn 2 / Amount 2 (data) and More / Match (UI-only) to a `bill_frame`, from
    the GLOBAL assignment (`exception_pairing.assign`) computed over the whole open population - so a
    transaction can appear against at most one bill whatever page or filter is showing. Txn 2 is filled
    only for a genuine split (the two amounts sum to the bill). An ambiguous cluster is left blank and
    flagged in `More`. Amounts are numbers; a screen formats them, a file keeps them."""
    out = frame.copy()
    cands = assignment.candidates
    for n in (1, 2):
        out[f"Txn {n}"] = [cands[k][n - 1].txn_id if len(cands.get(k) or []) >= n else "" for k in out[KEY]]
        out[f"Amount {n}"] = [cands[k][n - 1].amount if len(cands.get(k) or []) >= n else None for k in out[KEY]]
    out["More"] = [assignment.flag(k) or ("split payment" if assignment.kind.get(k) == "split" else "")
                   for k in out[KEY]]
    out["Match"] = ""
    return out, cands


def export_view(frame: pd.DataFrame, columns: list, what: str) -> pd.DataFrame:
    """A chunk shaped for a file: only the declared columns (a missing one is a named error), UI-only
    columns refused by name, dates DD/MM/YYYY. Ids stay strings; `export.build_export` writes them as text."""
    banned = [c for c in columns if c in BILL_UI_ONLY]
    if banned:
        raise ExportSchemaError(f"{what}: {banned} are screen affordances, not data, and cannot be exported.")
    out = select(frame, columns, what).copy()
    if "date" in out.columns:
        out["date"] = out["date"].map(display_date)
    return out


# --- the closed-by-a-person tables -------------------------------------------------------------

def bill_matched_columns() -> list:
    return list(BILL_EXPORT_COLUMNS) + MATCHED_BILL_EXTRA


def txn_matched_columns() -> list:
    return list(TXN_COLUMNS) + MATCHED_TXN_EXTRA


def _fmt_at(v) -> str:
    return v.strftime("%d/%m/%Y %H:%M") if v is not None and not pd.isna(v) else ""


def _in_range(day, date_from, date_to) -> bool:
    d = str(day)
    return (date_from is None or d >= str(date_from)) and (date_to is None or d <= str(date_to))


def manual_bill_frame(session, run_id: int, date_from=None, date_to=None, store=None) -> pd.DataFrame:
    """Manual match bills: every bill a person closed (active, accepted), with the columns of Exception
    bills and then matched_txn / matched_by / matched_at / source. Newest first."""
    import json

    from .db import Exception_, ManualMatch

    rows = [m for m in session.query(ManualMatch).filter(ManualMatch.lifecycle == "active",
                                                         ManualMatch.status == "matched_manual").all()
            if _in_range(m.business_date, date_from, date_to) and (not store or m.store == store)]
    cols = bill_matched_columns() + [KEY, "id"]
    if not rows:
        return pd.DataFrame(columns=cols)
    keys = [m.bill_key for m in rows]
    exc = {e.exception_key: e for e in session.query(Exception_).filter(Exception_.exception_key.in_(keys)).all()}
    facts = q.matched_bill_facts(session, run_id, [m.bill_no for m in rows])
    remarks = q.latest_remarks_full(session, keys)
    out = []
    for m in sorted(rows, key=lambda r: r.created_at, reverse=True):
        try:
            txn = (json.loads(m.validation_detail) if m.validation_detail else {}).get("txn") or {}
        except ValueError:
            txn = {}
        t, by = facts.get((str(m.business_date), str(m.store), str(m.bill_no), str(m.mop)), (None, None))
        rk = remarks.get(m.bill_key, ("", "", None))
        out.append({
            "date": m.business_date, "time": blank(t), "store": m.store, "bill_no": m.bill_no, "mop": m.mop,
            "amount": float(m.amount or 0), "created_by": blank(by),
            "status": exc[m.bill_key].status if m.bill_key in exc else "closed",
            "Txn 1": m.transaction_ref, "Amount 1": txn.get("amount"), "Txn 2": m.transaction_ref_2 or "",
            "Amount 2": (float(m.amount_2) if m.amount_2 is not None else None),
            "remark": rk[0], "remark_by": rk[1] or "", "remark_at": _fmt_at(rk[2]),
            "matched_txn": m.transaction_ref, "matched_by": m.operator, "matched_at": _fmt_at(m.created_at),
            "source": m.source or ("upload" if m.batch_id else "inline"), KEY: m.bill_key, "id": m.id})
    return pd.DataFrame(out)[cols]


def manual_txn_frame(session, date_from=None, date_to=None, store=None) -> pd.DataFrame:
    """Manual match transactions: every transaction a person closed - matched to a bill, or refunded to a
    customer (marked as such) - with the columns of Exception transactions and then resolution / matched_by /
    matched_at / source. Newest first."""
    import json

    from .db import Exception_, ManualMatch, TransactionRefund
    from .exceptions import txn_exception_key

    cols = txn_matched_columns() + [KEY, "id", "kind"]
    matches = session.query(ManualMatch).filter(ManualMatch.lifecycle == "active",
                                                ManualMatch.status == "matched_manual").all()
    refunds = session.query(TransactionRefund).all()
    items = []
    for m in matches:
        try:
            txn = (json.loads(m.validation_detail) if m.validation_detail else {}).get("txn") or {}
        except ValueError:
            txn = {}
        items.append(("match", m, txn, m.gateway, m.transaction_ref, str(txn.get("business_date") or m.business_date),
                      txn.get("store") or m.store, float(txn.get("amount") or m.amount or 0), m.created_at))
        if m.transaction_ref_2:                      # the second leg of a split is a transaction of its own
            try:
                txn2 = (json.loads(m.validation_detail) if m.validation_detail else {}).get("txn2") or {}
            except ValueError:
                txn2 = {}
            items.append(("match", m, txn2, m.gateway_2, m.transaction_ref_2,
                          str(txn2.get("business_date") or m.business_date), txn2.get("store") or m.store,
                          float(txn2.get("amount") or m.amount_2 or 0), m.created_at))
    for r in refunds:
        items.append(("refund", r, {}, r.gateway, r.transaction_ref, str(r.business_date), r.store,
                      float(r.amount or 0), r.uploaded_at))
    items = [i for i in items if _in_range(i[5], date_from, date_to) and (not store or i[6] == store)]
    if not items:
        return pd.DataFrame(columns=cols)
    keys = [txn_exception_key(i[3], i[4]) for i in items]
    exc = {e.exception_key: e for e in session.query(Exception_).filter(Exception_.exception_key.in_(keys)).all()}
    remarks = q.latest_remarks_full(session, keys)
    out = []
    for (kind, rec, txn, gw, ref, day, st, amt, at), key in sorted(zip(items, keys), key=lambda z: z[0][8],
                                                                    reverse=True):
        e = exc.get(key)
        detail = json.loads(e.detail) if e is not None and e.detail else {}
        rk = remarks.get(key, ("", "", None))
        is_match = kind == "match"
        out.append({
            "gateway": gw, "transaction_id": ref, "date": day,
            "time": blank(e.transaction_time) if e is not None else "", "store": st, "amount": amt,
            "payment_mode": detail.get("payment_mode") or txn.get("payment_mode") or "—",
            "status": e.status if e is not None else "closed",
            "matched_bill": rec.bill_no if is_match else "",
            "customer_name": "" if is_match else (rec.customer_name or ""),
            "zoho_no": "" if is_match else (rec.zoho_no or ""),
            "refund_date": "" if is_match else display_date(rec.refund_date),
            "remark": rk[0], "remark_by": rk[1] or "", "remark_at": _fmt_at(rk[2]),
            "resolution": "matched to bill" if is_match else "customer refund",
            "matched_by": rec.operator if is_match else rec.uploaded_by, "matched_at": _fmt_at(at),
            "source": (rec.source or ("upload" if getattr(rec, "batch_id", None) else "inline")),
            KEY: key, "id": rec.id, "kind": kind})
    return pd.DataFrame(out)[cols]
