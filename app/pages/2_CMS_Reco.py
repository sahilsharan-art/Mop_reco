"""CMS Reco - one block per DEPOSIT date, chained per store (2026-10-16).

Opening + Cash/CMS Deposit - CMS Report (credited same day) - CMS Report (credited next day) = Closing, and the
Closing is the next block's Opening. See mop_reco/cms_blocks.py for the rules, why a credit dated day+2 or later
is listed separately, and how a hand override sits on top of the bank's figure without ever replacing it.

Reads only rows written at run time (cms_slips, cms_ptc_deposits, cms_overrides) - never a raw file (6.14).
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import (confirm_with_operator, db_session, latest_run, no_run_yet, page, sidebar,
                              stop_if_no_dates, tile)
from mop_reco import cms_blocks as cb
from mop_reco import cms_blocks_export as cbx
from mop_reco.config import load_config
from mop_reco.formatting import display_date, money_short, money_table

page("CMS Reco")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)
cfg = load_config()

WARN_FILL = "background-color: #FDE9C8"


@st.cache_data(ttl=600, show_spinner="Building the CMS blocks...")
def _load(run_id: int):
    s = db_session()
    slips, deposits = cb.load(s, run_id)
    first, last = cb.run_range(s, run_id)
    openings = cb.opening_from_config(cfg.raw)
    if slips.empty and deposits.empty:
        return slips, deposits, None, first, last
    return slips, deposits, cb.build_model(slips, deposits, first, last, openings), first, last


slips, deposits, base_model, first, last = _load(run.id)
if base_model is None:
    st.warning("No CMS data has been stored for this run. Run `scripts/backfill_cms_slips.py` or re-run the reconciliation.")
    st.stop()

ovr = cb.active_overrides(session)            # read live: an override shows on the very next render, no re-run
model = cb.apply_overrides(base_model, ovr)

d0 = max(str(date_from), first)
d1 = min(str(date_to), last)
if d0 > d1:
    st.warning("The selected dates are outside this run.")
    st.stop()

# --- period -----------------------------------------------------------------------------------------
month_keys = sorted({d[:7] for d in model.dates if d0 <= d <= d1})
labels = {"all": f"Whole range ({display_date(d0)} - {display_date(d1)})"}
for mk in month_keys:
    labels[mk] = date.fromisoformat(mk + "-01").strftime("%B %Y")
choice = st.selectbox("Period", ["all"] + month_keys, index=len(month_keys) if month_keys else 0,
                      format_func=lambda k: labels[k], key=f"cms_period_{d0}_{d1}")


def _bounds(key: str):
    if key == "all":
        return d0, d1
    ds = [d for d in model.dates if d.startswith(key) and d0 <= d <= d1]
    return ds[0], ds[-1]


period0, period1 = _bounds(choice)

# --- the override control: before any total is read -------------------------------------------------
n_over, net_over, gross_over = cb.override_headline(ovr)
if n_over:
    st.markdown(
        f"<div style='border-left:4px solid #E8962E;padding:8px 12px;background:#FDF3E3;border-radius:6px'>"
        f"<b>{n_over:,} cell(s) overridden, Rs {money_table(net_over)} difference from the bank's figures</b> "
        f"(Rs {money_table(gross_over)} gross). Every total below uses the override.</div>", unsafe_allow_html=True)
else:
    st.markdown(f"**0 cells overridden, Rs {money_table(0)} difference from the bank's figures.**")
with st.expander("Overrides", expanded=bool(n_over)):
    if n_over:
        reg = ovr.copy()
        reg["note"] = [cb.override_note(r) for r in reg.itertuples(index=False)]
        show = reg.assign(dep_date=reg["dep_date"].map(display_date), col=reg["col"].map(cb.COL_LABEL),
                          entered_at=reg["entered_at"].map(lambda v: v.strftime("%d/%m/%Y %H:%M")))
        st.dataframe(show[["store", "dep_date", "col", "computed_amount", "override_amount", "difference", "reason",
                           "entered_by", "entered_at", "note"]].rename(columns={
                               "store": "Store", "dep_date": "Deposit date", "col": "Column",
                               "computed_amount": "Bank (computed)", "override_amount": "Override",
                               "difference": "Difference", "reason": "Reason", "entered_by": "By",
                               "entered_at": "At", "note": "Hover note"}),
                     hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="%.2f")
                                    for c in ("Bank (computed)", "Override", "Difference")})
        full = cb.all_overrides(session)
        st.download_button("Export every override, active and reverted (CSV)",
                           full.to_csv(index=False, quoting=1).encode("utf-8"), "cms_overrides.csv", "text/csv",
                           key="cms_ovr_csv")
        by_id = reg.set_index("id")
        pick = st.selectbox("Revert an override", [None] + list(reg["id"]), key="cms_revert_pick",
                            format_func=lambda i: "" if i is None else "{} | {} | {} | override {:,.2f}".format(
                                by_id.at[i, "store"], display_date(by_id.at[i, "dep_date"]),
                                cb.COL_LABEL[by_id.at[i, "col"]], by_id.at[i, "override_amount"]))
        if st.button("Revert to the bank's figure", disabled=pick is None, key="cms_revert_btn"):
            who = confirm_with_operator(True, "revert this override", "cms_revert")
            if who:
                cb.revert_override(session, int(pick), who)
                st.rerun()
    else:
        st.caption("No cell is overridden. Every CMS Report figure is the bank's own.")

# --- controls ---------------------------------------------------------------------------------------
controls = cb.block_controls(model, period0, period1)
tie_i = cb.deposit_tie(session, run.id, deposits, period0, period1)
failed = [c for c in controls + [tie_i] if not c.ok]
if failed:
    st.error(f"**{len(failed)} control(s) do not hold.**")
    for c in failed:
        st.markdown(f"- **{c.label}** - {c.detail} (difference {money_table(c.amount)})")

# --- filters over the monthly summary ---------------------------------------------------------------
with st.container(key="cms-filters"):
    f1, f2, f3, f4, f5 = st.columns([2.2, 1.2, 1.2, 1.3, 1.5])
    store_pick = f1.multiselect("Store code", model.stores, default=[], placeholder="All stores", key="cms_f_store")
    dep_from = f2.date_input("Deposit date from", value=date.fromisoformat(period0),
                             min_value=date.fromisoformat(period0), max_value=date.fromisoformat(period1),
                             format="DD/MM/YYYY", key=f"cms_f_from_{period0}_{period1}")
    dep_to = f3.date_input("Deposit date to", value=date.fromisoformat(period1),
                           min_value=date.fromisoformat(period0), max_value=date.fromisoformat(period1),
                           format="DD/MM/YYYY", key=f"cms_f_to_{period0}_{period1}")
    has_remark = f4.selectbox("Has remark", ["All", "With remark", "Without remark"], key="cms_f_remark")
    has_deposit = f5.selectbox("Has deposit", ["All", "Deposit in period", "No deposit in period"], key="cms_f_dep")
p0, p1 = dep_from.isoformat(), dep_to.isoformat()
if p0 > p1:
    st.warning("Deposit date 'from' is after 'to'.")
    st.stop()

sm_all = cb.summary_frame(model, p0, p1)
saved = cb.remarks_for(session, p0, p1)
sm_all["remark"] = [saved.get(s, ("", "", None))[0] for s in sm_all["store"]]
sm = sm_all
if store:
    sm = sm[sm["store"] == store]
if store_pick:
    sm = sm[sm["store"].isin(store_pick)]
if has_remark != "All":
    sm = sm[sm["remark"].astype(bool) == (has_remark == "With remark")]
if has_deposit != "All":
    sm = sm[(sm["deposit"].abs() > cb.EPS) == (has_deposit == "Deposit in period")]
sm = sm.reset_index(drop=True)
tot = sm[["opening", "deposit", "cms", "diff"]].sum()

# --- tiles ------------------------------------------------------------------------------------------
with st.container(key="cms-blocks-ribbon"):
    c1, c2, c3, c4 = st.columns(4)
    tile(c1, "OPENING", money_short(tot["opening"]), tooltip=f"Rs {money_table(tot['opening'])}")
    tile(c2, "CASH/CMS DEPOSIT", money_short(tot["deposit"]), tooltip=f"Rs {money_table(tot['deposit'])}")
    tile(c3, "CMS REPORT", money_short(tot["cms"]), tooltip=f"Rs {money_table(tot['cms'])}")
    tile(c4, "DIFF (STILL OUTSTANDING)", money_short(tot["diff"]),
         accent="warn" if abs(tot["diff"]) > cb.EPS else None, tooltip=f"Rs {money_table(tot['diff'])}")

# --- monthly summary + remarks ----------------------------------------------------------------------
st.markdown("##### Summary")
NAMES = {"store": "STORE NAME", "opening": "Opening", "deposit": "Cash/CMS Deposit", "cms": "CMS Report",
         "diff": "Diff", "remark": "Remarks"}
NUM_CFG = {c: st.column_config.NumberColumn(format="%.2f") for c in ("Opening", "Cash/CMS Deposit", "CMS Report", "Diff")}
event = st.dataframe(sm[list(NAMES)].rename(columns=NAMES), hide_index=True, width="stretch",
                     on_select="rerun", selection_mode="single-row", key=f"cms_sum_{run.id}_{p0}_{p1}",
                     column_config={"STORE NAME": st.column_config.TextColumn(pinned=True), **NUM_CFG,
                                    "Remarks": st.column_config.TextColumn(width="large")})
st.dataframe(pd.DataFrame([{"STORE NAME": "TOTAL", "Opening": tot["opening"], "Cash/CMS Deposit": tot["deposit"],
                            "CMS Report": tot["cms"], "Diff": tot["diff"]}]),
             hide_index=True, width="stretch", column_config=NUM_CFG)
picked = event.selection.rows if event and event.selection else []
if picked:
    row_store = sm.loc[picked[0], "store"]
    st.markdown(f"**Remark - {row_store}** ({display_date(p0)} - {display_date(p1)})")
    text = st.text_area("Remark", value=saved.get(row_store, ("",))[0], key=f"cms_rem_{row_store}_{p0}_{p1}",
                        label_visibility="collapsed")
    if st.button("Save remark", disabled=not text.strip(), key="cms_save_remark"):
        who = confirm_with_operator(True, "save this remark", "cms_rem")
        if who:
            cb.set_remark(session, row_store, p0, p1, text, who)
            st.rerun()
    hist = cb.remark_history(session, row_store, p0, p1)
    if len(hist):
        st.dataframe(hist, hide_index=True, width="stretch")
    else:
        st.caption("No remark recorded yet.")
else:
    st.caption("Select a row to add a remark or see its history.")

# --- credited later ---------------------------------------------------------------------------------
later = cb.credited_later(slips, period0, period1)
later_view = later if not store else later[later["store"] == store]
st.markdown("##### Credited later than day+1")
st.markdown(f"**{len(later_view):,} rows, Rs {money_table(later_view['amount'].sum())}** - not captured in the two "
            "credit columns. They stay in Closing as outstanding.")
if len(later_view):
    with st.expander("Drill down", expanded=False):
        show = later_view.copy()
        show["dep_date"] = show["dep_date"].map(display_date)
        show["cr_date"] = show["cr_date"].map(lambda v: display_date(v) if isinstance(v, str) else "")
        st.dataframe(show.rename(columns={"store": "Store", "pickup_code": "Pickup code", "slip_no": "Slip no",
                                          "dep_date": "DEPDATE", "cr_date": "CRDATE", "lag_days": "Lag days",
                                          "amount": "Amount", "flag": "Flag"}),
                     hide_index=True, width="stretch",
                     column_config={"Amount": st.column_config.NumberColumn(format="%.2f")})
        st.download_button("Export these rows (CSV)", later_view.to_csv(index=False, quoting=1).encode("utf-8"),
                           f"cms_credited_later_{period0}_{period1}.csv", "text/csv", key="cms_later_csv")

# --- unresolved and BNK -----------------------------------------------------------------------------
ur = cb.unresolved_rows(slips, period0, period1)
ps = cb.pseudo_store_totals(slips, period0, period1)
st.markdown("##### Pickup codes with no store mapping")
st.markdown(f"**UNRESOLVED:** {ps['UNRESOLVED'][0]:,} rows, Rs {money_table(ps['UNRESOLVED'][1])} - pickup code is not in "
            f"the master; kept in every total. **BNK:** {ps['BNK'][0]:,} rows, Rs {money_table(ps['BNK'][1])} - its own "
            "pseudo-store, never folded into a real store.")
if len(ur):
    st.dataframe(ur.rename(columns={"pickup_code": "Pickup code", "location": "Location", "rows": "Rows", "amount": "Amount"}),
                 hide_index=True, width="stretch", column_config={"Amount": st.column_config.NumberColumn(format="%.2f")})

# --- excluded types ---------------------------------------------------------------------------------
st.markdown("##### Not in the grid")
st.dataframe(cb.excluded_types(slips, period0, period1), hide_index=True, width="stretch",
             column_config={c: st.column_config.NumberColumn(format="%.2f") for c in ("SLIPAMOUNT", "ADJ", "RECOVEREDAMOUNT")})

# --- date blocks for ONE month: the two CMS columns are editable as overrides -----------------------
st.markdown("##### Date blocks")
block_month = choice if choice != "all" else month_keys[-1]
if choice == "all":
    st.caption(f"Blocks are shown one month at a time - showing {labels[block_month]}. Pick a month above to change it.")
b0, b1 = _bounds(block_month)
bf = cb.blocks_frame(model, b0, b1)
if store:
    bf = bf[bf["STORE NAME"] == store]
bf = bf.reset_index(drop=True)
CMS_COLS = [c for c in bf.columns if "CMS (" in c]
bi = {d: i for i, d in enumerate(model.dates)}
si = {s_: i for i, s_ in enumerate(model.stores)}
marked = {(model.dates[di], model.stores[sj], col) for di, sj, col in model.overridden}


def _cell_key(column_name: str):
    tag, rest = column_name.split(" ", 1)
    day = next(d for d in model.dates if b0 <= d <= b1 and date.fromisoformat(d).strftime("%d/%m") == tag)
    return day, ("same_day" if "same day" in rest else "next_day")


def _tint(frame):
    out = pd.DataFrame("", index=frame.index, columns=frame.columns)
    for c in CMS_COLS:
        day, colname = _cell_key(c)
        for r_i, s_ in enumerate(frame["STORE NAME"]):
            if (day, s_, colname) in marked:
                out.at[r_i, c] = WARN_FILL
    return out


ed_n = st.session_state.setdefault("cms_ed_n", 0)
edited = st.data_editor(
    bf.style.apply(_tint, axis=None), hide_index=True, width="stretch",
    key=f"cms_blocks_{run.id}_{block_month}_{store}_{ed_n}",
    disabled=[c for c in bf.columns if c not in CMS_COLS],
    column_config={"STORE NAME": st.column_config.TextColumn(pinned=True),
                   **{c: st.column_config.NumberColumn(format="%.2f") for c in bf.columns if c != "STORE NAME"}})
st.caption("Orange cells are overridden - the Overrides list above shows the bank's figure, the override and the reason. "
           "Edit a CMS Report cell to override it; the bank's figure is kept.")

pending = []
for c in CMS_COLS:
    diff_rows = edited.index[(edited[c].astype(float) - bf[c].astype(float)).abs() > cb.EPS]
    for r_i in diff_rows:
        day, colname = _cell_key(c)
        s_ = bf.at[r_i, "STORE NAME"]
        computed = float((model.cms1_computed if colname == "same_day" else model.cms2_computed)[bi[day], si[s_]])
        pending.append({"Store": s_, "Deposit date": day, "Column": colname, "Bank (computed)": computed,
                        "Override": float(edited.at[r_i, c]), "Reason": ""})
if pending:
    st.markdown(f"**{len(pending)} edit(s) waiting - give a reason for each, then apply.**")
    pend_df = st.data_editor(pd.DataFrame(pending), hide_index=True, width="stretch",
                             disabled=["Store", "Deposit date", "Column", "Bank (computed)", "Override"],
                             key=f"cms_pending_{ed_n}_{len(pending)}",
                             column_config={"Reason": st.column_config.TextColumn(required=True, width="large")})
    ready = bool((pend_df["Reason"].astype(str).str.strip() != "").all())
    a_col, b_col = st.columns([1, 1])
    if a_col.button("Apply overrides", disabled=not ready, key="cms_apply_ovr"):
        who = confirm_with_operator(True, "apply these overrides", "cms_ovr")
        if who:
            rows = [{"store": r["Store"], "deposit_date": r["Deposit date"], "column": r["Column"],
                     "amount": r["Override"], "reason": r["Reason"]} for r in pend_df.to_dict("records")]
            acc, rej = cb.validate_overrides(base_model, rows, b0, b1)
            if rej:
                st.error("; ".join(f"{x['store']} {x['deposit_date']}: {x['problems']}" for x in rej))
            else:
                cb.record_overrides(session, acc, who, batch_id=uuid.uuid4().hex[:12])
                st.session_state["cms_ed_n"] = ed_n + 1
                st.rerun()
    if b_col.button("Discard edits", key="cms_discard_ovr"):
        st.session_state["cms_ed_n"] = ed_n + 1
        st.rerun()
    if not ready:
        st.caption("A reason is required on every row.")

# --- bulk override upload ---------------------------------------------------------------------------
with st.expander("Bulk override upload", expanded=False):
    TEMPLATE = "store,deposit_date,column,amount,reason\nABD,2026-08-14,same_day,80290.00,Agency confirmed slip by phone\n"
    st.download_button("Download the template (CSV)", TEMPLATE.encode("utf-8"), "cms_override_template.csv", "text/csv",
                       key="cms_ovr_tpl")
    st.caption("Columns: store, deposit_date, column (same_day / next_day), amount, reason. Dates may be "
               "DD/MM/YYYY or YYYY-MM-DD. Checked against the period shown above "
               f"({display_date(period0)} - {display_date(period1)}); nothing is applied until you confirm, and a "
               "file with any rejected row applies nothing.")
    up = st.file_uploader("Overrides file", type=["csv", "xlsx"], key="cms_ovr_upload")
    if up is not None:
        raw = pd.read_csv(up, dtype=str) if up.name.lower().endswith(".csv") else pd.read_excel(up, dtype=str)
        raw.columns = [str(c).strip().lower() for c in raw.columns]
        miss = [c for c in ("store", "deposit_date", "column", "amount", "reason") if c not in raw.columns]
        if miss:
            st.error("Missing column(s): " + ", ".join(miss))
        else:
            acc, rej = cb.validate_overrides(base_model, raw.to_dict("records"), period0, period1)
            st.markdown(f"**{len(acc)} accepted, {len(rej)} rejected.**")
            if acc:
                st.dataframe(pd.DataFrame(acc).drop(columns=["row"]), hide_index=True, width="stretch")
            if rej:
                st.dataframe(pd.DataFrame(rej), hide_index=True, width="stretch")
            if st.button(f"Apply {len(acc)} override(s)", disabled=bool(rej) or not acc, key="cms_bulk_apply"):
                who = confirm_with_operator(True, "apply this bulk override", "cms_bulk")
                if who:
                    cb.record_overrides(session, acc, who, batch_id=uuid.uuid4().hex[:12])
                    st.rerun()

# --- export -----------------------------------------------------------------------------------------
st.markdown("##### Export")
if st.button("Build the Excel workbook", key="cms_build_xlsx"):
    all_rem = {}
    for mk in month_keys:
        lo, hi = _bounds(mk)
        all_rem[(lo, hi)] = {s_: v[0] for s_, v in cb.remarks_for(session, lo, hi).items()}
    data = cbx.build_workbook(
        model, d0, d1, all_rem, cb.credited_later(slips, d0, d1), cb.unresolved_rows(slips, d0, d1),
        cb.excluded_types(slips, d0, d1),
        cb.block_controls(model, d0, d1) + [cb.deposit_tie(session, run.id, deposits, d0, d1)],
        cb.store_differences(slips, deposits, d0, d1),
        {"run_id": run.id, "run_ts": run.started_at,
         "opening_note": f"Opening CMS outstanding assumed zero at {display_date(first)} unless configured."},
        overrides=cb.all_overrides(session))
    st.session_state["cms_xlsx"] = (data, f"CMS_Reco_{d0}_{d1}.xlsx")
if "cms_xlsx" in st.session_state:
    data, name = st.session_state["cms_xlsx"]
    st.download_button(f"Download {name}", data, name,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="cms_dl_xlsx")

# --- relation to store sheet column J: last, collapsed, remainder in the header ---------------------
bridge = cb.bridge_to_j(session, run.id, model, slips, period0, period1)
with st.expander(f"Relation to store sheet column J - remainder {money_table(bridge.unexplained)}", expanded=False):
    st.caption("Different measures, both correct: this page groups the bank's credits by DEPOSIT date (same day and "
               "next day); column J is cash credited in the period, by CREDIT date. They are not expected to tie. "
               "Overrides are left out here - this is the bank's own figure against column J.")
    rows = [
        ("Page CMS Report (by deposit date, day and day+1)", bridge.page_cms),
        ("add  credited later than day+1", bridge.credited_later),
        (f"less credited after {display_date(bridge.cutoff)}", -bridge.after_cutoff),
        ("less unmapped stores (UNRESOLVED; BNK is in column J, not a cause)", -bridge.unmapped),
        ("less ADJ net (ADJ.CR. - ADJ.DB., in column J only)", bridge.adj_net),
        ("= Store sheet column J (as reconstructed)", bridge.expected_j),
        ("Store sheet column J (as stored)", bridge.store_sheet_j),
        ("Unexplained remainder", bridge.unexplained),
    ]
    st.dataframe(pd.DataFrame({"Item": [r[0] for r in rows], "Rs": [money_table(r[1]) for r in rows]}),
                 hide_index=True, width="stretch")
    if abs(bridge.unexplained) > cb.EPS:
        st.error(f"MISMATCH - Rs {money_table(bridge.unexplained)} of the difference between this page and column J "
                 "is not explained by the items above.")
