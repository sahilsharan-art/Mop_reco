"""Upload a filled exception table - the same file the table exports (2026-10-16).

The round trip: download the exception bills (or transactions), type the transaction number into the Txn
column (or the bill number / refund details on a transaction), upload the SAME file. The columns are not
a separate template - they are `exception_tables`' own column definition, so the screen, the export and
this upload can never drift apart.

Every row is validated exactly as an inline one is (`manual_match.validate`, `refunds.validate_refund`):
  * the bill exists and is open; the transaction exists and is unmapped;
  * amounts equal (and store, and a SUCCESS / ACQUIRING transaction); any date is allowed, the lag recorded;
  * a transaction already mapped elsewhere is REFUSED, naming the bill that holds it;
  * a refund needs customer_name, zoho_no and refund_date, and a zoho_no not already used.
A file is previewed, then confirmed, then applied ALL OR NOTHING: one rejected row stops the whole file,
and one database transaction writes everything or nothing.

No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import csv
import io
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from . import exception_pairing as ep
from . import exception_tables as et
from . import io_
from . import manual_match as mm
from . import queries as q
from . import refunds as rf
from . import upload_prefetch as up
from .db import Exception_, ManualMatchBatch, Remark
from .exceptions import bill_exception_key, txn_exception_key

logger = logging.getLogger("mop_reco")

TABLE_UPLOAD_REASON_CODE = "table_upload"
EPS = 0.005


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return str(v).strip()


def read_table_file(file_bytes: bytes, filename: str, expected: list) -> pd.DataFrame:
    """The data rows of an export, as text. The export begins with a header block (run, filters, totals),
    so the header row is FOUND - the first row holding every identifying column - never assumed. Column
    names match the table's own, whatever the case. A file without them is refused, naming what is missing."""
    if filename.lower().endswith(".csv"):
        text = file_bytes.decode("utf-8-sig", errors="replace")
        rows = [r for r in csv.reader(io.StringIO(text))]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        try:
            rows = [list(r) for r in wb[wb.sheetnames[0]].iter_rows(values_only=True)]
        finally:
            wb.close()
    canon = {c.lower(): c for c in expected}
    needed = {"bill_no", "txn 1"} if "Txn 1" in expected else {"gateway", "transaction_id"}
    head = next((i for i, r in enumerate(rows)
                 if needed <= {_cell(c).lower() for c in r}), None)
    if head is None:
        raise ValueError(f"This does not look like the table's export: no row has the columns "
                         f"{sorted(needed)}. Download the table from this page and fill that file in.")
    names = [canon.get(_cell(c).lower(), _cell(c)) for c in rows[head]]
    missing = [c for c in expected if c not in names]
    if missing:
        raise ValueError(f"The file is missing column(s) {missing}. Its columns must be exactly the table's: "
                         f"{expected}.")
    data = [[_cell(c) for c in r] + [""] * (len(names) - len(r)) for r in rows[head + 1:]]
    data = [r[:len(names)] for r in data if any(x for x in r)]
    return pd.DataFrame(data, columns=names)


def template_frame(kind: str) -> pd.DataFrame:
    """The blank template: the export's columns, nothing else - from the same definition."""
    cols = et.bill_columns(pairing=True, screen=False) if kind == "bills" else list(et.TXN_COLUMNS)
    return pd.DataFrame(columns=cols)


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------

@dataclass
class UploadRow:
    row_no: int
    kind: str                       # match | refund | remark | match+remark | refund+remark | ignored
    ref: str                        # the bill no (bills file) or the transaction id (transactions file)
    counterpart: str = ""           # what it is being closed against
    verdict: str = "reject"         # accept | reject | ignore
    why: list = field(default_factory=list)
    payload: dict = field(default_factory=dict)


@dataclass
class TablePreview:
    kind: str                       # bills | txns
    rows: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)    # seconds per stage, for the page and the diagnosis
    queries: int = 0
    progress: object = None                        # optional callback(fraction, message)

    def tick(self, fraction: float, message: str):
        if self.progress is not None:
            self.progress(min(max(fraction, 0.0), 1.0), message)

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def actionable_rows(self) -> int:
        return len(self.rows) - len(self.ignored)

    def summary_line(self, what: str = "transaction") -> str:
        return (f"{self.actionable_rows:,} of {self.total_rows:,} rows carry a {what} or a change; "
                f"{len(self.ignored):,} skipped as unchanged.")

    @property
    def accepted(self) -> list:
        return [r for r in self.rows if r.verdict == "accept"]

    @property
    def rejected(self) -> list:
        return [r for r in self.rows if r.verdict == "reject"]

    @property
    def ignored(self) -> list:
        return [r for r in self.rows if r.verdict == "ignore"]

    @property
    def can_apply(self) -> bool:
        """All or nothing: a file with ANY rejected row applies nothing."""
        return bool(self.accepted) and not self.rejected

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"row": r.row_no, "action": r.kind, "id": r.ref, "against": r.counterpart,
                              "verdict": r.verdict, "why": " | ".join(r.why)} for r in self.rows])


def _num(value):
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def _engine_pool_row(gateway, ref, bdate, store, amount) -> pd.DataFrame:
    return pd.DataFrame([{"gateway": gateway, "txn_ref": ref, "business_date": bdate, "store": store,
                          "amount": amount, "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None}])


class _Pools:
    """Where a pasted transaction id is looked up: the unmapped pool first (one query, in memory), then the
    whole warehouse for an id that is not in it (one bounded read per id - it may be mapped elsewhere, which
    the validation must then name)."""

    def __init__(self, session, refs=()):
        pool = ep.unmapped_pool(session)
        self.unmapped = {r.txn_id: r for r in pool.itertuples(index=False)}
        missing = sorted({r for r in refs if r and r not in self.unmapped})
        self._targeted: dict = {}
        self.warehouse_reads = 0
        if missing:                                  # ONE pass over the warehouse for all of them
            found = mm.targeted_pool_many(missing)
            self.warehouse_reads = len(missing)
            for ref, grp in found.groupby("txn_ref"):
                self._targeted[ref] = grp.reset_index(drop=True)

    def for_ref(self, ref: str) -> pd.DataFrame:
        hit = self.unmapped.get(ref)
        if hit is not None:
            return _engine_pool_row(hit.gateway, hit.txn_id, hit.date, hit.store, hit.amount)
        if ref not in self._targeted:                # asked for after the batch: fall back to a single read
            self._targeted[ref] = mm.targeted_pool(ref)
        return self._targeted[ref]


def _remark_change(latest: dict, key: str, text: str) -> bool:
    return bool(text) and text != (latest.get(key, ("", ""))[0] or "")


def preview_bills(session, run_id: int, df: pd.DataFrame, progress=None) -> TablePreview:
    """Validate an uploaded Exception bills file. A row acts on its Txn 1 (a match) and/or its remark."""
    pre = TablePreview("bills", progress=progress)
    t0 = time.perf_counter()
    records = df.to_dict("records")
    keys = [bill_exception_key(rf.parse_refund_date(r.get("date")) or "", str(r.get("store", "")).strip(),
                               str(r.get("bill_no", "")).strip(), str(r.get("mop", "")).strip()) for r in records]
    latest = q.latest_remarks(session, keys)
    refs_of = [io_.clean_ref(r.get("Txn 1")) or "" for r in records]
    acts = [bool(refs_of[i]) or _remark_change(latest, keys[i], str(records[i].get("remark", "") or "").strip())
            for i in range(len(records))]
    # a transaction or a bill named by two rows of the file is wrong for BOTH rows - the first one is not
    # "more right" than the second, so neither is accepted
    ref_rows, bill_rows = defaultdict(list), defaultdict(list)
    for i, r in enumerate(records):
        if acts[i]:
            if refs_of[i]:
                ref_rows[refs_of[i]].append(i + 2)
            if str(r.get("bill_no", "")).strip():
                bill_rows[str(r.get("bill_no", "")).strip()].append(i + 2)
    pre.timings["classify"] = time.perf_counter() - t0
    n_act = sum(acts)
    pre.tick(0.1, f"Read {len(records):,} rows; {n_act:,} carry a transaction or a change, "
                  f"{len(records) - n_act:,} skipped as unchanged")
    t1 = time.perf_counter()
    pools = _Pools(session, [refs_of[i] for i in range(len(records)) if acts[i]])
    pre.tick(0.4, "Searched the warehouse for the transactions in the file")
    pre.timings["transaction_pool"] = time.perf_counter() - t1
    with up.prefetch(session, run_id, bill_nos=[str(records[i].get("bill_no", "")).strip()
                                                for i in range(len(records)) if acts[i]],
                     refs=[refs_of[i] for i in range(len(records)) if acts[i]]) as pf:
        pre.timings.update({f"prefetch_{k}": v for k, v in pf.timings.items()})
        t2 = time.perf_counter()
        _bill_rows(session, run_id, records, keys, latest, pools, ref_rows, bill_rows, pre)
        pre.timings["validate_rows"] = time.perf_counter() - t2
        pre.queries = pf.queries
    pre.timings["total"] = time.perf_counter() - t0
    return pre


def _existing_exception_keys(session, keys) -> set:
    found, keys = set(), sorted({k for k in keys if k})
    for i in range(0, len(keys), 500):
        found |= {k for (k,) in session.query(Exception_.exception_key)
                  .filter(Exception_.exception_key.in_(keys[i:i + 500])).all()}
    return found


def _bill_rows(session, run_id, records, keys, latest, pools, ref_rows, bill_rows, pre):
    total = max(len(records), 1)
    exc_keys = _existing_exception_keys(
        session, [keys[i] for i, r in enumerate(records)
                  if _remark_change(latest, keys[i], str(r.get("remark", "") or "").strip())])
    for pos, r in enumerate(records, start=2):
        if pos % 250 == 0:
            pre.tick(0.5 + 0.5 * (pos - 2) / total, f"Validated {pos - 2:,} of {total:,} rows")
        bill_no = str(r.get("bill_no", "")).strip()
        ref = io_.clean_ref(r.get("Txn 1")) or ""
        txn2 = io_.clean_ref(r.get("Txn 2")) or ""
        remark = str(r.get("remark", "") or "").strip()
        key = keys[pos - 2]
        wants_match = bool(ref)
        wants_remark = _remark_change(latest, key, remark)
        if not wants_match and not wants_remark:
            pre.rows.append(UploadRow(pos, "ignored", bill_no, verdict="ignore", why=["no change on this row"]))
            continue
        kind = "match" if wants_match and not wants_remark else ("remark" if wants_remark and not wants_match
                                                                else "match+remark")
        row = UploadRow(pos, kind, bill_no, ref)
        why = []
        pre.rows.append(row)

        if not bill_no:
            why.append("bill_no is blank")
        if bill_no and len(bill_rows[bill_no]) > 1:
            others = [n for n in bill_rows[bill_no] if n != pos]
            why.append(f"bill {bill_no} is also on row {', '.join(map(str, others))} of this file")

        bill = is_open = None
        if bill_no:
            bill, is_open, why_not = mm.find_bill_for_match(session, run_id, bill_no)
            if bill is None:
                why.append(why_not)
            else:
                # what the row says about the bill must be what the system holds - never silently overridden
                typed_date = rf.parse_refund_date(r.get("date"))
                if typed_date and typed_date != str(bill["business_date"]):
                    why.append(f"the file says date {r.get('date')} but the bill is dated {bill['business_date']}")
                if str(r.get("store", "")).strip() and str(r.get("store")).strip() != str(bill["store"]):
                    why.append(f"the file says store {r.get('store')} but the bill is {bill['store']}")
                if str(r.get("mop", "")).strip() and str(r.get("mop")).strip() != str(bill["mop"]):
                    why.append(f"the file says MOP {r.get('mop')} but the bill is {bill['mop']}")
                amt = _num(r.get("amount"))
                if amt is not None and abs(amt - float(bill["amount"])) > EPS:
                    why.append(f"the file says amount {amt:,.2f} but the bill is {float(bill['amount']):,.2f}")
                if wants_match and not is_open:
                    why.append(why_not)

        validation = None
        if wants_match:
            if txn2:
                why.append("Txn 2 is filled: a manual match holds one transaction per bill, so a split payment "
                           "is decided by hand")
            if len(ref_rows[ref]) > 1:
                others = [n for n in ref_rows[ref] if n != pos]
                why.append(f"transaction {ref} is also on row {', '.join(map(str, others))} of this file - "
                           "one transaction cannot pay two bills")
            if bill is not None and is_open and not why:
                validation = mm.validate_by_id(session, bill, ref, gateway_pool=pools.for_ref(ref))
                if not validation.ok:
                    why.extend(validation.problems)
                elif validation.date_lag_note:
                    row.why.append(validation.date_lag_note)
        if wants_remark and key and key not in exc_keys:
            why.append("there is no exception for this bill to attach a remark to")

        row.why = row.why + why if why else row.why
        row.verdict = "reject" if why else "accept"
        row.payload = {"bill": bill, "ref": ref, "validation": validation, "key": key,
                       "remark": remark if wants_remark else "", "gateway": getattr(validation, "gateway", None)}


def preview_txns(session, run_id: int, df: pd.DataFrame, progress=None) -> TablePreview:
    """Validate an uploaded Exception transactions file. A row acts on matched_bill (a match), or on the
    three refund fields (a customer refund) - never both - and/or on its remark."""
    pre = TablePreview("txns", progress=progress)
    t0 = time.perf_counter()
    records = df.to_dict("records")
    keys = [txn_exception_key(str(r.get("gateway", "")).strip().lower(), io_.clean_ref(r.get("transaction_id")) or "")
            for r in records]
    exc = {}
    for i in range(0, len(keys), 500):
        exc.update({e.exception_key: e for e in session.query(Exception_)
                    .filter(Exception_.exception_key.in_(keys[i:i + 500])).all()})
    latest = q.latest_remarks(session, keys)

    def _acts(r, key):
        return bool(str(r.get("matched_bill", "") or "").strip()
                    or str(r.get("customer_name", "") or "").strip() or str(r.get("zoho_no", "") or "").strip()
                    or str(r.get("refund_date", "") or "").strip()
                    or _remark_change(latest, key, str(r.get("remark", "") or "").strip()))

    active = [i for i, r in enumerate(records) if _acts(r, keys[i])]
    txn_rows, bill_rows, zoho_rows = defaultdict(list), defaultdict(list), defaultdict(list)
    for i in active:
        r = records[i]
        ref = io_.clean_ref(r.get("transaction_id")) or ""
        txn_rows[ref].append(i + 2)
        if str(r.get("matched_bill", "") or "").strip():
            bill_rows[str(r.get("matched_bill")).strip()].append(i + 2)
        if str(r.get("zoho_no", "") or "").strip():
            zoho_rows[str(r.get("zoho_no")).strip().upper()].append(i + 2)
    pre.timings["classify"] = time.perf_counter() - t0
    pre.tick(0.2, f"Read {len(records):,} rows; {len(active):,} carry a change, "
                  f"{len(records) - len(active):,} skipped as unchanged")
    t2 = time.perf_counter()
    with up.prefetch(session, run_id,
                     bill_nos=[str(records[i].get("matched_bill", "") or "").strip() for i in active],
                     refs=[io_.clean_ref(records[i].get("transaction_id")) or "" for i in active]) as pf:
        pre.timings.update({f"prefetch_{k}": v for k, v in pf.timings.items()})
        _txn_rows(session, run_id, records, keys, exc, latest, txn_rows, bill_rows, zoho_rows, pre)
        pre.queries = pf.queries
    pre.timings["validate_rows"] = time.perf_counter() - t2
    pre.timings["total"] = time.perf_counter() - t0
    return pre


def _txn_rows(session, run_id, records, keys, exc, latest, txn_rows, bill_rows, zoho_rows, pre):
    total = max(len(records), 1)
    for pos, r in enumerate(records, start=2):
        if pos % 250 == 0:
            pre.tick(0.4 + 0.6 * (pos - 2) / total, f"Validated {pos - 2:,} of {total:,} rows")
        gateway = str(r.get("gateway", "")).strip().lower()
        ref = io_.clean_ref(r.get("transaction_id")) or ""
        key = keys[pos - 2]
        bill_no = str(r.get("matched_bill", "") or "").strip()
        name = str(r.get("customer_name", "") or "").strip()
        zoho = str(r.get("zoho_no", "") or "").strip()
        rdate = str(r.get("refund_date", "") or "").strip()
        remark = str(r.get("remark", "") or "").strip()
        wants_match = bool(bill_no)
        wants_refund = bool(name or zoho or rdate)
        wants_remark = _remark_change(latest, key, remark)
        if not (wants_match or wants_refund or wants_remark):
            pre.rows.append(UploadRow(pos, "ignored", ref, verdict="ignore", why=["no change on this row"]))
            continue
        parts = [p for p, w in (("match", wants_match), ("refund", wants_refund), ("remark", wants_remark)) if w]
        row = UploadRow(pos, "+".join(parts), ref, bill_no if wants_match else (zoho if wants_refund else ""))
        pre.rows.append(row)
        why = []

        if wants_match and wants_refund:
            why.append("a transaction is either matched to a bill or refunded to a customer - not both")
        e = exc.get(key)
        if e is None:
            why.append(f"no exception transaction {gateway}|{ref}")
        elif e.status not in ("open", "under_investigation"):
            why.append(f"this transaction is not open (it is {e.status})")
        if len(txn_rows[ref]) > 1:
            others = [n for n in txn_rows[ref] if n != pos]
            why.append(f"transaction {ref} is also on row {', '.join(map(str, others))} of this file")

        txn = None
        if e is not None:
            txn = {"gateway": gateway, "txn_ref": ref, "business_date": e.business_date, "store": e.store,
                   "amount": float(e.amount or 0), "status": "SUCCESS", "txn_type": "ACQUIRING",
                   "payment_mode": None}
            amt = _num(r.get("amount"))
            if amt is not None and abs(amt - txn["amount"]) > EPS:
                why.append(f"the file says amount {amt:,.2f} but the transaction is {txn['amount']:,.2f}")
            if str(r.get("store", "")).strip() and str(r.get("store")).strip() != str(e.store):
                why.append(f"the file says store {r.get('store')} but the transaction is {e.store}")

        validation = bill = None
        if wants_match and not wants_refund and txn is not None:
            if len(bill_rows[bill_no]) > 1:
                others = [n for n in bill_rows[bill_no] if n != pos]
                why.append(f"bill {bill_no} is also on row {', '.join(map(str, others))} of this file - "
                           "one bill cannot take two transactions")
            bill, is_open, why_not = mm.find_bill_for_match(session, run_id, bill_no)
            if bill is None or not is_open:
                why.append(why_not)
            elif not why:
                validation = mm.validate(session, bill, gateway, ref,
                                         _engine_pool_row(gateway, ref, txn["business_date"], txn["store"], txn["amount"]))
                if not validation.ok:
                    why.extend(validation.problems)
                elif validation.date_lag_note:
                    row.why.append(validation.date_lag_note)
        if wants_refund and not wants_match and txn is not None:
            problems = rf.validate_refund(session, txn, name, zoho, rdate)
            why.extend(problems)
            if zoho and len(zoho_rows[zoho.upper()]) > 1:
                others = [n for n in zoho_rows[zoho.upper()] if n != pos]
                why.append(f"zoho_no {zoho} is also on row {', '.join(map(str, others))} of this file")
        if wants_remark and e is None:
            why.append("there is no exception to attach a remark to")

        row.why = row.why + why if why else row.why
        row.verdict = "reject" if why else "accept"
        row.payload = {"txn": txn, "bill": bill, "validation": validation, "key": key, "gateway": gateway,
                       "ref": ref, "customer_name": name, "zoho_no": zoho, "refund_date": rdate,
                       "match": wants_match and not wants_refund, "refund": wants_refund and not wants_match,
                       "remark": remark if wants_remark else ""}


# ---------------------------------------------------------------------------
# Applying - all or nothing
# ---------------------------------------------------------------------------

def _add_remark(session, key: str, text: str, operator: str, now: datetime):
    row = session.query(Exception_).filter_by(exception_key=key).one_or_none()
    if row is None:
        raise ValueError(f"no exception with key {key!r}")
    session.add(Remark(exception_id=row.id, remark_text=text, created_by=operator, created_at=now))


def apply_preview(session, preview: TablePreview, operator: str, file_name: str = None,
                  commit: bool = True) -> dict:
    """Write every accepted row in ONE database transaction. Refused outright if any row was rejected -
    the file is fixed and uploaded again, never half applied. Returns counts."""
    if not (operator or "").strip():
        raise ValueError("An upload must record who applied it")
    if preview.rejected:
        raise ValueError(f"{len(preview.rejected)} row(s) were rejected - nothing is applied. "
                         "Fix them and upload the file again.")
    if not preview.accepted:
        raise ValueError("Nothing to apply.")
    now = datetime.now()
    counts = {"matched": 0, "refunded": 0, "remarks": 0}
    try:
        batch = ManualMatchBatch(created_at=now, operator=operator, source="upload", file_name=file_name,
                                 rows_submitted=len(preview.rows), rows_accepted=len(preview.accepted),
                                 rows_rejected=0)
        session.add(batch)
        session.flush()
        for row in preview.accepted:
            p = row.payload
            if preview.kind == "bills" and p.get("validation") is not None:
                mm.record(session, p["bill"], p["gateway"], p["ref"], TABLE_UPLOAD_REASON_CODE, operator,
                          p["validation"], batch=batch, source="upload", commit=False)
                counts["matched"] += 1
            if preview.kind == "txns" and p.get("match"):
                mm.record(session, p["bill"], p["gateway"], p["ref"], TABLE_UPLOAD_REASON_CODE, operator,
                          p["validation"], batch=batch, source="upload", commit=False)
                counts["matched"] += 1
            if preview.kind == "txns" and p.get("refund"):
                rf.record_refund(session, p["txn"], p["customer_name"], p["zoho_no"], p["refund_date"], operator,
                                 source="upload", commit=False)
                counts["refunded"] += 1
            if p.get("remark"):
                _add_remark(session, p["key"], p["remark"], operator, now)
                counts["remarks"] += 1
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(f"table upload ({preview.kind}): {counts} by {operator} (file={file_name})")
    counts["batch_id"] = batch.id
    return counts
