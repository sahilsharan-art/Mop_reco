"""Manual matches — the data-entry page for R1 (rebuilt 2026-09-20 into two
modes: By bill and By transaction). Matching happens here by pasting an id
into an editable cell; every submission validates immediately and shows its
result inline, and nothing is written until "Save matches" is pressed —
rejected rows stay on screen with their reason. Customer refunds (a third
way an unmapped transaction closes — never a bill match) are uploaded from
By transaction mode. The register below is the audit trail: every manual
match ever recorded, in force or not.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import (
    confirm_with_operator,
    db_session,
    export_all_button,
    frame_export_button,
    latest_run,
    no_run_yet,
    page,
    sidebar,
    status_styler,
    stop_if_no_dates,
    tile,
)
from mop_reco import gateway_txns as gt
from mop_reco import io_
from mop_reco import manual_match as mm
from mop_reco import queries as q
from mop_reco import refunds as rf
from mop_reco.exceptions import bill_exception_key, txn_exception_key
from mop_reco.export import ExportMeta
from mop_reco.formatting import blank as fmt_blank
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_short, money_table

page("Manual matches",
     "Match a bill to a transaction — by bill, or by transaction — validate "
     "before you save, and the audit register of every human override on "
     "R1, in force or not.")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)

LIFECYCLE_HELP = q.MANUAL_LIFECYCLE_LABEL
PAGE_SIZE = 50

# ===========================================================================
# Both two-sided controls, always visible regardless of which mode is open —
# bills: total = matched + unmatched; transactions: total = mapped +
# refunded + unmapped (rules, Request B).
# ===========================================================================
bill_summary = q.bill_match_summary(session, run.id, date_from, date_to, store)
txn_summary = q.transaction_resolution_summary(session, date_from, date_to, store)

with st.container(key="mm-open-ribbon"):
    st.markdown("**Bills**")
    r1, r2, r3 = st.columns(3)
    tile(r1, "OPEN BILLS", fmt_count(bill_summary["open"]["count"]),
         state="attention" if bill_summary["open"]["count"] else "clean",
         note=money_short(bill_summary["open"]["value"]),
         tooltip=f"Rs {money_table(bill_summary['open']['value'])} — no transaction matched yet")
    tile(r2, "CLOSED BILLS", fmt_count(bill_summary["closed"]["count"]), state="clean",
         note=money_short(bill_summary["closed"]["value"]),
         tooltip=f"Rs {money_table(bill_summary['closed']['value'])} — matched, by the "
                 "engine or by a person.")
    tile(r3, "TOTAL BILLS", fmt_count(bill_summary["total"]["count"]), state="neutral",
         note=f"{fmt_count(bill_summary['open']['count'])} open + "
              f"{fmt_count(bill_summary['closed']['count'])} closed",
         tooltip=f"Rs {money_table(bill_summary['total']['value'])}")

    if (bill_summary["open"]["count"] + bill_summary["closed"]["count"]
            == bill_summary["total"]["count"]):
        st.caption(
            f"✓ open ({fmt_count(bill_summary['open']['count'])}) + closed "
            f"({fmt_count(bill_summary['closed']['count'])}) = total "
            f"({fmt_count(bill_summary['total']['count'])})."
        )
    else:
        st.error(
            f"**MISMATCH**: open ({fmt_count(bill_summary['open']['count'])}) + "
            f"closed ({fmt_count(bill_summary['closed']['count'])}) ≠ total "
            f"({fmt_count(bill_summary['total']['count'])})."
        )

    st.markdown("**Transactions**")
    t1, t2, t3, t4 = st.columns(4)
    tile(t1, "MAPPED TO BILL", fmt_count(txn_summary.mapped_n), state="clean",
         note=money_short(txn_summary.mapped_v))
    tile(t2, "REFUNDED TO CUSTOMER", fmt_count(txn_summary.refunded_n),
         state="neutral", note=money_short(txn_summary.refunded_v),
         tooltip=f"Pending {fmt_count(txn_summary.refund_pending_n)} · "
                 f"Released {fmt_count(txn_summary.refund_released_n)} — "
                 "never merged.")
    tile(t3, "UNMAPPED (OPEN)", fmt_count(txn_summary.unmapped_n),
         state="attention" if txn_summary.unmapped_n else "clean",
         note=money_short(txn_summary.unmapped_v))
    tile(t4, "TOTAL TRANSACTIONS", fmt_count(txn_summary.total_n), state="neutral",
         note=money_short(txn_summary.total_v))
    st.caption(
        f"Refund pending {fmt_count(txn_summary.refund_pending_n)} · "
        f"refund released {fmt_count(txn_summary.refund_released_n)} — "
        "both visible, never merged into one figure."
    )
    if txn_summary.ok:
        st.caption(
            f"✓ mapped ({fmt_count(txn_summary.mapped_n)}) + refunded "
            f"({fmt_count(txn_summary.refunded_n)}) + unmapped "
            f"({fmt_count(txn_summary.unmapped_n)}) = total "
            f"({fmt_count(txn_summary.total_n)})."
        )
    else:
        st.error(
            f"**MISMATCH**: mapped + refunded + unmapped differs from total "
            f"by {txn_summary.difference}."
        )

# --- date-lag matches: information, never an exception (2026-09-13) -------
lag_matches = q.date_lag_matches(session, run.id, date_from, date_to, store)
if not lag_matches.empty:
    with st.expander(
        f"📅 {fmt_count(len(lag_matches))} bill(s) matched to a transaction "
        "dated on a different day"):
        st.caption(
            "Information, not an exception — the reference, store and amount "
            "all agree; only the gateway's own transaction date differs from "
            "the bill's. Revenue recognition stays on the bill's date "
            "regardless."
        )
        shown_lag = lag_matches.rename(columns={
            "bill_no": "Bill no", "business_date": "Bill date",
            "txn_date": "Txn date", "lag_days": "Lag (days)",
            "amount": "Amount (Rs)", "gateway": "Gateway",
            "match_note": "Note",
        }).copy()
        shown_lag["Bill date"] = shown_lag["Bill date"].map(display_date)
        shown_lag["Txn date"] = shown_lag["Txn date"].map(display_date)
        shown_lag["Amount (Rs)"] = shown_lag["Amount (Rs)"].map(money_table)
        st.dataframe(shown_lag, hide_index=True, width="stretch")

st.divider()

# ===========================================================================
# Segmented toggle — [By bill] [By transaction]. Two buttons, not tabs.
# ===========================================================================
st.session_state.setdefault("mm_mode", "by_bill")
m1, m2, _ = st.columns([1, 1, 4])
if m1.button("By bill", type="primary" if st.session_state["mm_mode"] == "by_bill"
             else "secondary", width="stretch", key="mm_mode_bill"):
    st.session_state["mm_mode"] = "by_bill"
    st.rerun()
if m2.button("By transaction", type="primary" if st.session_state["mm_mode"] == "by_transaction"
             else "secondary", width="stretch", key="mm_mode_txn"):
    st.session_state["mm_mode"] = "by_transaction"
    st.rerun()

mode = st.session_state["mm_mode"]

# ===========================================================================
# BY BILL — open bills, editable "Matched txn", validate on entry, batch save
# ===========================================================================
if mode == "by_bill":
    st.markdown("##### By bill — open bills, no transaction matched yet")

    f1, f2, f3 = st.columns([1.5, 1.15, 1.15])
    search = f1.text_input("Find a bill or transaction", placeholder="bill no, txn id or RRN",
                           help="Searches bill number and the Paytm, Axis and "
                                "Bajaj references.")
    status_pick = f2.selectbox("Status", ["Any"] + sorted(q.status_options(session, run.id)))
    mop_pick = f3.selectbox("MOP", ["Any"] + sorted(q.mop_options(session, run.id)))

    state_key = "mm_open_offset"
    st.session_state.setdefault(state_key, 0)
    fingerprint = (str(date_from), str(date_to), store, search, status_pick, mop_pick)
    if st.session_state.get("mm_open_fingerprint") != fingerprint:
        st.session_state["mm_open_fingerprint"] = fingerprint
        st.session_state[state_key] = 0

    df, total = q.bill_match_page(
        session, run.id, date_from, date_to, store, show="open",
        status=None if status_pick == "Any" else status_pick,
        mop=None if mop_pick == "Any" else mop_pick,
        search=search or None,
        limit=PAGE_SIZE, offset=st.session_state[state_key],
    )

    if total == 0:
        st.success("No open bills match this selection.")
    else:
        offset = min(st.session_state[state_key], max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE))
        first_row, last_row = offset + 1, min(offset + PAGE_SIZE, total)
        pages = max(1, -(-total // PAGE_SIZE))
        current_page = offset // PAGE_SIZE + 1

        p1, p2, p3, p4, p5 = st.columns([0.9, 0.9, 2.2, 1.1, 1.4])
        if p1.button("Previous", disabled=offset == 0, width="stretch", key="mm_open_prev"):
            st.session_state[state_key] = max(0, offset - PAGE_SIZE)
            st.rerun()
        if p2.button("Next", disabled=last_row >= total, width="stretch", key="mm_open_next"):
            st.session_state[state_key] = offset + PAGE_SIZE
            st.rerun()
        p3.markdown(
            f"<div class='mop-page-info'>Showing <b>{fmt_count(first_row)}-{fmt_count(last_row)}</b> "
            f"of <b>{fmt_count(total)}</b> · {money_short(float(df['amount'].sum()))} on this page"
            f"</div>", unsafe_allow_html=True)
        jump = p4.number_input("Page", min_value=1, max_value=pages, value=current_page,
                               step=1, label_visibility="collapsed", key="mm_open_jump")
        if p5.button(f"Go to page (of {fmt_count(pages)})", width="stretch", key="mm_open_go"):
            st.session_state[state_key] = (int(jump) - 1) * PAGE_SIZE
            st.rerun()

        remark_keys = [bill_exception_key(r.date, r.store, r.bill_no, r.mop)
                      for r in df.itertuples()]
        remarks = q.latest_remarks(session, remark_keys)

        st.session_state.setdefault("bybill_pending", {})
        pending = st.session_state["bybill_pending"]

        shown = pd.DataFrame({
            "Date": [display_date(r.date) for r in df.itertuples()],
            "Store": df["store"],
            "Bill no": df["bill_no"],
            "MOP": df["mop"],
            "Amount (Rs)": df["amount"].map(money_table),
            "Status": df["status"],
            "Matched txn": [
                pending.get(bill_exception_key(r.date, r.store, r.bill_no, r.mop), {})
                       .get("ref", "")
                for r in df.itertuples()
            ],
            "Remark": [fmt_blank(remarks.get(k, (None, None))[0]) for k in remark_keys],
        })
        bill_keys = list(remark_keys)

        st.caption(
            "Paste a transaction id or RRN into **Matched txn** and press enter. "
            "The gateway is worked out from the id — you are not asked for it. "
            "It validates immediately below; nothing is written until you "
            "press **Save matches**."
        )

        edited = st.data_editor(
            shown, hide_index=True, width="stretch",
            disabled=[c for c in shown.columns if c != "Matched txn"],
            key=f"mm_bybill_editor_{st.session_state[state_key]}",
            column_config={
                "Matched txn": st.column_config.TextColumn(
                    "Matched txn", width="medium",
                    help="Paste the id exactly as the gateway shows it, then press enter."),
            },
        )

        changed = shown["Matched txn"] != edited["Matched txn"]
        if changed.any():
            idx = changed.idxmax()
            key = bill_keys[idx]
            pasted = io_.clean_ref(edited["Matched txn"].iloc[idx]) or ""
            if not pasted:
                pending.pop(key, None)
            else:
                bill = {"business_date": df["date"].iloc[idx], "store": df["store"].iloc[idx],
                        "bill_no": df["bill_no"].iloc[idx], "mop": df["mop"].iloc[idx],
                        "amount": float(df["amount"].iloc[idx])}
                validation = mm.validate_by_id(session, bill, pasted)
                pending[key] = {"bill": bill, "ref": pasted, "validation": validation}
            st.rerun()

        this_page_pending = {k: v for k, v in pending.items() if k in bill_keys}
        if this_page_pending:
            st.markdown("###### Pending — validated, not yet saved")
            rows = []
            for key, entry in this_page_pending.items():
                v = entry["validation"]
                verdict = "Accept" if v.ok else "Reject"
                reason = "Clean match" if v.ok else " | ".join(v.problems)
                lag = f" · lag {v.date_lag_days:+d}d" if v.date_lag_days else ""
                rows.append({
                    "Bill no": entry["bill"]["bill_no"], "Transaction": entry["ref"],
                    "Gateway": v.gateway or "—", "Verdict": verdict,
                    "Reason": reason + lag,
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={"Reason": st.column_config.TextColumn(width="large")})

            acceptable = {k: v for k, v in this_page_pending.items() if v["validation"].ok}
            operator = confirm_with_operator(
                st.button(f"Save matches ({fmt_count(len(acceptable))} accepted)",
                         type="primary", key="mm_bybill_save",
                         disabled=not acceptable),
                "saving these matches", key="mm_bybill_save_op")
            if operator:
                saved, failed = [], []
                for key, entry in list(acceptable.items()):
                    try:
                        mm.record(session, entry["bill"], entry["validation"].gateway,
                                 entry["ref"], "manual_match_by_bill", operator,
                                 entry["validation"])
                        saved.append(entry["bill"]["bill_no"])
                        pending.pop(key, None)
                    except (ValueError, LookupError) as exc:
                        failed.append((entry["bill"]["bill_no"], str(exc)))
                st.success(f"Saved {len(saved)} match(es).")
                for bill_no, why in failed:
                    st.warning(f"{bill_no} — {why}")
                st.rerun()

        export_all_button(
            lambda progress: q.bill_match_export(
                session, run.id, date_from, date_to, store, show="open",
                status=None if status_pick == "Any" else status_pick,
                mop=None if mop_pick == "Any" else mop_pick,
                search=search or None, progress=progress,
            ),
            bill_summary["open"]["count"],
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store, "status": status_pick,
                              "mop": mop_pick, "search": search or None},
                      value_column="amount", title="Open bills"),
            key="dl_mm_open", noun="open bills",
        )

    with st.expander("Bulk upload — bill number → transaction id"):
        st.caption(
            "Clear many open bills at once from a spreadsheet. Every row is "
            "validated first and **nothing is applied** until you choose — "
            "the same global reference search the inline paste uses, no "
            "gateway column (worked out from the id), one batch id so it "
            "reverses as a unit."
        )
        template = mm.upload_template()
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            template.to_excel(writer, index=False, sheet_name="manual_matches")
        st.download_button("Download the template", buffer.getvalue(),
                           file_name="manual_match_template.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")
        st.caption(f"Columns, exactly: `{'`, `'.join(mm.UPLOAD_COLUMNS)}`.")

        uploaded = st.file_uploader("The completed template",
                                   type=["xlsx", "csv"], key="mm_upload")

        if uploaded is not None:
            try:
                frame = (pd.read_csv(uploaded, dtype=str) if uploaded.name.endswith(".csv")
                         else pd.read_excel(uploaded, dtype=str))
            except Exception as exc:  # noqa: BLE001 — show the real reason
                st.error(f"Could not read the file — {type(exc).__name__}: {exc}")
                st.stop()

            st.caption(f"{fmt_count(len(frame))} row(s) read from **{uploaded.name}**.")
            pool = mm.gateway_pool_for_date()

            try:
                preview = mm.preview_upload(session, frame, pool, run.id)
            except ValueError as exc:
                st.error(str(exc))
                st.stop()

            p1, p2, p3 = st.columns(3)
            p1.metric("Would be accepted", len(preview.accepted))
            p2.metric("Carry warnings", len(preview.warned))
            p3.metric("Rejected", len(preview.rejected))

            st.dataframe(preview.as_frame(), hide_index=True, width="stretch",
                         column_config={"why": st.column_config.TextColumn(width="large")})

            if preview.rejected:
                st.error(f"**{len(preview.rejected)} row(s) rejected and listed above.**")

            include = st.checkbox(
                f"Also apply the {len(preview.warned)} row(s) with warnings",
                value=False, disabled=not preview.warned)
            override = st.text_area(
                "Override reason — required to apply warned rows",
                key="mm_batch_override") if include else None

            operator = confirm_with_operator(
                st.button("Apply this upload", type="primary", key="mm_apply"),
                "applying this upload", key="mm_apply_op")
            if operator:
                try:
                    batch, applied, failed = mm.apply_upload(
                        session, preview, operator, include_warnings=include,
                        override_reason=override, file_name=uploaded.name)
                except ValueError as exc:
                    st.error(f"Refused — {exc}")
                    st.stop()
                st.success(
                    f"Batch #{batch.id}: {len(applied)} applied, {len(failed)} failed, "
                    f"{len(preview.rejected)} rejected."
                )
                for row, why in failed:
                    st.warning(f"Row {row.row_number} ({row.bill.get('bill_no')}) — {why}")

# ===========================================================================
# BY TRANSACTION — unmapped transactions, editable "Matched bill no",
# a status/filter "Resolution" (see note below), and the refund upload.
# ===========================================================================
else:
    st.markdown("##### By transaction")
    st.caption(
        "**Resolution** is shown as the transaction's current state, plus a "
        "**Show** filter above the table — it is not a free-standing dropdown "
        "you set directly, because recording a customer refund needs more "
        "than one field (a Zoho number, the customer's name); that goes "
        "through the Refund upload below. Matching to a bill is still done "
        "inline, in **Matched bill no**."
    )

    resolution_pick = st.selectbox(
        "Show", ["Unmapped (open)", "Matched to bill", "Refunded to customer", "All"])
    _RES_MAP = {"Unmapped (open)": "unmapped", "Matched to bill": "mapped",
               "Refunded to customer": "refunded", "All": None}

    txn_frame = gt.gateway_transactions_with_resolution(
        session, date_from, date_to) if date_from and date_to else pd.DataFrame()
    if not txn_frame.empty:
        # The engine's own pool shape (gateway/txn_ref/business_date/...) —
        # renamed to the names this page's table/pending-edit code uses,
        # the same rename q.unmapped_transactions_frame() applies.
        txn_frame = txn_frame.rename(columns={"txn_ref": "transaction_id",
                                              "business_date": "date"})
    if store and not txn_frame.empty:
        txn_frame = txn_frame[txn_frame["store"] == store]
    wanted = _RES_MAP[resolution_pick]
    if wanted and not txn_frame.empty:
        txn_frame = txn_frame[txn_frame["resolution"] == wanted]
    txn_frame = txn_frame.reset_index(drop=True)

    state_key = "mm_txn_offset"
    st.session_state.setdefault(state_key, 0)
    fingerprint = (str(date_from), str(date_to), store, resolution_pick)
    if st.session_state.get("mm_txn_fingerprint") != fingerprint:
        st.session_state["mm_txn_fingerprint"] = fingerprint
        st.session_state[state_key] = 0

    total = len(txn_frame)
    if total == 0:
        st.success("No transactions match this selection.")
    else:
        offset = min(st.session_state[state_key], max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE))
        page_df = txn_frame.iloc[offset:offset + PAGE_SIZE].reset_index(drop=True)
        first_row, last_row = offset + 1, min(offset + PAGE_SIZE, total)
        pages = max(1, -(-total // PAGE_SIZE))
        current_page = offset // PAGE_SIZE + 1

        p1, p2, p3, p4, p5 = st.columns([0.9, 0.9, 2.2, 1.1, 1.4])
        if p1.button("Previous", disabled=offset == 0, width="stretch", key="mm_txn_prev"):
            st.session_state[state_key] = max(0, offset - PAGE_SIZE)
            st.rerun()
        if p2.button("Next", disabled=last_row >= total, width="stretch", key="mm_txn_next"):
            st.session_state[state_key] = offset + PAGE_SIZE
            st.rerun()
        p3.markdown(
            f"<div class='mop-page-info'>Showing <b>{fmt_count(first_row)}-{fmt_count(last_row)}</b> "
            f"of <b>{fmt_count(total)}</b> · "
            f"{money_short(float(pd.to_numeric(page_df['amount'], errors='coerce').sum()))} on this page"
            f"</div>", unsafe_allow_html=True)
        jump = p4.number_input("Page", min_value=1, max_value=pages, value=current_page,
                               step=1, label_visibility="collapsed", key="mm_txn_jump")
        if p5.button(f"Go to page (of {fmt_count(pages)})", width="stretch", key="mm_txn_go"):
            st.session_state[state_key] = (int(jump) - 1) * PAGE_SIZE
            st.rerun()

        remark_keys = [txn_exception_key(r.gateway, r.transaction_id)
                      for r in page_df.itertuples()]
        remarks = q.latest_remarks(session, remark_keys)

        st.session_state.setdefault("bytxn_pending", {})
        pending = st.session_state["bytxn_pending"]

        _RES_LABEL = {"mapped": "Matched to bill", "refunded": "Refunded to customer",
                     "unmapped": "Open"}
        can_edit = wanted == "unmapped"

        shown = pd.DataFrame({
            "Gateway": page_df["gateway"],
            "Transaction id": page_df["transaction_id"],
            "Date": [display_date(d) for d in page_df["date"]],
            "Store": page_df["store"],
            "Amount (Rs)": page_df["amount"].map(money_table),
            "Payment mode": page_df["payment_mode"].map(fmt_blank),
            "Matched bill no": [
                pending.get(f"{r.gateway}|{r.transaction_id}", {}).get("bill_no",
                    fmt_blank(r.matched_bill_no))
                for r in page_df.itertuples()
            ] if can_edit else page_df["matched_bill_no"].map(fmt_blank),
            "Resolution": page_df["resolution"].map(_RES_LABEL),
            "Remark": [fmt_blank(remarks.get(k, (None, None))[0]) for k in remark_keys],
        })
        txn_keys = [f"{r.gateway}|{r.transaction_id}" for r in page_df.itertuples()]

        st.caption(
            "Paste a bill number into **Matched bill no** and press enter — "
            "validated the same way, in reverse. Nothing is written until "
            "you press **Save matches**." if can_edit else
            "Read-only — switch **Show** to \"Unmapped (open)\" to match one."
        )

        edited = st.data_editor(
            shown, hide_index=True, width="stretch",
            disabled=[c for c in shown.columns if c != "Matched bill no"] if can_edit
                     else list(shown.columns),
            key=f"mm_bytxn_editor_{st.session_state[state_key]}_{wanted}",
            column_config={
                "Transaction id": st.column_config.TextColumn(width="medium"),
                "Matched bill no": st.column_config.TextColumn(
                    "Matched bill no", width="medium",
                    help="Paste the bill number exactly, then press enter."),
            },
        )

        if can_edit:
            changed = shown["Matched bill no"] != edited["Matched bill no"]
            if changed.any():
                idx = changed.idxmax()
                key = txn_keys[idx]
                raw_bill_no = edited["Matched bill no"].iloc[idx]
                typed_bill_no = "" if raw_bill_no is None else str(raw_bill_no).strip()
                if not typed_bill_no:
                    pending.pop(key, None)
                else:
                    txn_row = page_df.iloc[idx]
                    bill, is_open, why = mm.find_bill_for_match(session, run.id, typed_bill_no)
                    if bill is None or not is_open:
                        validation = mm.Validation("b", False, "asserted_unverified",
                                                   [why or "bill not found"])
                    else:
                        gateway_pool = mm.gateway_pool_for_date()
                        validation = mm.validate(session, bill, str(txn_row["gateway"]),
                                                 str(txn_row["transaction_id"]), gateway_pool)
                    pending[key] = {"bill": bill, "bill_no": typed_bill_no,
                                    "gateway": str(txn_row["gateway"]),
                                    "ref": str(txn_row["transaction_id"]),
                                    "validation": validation}
                st.rerun()

            this_page_pending = {k: v for k, v in pending.items() if k in txn_keys}
            if this_page_pending:
                st.markdown("###### Pending — validated, not yet saved")
                rows = []
                for key, entry in this_page_pending.items():
                    v = entry["validation"]
                    verdict = "Accept" if v.ok else "Reject"
                    reason = "Clean match" if v.ok else " | ".join(v.problems)
                    rows.append({"Transaction": entry["ref"], "Bill no": entry["bill_no"],
                                "Verdict": verdict, "Reason": reason})
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                             column_config={"Reason": st.column_config.TextColumn(width="large")})

                acceptable = {k: v for k, v in this_page_pending.items() if v["validation"].ok}
                operator = confirm_with_operator(
                    st.button(f"Save matches ({fmt_count(len(acceptable))} accepted)",
                             type="primary", key="mm_bytxn_save",
                             disabled=not acceptable),
                    "saving these matches", key="mm_bytxn_save_op")
                if operator:
                    saved, failed = [], []
                    for key, entry in list(acceptable.items()):
                        try:
                            mm.record(session, entry["bill"], entry["gateway"], entry["ref"],
                                     "manual_match_by_transaction", operator,
                                     entry["validation"])
                            saved.append(entry["bill_no"])
                            pending.pop(key, None)
                        except (ValueError, LookupError) as exc:
                            failed.append((entry["bill_no"], str(exc)))
                    st.success(f"Saved {len(saved)} match(es).")
                    for bill_no, why in failed:
                        st.warning(f"{bill_no} — {why}")
                    st.rerun()

        export_all_button(
            lambda progress: q.unmapped_transactions_export(session, date_from, date_to, store)
                             if wanted == "unmapped" else txn_frame,
            total,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store, "show": resolution_pick},
                      value_column="amount", title="Transactions"),
            key="dl_mm_txn", noun="transactions",
        )

    # -----------------------------------------------------------------
    # Customer refund resolution (§ Request B part 3) — a Zoho upload,
    # never a bare dropdown selection: it needs zoho_no and customer_name,
    # and every row is validated before anything is applied.
    # -----------------------------------------------------------------
    with st.expander("Refund upload — mark unmapped transactions as refunded to the customer"):
        st.caption(
            "A refund is NOT a bill match. The transaction moves to "
            "`resolution = customer_refund`; it is never counted as mapped "
            "to a bill and never closes one. All rows are validated first "
            "and applied all-or-nothing."
        )
        template = rf.upload_template()
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            template.to_excel(writer, index=False, sheet_name="refunds")
        st.download_button("Download the refund template", buffer.getvalue(),
                           file_name="refund_template.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet", key="dl_refund_template")
        st.caption(f"Columns, exactly: `{'`, `'.join(rf.UPLOAD_COLUMNS)}`. "
                  "`status` is `pending` (refundable, not yet paid) or "
                  "`released`/`paid` (already paid back).")

        refund_upload = st.file_uploader("The completed refund template",
                                        type=["xlsx", "csv"], key="mm_refund_upload")
        if refund_upload is not None:
            try:
                rframe = (pd.read_csv(refund_upload, dtype=str) if refund_upload.name.endswith(".csv")
                         else pd.read_excel(refund_upload, dtype=str))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read the file — {type(exc).__name__}: {exc}")
                st.stop()

            st.caption(f"{fmt_count(len(rframe))} row(s) read from **{refund_upload.name}**.")
            try:
                rpreview = rf.preview_upload(session, rframe)
            except ValueError as exc:
                st.error(str(exc))
                st.stop()

            rp1, rp2 = st.columns(2)
            rp1.metric("Would be accepted", len(rpreview.accepted))
            rp2.metric("Rejected", len(rpreview.rejected))
            st.dataframe(rpreview.as_frame(), hide_index=True, width="stretch",
                         column_config={"why": st.column_config.TextColumn(width="large")})

            if rpreview.rejected:
                st.error(
                    f"**{len(rpreview.rejected)} row(s) rejected and listed above.** "
                    "Applied all-or-nothing — only the accepted rows are written, "
                    "never partially."
                )

            operator = confirm_with_operator(
                st.button("Apply refunds", type="primary", key="mm_refund_apply",
                         disabled=not rpreview.accepted),
                "applying these refunds", key="mm_refund_apply_op")
            if operator:
                try:
                    applied, rejected = rf.apply_upload(session, rpreview, operator,
                                                        file_name=refund_upload.name)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Refused — {exc}")
                    st.stop()
                st.success(f"Applied {len(applied)} refund(s). "
                          f"{len(rejected)} row(s) were not applied (see above).")
                st.rerun()

    # -----------------------------------------------------------------
    # Refund report — its own table and export.
    # -----------------------------------------------------------------
    st.markdown("##### Refund report")
    refund_rows = rf.refund_records(session)
    if refund_rows.empty:
        st.caption("No refunds recorded yet.")
    else:
        rshown = refund_rows.copy()
        rshown["refund_date"] = rshown["refund_date"].map(display_date)
        rshown["uploaded_at"] = pd.to_datetime(rshown["uploaded_at"]).dt.strftime("%d/%m/%Y %H:%M")
        rshown["amount"] = rshown["amount"].map(money_table)
        rshown["customer_name"] = rshown["customer_name"].map(fmt_blank)
        rshown.columns = ["Refund date", "Store", "Gateway", "Transaction id", "Amount (Rs)",
                          "Zoho no", "Customer name", "Status", "Uploaded by", "Uploaded at"]
        st.dataframe(rshown, hide_index=True, width="stretch",
                     column_config={"Transaction id": st.column_config.TextColumn(width="medium")})
        frame_export_button(
            refund_rows,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      value_column="amount", title="Refund report"),
            key="dl_refund_report", noun="refunds",
        )

# ===========================================================================
# Manual match bill report — below both modes, its own export.
# ===========================================================================
st.divider()
st.markdown("##### Manual match bill report")
report = q.manual_match_bill_report(session, date_from, date_to, store)
if report.empty:
    st.caption("No manual matches recorded for this selection.")
else:
    rshown = report.copy()
    rshown["date"] = rshown["date"].map(display_date)
    rshown["txn_date"] = rshown["txn_date"].map(display_date)
    rshown["matched_at"] = pd.to_datetime(rshown["matched_at"]).dt.strftime("%d/%m/%Y %H:%M")
    rshown["amount"] = rshown["amount"].map(money_table)
    rshown.columns = ["Date", "Store", "Bill no", "MOP", "Amount (Rs)", "Gateway",
                      "Transaction id", "Txn date", "Lag", "Matched by",
                      "Matched at", "Reason", "Status"]
    st.dataframe(rshown, hide_index=True, width="stretch",
                 column_config={"Transaction id": st.column_config.TextColumn(width="medium")})
    frame_export_button(
        report,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                  date_from=date_from, date_to=date_to,
                  filters={"store": store},
                  value_column="amount", title="Manual match bill report"),
        key="dl_manual_bill_report", noun="manual matches",
    )

# ===========================================================================
# The register — every manual match ever recorded, in force or not. The
# "Open one" detail panel (dropdown, Bill/Amount/Transaction/Status block,
# Lifecycle line, Reason code line, Audit trail expander, "Reverse this
# match") is REMOVED from display, per instruction — the underlying audit
# data (manual_match_events) and mm.reverse()/mm.reverse_batch() are
# untouched; only this page stopped rendering them.
# ===========================================================================
st.divider()
st.markdown("##### The register")
st.caption(
    "The first thing an auditor asks to see, so nothing is hidden by "
    "default: reversed, superseded, conflicted and orphaned records are all "
    "listed alongside the ones in force."
)

split = q.match_split(session, run.id, date_from, date_to, store)

c1, c2, c3 = st.columns(3)
c1.metric("Matched by system", fmt_count(split.system),
          help="The engine's own result. The only rate comparable across runs.")
c1.caption(f"{split.system_rate * 100:.3f}% · {money_short(split.system_value)}")
c2.metric("Matched manually", fmt_count(split.manual),
          help="A person matched it to a transaction that was actually found.")
c2.caption(money_short(split.manual_value))
c3.metric("Asserted, unverified", fmt_count(split.asserted),
          help="Asserted with no gateway row behind it. NOT a match, and "
               "not counted in any rate.")
c3.caption(money_short(split.asserted_value))

st.caption(
    "These are never blended. A single rate that counts human assertions as "
    "matches is a number that flatters itself."
)

st.divider()

f1, f2, f3, f4 = st.columns(4)
operators = q.manual_match_options(session, "operator")
codes = q.manual_match_options(session, "reason_code")
lifecycles = q.manual_match_options(session, "lifecycle")
who = f1.selectbox("Operator", ["Any"] + operators)
code = f2.selectbox("Reason code", ["Any"] + codes)
life = f3.selectbox("Lifecycle", ["Any"] + lifecycles)
only_overrides = f4.checkbox("Overrides only", value=False,
                             help="Rows recorded despite a validation "
                                  "problem (case b).")

rows = q.manual_matches(
    session, date_from, date_to, store,
    operator=None if who == "Any" else who,
    reason_code=None if code == "Any" else code,
    lifecycle=None if life == "Any" else life,
)
if not rows.empty and only_overrides:
    rows = rows[rows["override_used"]]

if rows.empty:
    st.info("No manual matches recorded for this selection.")
else:
    conflicted = rows[rows["lifecycle"] == "conflicted"]
    if not conflicted.empty:
        st.error(
            f"**{len(conflicted)} conflict(s) need resolving.** The engine "
            "matched these bills to a different transaction than the manual "
            "match names. Both are kept; neither was overwritten."
        )

    reg_shown = rows.copy()
    reg_shown["Amount (Rs)"] = reg_shown["amount"].map(money_table)
    reg_shown["Date"] = reg_shown["business_date"].map(display_date)
    reg_shown["When"] = pd.to_datetime(reg_shown["created_at"]).dt.strftime("%d/%m/%Y %H:%M")
    reg_shown["Override"] = reg_shown["override_used"].map({True: "yes", False: "—"})
    reg_shown["reason_code"] = reg_shown["reason_code"].map(fmt_blank)
    reg_shown["lifecycle_note"] = reg_shown["lifecycle_note"].map(fmt_blank)
    reg_display = reg_shown[["id", "Date", "store", "bill_no", "mop",
                             "Amount (Rs)", "gateway", "transaction_id", "status",
                             "case", "Override", "reason_code", "operator", "When",
                             "lifecycle", "batch_id"]]
    reg_display.columns = ["#", "Date", "Store", "Bill no", "MOP", "Amount (Rs)",
                           "Gateway", "Transaction id", "Status", "Case",
                           "Override", "Reason", "By", "When", "Lifecycle",
                           "Batch"]
    # Status pills (2026-09-18 restyle) — a pandas Styler tint, the value
    # itself untouched, so sorting/export both still read the plain word.
    reg_styled = status_styler(reg_display, {
        "Status": {"matched_manual": "good", "asserted_unverified": "warn"},
        "Lifecycle": {"active": "good", "reversed": "warn", "orphaned": "warn",
                      "superseded": "accent", "conflicted": "bad"},
    })
    st.dataframe(reg_styled, hide_index=True, width="stretch",
                 column_config={"Transaction id":
                                st.column_config.TextColumn(width="medium")})

    frame_export_button(
        rows,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                   date_from=date_from, date_to=date_to,
                   filters={"store": store, "operator": who,
                            "reason_code": code, "lifecycle": life},
                   value_column="amount", title="Manual matches"),
        key="dl_manual", noun="manual matches",
    )
