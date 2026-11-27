"""Store master with Cluster and Region: upload, check, save as a new version (2026-11-27).

Used by the Upload page ("Store master (Cluster and Region)"). The workbook is read by column name, previewed in full - stores, regions,
clusters, what would be added / removed / moved - and saved only after a confirmation, as a NEW version of the store master (the
previous version is kept and can be restored on the Masters page). A store of the current master that the file leaves out is named
and needs an explicit tick: it would stop resolving for later dates.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard_common import confirm_with_operator
from mop_reco import masters_db
from mop_reco import store_master_file as smf
from mop_reco import uploads as up
from mop_reco.formatting import count as fmt_count


def render(session, key: str = "smu") -> None:
    done = st.session_state.pop(f"_{key}_done", None)
    if done:
        st.success(done)
    if not masters_db.in_database(session):
        st.info("The masters are read from workbooks on this machine, so an uploaded store master would not be used. "
                "This upload works where the masters are held in the database (production).")
        return
    st.caption("Upload the store master with Cluster and Region (.xlsx or .csv). Needs Store Code, Cluster and Region; a site-name column "
               "is optional - without it the file is applied onto the stored store master by Store Code. Columns are found by name; "
               "extra columns are ignored. It is saved as a NEW version of the store master - the current one is kept and can be "
               "restored on the Masters page.")
    uploaded = st.file_uploader("Store master workbook (.xlsx / .csv)", type=["xlsx", "csv"], key=f"{key}_upload")
    if uploaded is None:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / Path(uploaded.name).name
        path.write_bytes(uploaded.getvalue())
        parsed = smf.parse(path, uploaded.name)
    for e in parsed.errors[:25]:
        st.error(e)
    if len(parsed.errors) > 25:
        st.error(f"... and {len(parsed.errors) - 25} more problems.")
    if not parsed.ok:
        st.error("Rejected - nothing was saved. Fix the file and upload it again.")
        return
    for w in parsed.warnings[:10]:
        st.warning(w)

    current = masters_db.current_rows(session, "store")
    save_rows, merge = parsed.rows, None
    if parsed.mode == "merge":
        # no site names in the file: it is applied onto the STORED store master by Store Code (each stored row keeps its site name)
        merge = smf.merge_into_current(parsed.rows, current)
        save_rows = merge["merged"]
        st.info("This file is read as the list of your ACTIVE stores. It has no site-name column, so it is applied onto the stored store "
                f"master by Store Code: each store keeps its site name and takes the Cluster and Region from your file "
                f"({fmt_count(merge['applied'])} stores matched). A store of the master that is not in the file is marked Closed. "
                "No store is added or removed.")
        if merge["not_in_master"]:
            st.warning(f"{len(merge['not_in_master'])} store code(s) in your file are NOT in the store master, so their cluster and "
                       "region cannot be stored (no site name is known for them): " + ", ".join(merge["not_in_master"][:40])
                       + (" ..." if len(merge["not_in_master"]) > 40 else "")
                       + ". Add them on the Masters page (store master) first, then upload again.")
        if merge["closed"]:
            st.warning(f"{len(merge['closed'])} store(s) of the store master are not in your file, so they will be marked CLOSED: "
                       + ", ".join(merge["closed"][:40]) + (" ..." if len(merge["closed"]) > 40 else "")
                       + ". When one opens again, upload the updated store master that lists it.")
    info = smf.summary(save_rows)
    diff = smf.compare(save_rows, current)
    already = (masters_db._fingerprint([masters_db._clean_row("store", r) for r in save_rows])
               == masters_db._fingerprint([masters_db._clean_row("store", r) for r in current]))
    if already:
        st.success("This file is ALREADY saved as the store master - there is nothing to save. The summary below is what the store "
                   "master holds now.")
    else:
        st.warning("**Preview only - nothing is saved yet.** Check the numbers below, then press **Save as the new store master** "
                   "at the bottom of this section.")
    st.markdown(f"**{fmt_count(info['stores'])} stores · {len(info['regions'])} regions · {fmt_count(info['clusters'])} clusters** "
                f"(header on row {parsed.header_row} of '{uploaded.name}')")
    if info["regions"]:
        st.caption("Regions: " + ", ".join(info["regions"]))
    st.dataframe(pd.DataFrame(info["per_region"]).rename(columns={"region": "Region", "clusters": "Clusters", "stores": "Stores"}),
                 hide_index=True, width="stretch")
    with st.expander(f"Every cluster ({len(info['per_cluster'])})"):
        st.dataframe(pd.DataFrame(info["per_cluster"]).rename(columns={"region": "Region", "cluster": "Cluster", "stores": "Stores"}),
                     hide_index=True, width="stretch", height=300)
    if info.get("closed"):
        st.caption(f"{len(info['closed'])} closed store(s) are kept in the store master and shown under 'Closed' in the Region filter.")
    if info["no_region"] or info["no_cluster"]:
        st.warning(f"{len(info['no_region'])} store(s) have no region and {len(info['no_cluster'])} have no cluster. They are kept and "
                   "shown as Unassigned - never guessed into a region.")
    if info["clusters_in_several_regions"]:
        st.warning("A cluster name sits under more than one region: "
                   + "; ".join(f"{c} ({', '.join(r)})" for c, r in list(info["clusters_in_several_regions"].items())[:8])
                   + ". Choose the region first in the sidebar to tell them apart.")

    st.markdown(f"**Compared with the current store master:** {len(diff['added'])} store(s) new, {len(diff['removed'])} left out, "
                f"{len(diff['cluster_region_changed'])} with a changed cluster / region, {len(diff['renamed'])} renamed, "
                f"{diff['unchanged']} unchanged.")
    if diff["added"]:
        st.caption("New: " + ", ".join(diff["added"][:40]) + (" ..." if len(diff["added"]) > 40 else ""))
    if diff["renamed"]:
        with st.expander(f"Renamed sites ({len(diff['renamed'])})"):
            st.dataframe(pd.DataFrame(diff["renamed"]), hide_index=True, width="stretch")
    confirm_removed = True
    if diff["removed"]:
        st.error(f"{len(diff['removed'])} store(s) of the current master are NOT in this file and would be removed: "
                 + ", ".join(diff["removed"][:40]) + (" ..." if len(diff["removed"]) > 40 else ""))
        confirm_removed = st.checkbox(f"I understand these {len(diff['removed'])} store(s) will no longer be in the store master "
                                      "(the old version is kept and can be restored).", key=f"{key}_ok_removed")
    candidate = masters_db.validate("store", save_rows, current)
    for w in candidate.warnings[:6]:
        st.caption(f"Note: {w}")
    if not candidate.ok:
        st.error("The store master rules refuse this file: " + "; ".join(candidate.errors[:6]))
        return

    if already:
        return
    save = st.button("Save as the new store master", type="primary", key=f"{key}_save", disabled=not confirm_removed)
    operator = confirm_with_operator(save, "saving the store master", key=f"{key}_op")
    if operator:
        note = f"uploaded {uploaded.name}: cluster and region ({info['stores']} stores, {len(info['regions'])} regions)"
        try:
            version, created, _ = masters_db.save_version(
                session, "store", save_rows, operator, note, source_kind="upload",
                meta={"source_file": uploaded.name, "sha256": parsed.sha256, "uploaded_by": operator})
        except masters_db.MasterEditError as exc:
            session.rollback()
            st.error(f"Nothing was saved: {exc}")
            return
        up.note_store_master_upload(session, uploaded.name, uploaded.size, info["stores"], operator,
                                    f"store master version #{version.id} saved by {operator}: {info['stores']} stores, "
                                    f"{len(info['regions'])} regions, {info['clusters']} clusters" if created
                                    else f"no change - the file matches the current store master (checked by {operator})")
        st.session_state[f"_{key}_done"] = (f"Store master saved as version #{version.id}: {info['stores']} stores in "
                                            f"{len(info['regions'])} regions." if created
                                            else "No change: this file matches the store master already stored.")
        st.cache_data.clear()
        st.rerun()
