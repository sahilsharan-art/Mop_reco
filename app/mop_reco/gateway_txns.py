"""The gateway side of R1 — every Paytm/Axis/Bajaj transaction in a range.

CLAUDE.md "TRANSACTIONS" page (2026-09-12): the bill-side view (Bill match)
answers "was this bill paid"; this answers "what did the gateway say" —
every transaction, whether or not a bill is behind it. Mapped means a bill is
matched against it (system or manual); unmapped means none is.

Pure functions plus one session-backed helper for the bill-mapping join —
same shape as manual_match.py: DataFrames in/out, no Streamlit import (§8).
Reads warehouse Parquet only — already-normalised, precomputed rows, one file
per gateway per business date (CLAUDE.md 6.14) — never a raw file, and never
more dates than the range asked for.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

GATEWAYS = ("paytm", "axis", "bajaj")
POOL_SOURCE = {"paytm": "paytm_settlement", "axis": "axis_edc", "bajaj": "bajaj"}

# Per-gateway source column -> the common output shape. Kept separate from
# manual_match.POOL_COLUMNS (a narrower shape built only for validating one
# pasted id) because this page also needs MID and time, which that lookup
# never uses.
COLUMN_MAP = {
    "paytm": {
        "transaction_id": "txn_ref", "transaction_time": "txn_time",
        "amount": "amount", "status": "status", "transaction_type": "txn_type",
        "payment_mode": "payment_mode", "original_mid": "mid", "store": "store",
    },
    "axis": {
        # NOT "TRAN_TIME" (2026-09-29) — that raw field is a constant
        # '000000' placeholder on every row, never a captured time.
        # "transaction_time" is ingest_axis_edc's own, genuinely-None
        # column for exactly this reason — see r1_bill_txn.build_gateway_
        # pool's identical fix.
        "RRN": "txn_ref", "transaction_time": "txn_time", "GROSS_AMT": "amount",
        "CARD_TYPE": "payment_mode", "MID": "mid", "store": "store",
    },
    "bajaj": {
        "Transaction Id": "txn_ref", "Transaction Amount": "amount",
        "Transaction Status": "status", "Card Scheme": "payment_mode",
        "MID": "mid", "store": "store",
        # A real, per-row time embedded in "Transaction Date" — see
        # ingest_bajaj's own transaction_time column (2026-09-29).
        "transaction_time": "txn_time",
    },
}

OUT_COLUMNS = ["gateway", "txn_ref", "business_date", "txn_time", "mid", "store",
              "amount", "status", "txn_type", "payment_mode"]


def _fill_defaults(gateway: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Axis and Bajaj carry no status/type column of their own — see
    r1_bill_txn.build_gateway_pool, which makes the same assumption for the
    matching pool. Axis EDC is settled card transactions only (always
    SUCCESS/ACQUIRING); Bajaj's own type column does not exist either, so
    ACQUIRING is the only type this listing can show for it."""
    if "status" not in frame.columns:
        frame["status"] = "SUCCESS" if gateway == "axis" else None
    if "txn_type" not in frame.columns:
        frame["txn_type"] = "ACQUIRING" if gateway in ("axis", "bajaj") else None
    return frame


def _dates_between(date_from, date_to) -> list:
    d_from = date_from if isinstance(date_from, date) else date.fromisoformat(str(date_from))
    d_to = date_to if isinstance(date_to, date) else date.fromisoformat(str(date_to))
    out, d = [], d_from
    while d <= d_to:
        out.append(d)
        d += timedelta(days=1)
    return out


def gateway_pool_for_range(date_from, date_to, gateway: str = None,
                           warehouse=None) -> pd.DataFrame:
    """Every gateway row whose own business_date falls in [date_from, date_to].

    One Parquet read per gateway per date — the same per-date warehouse layout
    manual_match.gateway_pool_for_date() already reads for its (narrower)
    window lookup. Bounded on purpose: only the dates asked for, never the
    whole month, and never a raw file.
    """
    root = Path(warehouse) if warehouse else Path(__file__).resolve().parents[2] / "warehouse"
    wanted = [gateway] if gateway else list(GATEWAYS)
    dates = _dates_between(date_from, date_to)

    frames = []
    for name in wanted:
        mapping = COLUMN_MAP[name]
        for one_date in dates:
            path = root / POOL_SOURCE[name] / f"{one_date}.parquet"
            if not path.exists():
                continue
            raw = pd.read_parquet(path)
            present = {src: dst for src, dst in mapping.items() if src in raw.columns}
            frame = raw[list(present)].rename(columns=present)
            frame["gateway"] = name
            frame["business_date"] = str(one_date)
            frame = _fill_defaults(name, frame)
            for column in OUT_COLUMNS:
                if column not in frame.columns:
                    frame[column] = None
            frames.append(frame[OUT_COLUMNS])

    if not frames:
        return pd.DataFrame(columns=OUT_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    out["txn_ref"] = out["txn_ref"].astype("string").str.strip()
    return out


def with_bill_mapping(session, pool: pd.DataFrame) -> pd.DataFrame:
    """Attach `matched_bill_no` and `match_source` ("system" | "manual" | None).

    system: R1BillLine rows the engine matched, joined on (matched_gateway,
    matched_txn_ref) — no run_id filter, so a transaction shows as mapped
    against whichever run last matched it, same as the rest of this page reads
    "as of the last run" (CLAUDE.md 6.14).
    manual: active ManualMatch rows, joined on (gateway, transaction_ref).
    Both lookups are dictionaries built from a population the size of "bills
    matched" and "manual matches" — never the whole gateway pool — so this
    scales with mapped rows, not with pool size.
    """
    from .db import ManualMatch, R1BillLine

    out = pool.copy()
    if out.empty:
        out["matched_bill_no"] = pd.Series(dtype="object")
        out["match_source"] = pd.Series(dtype="object")
        out["mapped"] = pd.Series(dtype="bool")
        return out

    system = {}
    for gw, ref, bill_no in (
        session.query(R1BillLine.matched_gateway, R1BillLine.matched_txn_ref,
                      R1BillLine.bill_no)
        .filter(R1BillLine.matched.is_(True), R1BillLine.matched_txn_ref.isnot(None))
        .all()
    ):
        if gw and ref:
            system[(str(gw).lower(), str(ref).strip())] = bill_no

    manual = {}
    for gw, ref, bill_no in (
        session.query(ManualMatch.gateway, ManualMatch.transaction_ref,
                      ManualMatch.bill_no)
        .filter(ManualMatch.lifecycle == "active").all()
    ):
        if gw and ref:
            manual[(str(gw).lower(), str(ref).strip())] = bill_no

    keys = list(zip(out["gateway"].astype(str).str.lower(), out["txn_ref"].astype(str)))
    matched_bill_no, match_source = [], []
    for key in keys:
        if key in system:
            matched_bill_no.append(system[key])
            match_source.append("system")
        elif key in manual:
            matched_bill_no.append(manual[key])
            match_source.append("manual")
        else:
            matched_bill_no.append(None)
            match_source.append(None)

    out["matched_bill_no"] = matched_bill_no
    out["match_source"] = match_source
    out["mapped"] = out["match_source"].notna()
    return out


def with_refunds(session, pool: pd.DataFrame) -> pd.DataFrame:
    """Attach the customer-refund resolution (CLAUDE.md §Request-B-3, 2026-09-20).

    A refund is a THIRD way a transaction closes, never a bill match — see
    `refunds.py`. Adds `resolution` (one of "mapped", "refunded", "unmapped" —
    "refund_pending" is folded into "unmapped" here since the transaction is
    NOT yet resolved; the pending/released split lives in `refund_status`) and
    the refund's own columns (`zoho_no`, `customer_name`, `refund_date`,
    `refund_status`), all None where no refund row exists. Must be called
    AFTER `with_bill_mapping` — `mapped` already reflects the bill side and is
    left untouched; a refunded transaction is never "mapped" (rule: a refund
    never closes a bill).
    """
    from .db import TransactionRefund

    out = pool.copy()
    if out.empty:
        for column in ("zoho_no", "customer_name", "refund_date", "refund_status",
                      "resolution"):
            out[column] = pd.Series(dtype="object")
        return out

    refunds = {}
    for r in session.query(TransactionRefund).all():
        refunds[(str(r.gateway).lower(), str(r.transaction_ref).strip())] = r

    zoho_no, customer_name, refund_date, refund_status, resolution = [], [], [], [], []
    keys = list(zip(out["gateway"].astype(str).str.lower(), out["txn_ref"].astype(str)))
    mapped_flags = out["mapped"] if "mapped" in out.columns else [False] * len(out)
    for key, is_mapped in zip(keys, mapped_flags):
        r = refunds.get(key)
        if r is not None:
            zoho_no.append(r.zoho_no)
            customer_name.append(r.customer_name)
            refund_date.append(r.refund_date)
            refund_status.append(r.refund_status)
            resolution.append("refunded")
        else:
            zoho_no.append(None)
            customer_name.append(None)
            refund_date.append(None)
            refund_status.append(None)
            resolution.append("mapped" if is_mapped else "unmapped")

    out["zoho_no"] = zoho_no
    out["customer_name"] = customer_name
    out["refund_date"] = refund_date
    out["refund_status"] = refund_status
    out["resolution"] = resolution
    return out


def gateway_transactions(session, date_from, date_to, warehouse=None) -> pd.DataFrame:
    """The full listing for the Transactions page: pool + mapping, one call."""
    pool = gateway_pool_for_range(date_from, date_to, warehouse=warehouse)
    return with_bill_mapping(session, pool)


def gateway_transactions_with_resolution(session, date_from, date_to,
                                         warehouse=None) -> pd.DataFrame:
    """Pool + bill mapping + refund resolution, one call — the shape the new
    3-line Mapped/Refunded/Unmapped summary and the "By transaction" mode
    both need."""
    pool = gateway_pool_for_range(date_from, date_to, warehouse=warehouse)
    mapped = with_bill_mapping(session, pool)
    return with_refunds(session, mapped)
