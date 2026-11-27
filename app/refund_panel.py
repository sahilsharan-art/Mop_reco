"""Customer refund workbook: upload, check, record (2026-11-27).

One panel, used by the Upload page (the "Customer refunds" report type) and by the Exceptions page, so the two cannot differ.
The finance team's workbook is read by column NAME (bank details are never read), every line is judged by the ordinary refund
rules and previewed, and only the lines that pass are recorded - after a confirmation, in ONE database transaction.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from dashboard_common import confirm_with_operator
from mop_reco import refunds as rf
from mop_reco import uploads as up
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import money_table


def render(session, key: str = "rf") -> None:
    done = st.session_state.pop(f"_{key}_done", None)
    if done:
        st.success(done)
    st.caption("Upload the finance team's Customer Refund workbook (.xlsx). It is read by column name - Transaction No, Zoho ID, "
               "Name, Store Code, Tran Date, Process Date, Amt (and Bill No / Remarks when present). Bank details in the "
               "workbook are never read.")
    uploaded = st.file_uploader("Customer Refund workbook (.xlsx)", type=["xlsx"], key=f"{key}_upload")
    source_key = (uploaded.name, uploaded.size) if uploaded is not None else None
    if st.session_state.get(f"_{key}_src") != source_key:
        st.session_state[f"_{key}_src"] = source_key
        st.session_state.pop(f"_{key}_judged", None)
    if uploaded is not None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            (Path(tmp) / Path(uploaded.name).name).write_bytes(uploaded.getvalue())
            try:
                frame, files = rf.read_refund_folder(tmp)
            except Exception as exc:  # noqa: BLE001 - shown with its reason; nothing was read
                st.error(f"The workbook could not be read: {exc}")
                return
        if frame.empty:
            st.error("No refund lines found. The header row needs Transaction No, Zoho ID, Name, Store Code, Tran Date, "
                     "Process Date and Amt.")
            return
    else:
        frame, files = rf.read_refund_folder()
    if not files:
        st.info("Choose the Customer Refund workbook above"
                + (f", or place it in {rf.DEFAULT_FOLDER}." if rf.DEFAULT_FOLDER.exists() else "."))
        return
    st.caption("Read: " + "; ".join(f"{f['file']} ({f['rows']:,} lines)" for f in files) + ".")
    checker = confirm_with_operator(st.button("Check the workbook", key=f"{key}_check"), "checking this workbook",
                                    key=f"{key}_checkop")
    if checker:
        st.session_state[f"_{key}_judged"] = rf.preview_folder(session, frame)
        if uploaded is not None:           # the upload history keeps one line per workbook: who checked it, and what it held
            up.note_refund_workbook(session, uploaded.name, uploaded.size, frame, st.session_state[f"_{key}_judged"], checker)
    judged = st.session_state.get(f"_{key}_judged")
    if judged is None:
        return
    ok = judged[judged["verdict"] == "apply"]
    hold = judged[judged["verdict"] == "hold"]
    done_n = int((judged["verdict"] == "recorded").sum())
    st.markdown(f"**{fmt_count(done_n)} already recorded** · **{fmt_count(len(ok))} ready to record - "
                f"{money_table(float(ok['txn_amount'].sum()))}** · **{fmt_count(len(hold))} held** "
                "(nothing is recorded for a held line).")
    noted = ok[ok["notes"].fillna("").str.len() + ok["extra_remark"].fillna("").str.len() > 0]
    if len(noted):
        st.caption(f"{len(noted)} of the lines to record carry a note that is written into the exception's remark:")
        st.dataframe(noted[["file_row", "transaction_no", "store_code", "store_used", "zoho_no", "notes", "extra_remark"]],
                     hide_index=True, width="stretch", height=200)
    if len(hold):
        st.dataframe(hold[["file", "file_row", "transaction_no", "store_code", "zoho_no", "customer_name",
                           "amount_file", "txn_amount", "why"]], hide_index=True, width="stretch", height=260)
    a1, a2, _a3 = st.columns([1.4, 1, 3])
    operator = confirm_with_operator(
        a1.button(f"Record {fmt_count(len(ok))} refunds", type="primary", key=f"{key}_apply", disabled=ok.empty),
        "recording these customer refunds", key=f"{key}_op")
    if a2.button("Cancel", key=f"{key}_cancel"):
        st.session_state.pop(f"_{key}_judged", None)
        st.rerun()
    if operator:
        try:
            n = rf.apply_folder(session, judged, operator)
        except Exception as exc:  # noqa: BLE001 - shown with its reason; nothing was recorded
            st.error(f"Nothing was recorded: {exc}")
            return
        if uploaded is not None:
            up.note_refund_workbook(session, uploaded.name, uploaded.size, frame, judged, operator, recorded=int(n),
                                    recorded_value=float(ok["txn_amount"].sum()))
        st.session_state[f"_{key}_done"] = f"{n} customer refund(s) recorded; they leave Exception transactions."
        st.session_state.pop(f"_{key}_judged", None)
        st.cache_data.clear()
        st.rerun()
