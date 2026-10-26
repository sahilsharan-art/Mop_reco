"""Drill-down behind the Summary particulars matrix (2026-10-26).

A cell of the matrix (row Sale or Collected, column Total sale / UPI / Card / Total digital / Total cash) opens a
STORE-wise table of that cell, and a store opens the BILL-wise rows behind it (for Collected on a digital column those same
rows are the TRANSACTION-wise detail: gateway, transaction id, transaction date, payment mode).

Every store-wise figure is built from the same aggregates and the same live manual-match overlay the matrix itself uses
(`store_sheet_source.mop_particulars_matrix`), so the store rows sum to the cell they were opened from - and the caller is
given that sum so the page can assert it. No Streamlit here: DataFrames in, DataFrames out.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import func

from . import mop_matrix as mm
from .db import DigitalModeAgg, ManualMatch, MopDayAgg, R1BillLine

logger = logging.getLogger("mop_reco")

ROWS = ("sale", "collected")
COLS = ("total", "upi", "card", "digital", "cash")
COL_LABEL = {"total": "Total sale", "upi": "UPI", "card": "Card", "digital": "Total digital", "cash": "Total cash"}
ROW_LABEL = {"sale": "Sale", "collected": "Collected"}
PROVISIONAL = "UPI (provisional)"

WAREHOUSE = Path(__file__).resolve().parents[2] / "warehouse"


def _manual_overlay(session, run_id, d_from, d_to, store=None) -> pd.DataFrame:
    """Active manual matches whose bill the ENGINE still shows unmatched: store, bill_no, amount, mode, gateway, txn ref."""
    q = session.query(ManualMatch.store, ManualMatch.bill_no, ManualMatch.gateway, ManualMatch.mop, ManualMatch.amount,
                      ManualMatch.validation_detail, ManualMatch.transaction_ref, ManualMatch.business_date,
                      ManualMatch.operator).filter(
        ManualMatch.lifecycle == "active",
        ManualMatch.business_date >= d_from.isoformat(), ManualMatch.business_date <= d_to.isoformat())
    if store:
        q = q.filter(ManualMatch.store == store)
    rows = q.all()
    if not rows:
        return pd.DataFrame(columns=["store", "bill_no", "amount", "mode", "gateway", "txn_ref", "business_date", "by"])
    still = {b for (b,) in session.query(R1BillLine.bill_no).filter(
        R1BillLine.run_id == run_id, R1BillLine.matched == False,  # noqa: E712
        R1BillLine.bill_no.in_([r.bill_no for r in rows if r.bill_no])).all()}
    out = []
    for r in rows:
        if r.bill_no not in still:
            continue
        pm = None
        if r.validation_detail:
            try:
                pm = (json.loads(r.validation_detail).get("txn") or {}).get("payment_mode")
            except (ValueError, TypeError):
                pm = None
        mode = mm.classify_digital_mode(r.gateway, pm, r.mop)
        out.append({"store": r.store, "bill_no": r.bill_no, "amount": float(r.amount or 0), "mode": mode,
                    "gateway": r.gateway, "txn_ref": r.transaction_ref, "business_date": r.business_date,
                    "by": r.operator})
    return pd.DataFrame(out, columns=["store", "bill_no", "amount", "mode", "gateway", "txn_ref", "business_date", "by"])


def store_parts(session, run_id, d_from: date, d_to: date, store: str = None) -> pd.DataFrame:
    """One row per store with every component the matrix cells are made of."""
    b = session.query(MopDayAgg.store, MopDayAgg.bucket, func.sum(MopDayAgg.sale)).filter(
        MopDayAgg.run_id == run_id, MopDayAgg.business_date >= d_from.isoformat(),
        MopDayAgg.business_date <= d_to.isoformat())
    m = session.query(DigitalModeAgg.store, DigitalModeAgg.mode, func.sum(DigitalModeAgg.sale)).filter(
        DigitalModeAgg.run_id == run_id, DigitalModeAgg.business_date >= d_from.isoformat(),
        DigitalModeAgg.business_date <= d_to.isoformat())
    if store:
        b, m = b.filter(MopDayAgg.store == store), m.filter(DigitalModeAgg.store == store)
    parts: dict = {}

    def cell(s):
        return parts.setdefault(s, {"cash": 0.0, "gv": 0.0, "cni": 0.0, "cnr": 0.0, "digital_pos": 0.0,
                                    "card": 0.0, "upi_conf": 0.0, "open": 0.0})

    key = {"cash": "cash", "gift_voucher": "gv", "credit_note_issued": "cni", "credit_note_received": "cnr",
           "digital": "digital_pos"}
    for s, bucket, v in b.group_by(MopDayAgg.store, MopDayAgg.bucket).all():
        if bucket in key:
            cell(s)[key[bucket]] += float(v or 0)
    for s, mode, v in m.group_by(DigitalModeAgg.store, DigitalModeAgg.mode).all():
        c = cell(s)
        if mode == "card":
            c["card"] += float(v or 0)
        elif mode == "upi":
            c["upi_conf"] += float(v or 0)
        else:
            c["open"] += float(v or 0)
    for r in _manual_overlay(session, run_id, d_from, d_to, store).itertuples(index=False):
        if r.mode not in ("card", "upi"):
            continue
        c = cell(r.store)
        c["open"] -= r.amount
        c["card" if r.mode == "card" else "upi_conf"] += r.amount
    frame = pd.DataFrame.from_dict(parts, orient="index").rename_axis("store").reset_index()
    if frame.empty:
        return pd.DataFrame(columns=["store", "cash", "gv", "cni", "cnr", "digital_pos", "card", "upi_conf", "open"])
    return frame


def by_store(session, run_id, d_from: date, d_to: date, store: str, row: str, col: str):
    """(frame[store, amount], total) for one matrix cell, biggest first."""
    p = store_parts(session, run_id, d_from, d_to, store)
    if p.empty:
        return pd.DataFrame(columns=["store", "amount"]), 0.0
    p = p.copy()
    p["upi"] = p["upi_conf"] + p["open"]            # UPI sale = confirmed UPI + the provisional (open) pool
    p["digital_sale"] = p["card"] + p["upi"]
    p["digital_coll"] = p["card"] + p["upi_conf"]
    if row == "sale":
        amount = {"total": p["digital_sale"] + p["cash"] + p["gv"] + p["cnr"] + p["cni"], "upi": p["upi"],
                  "card": p["card"], "digital": p["digital_sale"], "cash": p["cash"]}[col]
    else:
        amount = {"total": p["digital_coll"] + p["cash"] + p["gv"], "upi": p["upi_conf"], "card": p["card"],
                  "digital": p["digital_coll"], "cash": p["cash"]}[col]
    out = pd.DataFrame({"store": p["store"], "amount": amount.round(2)})
    out = out[out["amount"].abs() > 0.004].sort_values("amount", ascending=False).reset_index(drop=True)
    return out, float(out["amount"].sum())


# --- bill-wise / transaction-wise rows for one store ------------------------------------------------------------

def digital_rows(session, run_id, d_from: date, d_to: date, store: str) -> pd.DataFrame:
    """Every digital bill line of the store with its mode, whether it is collected, and the transaction behind it."""
    q = session.query(R1BillLine.business_date, R1BillLine.bill_no, R1BillLine.mop, R1BillLine.amount, R1BillLine.matched,
                      R1BillLine.matched_gateway, R1BillLine.matched_txn_ref, R1BillLine.payment_mode,
                      R1BillLine.date_diff_days, R1BillLine.status).filter(
        R1BillLine.run_id == run_id, R1BillLine.store == store,
        R1BillLine.business_date >= d_from.isoformat(), R1BillLine.business_date <= d_to.isoformat())
    frame = pd.DataFrame(q.all(), columns=["business_date", "bill_no", "mop", "amount", "matched", "gateway", "transaction_id",
                                           "payment_mode", "diff", "status"])
    if frame.empty:
        return frame.assign(mode=[], collected=[], matched_by=[], txn_date=[])
    frame["amount"] = frame["amount"].astype(float)
    frame["matched"] = frame["matched"].astype(bool)
    over = _manual_overlay(session, run_id, d_from, d_to, store)
    by_bill = {r.bill_no: r for r in over.itertuples(index=False)}

    # classified once per distinct (gateway, payment_mode, mop) - a handful - never per row
    keyed = pd.DataFrame({"g": frame["gateway"].fillna(""), "pm": frame["payment_mode"].fillna(""),
                          "mop": frame["mop"].fillna("")})
    combos = keyed.drop_duplicates()
    cmap = {}
    for g, pm, mop in combos.itertuples(index=False):
        mode = mm.classify_digital_mode(g, pm, mop)
        cmap[(g, pm, mop)] = "unclassified" if mode == "other" else mode
    key = list(zip(keyed["g"], keyed["pm"], keyed["mop"]))
    engine_mode = pd.Series([cmap[k] for k in key], index=frame.index)
    manual_mode = frame["bill_no"].map({b: m.mode for b, m in by_bill.items() if m.mode in ("card", "upi")})
    is_manual = ~frame["matched"] & manual_mode.notna()
    frame["mode"] = engine_mode.where(frame["matched"], manual_mode.where(is_manual, PROVISIONAL))
    frame["collected"] = frame["matched"] | is_manual
    frame["matched_by"] = ""
    frame.loc[frame["matched"], "matched_by"] = "system"
    frame.loc[is_manual, "matched_by"] = "manual"
    if is_manual.any():
        frame.loc[is_manual, "gateway"] = frame.loc[is_manual, "bill_no"].map({b: m.gateway for b, m in by_bill.items()})
        frame.loc[is_manual, "transaction_id"] = frame.loc[is_manual, "bill_no"].map({b: m.txn_ref for b, m in by_bill.items()})
    d = pd.to_datetime(frame["business_date"], errors="coerce")
    diff = pd.to_numeric(frame["diff"], errors="coerce").fillna(0).astype(int)
    frame["txn_date"] = (d + pd.to_timedelta(diff, unit="D")).dt.strftime("%Y-%m-%d").where(frame["collected"] & frame["transaction_id"].notna())
    return frame


def pos_rows(store: str, d_from: date, d_to: date, mop_buckets: dict) -> pd.DataFrame:
    """The store's non-digital POS rows (cash, gift voucher, credit notes) with a bill number, from the warehouse."""
    folder = WAREHOUSE / "pos_bill_wise"
    frames = []
    for p in sorted(folder.glob("*.parquet")):
        if p.stem.startswith("_") or not (d_from.isoformat() <= p.stem <= d_to.isoformat()):
            continue
        try:
            f = pd.read_parquet(p, columns=["business_date", "bill_no", "mop", "amount", "store"],
                                filters=[("store", "==", store)])
        except Exception:  # noqa: BLE001 - fall back to a plain read for a file pyarrow cannot filter
            f = pd.read_parquet(p, columns=["business_date", "bill_no", "mop", "amount", "store"])
            f = f[f["store"] == store]
        if len(f):
            frames.append(f)
    if not frames:
        return pd.DataFrame(columns=["business_date", "bill_no", "mop", "amount", "type"])
    f = pd.concat(frames, ignore_index=True)
    f["business_date"] = f["business_date"].astype(str)
    has_bill = f["bill_no"].notna() & ~f["bill_no"].astype(str).str.strip().isin(["", "nan", "None"])
    f = f[has_bill].copy()
    f["type"] = f["mop"].map(mop_buckets)
    f = f[f["type"].isin(["cash", "gift_voucher", "credit_note_issued", "credit_note_received"])]
    f["amount"] = f["amount"].astype(float)
    return f[["business_date", "bill_no", "mop", "amount", "type"]].reset_index(drop=True)


def select_rows(digital: pd.DataFrame, pos: pd.DataFrame, row: str, col: str) -> pd.DataFrame:
    """The rows of one cell, in one display shape (pure)."""
    d = digital.copy()
    if col == "upi":
        d = d[d["mode"].isin(["upi", PROVISIONAL])]
    elif col == "card":
        d = d[d["mode"].eq("card")]
    elif col == "cash":
        d = d.iloc[0:0]
    if row == "collected":
        d = d[d["collected"]]
    d = d.assign(type=d["mode"].map({"upi": "UPI", "card": "Card", PROVISIONAL: PROVISIONAL, "unclassified": "Digital"}))
    p = pos.copy()
    wanted = {"total": {"cash", "gift_voucher", "credit_note_issued", "credit_note_received"}, "cash": {"cash"}}.get(col, set())
    if row == "collected":
        wanted = wanted - {"credit_note_issued", "credit_note_received"}
    p = p[p["type"].isin(wanted)]
    p = p.assign(type=p["type"].map({"cash": "Cash", "gift_voucher": "Gift voucher", "credit_note_issued": "Credit note issued",
                                     "credit_note_received": "Credit note received"}),
                 collected=True, matched_by="", gateway=None, transaction_id=None, payment_mode=None, txn_date=None)
    cols = ["business_date", "bill_no", "mop", "type", "amount", "collected", "matched_by", "gateway", "transaction_id",
            "payment_mode", "txn_date"]
    out = pd.concat([d.reindex(columns=cols), p.reindex(columns=cols)], ignore_index=True)
    return out.sort_values(["business_date", "bill_no"]).reset_index(drop=True)


def bills(session, run_id, d_from: date, d_to: date, store: str, row: str, col: str, mop_buckets: dict) -> pd.DataFrame:
    digital = digital_rows(session, run_id, d_from, d_to, store) if col != "cash" else pd.DataFrame(
        columns=["business_date", "bill_no", "mop", "amount", "matched", "gateway", "transaction_id", "payment_mode", "diff",
                 "status", "mode", "collected", "matched_by", "txn_date"])
    pos = pos_rows(store, d_from, d_to, mop_buckets) if col in ("total", "cash") else pd.DataFrame(
        columns=["business_date", "bill_no", "mop", "amount", "type"])
    return select_rows(digital, pos, row, col)
