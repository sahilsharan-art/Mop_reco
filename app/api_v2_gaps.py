"""V2 parity gaps closed on 25 Nov 2026 (docs/V2_AUDIT_2026-11-25.md, section 3): the Float screen, the Summary tables, and the
Exceptions pairing (Txn 1 / Txn 2 against every open bill). Every figure and every rule comes from the engines the Streamlit pages use
(`float_tracker`, `store_sheet`, `store_sheet_source`, `queries.match_overview`, `exception_pairing`, `exception_tables`, `manual_match`);
this module only shapes them for the React screens. Nothing here reads a raw source file.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import date
from io import BytesIO
from typing import Literal

import pandas as pd
from fastapi import Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from mop_reco import exception_pairing as ep
from mop_reco import exception_tables as et
from mop_reco import float_tracker as floats
from mop_reco import manual_match as mm
from mop_reco import queries as q
from mop_reco import store_sheet as ss
from mop_reco import store_sheet_source as source
from mop_reco.db import CmsSlip, Exception_, ManualMatch, Run, TransactionRefund
from mop_reco.export import ExportMeta, build_export
from sqlalchemy import func

LIVE = ("open", "under_investigation")
BILL_TYPES = q.NOT_RECEIVED_TYPES
TXN_TYPES = q.RECEIVED_NOT_BILLED_TYPES + ("original_txn_not_found", "store_ambiguous")
FLOAT_VIEWS = ("unremarked", "high", "weekend", "all")
MATCH_REASON = "manual_match_by_bill"
TXN_MATCH_REASON = "manual_match_by_transaction"


class PairMatch(BaseModel):
    run_id: int
    bill_no: str = Field(min_length=1, max_length=200)
    txn1: str = Field(default="", max_length=200)
    txn2: str = Field(default="", max_length=200)
    shared_bill_nos: list[str] = Field(default_factory=list, max_length=20)


class TxnMatch(BaseModel):
    run_id: int
    gateway: str = Field(min_length=1, max_length=50)
    transaction_id: str = Field(min_length=1, max_length=200)
    bill_no: str = Field(min_length=1, max_length=200)


class ClusterPair(BaseModel):
    run_id: int


def _xlsx(frame: pd.DataFrame) -> bytes:
    out = BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter", engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False}}) as w:
        frame.to_excel(w, index=False, sheet_name="float_remarks")
    return out.getvalue()


def register(app, *, db, signed_in, cfg, factory, clean, prefix, period, sheet, match_state):
    lock = threading.Lock()
    float_cache: dict = {}
    pair_cache: dict = {}

    # ------------------------------------------------------------------ shared helpers
    def latest_run(s, run_id):
        run = s.get(Run, run_id)
        if not run or run.status != "completed":
            raise HTTPException(409, "Choose a completed run.")
        newest = s.query(Run.id).filter(Run.status == "completed").order_by(Run.id.desc()).first()
        if newest and newest[0] != run.id:
            raise HTTPException(409, f"Run #{newest[0]} is the latest completed run. Choose it before saving changes.")
        return run

    inflight: dict = {}

    def keep(cache, key, build, ttl, size):
        """Cache with single flight: when two requests ask for the same expensive result at once, one builds it and the other waits for it."""
        def fresh():
            hit = cache.get(key)
            return hit[1] if hit and time.monotonic() - hit[0] < ttl else None
        with lock:
            value = fresh()
            gate = None if value is not None else inflight.setdefault((id(cache), key), threading.Lock())
        if value is not None:
            return value
        with gate:
            with lock:
                value = fresh()
            if value is not None:
                return value
            value = build()
            with lock:
                while len(cache) >= size:
                    cache.pop(min(cache, key=lambda k: cache[k][0]))
                cache[key] = (time.monotonic(), value)
                inflight.pop((id(cache), key), None)
            return value

    # ------------------------------------------------------------------ FLOAT
    def float_full(s, p):
        run, lo, hi, st = p
        key = (run.id, lo, hi, st, match_state(s))

        def build():
            grid = floats.build_float_grid(sheet(s, p), cfg().raw)
            return floats.with_remarks(grid, floats.float_remarks_for_range(s, lo, hi, st))
        return keep(float_cache, key, build, 120, 6).copy()

    def float_view(full: pd.DataFrame, view: str) -> pd.DataFrame:
        if view == "high":
            out = full[full["high_float"]]
        elif view == "weekend":
            out = full[full["is_auto"]]
        elif view == "all":
            out = full
        else:                                   # the work queue: high float and no remark at all, auto or manual
            out = full[(full["high_float"]) & (full["remark"] == "")]
        return out.sort_values("business_date", ascending=False).reset_index(drop=True)

    def float_tiles(full: pd.DataFrame) -> dict:
        """'Today' is the last loaded business date of the period (rule 8: no hardcoded date). The unremarked count and the longest
        run are whole-period figures: they describe the backlog of explaining, not one day."""
        if full.empty:
            return {}
        last = sorted(full["business_date"].unique())[-1]
        today_high = full[(full["business_date"] == last) & (full["high_float"])]
        unremarked = full[(full["high_float"]) & (full["remark"] == "")]
        longest = full.loc[full["days_held_high"].idxmax()]
        return {"as_of": last, "stores_above": int(today_high["store"].nunique()),
                "float_above": round(float(today_high["float_amount"].sum()), 2),
                "high_no_remark": int(len(unremarked)),
                "longest": {"days": int(longest["days_held_high"]), "store": str(longest["store"]),
                            "through": str(longest["business_date"])} if int(longest["days_held_high"]) > 0 else None}

    @app.get(prefix + "/float")
    def float_rows(p=Depends(period), u=Depends(signed_in), s=Depends(db), view: Literal["unremarked", "high", "weekend", "all"] = "unremarked",
                   offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        full = float_full(s, p)
        if full.empty:
            return {"rows": [], "total": 0, "tiles": {}, "quality": {"negative_n": 0, "negative_value": 0.0, "defect_b_n": 0, "defect_b_value": 0.0}}
        shown = float_view(full, view)
        neg = floats.negative_float_rows(full)
        dq_n, dq_v = source.defect_b_summary(s, p[0].id, p[1], p[2], p[3])
        return {"rows": clean(shown.iloc[offset:offset + limit]), "total": int(len(shown)), "tiles": clean(float_tiles(full)),
                "quality": {"negative_n": int(len(neg)), "negative_value": round(float(neg["float_amount"].sum()), 2) if len(neg) else 0.0,
                            "defect_b_n": int(dq_n), "defect_b_value": round(float(dq_v), 2)}}

    @app.get(prefix + "/float/data-quality")
    def float_quality(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        """Negative float (a deposit above the opening: catch-up or misdated) and credits exceeding deposits (Defect B). Neither is floored
        or hidden (rule 4); the pickup codes and slip numbers come from the CMS slips the run stored, never from a raw file."""
        run, lo, hi, st = p
        full = float_full(s, p)
        negative = floats.negative_float_rows(full) if not full.empty else pd.DataFrame()
        slips = pd.DataFrame(s.query(CmsSlip.row_type, CmsSlip.dep_date, CmsSlip.store, CmsSlip.pickup_code, CmsSlip.slip_no)
                             .filter(CmsSlip.run_id == run.id, CmsSlip.dep_date >= str(lo), CmsSlip.dep_date <= str(hi)).all(),
                             columns=["TYPE", "DEPDATE", "store", "PICKUPPOINTCODE", "SLIPNO"])
        defect_b = source.data_quality_rows(sheet(s, p), slips)
        return {"negative": clean(negative), "defect_b": clean(defect_b)}

    @app.get(prefix + "/float/history")
    def float_history(store: str, business_date: date, u=Depends(signed_in), s=Depends(db)):
        return clean(floats.float_remark_history(s, store, business_date.isoformat()))

    @app.get(prefix + "/float/template")
    def float_template(u=Depends(signed_in)):
        return Response(_xlsx(floats.upload_template().iloc[0:0]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="float_remark_template.xlsx"'})

    @app.post(prefix + "/float/upload")
    def float_upload(file: UploadFile = File(...), confirm: bool = Query(False), p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        """Every row is validated against the period's float grid and NOTHING is applied until confirm=true; a rejected row is never applied."""
        name = (file.filename or "").lower()
        try:
            frame = pd.read_csv(file.file, dtype=str) if name.endswith(".csv") else pd.read_excel(file.file, dtype=str)
        except Exception as exc:  # noqa: BLE001 - the real reason is the useful answer
            raise HTTPException(422, f"Could not read the file: {type(exc).__name__}: {exc}")
        if len(frame) > 20000:
            raise HTTPException(422, "A float upload may hold at most 20,000 rows.")
        full = float_full(s, p)
        try:
            preview = floats.preview_upload(frame, full)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        result = {"rows": clean(preview.as_frame()), "accepted": len(preview.accepted), "rejected": len(preview.rejected), "applied": 0, "failed": []}
        if confirm:
            if not preview.accepted:
                raise HTTPException(422, "No row is acceptable; nothing was applied.")
            applied, failed = floats.apply_upload(s, preview, u.display_name)
            result["applied"] = len(applied)
            result["failed"] = [{"row": r.row_number, "store": r.store, "business_date": r.business_date, "why": why} for r, why in failed]
            with lock:
                float_cache.clear()
        return result

    @app.get(prefix + "/float/export")
    def float_export(p=Depends(period), u=Depends(signed_in), s=Depends(db), view: Literal["unremarked", "high", "weekend", "all"] = "unremarked"):
        run, lo, hi, st = p
        shown = float_view(float_full(s, p), view)
        result = build_export(shown, ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=lo, date_to=hi,
                                                filters={"store": st or "All stores", "show": view}, title="Float"))
        mime = "text/csv" if result.fmt == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return Response(result.data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{result.filename}"'})

    # ------------------------------------------------------------------ SUMMARY TABLES
    @app.get(prefix + "/summary/tables")
    def summary_tables(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        """The two two-sided controls made visible - bills and transactions - from the ONE MatchOverview the old Summary and Exceptions tiles read."""
        run, lo, hi, st = p
        ov = q.match_overview(s, run.id, lo, hi, st)
        return clean({"bills": ov.bills, "bills_ok": ov.bills_ok, "bills_difference": ov.bills_difference,
                      "bills_agree": ov.bills_agree_with_exceptions, "exception_bills_n": ov.exception_bills_n,
                      "merged_bill_lines": ov.merged_bill_lines, "txns": dataclasses.asdict(ov.txns), "txns_ok": ov.txns_ok,
                      "txns_difference": ov.txns_difference, "unmapped_agrees": ov.unmapped_agrees_with_exceptions,
                      "exception_txns_n": ov.exception_txns_n, "merged_txn_rows": ov.merged_txn_rows})

    @app.get(prefix + "/summary/controls")
    def summary_controls(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        """The eight lettered controls stored by the run (never recomputed from a raw file) and the run's own data notes."""
        run = s.get(Run, p[0].id)
        static = source.load_static(run)
        if static is None:
            return {"controls": [], "notes": ["This run predates the stored controls; re-run to produce them."], "unresolved_pickups": []}
        return {"controls": [{"key": k, "what": w, "holds": bool(h), "detail": d} for k, w, h, d in static["controls"]],
                "notes": [str(n) for n in static["notes"]], "unresolved_pickups": clean(static["unresolved_pickups"])}

    @app.get(prefix + "/summary/by-date")
    def summary_by_date(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        """One row per business date, all stores consolidated, plus TOTAL - columns A..N. Each date opens onto its stores (by-date/{day})."""
        frame = sheet(s, p)
        by_date = ss.summarise_by_date(frame)
        bad = ss.verify_date_rows_equal_store_sums(frame, by_date)
        return {"rows": clean(by_date), "date_check": clean(bad)}

    @app.get(prefix + "/summary/by-date/{day}")
    def summary_day(day: date, p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        frame = sheet(s, p)
        return {"rows": clean(frame[frame["date"] == day.isoformat()].sort_values("store"))}

    @app.get(prefix + "/summary/matched")
    def summary_matched(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        run, lo, hi, st = p
        rows, total = q.matched_bills_page(s, run.id, lo, hi, st, limit=limit, offset=offset)
        return {"rows": clean(rows), "total": int(total)}

    @app.get(prefix + "/summary/matched/export")
    def summary_matched_export(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        run, lo, hi, st = p
        frame = q.matched_bills_export(s, run.id, lo, hi, st)
        result = build_export(frame, ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=lo, date_to=hi,
                                                filters={"store": st or "All stores"}, value_column="amount", title="Matched bills"))
        mime = "text/csv" if result.fmt == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return Response(result.data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{result.filename}"'})

    # ------------------------------------------------------------------ EXCEPTIONS: bills and transactions, with pairing
    def pairing_state(s, run_id):
        """ONE global assignment over the whole open population (a transaction is suggested to at most one bill, 6.20). Cached on exactly
        what can change it: the run, the active manual matches, the refunds and the open exception population."""
        active = s.query(func.count(ManualMatch.id)).filter(ManualMatch.lifecycle == "active").scalar() or 0
        top = s.query(func.max(ManualMatch.id)).scalar() or 0
        refunds = s.query(func.count(TransactionRefund.id)).scalar() or 0
        live = s.query(func.count(Exception_.id), func.max(Exception_.id)).filter(
            Exception_.type.in_(tuple(BILL_TYPES) + ("unmapped_gateway_transaction",)), Exception_.status.in_(LIVE)).one()
        key = (run_id, int(active), int(top), int(refunds), int(live[0] or 0), int(live[1] or 0))

        def build():
            pool = ep.unmapped_pool(s)
            bills = ep.open_population(s, run_id)
            minutes, days = ep.window_settings(cfg().raw)
            return pool, bills, ep.assign(bills, pool, minutes, days, ep.mop_family(cfg().raw))
        return keep(pair_cache, key, build, 300, 3)

    def statuses_of(status: str) -> tuple:
        if status == "all":
            return ("open", "under_investigation", "closed", "auto_resolved")
        return (status,) if status else LIVE

    @app.get(prefix + "/exceptions/bills")
    def exception_bills(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                        status: str = "", sort: str = q.DEFAULT_EXCEPTION_SORT):
        run, lo, hi, st = p
        statuses = statuses_of(status)
        page_df, total, value = q.exception_page(s, lo, hi, st, BILL_TYPES, statuses, limit=limit, offset=offset, sort=sort)
        frame = et.bill_frame(s, run.id, page_df)
        summary = None
        if not frame.empty and set(statuses) <= set(LIVE):          # suggestions only for work that is still open
            _pool, bills, assignment = pairing_state(s, run.id)
            frame, _cands = et.add_pairing(frame, assignment)
            frame["cluster"] = [assignment.ambiguous.get(k) for k in frame["exception_key"]]
            frame["pair_kind"] = [assignment.kind.get(k, "") for k in frame["exception_key"]]
            summary = ep.count_summary(bills, assignment)
        return {"rows": clean(frame), "total": total, "value": round(value, 2), "pairing": clean(summary)}

    @app.get(prefix + "/exceptions/transactions")
    def exception_transactions(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                               status: str = "", sort: str = q.DEFAULT_EXCEPTION_SORT):
        run, lo, hi, st = p
        page_df, total, value = q.exception_page(s, lo, hi, st, TXN_TYPES, statuses_of(status), limit=limit, offset=offset, sort=sort)
        return {"rows": clean(et.txn_frame(s, page_df)), "total": total, "value": round(value, 2)}

    @app.get(prefix + "/exceptions/{kind}/export")
    def exception_table_export(kind: Literal["bills", "transactions"], p=Depends(period), u=Depends(signed_in), s=Depends(db), status: str = "",
                               sort: str = q.DEFAULT_EXCEPTION_SORT):
        """The WHOLE filtered table, never the page on screen. Bills carry the same suggestions the screen shows (one global assignment)."""
        run, lo, hi, st = p
        statuses = statuses_of(status)
        if kind == "bills":
            live = set(statuses) <= set(LIVE)
            assignment = pairing_state(s, run.id)[2] if live else None
            cols = et.bill_columns(pairing=live, screen=False)

            def shape(f):
                framed = et.bill_frame(s, run.id, f)
                return et.add_pairing(framed, assignment)[0] if assignment is not None else framed
            frame = q.exception_export(s, lo, hi, st, BILL_TYPES, statuses, sort=sort, transform=shape)
            title = "Exception bills"
        else:
            cols = et.TXN_COLUMNS
            frame = q.exception_export(s, lo, hi, st, TXN_TYPES, statuses, sort=sort, transform=lambda f: et.txn_frame(s, f))
            title = "Exception transactions"
        frame = et.export_view(frame, cols, title)
        result = build_export(frame, ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=lo, date_to=hi,
                                                filters={"store": st or "All stores", "show": status or "Open and under investigation"},
                                                value_column="amount", title=title))
        mime = "text/csv" if result.fmt == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return Response(result.data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{result.filename}"'})

    def gateway_of(pool: pd.DataFrame, ref: str):
        hits = pool[pool["txn_ref"].astype(str) == ref] if len(pool) else pool
        if hits.empty:
            raise HTTPException(422, f"Transaction {ref} was not found in the Paytm, Axis or Bajaj data.")
        if hits["gateway"].nunique() > 1:
            raise HTTPException(422, f"Transaction {ref} is held by more than one gateway - not guessed.")
        return str(hits["gateway"].iloc[0]).lower(), float(hits["amount"].iloc[0])

    def open_bill(s, run_id, number):
        bill, is_open, why = mm.find_bill_for_match(s, run_id, number)
        if bill is None or not is_open:
            raise HTTPException(422, why or f"Bill {number} is not an open bill.")
        return bill

    @app.post(prefix + "/exceptions/match")
    def exception_match(data: PairMatch, u=Depends(signed_in), s=Depends(db)):
        """Typing the transaction id IS the match (2026-10-28): a clean, unused, same-store, exact-amount SUCCESS transaction is recorded at
        once; two ids that add up to the bill are one split match; one id typed against two bills that add up to it is one shared payment.
        Anything that does not validate is refused WITH its reasons - this route never records an override."""
        latest_run(s, data.run_id)
        bill = open_bill(s, data.run_id, data.bill_no)
        t1, t2 = mm._clean_ref(data.txn1) or "", mm._clean_ref(data.txn2) or ""
        if data.shared_bill_nos:
            if not t1 or t2:
                raise HTTPException(422, "A shared payment takes exactly one transaction id.")
            others = [open_bill(s, data.run_id, n) for n in data.shared_bill_nos if n != data.bill_no]
            if not others:
                raise HTTPException(422, "Name the other bill the same transaction paid.")
            bills = [bill] + others
            pool = mm.targeted_pool_many([t1])
            gateway, _ = gateway_of(pool, t1)
            v = mm.validate_shared(s, bills, gateway, t1, pool)
            if not v.ok or v.case != mm.CASE_CLEAN:
                raise HTTPException(422, "These bills cannot be matched to " + t1 + " as one payment: " + " | ".join(v.problems or ["not a clean match"]))
            mm.record_shared(s, bills, gateway, t1, MATCH_REASON, u.display_name, v, source="inline")
            return {"ok": True, "message": f"Matched {' + '.join(b['bill_no'] for b in bills)} to {t1} ({gateway}) as one shared payment."}
        if t1 and t2:
            pool = mm.targeted_pool_many([t1, t2])
            g1, a1 = gateway_of(pool, t1)
            g2, a2 = gateway_of(pool, t2)
            v = mm.validate_split(s, bill, [(g1, t1), (g2, t2)], pool)
            if not v.ok or v.case != mm.CASE_CLEAN:
                raise HTTPException(422, "The two transactions cannot be matched: " + " | ".join(v.problems or ["not a clean split"]))
            mm.record(s, bill, g1, t1, MATCH_REASON, u.display_name, v, source="inline", second={"gateway": g2, "txn_ref": t2, "amount": a2})
            return {"ok": True, "message": f"Matched {bill['bill_no']} to {t1} + {t2} (split payment, {a1:,.2f} + {a2:,.2f})."}
        ref = t1 or t2
        if not ref:
            raise HTTPException(422, "Enter a transaction id.")
        pool = mm.targeted_pool_many([ref])
        v = mm.validate_by_id(s, bill, ref, gateway_pool=pool)
        if not v.ok or v.case != mm.CASE_CLEAN:
            hint = ""
            txn = v.txn or {}
            if txn and float(txn.get("amount") or 0) > float(bill["amount"]) + 0.005 and str(txn.get("store") or "") == str(bill["store"]):
                hint = " If this one transaction paid two bills, match them together as a shared payment."
            elif any("amount" in str(x).lower() for x in (v.problems or [])):
                hint = " If the bill was paid in two parts, enter the second transaction as well."
            raise HTTPException(422, f"{ref} cannot be matched to {bill['bill_no']}: " + " | ".join(v.problems or ["not a clean match"]) + hint)
        mm.record(s, bill, v.gateway, ref, MATCH_REASON, u.display_name, v, source="inline")
        return {"ok": True, "message": f"Matched {bill['bill_no']} to {ref} ({v.gateway})."}

    @app.post(prefix + "/exceptions/txn-match")
    def exception_txn_match(data: TxnMatch, u=Depends(signed_in), s=Depends(db)):
        """A transaction closed by typing the bill it paid - validated exactly as the by-bill match is."""
        latest_run(s, data.run_id)
        ref = mm._clean_ref(data.transaction_id) or ""
        pool, _bills, _a = pairing_state(s, data.run_id)
        hit = pool[(pool["txn_id"] == ref) & (pool["gateway"] == data.gateway.lower())]
        if hit.empty:
            raise HTTPException(409, "That transaction is no longer an open unmapped transaction. Refresh the page.")
        t = hit.iloc[0]
        bill = open_bill(s, data.run_id, data.bill_no)
        txn_pool = pd.DataFrame([{"gateway": t["gateway"], "txn_ref": ref, "business_date": t["date"], "store": t["store"], "amount": float(t["amount"]),
                                  "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None}])
        v = mm.validate(s, bill, t["gateway"], ref, txn_pool)
        if not v.ok or v.case != mm.CASE_CLEAN:
            raise HTTPException(422, f"{ref} cannot be matched to {bill['bill_no']}: " + " | ".join(v.problems or ["not a clean match"]))
        mm.record(s, bill, t["gateway"], ref, TXN_MATCH_REASON, u.display_name, v, source="inline")
        return {"ok": True, "message": f"Matched {ref} to {bill['bill_no']}."}

    def cluster_members(s, run_id, cid):
        pool, bills, assignment = pairing_state(s, run_id)
        cluster = assignment.clusters.get(cid)
        if cluster is None:
            raise HTTPException(404, "That cluster no longer exists. Refresh the page.")
        b = bills[bills["bill_key"].isin(cluster["bills"])].sort_values(["time", "bill_no"])
        t = pool[pool["txn_id"].isin(cluster["txns"])].sort_values(["time", "txn_id"])
        return b, t

    @app.get(prefix + "/exceptions/clusters/{cid}")
    def cluster_detail(cid: int, run_id: int, u=Depends(signed_in), s=Depends(db)):
        """An ambiguous cluster: N bills and M transactions of one store and amount that cannot be told apart. Listed, never guessed."""
        b, t = cluster_members(s, run_id, cid)
        return {"bills": clean(b[["bill_no", "date", "time", "mop", "store", "amount"]]),
                "transactions": clean(t[["txn_id", "gateway", "date", "time", "store", "amount"]]),
                "can_pair": bool(len(b) == len(t) and len(b) > 0)}

    @app.post(prefix + "/exceptions/clusters/{cid}/pair")
    def cluster_pair(cid: int, data: ClusterPair, u=Depends(signed_in), s=Depends(db)):
        """N bills and N transactions of one store and amount: pair them in time order. The amounts are equal, so no figure moves; each
        pair is validated and recorded as an ordinary manual match."""
        latest_run(s, data.run_id)
        b, t = cluster_members(s, data.run_id, cid)
        if len(b) != len(t) or b.empty:
            raise HTTPException(422, "The counts differ, so this cannot be paired as a set. Match each bill by hand.")
        done, failed = 0, []
        for brow, trow in zip(b.itertuples(), t.itertuples()):
            bill = {"business_date": brow.date, "store": brow.store, "bill_no": brow.bill_no, "mop": brow.mop, "amount": float(brow.amount)}
            v = mm.validate_by_id(s, bill, trow.txn_id, gateway_pool=pd.DataFrame([{
                "gateway": trow.gateway, "txn_ref": trow.txn_id, "business_date": trow.date, "store": trow.store, "amount": trow.amount,
                "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None}]))
            if not v.ok:
                failed.append({"bill_no": brow.bill_no, "why": " | ".join(v.problems)})
                continue
            try:
                mm.record(s, bill, v.gateway, trow.txn_id, MATCH_REASON, u.display_name, v, source="inline")
                done += 1
            except (ValueError, LookupError) as exc:
                s.rollback()
                failed.append({"bill_no": brow.bill_no, "why": str(exc)})
        return {"paired": done, "of": int(len(b)), "failed": failed}

    @app.get(prefix + "/exceptions/second-open")
    def second_open(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        """Bills a person matched with ONE transaction that still have another unmapped transaction of the same store and amount nearby: a
        possible duplicate payment. The bill stays closed; a person decides (refund it, or find the second bill)."""
        pool, _bills, _a = pairing_state(s, p[0].id)
        minutes, days = ep.window_settings(cfg().raw)
        return {"rows": clean(ep.second_transaction_still_open(s, pool, days))}
