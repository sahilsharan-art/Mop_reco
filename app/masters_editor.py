"""Edit the masters on screen (2026-11-21).

Once the masters are in the database (`masters_db`), THIS is the only way they change - no workbook is read or
written. Every save is a new version that keeps the old one: who, why, when, and exactly what changed. A save that
breaks a rule is refused with the row named; a problem the data already had is shown as a warning, never hidden.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard_common import confirm_with_operator
from mop_reco import masters as masters_mod
from mop_reco import masters_db as mdb
from mop_reco.formatting import display_date

LABELS = {
    "store": "Store master (store name -> store code)",
    "mid_axis": "Axis MID -> store",
    "mid_paytm": "Paytm MID -> store",
    "pickup_code": "CMS pickup code -> store",
    "pickup_location": "Pickup location (shared pickup code + location -> store)",
    "opening_balance": "Opening cash balance (cost centre -> opening)",
}
HELP = {
    "store": "store_name is the full name Tally carries; store is the short code POS and every gateway carry.",
    "pickup_location": "Only for a pickup code that two stores share (e.g. AX31918: ARRAH -> APM, MADHB -> MMR).",
    "opening_balance": "Cash in the drawer as at the opening date. A store missing here starts at zero and stays visible.",
}


def _frame(name: str, rows: list) -> pd.DataFrame:
    cols = list(mdb.MASTER_FIELDS[name])
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)
    return df


def _rows(df: pd.DataFrame, name: str) -> list:
    out = []
    for rec in df.to_dict("records"):
        row = {}
        for f in mdb.MASTER_FIELDS[name]:
            v = rec.get(f)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                v = None
            if f == "opening" and v is not None:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    pass
            row[f] = v
        if all(row[f] in (None, "") for f in row):          # a blank line the grid added - not a row
            continue
        out.append(row)
    return out


def render(session, key: str = "mastered") -> None:
    st.header("Edit masters")
    if not mdb.in_database(session):
        st.warning("The masters are still read from the Excel workbooks on this machine. They become editable here "
                   "after the one-time import into the database (`scripts/import_masters_snapshot.py`).")
        return
    st.caption("Changes are made here only - no workbook is read or written. Every save keeps the old version, with "
               "who changed it, why and when. Past business dates keep resolving against the version that was in "
               "force then.")

    name = st.selectbox("Master", list(mdb.MASTER_FIELDS), format_func=lambda n: LABELS[n], key=f"{key}_name")
    if HELP.get(name):
        st.caption(HELP[name])
    version = mdb.current_version(session, name)
    current = mdb.current_rows(session, name)
    if version is None:
        st.error("No version of this master is stored - run the import first.")
        return
    meta = mdb.meta_of(session, name)
    st.caption(f"Current: version #{version.id}, {len(current):,} rows, saved {display_date(version.loaded_at.date())} "
               f"{version.loaded_at:%H:%M} by {version.created_by or 'system'}"
               + (f" - as at {meta['as_at']}" if meta.get("as_at") else ""))

    editor_key = f"{key}_grid_{name}_{version.id}"          # a new version = a fresh grid, never stale edits
    cfg = {}
    if name == "opening_balance":
        cfg["opening"] = st.column_config.NumberColumn("opening", format="%.2f", required=True)
    edited = st.data_editor(_frame(name, current), num_rows="dynamic", hide_index=True, width="stretch",
                            key=editor_key, column_config=cfg, height=420)
    new_rows = _rows(edited, name)
    change = mdb.diff(current, new_rows, name)
    changed = bool(change["added"] or change["removed"] or change["changed"])

    stores = {r["store"] for r in mdb.current_rows(session, "store")} if name != "store" else None
    result = mdb.validate(name, new_rows, current, stores)

    if not changed:
        st.caption("No changes yet. Edit a cell, add a row at the bottom, or select rows and delete them.")
    else:
        st.markdown(f"**{change['summary']}.**")
        with st.expander("What will change", expanded=len(change["added"]) + len(change["removed"]) + len(change["changed"]) <= 15):
            if change["added"]:
                st.caption("Added")
                st.dataframe(pd.DataFrame(change["added"]), hide_index=True, width="stretch")
            if change["removed"]:
                st.caption("Removed")
                st.dataframe(pd.DataFrame(change["removed"]), hide_index=True, width="stretch")
            if change["changed"]:
                st.caption("Changed")
                st.dataframe(pd.DataFrame([{"key": c["key"], **{f"was {k}": v for k, v in c["old"].items()},
                                            **{f"now {k}": v for k, v in c["new"].items()}} for c in change["changed"]]),
                             hide_index=True, width="stretch")
        for e in result.errors:
            st.error(e)
        for w in result.warnings[:15]:
            st.warning(w)
        if len(result.warnings) > 15:
            st.caption(f"... and {len(result.warnings) - 15} more warning(s).")

        reason = st.text_input("Why is this being changed? (required)", key=f"{key}_reason_{name}_{version.id}")
        clicked = st.button("Save as a new version", type="primary", key=f"{key}_save_{name}",
                            disabled=not result.ok or not reason.strip())
        who = confirm_with_operator(clicked, "saving this master", key=f"{key}_op_{name}")
        if who:
            try:
                _v, created, _d = mdb.save_version(session, name, new_rows, who, reason.strip())
            except mdb.MasterEditError as exc:
                session.rollback()
                for e in exc.args[0] if exc.args and isinstance(exc.args[0], list) else [str(exc)]:
                    st.error(str(e))
            else:
                masters_mod.clear_caches()
                st.session_state.pop(f"_pending_{key}_op_{name}", None)
                st.success("Saved as a new version." if created else "Nothing changed - no new version was made.")
                st.rerun()

    _history(session, name, version, key)


def _history(session, name: str, version, key: str) -> None:
    st.subheader("History")
    hist = mdb.history(session, name)
    st.dataframe(pd.DataFrame([{
        "version": h["id"], "saved": f"{display_date(h['loaded_at'].date())} {h['loaded_at']:%H:%M}",
        "by": h["created_by"], "why": h["change_note"], "kind": h["source_kind"], "rows": h["rows"],
        "in force": "current" if h["current"] else ""} for h in hist]), hide_index=True, width="stretch")
    older = [h["id"] for h in hist if not h["current"]]
    if not older:
        return
    with st.expander("Compare with, or restore, an older version"):
        vid = st.selectbox("Version", older, key=f"{key}_hist_{name}")
        d = mdb.diff(mdb.version_rows(session, vid), mdb.current_rows(session, name), name)
        st.caption(f"Going from version #{vid} to the current one: {d['summary']}.")
        reason = st.text_input("Why restore it? (required)", key=f"{key}_rreason_{name}")
        clicked = st.button(f"Make version #{vid} current again", key=f"{key}_restore_{name}", disabled=not reason.strip())
        who = confirm_with_operator(clicked, "restoring this version", key=f"{key}_rop_{name}")
        if who:
            try:
                mdb.restore_version(session, name, vid, who, reason.strip())
            except mdb.MasterEditError as exc:
                session.rollback()
                for e in exc.args[0] if exc.args and isinstance(exc.args[0], list) else [str(exc)]:
                    st.error(str(e))
            else:
                masters_mod.clear_caches()
                st.session_state.pop(f"_pending_{key}_rop_{name}", None)
                st.success("Restored as a new version - the history keeps both.")
                st.rerun()
