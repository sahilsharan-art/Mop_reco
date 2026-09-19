"""Master data — record counts, version history, and health problems."""

from __future__ import annotations

import sys
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import db_session, latest_run, page, sidebar
from mop_reco.config import load_config
from mop_reco.masters import (
    MASTER_SPECS,
    build_unmapped_report,
    check_mid_integrity,
    derive_provisional_store_master,
    due_date,
    get_rows_asof,
    load_all_masters,
    version_history,
)

page("Master data",
     "The lookup tables that turn a name, terminal or pickup code into a store, "
     "and whether any of them have gaps.")

config = load_config()
# ONE session for this whole page run, from the shared thread-safe engine
# (dashboard_common.db_session() — never dashboard_common's own db_session()
# called again for the sidebar with a second, separate session, and never a
# second engine built here via init_db()/get_session()).
session = db_session()
sidebar(session, latest_run(session))

# --- Load every master (versions on load; a no-op if content is unchanged) --
load_results = load_all_masters(config, session)

st.header("Masters")
rows = []
for master_name, (version, created) in load_results.items():
    rows.append(
        {
            "master": master_name,
            "current_row_count": version.row_count,
            "loaded_at (this version)": version.loaded_at,
            "new version created this run": created,
            "source_file": version.source_file_path,
        }
    )
st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

st.header("Version history")
for master_name in MASTER_SPECS:
    with st.expander(f"{master_name} — {len(version_history(session, master_name))} version(s)"):
        hist_rows = [
            {
                "id": v.id,
                "loaded_at": v.loaded_at,
                "effective_to": v.effective_to or "(current)",
                "row_count": v.row_count,
                "is_current": v.is_current,
                "content_hash": v.content_hash[:12] + "...",
            }
            for v in version_history(session, master_name)
        ]
        st.dataframe(pd.DataFrame(hist_rows), width='stretch', hide_index=True)

# --- Health: MID integrity check -------------------------------------------
st.header("Health checks")
business_date = st.date_input("Check as of business date", value=date(2026, 7, 20),
                              format="DD/MM/YYYY")

violations = check_mid_integrity(session, business_date)
if violations:
    st.error(f"{len(violations)} MID-integrity violation(s) as of {business_date}:")
    st.dataframe(
        pd.DataFrame(
            [{"master": v.master_name, "mid": v.mid, "stores": ", ".join(v.stores)} for v in violations]
        ),
        width='stretch',
        hide_index=True,
    )
else:
    st.success(f"No MID maps to more than one store as of {business_date}.")

# --- Health: unmapped identifiers, cross-checked against real gateway data --
st.subheader("Unmapped identifiers (cross-checked against real files)")


# Reading these real files (especially the three Paytm zip parts, ~17k rows)
# is the expensive part of this page. Streamlit reruns the whole script on
# every widget interaction (e.g. changing the date below), so without caching
# this would re-read and re-parse every file on every single rerun. Cached on
# the resolved file path + its mtime, not on `config` itself (which holds
# dicts and isn't hashable) — a re-dump still busts the cache correctly.

@st.cache_data(show_spinner="Reading Axis EDC file...")
def _axis_mid_records(file_path_str: str, _mtime: float):
    import openpyxl

    wb = openpyxl.load_workbook(file_path_str, read_only=True, data_only=True)
    # Never hardcode a sheet name — the daily Axis file uses 'Sheet1', the
    # monthly one uses 'Axis'.
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    header = list(next(it))
    mid_idx = header.index("MID")
    amt_idx = header.index("NET_AMT")
    return [(row[mid_idx], row[amt_idx]) for row in it]


@st.cache_data(show_spinner="Reading Paytm settlement export (3 parts)...")
def _paytm_mid_records(file_paths_and_mtimes: tuple):
    import csv

    records = []
    for file_path_str, _mtime in file_paths_and_mtimes:
        with zipfile.ZipFile(file_path_str) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            for name in csv_names:
                with zf.open(name) as fh:
                    reader = csv.DictReader(line.decode("utf-8", errors="replace") for line in fh)
                    for row in reader:
                        mid = row["original_mid"].strip("'")
                        amt = row["amount"].strip("'")
                        records.append((mid, amt))
    return records


axis_status = config.source_status("axis_edc")
axis_records = []
if axis_status.files:
    axis_file = max(axis_status.files, key=lambda p: p.stat().st_mtime)
    axis_records = _axis_mid_records(str(axis_file), axis_file.stat().st_mtime)

paytm_status = config.source_status("paytm_settlement")
paytm_zip_info = tuple(
    (str(p), p.stat().st_mtime) for p in paytm_status.files if p.suffix.lower() == ".zip"
)
paytm_records = _paytm_mid_records(paytm_zip_info)

axis_unmapped = build_unmapped_report(session, "axis_mid", axis_records, business_date)
paytm_unmapped = build_unmapped_report(session, "paytm_mid", paytm_records, business_date)

col1, col2 = st.columns(2)
with col1:
    st.caption(f"Axis MIDs — {len(axis_unmapped)} unmapped")
    if axis_unmapped:
        st.dataframe(pd.DataFrame(axis_unmapped), width='stretch', hide_index=True)
with col2:
    st.caption(f"Paytm MIDs — {len(paytm_unmapped)} unmapped")
    if paytm_unmapped:
        st.dataframe(pd.DataFrame(paytm_unmapped), width='stretch', hide_index=True)

# --- No store master: provisional list --------------------------------------
st.header("Store master")
st.error("No store master exists — confirmed absent, not just unsupplied (see CLAUDE.md section 5).")
provisional, note = derive_provisional_store_master(config)
st.warning(note)
st.metric("Stores derived from POS", len(provisional))
with st.expander("Provisional store list"):
    st.dataframe(
        pd.DataFrame([{"store": p.store, "cms_cycle_days": p.cms_cycle_days, "cms_cycle_source": p.cms_cycle_source} for p in provisional]),
        width='stretch',
        hide_index=True,
    )

# --- Working-day helper demo --------------------------------------------------
st.header("Working-day due date")
sample = due_date(business_date, 1)
st.info(f"T+1 due date for {business_date}: **{sample.date}** — basis: *{sample.basis}*")

# --- Admin: settings, not information ---------------------------------------
# These are configuration values, so they live behind a fold on the masters
# page rather than greeting an operator who opens the app.
st.divider()
st.header("Admin — configuration in force")
with st.expander("Settings this run obeys"):
    a1, a2, a3 = st.columns(3)
    a1.metric("Tolerance", f"{config.tolerance:g}")
    a1.caption("Nil tolerance: exact match only (non-negotiable rule 1).")
    a2.metric("MOP classes configured", len(config.mop_classification))
    a2.caption("An unknown MOP stops that store-day and is reported.")
    a3.metric("Sources configured", len(config.all_source_statuses()))
    a3.caption("Data availability per source is on the **Run** page.")

    st.dataframe(
        pd.DataFrame(sorted(config.mop_classification.items()), columns=["MOP", "Class"]),
        hide_index=True, width="stretch",
    )
    st.caption(
        "Held in config.yaml, not in code. Changing a class here changes which "
        "reconciliation a MOP lands in — it does not change past runs, which keep "
        "the classification in force when they ran."
    )

session.close()
