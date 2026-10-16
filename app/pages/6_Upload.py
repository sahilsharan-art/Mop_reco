"""Upload — supply or replace a single report for a single month without
touching the numbered source folders directly (2026-09-30).

The numbered folders stay the PRIMARY way data arrives (CLAUDE.md section
4); this page is the validated, audited side door for a specific file. See
mop_reco/uploads.py for the full validate-then-store design.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import (
    confirm_with_operator,
    db_session,
    frame_export_button,
    latest_run,
    loaded_dates,
    page,
    sidebar,
)
import run_panel
from mop_reco import uploads as up
from mop_reco.config import load_config
from mop_reco.db import Run
from mop_reco.export import ExportMeta
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date

page("Upload", "Supply or replace a report, then reconcile a date range. Upload first, Run below.")

config = load_config()
session = db_session()
run = latest_run(session)
sidebar(session, run)


def _run_in_progress() -> bool:
    return session.query(Run).filter(Run.status == "running").first() is not None


def _available_months() -> list:
    """Months with loaded data, plus the current and next calendar month —
    never a hardcoded list (rule 8)."""
    months = set()
    if run is not None:
        for d in loaded_dates(run.id):
            months.add(d[:7])
    today = date.today()
    months.add(f"{today.year:04d}-{today.month:02d}")
    nxt_year, nxt_month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    months.add(f"{nxt_year:04d}-{nxt_month:02d}")
    return sorted(months)


def _month_label(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{up.MONTH_LABELS[mo - 1]}-{y}"


# ===========================================================================
# The form
# ===========================================================================
st.header("Upload")
st.markdown("##### Supply a file")

report_names = [s.display_name for s in up.SOURCE_REGISTRY]
c1, c2 = st.columns(2)
report_name = c1.selectbox("Report name", report_names, key="up_report_name")
spec = next(s for s in up.SOURCE_REGISTRY if s.display_name == report_name)

months = _available_months()
month_labels = [_month_label(m) for m in months]
if spec.month_specific:
    month_label = c2.selectbox("Month", month_labels, key="up_month")
    month = months[month_labels.index(month_label)]
else:
    c2.selectbox("Month", ["not month-specific"], key="up_month_disabled", disabled=True)
    month = None

if spec.note:
    st.caption(spec.note)
st.caption(f"Accepted file types: {', '.join(spec.extensions)}.")

uploaded = st.file_uploader("Drop the file here", key=f"up_file_{report_name}")

if uploaded is not None:
    suffix = Path(uploaded.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded.getbuffer())
        temp_path = Path(tmp.name)

    preview = up.validate_upload(spec, temp_path, uploaded.name, month, config)

    st.markdown("###### Preview")
    pc1, pc2 = st.columns(2)
    pc1.metric("Rows", fmt_count(preview.row_count))
    if preview.date_range:
        pc2.metric("Date range in file", f"{preview.date_range[0]} to {preview.date_range[1]}")
    else:
        pc2.metric("Date range in file", "—")

    if preview.columns_found:
        st.caption("Columns detected: " + ", ".join(str(c) for c in preview.columns_found))
    if preview.first_rows is not None and not preview.first_rows.empty:
        st.dataframe(preview.first_rows, hide_index=True, width="stretch")

    for w in preview.warnings:
        st.warning(w)
    for e in preview.errors:
        st.error(e)

    if preview.ok:
        st.success("Validation passed. Nothing is stored until you confirm below.")
        operator = confirm_with_operator(
            st.button("Upload", type="primary", key="up_confirm"),
            "uploading this file", key="up_op")
        if operator:
            row = up.store_file(spec, temp_path, uploaded.name, month, operator,
                               session, config, preview.row_count, preview.date_range)
            msg = f"Stored as '{Path(row.stored_path).name}'."
            if getattr(row, "_renamed", False):
                msg += (" A file with that name already existed, so this one "
                       "was saved with a timestamp suffix — the original was "
                       "never overwritten.")
            st.success(msg)
            if getattr(row, "_config_gap_note", None):
                st.warning(row._config_gap_note)
            st.rerun()
    else:
        st.error("Rejected — nothing was stored. Fix the file (or the month "
                 "selected) and drop it again.")
        try:
            temp_path.unlink()
        except OSError:
            pass

st.divider()

# ===========================================================================
# Upload history
# ===========================================================================
st.markdown("##### Upload history")

records = up.history(session)
if not records:
    st.caption("Nothing uploaded yet.")
else:
    run_in_progress = _run_in_progress()
    rows = []
    for r in records:
        used_run = up.used_by_run(session, r.stored_path)
        rows.append({
            "id": r.id,
            "Uploaded at": r.uploaded_at.strftime("%d/%m/%Y %H:%M"),
            "Report name": r.report_name,
            "Month": _month_label(r.month) if r.month else "—",
            "File name": r.original_file_name,
            "Size": f"{r.size_bytes / 1024:,.1f} KB",
            "Rows": fmt_count(r.row_count) if r.row_count is not None else "—",
            "Date range in file": (f"{r.date_range_start} to {r.date_range_end}"
                                   if r.date_range_start else "—"),
            "Uploaded by": r.uploaded_by,
            "Status": r.status,
            "Used by run #": f"#{used_run}" if used_run else "not yet used",
        })
    history_df = pd.DataFrame(rows)
    st.dataframe(history_df.drop(columns=["id"]), hide_index=True, width="stretch")
    frame_export_button(
        history_df.drop(columns=["id"]),
        ExportMeta(title="Upload history"),
        key="dl_upload_history", noun="upload history rows",
    )

    st.markdown("###### Delete an upload")
    stored_rows = [r for r in records if r.status == "stored"]
    if not stored_rows:
        st.caption("Nothing to delete — every upload is already withdrawn.")
    else:
        labels = [f"#{r.id} — {r.report_name} — {r.original_file_name} "
                 f"({r.uploaded_at.strftime('%d/%m/%Y')})" for r in stored_rows]
        chosen_label = st.selectbox("Choose an upload to delete", labels, key="up_delete_choice")
        chosen = stored_rows[labels.index(chosen_label)]

        used_run = up.used_by_run(session, chosen.stored_path)
        if used_run:
            st.warning(
                f"Run #{used_run} used this file. Deleting it does not change "
                f"run #{used_run}'s figures; a future run will not see it."
            )
        if run_in_progress:
            st.error("A run is currently in progress — deletion is refused until it finishes.")

        del_operator = confirm_with_operator(
            st.button(f"Delete '{chosen.original_file_name}'", key="up_delete_btn",
                     disabled=run_in_progress),
            f"deleting '{chosen.original_file_name}'", key="up_delete_op")
        if del_operator:
            ok, msg = up.delete_upload(session, chosen.id, del_operator, run_in_progress)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()


# ===========================================================================
# Run (2026-10-14) - merged in from the former Run page, a separate section below the upload
# ===========================================================================
st.divider()
st.header("Run reconciliation")
run_panel.render(config, session)
