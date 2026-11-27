"""Upload — supply or replace a single report for a single month without
touching the numbered source folders directly (2026-09-30).

The numbered folders stay the PRIMARY way data arrives (CLAUDE.md section
4); this page is the validated, audited side door for a specific file. See
mop_reco/uploads.py for the full validate-then-store design. History
(2026-11-19): one row per upload with its own Delete button; Delete moves
the file to uploads_archive/ (never erases it) and keeps the history row.
"""

from __future__ import annotations

import html
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
import refund_panel
import run_panel
import store_master_panel
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


def _add_months(year: int, month: int, n: int) -> tuple:
    index = year * 12 + (month - 1) + n
    return index // 12, index % 12 + 1


def _available_months() -> list:
    """EVERY calendar month from the first loaded one to three months past the current month - contiguous, so a
    month between the last reconciled one and today (September, when only April-August are loaded) is never
    missing from the list. Computed from the data and the clock, never a hardcoded list (rule 8)."""
    loaded = sorted({d[:7] for d in loaded_dates(run.id)}) if run is not None else []
    today = date.today()
    first = (int(loaded[0][:4]), int(loaded[0][5:7])) if loaded else (today.year, today.month)
    last = _add_months(today.year, today.month, 3)
    last_loaded = (int(loaded[-1][:4]), int(loaded[-1][5:7])) if loaded else first
    last = max(last, _add_months(last_loaded[0], last_loaded[1], 1))
    months, (y, m) = [], first
    while (y, m) <= last:
        months.append(f"{y:04d}-{m:02d}")
        y, m = _add_months(y, m, 1)
    return months


def _next_to_reconcile(months: list) -> int:
    """The month right after the last loaded one - the one most likely being supplied."""
    loaded = sorted({d[:7] for d in loaded_dates(run.id)}) if run is not None else []
    if not loaded:
        return 0
    y, m = _add_months(int(loaded[-1][:4]), int(loaded[-1][5:7]), 1)
    target = f"{y:04d}-{m:02d}"
    return months.index(target) if target in months else 0


def _month_label(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{up.MONTH_LABELS[mo - 1]}-{y}"


# ===========================================================================
# The form
# ===========================================================================
st.header("Upload")
st.markdown("##### Supply a file")

# Once the masters live in the database (2026-11-21) nothing is uploaded for them: they are edited on the Masters page.
from mop_reco import masters_db
_uploadable = [s for s in up.SOURCE_REGISTRY
               if not (masters_db.in_database(session) and s.key in masters_db.UPLOAD_KEYS_REPLACED)]
if len(_uploadable) < len(up.SOURCE_REGISTRY):
    st.caption("The masters (store, MID, pickup code, pickup location) and the opening cash balance are held in the "
               "database and edited on the **Masters** page - they are not uploaded.")
REFUND_REPORT = "Customer refunds"      # not a run source: it is checked and recorded as refunds, not stored in a month folder
STORE_MASTER_REPORT = up.STORE_MASTER_REPORT_NAME      # saved as a new version of the store master (with its Cluster and Region), not stored in a folder
report_names = [s.display_name for s in _uploadable] + [REFUND_REPORT, STORE_MASTER_REPORT]
c1, c2 = st.columns(2)
report_name = c1.selectbox("Report name", report_names, key="up_report_name")
spec = None if report_name in (REFUND_REPORT, STORE_MASTER_REPORT) else next(s for s in _uploadable if s.display_name == report_name)

if spec is None:
    c2.selectbox("Month", ["not month-specific"], key="up_month_refund_disabled", disabled=True)
    if report_name == STORE_MASTER_REPORT:
        store_master_panel.render(session, key="smu")
    else:
        refund_panel.render(session, key="rfu")
else:
    months = _available_months()
    month_labels = [_month_label(m) for m in months]
    if spec.month_specific:
        month_label = c2.selectbox("Month", month_labels, index=_next_to_reconcile(months), key="up_month")
        month = months[month_labels.index(month_label)]
    else:
        c2.selectbox("Month", ["not month-specific"], key="up_month_disabled", disabled=True)
        month = None

    if spec.note:
        st.caption(spec.note)
    st.caption(f"Accepted file types: {', '.join(spec.extensions)}.")

    uploaded_files = st.file_uploader("Drop the file(s) here - several can be dropped at once (for example the "
                                      "parts of one Paytm export)", key=f"up_file_{report_name}",
                                      accept_multiple_files=True)

    if uploaded_files:
        # Streamlit re-runs this whole script on every click. A big file must not be re-saved and re-checked each time
        # (264 MB written and read again on every press), so each saved copy and its preview are remembered for the same
        # file + report + month, and dropped when any of them changes or the file is stored / removed from the box.
        cache = st.session_state.setdefault("up_check_cache", {})
        wanted_keys = set()
        checked = []          # (UploadedFile, temp_path, preview)
        for uploaded in uploaded_files:
            cache_key = repr((getattr(uploaded, "file_id", None) or uploaded.name, uploaded.size, report_name, month))
            wanted_keys.add(cache_key)
            entry = cache.get(cache_key)
            if entry and Path(entry["path"]).exists():
                temp_path, preview = Path(entry["path"]), entry["preview"]
            else:
                suffix = Path(uploaded.name).suffix
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(uploaded.getbuffer())
                    temp_path = Path(tmp.name)
                with st.spinner(f"Checking {uploaded.name} ({uploaded.size / 1048576:,.0f} MB) - a large file can take "
                                "a minute..."):
                    try:
                        preview = up.validate_upload(spec, temp_path, uploaded.name, month, config)
                    except Exception as exc:  # noqa: BLE001 - say what failed instead of crashing the page
                        preview = up.UploadPreview(ok=False)
                        preview.errors.append(f"Could not check '{uploaded.name}': {type(exc).__name__}: {exc}")
                cache[cache_key] = {"path": str(temp_path), "preview": preview}
            checked.append((uploaded, temp_path, preview))
        for key in [k for k in cache if k not in wanted_keys]:       # a file removed from the box: free its saved copy
            try:
                Path(cache[key]["path"]).unlink()
            except OSError:
                pass
            cache.pop(key, None)

        for uploaded, temp_path, preview in checked:
            with st.container(border=True):
                st.markdown(f"**{uploaded.name}** - {uploaded.size / 1048576:,.1f} MB")
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
                    st.success("Validation passed.")
                else:
                    st.error("Rejected - this file will not be stored. Fix it (or the month selected) and drop it again.")

        ready = [(u, t, p) for u, t, p in checked if p.ok]
        if ready:
            label = "Upload" if len(ready) == 1 else f"Upload {len(ready)} files"
            if len(ready) < len(checked):
                st.warning(f"{len(checked) - len(ready)} of {len(checked)} file(s) failed the check and will be left out.")
            st.caption("Nothing is stored until you press the button.")
            operator = confirm_with_operator(
                st.button(label, type="primary", key="up_confirm"),
                "uploading these files" if len(ready) > 1 else "uploading this file", key="up_op")
            if operator:
                for uploaded, temp_path, preview in ready:
                    row = up.store_file(spec, temp_path, uploaded.name, month, operator,
                                        session, config, preview.row_count, preview.date_range)
                    msg = f"Stored '{uploaded.name}' as '{Path(row.stored_path).name}'."
                    if getattr(row, "_renamed", False):
                        msg += (" A file with that name already existed, so this one was saved with a timestamp "
                                "suffix - the original was never overwritten.")
                    st.success(msg)
                    if getattr(row, "_config_gap_note", None):
                        st.warning(row._config_gap_note)
                st.session_state.pop("up_check_cache", None)        # the saved copies were moved into place
                st.session_state.pop(f"up_file_{report_name}", None)
                st.rerun()
        else:
            st.error("None of the files passed the check - nothing can be stored.")

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
    used_by = {r.id: up.used_by_run(session, r.stored_path) for r in records}
    rows = []
    for r in records:
        used_run = used_by[r.id]
        rows.append({
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
            "Note": r.note or "",
            "Used by run #": f"#{used_run}" if used_run else "not yet used",
            "Deleted at": r.deleted_at.strftime("%d/%m/%Y %H:%M") if r.deleted_at else "",
            "Deleted by": r.deleted_by or "",
            "Archived at": r.archived_path or "",
        })
    history_df = pd.DataFrame(rows)
    frame_export_button(
        history_df,
        ExportMeta(title="Upload history"),
        key="dl_upload_history", noun="upload history rows",
    )

    if run_in_progress:
        st.info("A run is in progress - Delete is disabled until it finishes.")

    # One line per upload with its own Delete button (a stored file only). Newest first; the older ones sit behind
    # "Show all" so a long history does not push the Run section far down the page.
    PAGE_ROWS = 20
    show_all = (st.toggle(f"Show all {len(records)} uploads", value=False, key="up_show_all")
                if len(records) > PAGE_ROWS else True)
    visible = records if show_all else records[:PAGE_ROWS]

    widths = [3.0, 2.0, 0.8, 1.9, 1.4, 1.3, 1.5]
    head = st.columns(widths)
    for col, label in zip(head, ["File", "Report / month", "Rows", "Date range in file", "Uploaded", "Status",
                                  "Action"]):
        col.markdown(f"<small style='color:#6b7a8c;font-weight:600;text-transform:uppercase'>{label}</small>",
                     unsafe_allow_html=True)

    for r in visible:
        used_run = used_by[r.id]
        cols = st.columns(widths)
        month_txt = _month_label(r.month) if r.month else "not month-specific"
        # A file name is chosen by whoever uploads it and is shown to everyone: escape it (audit S-3; an <img> in a name rendered).
        cols[0].markdown(f"{html.escape(str(r.original_file_name))}<br><small>by {html.escape(str(r.uploaded_by))}</small>", unsafe_allow_html=True)
        cols[1].markdown(f"{html.escape(str(r.report_name))}<br><small>{html.escape(str(month_txt))}</small>", unsafe_allow_html=True)
        cols[2].write(fmt_count(r.row_count) if r.row_count is not None else "—")
        cols[3].write(f"{r.date_range_start} to {r.date_range_end}" if r.date_range_start else "—")
        cols[4].write(r.uploaded_at.strftime("%d/%m/%Y %H:%M"))
        if r.source_key in up.NO_FILE_SOURCES:
            # A Customer refunds workbook (bank details never stored) or a store master upload (saved as a master version): no file is kept, nothing to delete.
            cols[5].write(r.status)
            cols[6].caption("no file kept")
            cols[0].caption(html.escape(str(r.note or "")))
        elif r.status == "stored":
            cols[5].write(f"in use by run #{used_run}" if used_run else "stored")
            if cols[6].button("Delete", key=f"up_del_{r.id}", disabled=run_in_progress, width="stretch"):
                st.session_state["up_delete_pending"] = r.id
        else:
            cols[5].write("deleted")
            cols[6].caption(f"{r.deleted_by or ''} {r.deleted_at.strftime('%d/%m/%Y') if r.deleted_at else ''}")

    pending_id = st.session_state.get("up_delete_pending")
    chosen = next((r for r in records if r.id == pending_id and r.status == "stored"), None)
    if pending_id is not None and chosen is None:
        st.session_state.pop("up_delete_pending", None)
    if chosen is not None:
        used_run = used_by[chosen.id]
        with st.container(border=True):
            st.markdown(f"**Delete '{chosen.original_file_name}'?**")
            st.caption("The file is moved out of its source folder into the uploads_archive folder, so the next "
                       "run will not read it. It is not erased, and this history row stays, marked deleted.")
            if used_run:
                st.warning(f"Run #{used_run} used this file. Deleting it does not change run #{used_run}'s "
                           "figures; a future run will not see it, so the month's figures may change.")
            c_yes, c_no = st.columns([1, 1])
            confirmed = c_yes.button("Yes, delete", key="up_delete_yes", disabled=run_in_progress)
            if c_no.button("Cancel", key="up_delete_cancel"):
                st.session_state.pop("up_delete_pending", None)
                st.rerun()
            del_operator = confirm_with_operator(confirmed, f"deleting '{chosen.original_file_name}'",
                                                 key="up_delete_op")
            if del_operator:
                ok, msg = up.delete_upload(session, chosen.id, del_operator, _run_in_progress(), config)
                st.session_state.pop("up_delete_pending", None)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()


# ===========================================================================
# Run (2026-10-14) - merged in from the former Run page, a separate section below the upload
# ===========================================================================
st.divider()
st.header("Run reconciliation")
run_panel.render(config, session)
