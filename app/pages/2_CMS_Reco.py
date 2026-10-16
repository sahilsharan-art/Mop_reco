"""CMS Reco - one block per DEPOSIT DATE, chained per store (2026-10-16).

The operator's workbook model: Opening + Cash/CMS Deposit - CMS Report (credited same day) - CMS Report
(credited next day) = Closing, and the Closing becomes the next block's Opening. See mop_reco/cms_blocks.py
for the rules and for why a credit dated day+2 or later is listed separately instead of dropped.

Reads only rows written at run time (cms_slips, cms_ptc_deposits) - never a raw file (CLAUDE.md 6.14).
"""

from __future__ import annotations

import sys
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
from mop_reco.formatting import blank, display_date, money_short, money_table

page("CMS Reco", "Cash handed to the agency against what the bank credited, block by deposit date, chained per store.")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)
cfg = load_config()


@st.cache_data(ttl=600, show_spinner="Building the CMS blocks...")
def _load(run_id: int):
    s = db_session()
    slips, deposits = cb.load(s, run_id)
    first, last = cb.run_range(s, run_id)
    openings = cb.opening_from_config(cfg.raw)
    if slips.empty and deposits.empty:
        return slips, deposits, None, first, last
    return slips, deposits, cb.build_model(slips, deposits, first, last, openings), first, last


slips, deposits, model, first, last = _load(run.id)
if model is None:
    st.warning("No CMS data has been stored for this run. Run `scripts/backfill_cms_slips.py` or re-run the reconciliation.")
    st.stop()

d0 = max(str(date_from), first)
d1 = min(str(date_to), last)
if d0 > d1:
    st.warning("The selected dates are outside this run.")
    st.stop()

st.caption(f"Opening CMS outstanding assumed zero at {display_date(first)}. "
           "It is a configured value (cms_reco.opening in config.yaml), so a file can be supplied later.")

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


p0, p1 = _bounds(choice)

# --- controls ---------------------------------------------------------------------------------------
controls = cb.block_controls(model, p0, p1)
tie_i = cb.deposit_tie(session, run.id, deposits, p0, p1)
bridge = cb.bridge_to_j(session, run.id, model, slips, p0, p1)
failed = [c for c in controls + [tie_i] if not c.ok]
if failed:
    st.error(f"**{len(failed)} control(s) do not hold.**")
    for c in failed:
        st.markdown(f"- **{c.label}** - {c.detail} (difference {money_table(c.amount)})")

st.markdown("##### How the page relates to store sheet column J")
st.caption("Different measures, both correct: this page groups credits by DEPOSIT date (same day and next day); "
           "column J is cash credited in the period, by CREDIT date. They are not expected to tie.")
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
    st.error(f"MISMATCH - Rs {money_table(bridge.unexplained)} of the difference between this page and column J is "
             "not explained by the items above.")

# --- tiles ------------------------------------------------------------------------------------------
sm = cb.summary_frame(model, p0, p1)
if store:
    sm = sm[sm["store"] == store]
tot = sm[["opening", "deposit", "cms", "diff"]].sum()
with st.container(key="cms-blocks-ribbon"):
    c1, c2, c3, c4 = st.columns(4)
    tile(c1, "OPENING", money_short(tot["opening"]), tooltip=f"Rs {money_table(tot['opening'])}")
    tile(c2, "CASH/CMS DEPOSIT", money_short(tot["deposit"]), tooltip=f"Rs {money_table(tot['deposit'])}")
    tile(c3, "CMS REPORT", money_short(tot["cms"]), tooltip=f"Rs {money_table(tot['cms'])}")
    tile(c4, "DIFF (STILL OUTSTANDING)", money_short(tot["diff"]), accent="warn" if abs(tot["diff"]) > cb.EPS else None,
         tooltip=f"Rs {money_table(tot['diff'])}")

# --- monthly summary + remarks ----------------------------------------------------------------------
st.markdown("##### Summary")
saved = cb.remarks_for(session, p0, p1)
view = sm.copy()
view["Remarks"] = [saved.get(s, ("", "", None))[0] for s in view["store"]]
total_row = pd.DataFrame([{"store": "TOTAL", "opening": tot["opening"], "deposit": tot["deposit"], "cms": tot["cms"],
                           "diff": tot["diff"], "Remarks": ""}])
edit_src = view.rename(columns={"store": "STORE NAME", "opening": "Opening", "deposit": "Cash/CMS Deposit",
                                "cms": "CMS Report", "diff": "Diff"})
edited = st.data_editor(
    edit_src, hide_index=True, width="stretch", key=f"cms_sum_{run.id}_{p0}_{p1}_{store}",
    disabled=["STORE NAME", "Opening", "Cash/CMS Deposit", "CMS Report", "Diff"],
    column_config={"STORE NAME": st.column_config.TextColumn(pinned=True),
                   **{c: st.column_config.NumberColumn(format="%.2f") for c in ("Opening", "Cash/CMS Deposit", "CMS Report", "Diff")},
                   "Remarks": st.column_config.TextColumn(width="large")})
st.dataframe(total_row.rename(columns={"store": "STORE NAME", "opening": "Opening", "deposit": "Cash/CMS Deposit",
                                       "cms": "CMS Report", "diff": "Diff"}),
             hide_index=True, width="stretch",
             column_config={c: st.column_config.NumberColumn(format="%.2f") for c in ("Opening", "Cash/CMS Deposit", "CMS Report", "Diff")})
changed = [(r["STORE NAME"], str(r["Remarks"] or "").strip()) for _, r in edited.iterrows()
           if str(r["Remarks"] or "").strip() != (saved.get(r["STORE NAME"], ("",))[0] or "")]
changed = [c for c in changed if c[1]]
if st.button(f"Save {len(changed)} remark(s)", disabled=not changed, key="cms_save_remarks"):
    who = confirm_with_operator(True, "save these remarks", "cms_rem")
    if who:
        for s_, text in changed:
            cb.set_remark(session, s_, p0, p1, text, who)
        st.success(f"Saved {len(changed)} remark(s) for {display_date(p0)} - {display_date(p1)}.")
        st.rerun()
hist_store = st.selectbox("Remark history for", [""] + list(sm["store"]), key="cms_hist")
if hist_store:
    h = cb.remark_history(session, hist_store, p0, p1)
    st.dataframe(h, hide_index=True, width="stretch") if len(h) else st.caption("No remark recorded.")

# --- credited later ---------------------------------------------------------------------------------
later = cb.credited_later(slips, p0, p1)
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
                           f"cms_credited_later_{p0}_{p1}.csv", "text/csv", key="cms_later_csv")

# --- unresolved and BNK -----------------------------------------------------------------------------
ur = cb.unresolved_rows(slips, p0, p1)
ps = cb.pseudo_store_totals(slips, p0, p1)
st.markdown("##### Pickup codes with no store mapping")
st.markdown(f"**UNRESOLVED:** {ps['UNRESOLVED'][0]:,} rows, Rs {money_table(ps['UNRESOLVED'][1])} - pickup code is not in "
            f"the master; kept in every total. **BNK:** {ps['BNK'][0]:,} rows, Rs {money_table(ps['BNK'][1])} - its own "
            "pseudo-store, never folded into a real store.")
if len(ur):
    st.dataframe(ur.rename(columns={"pickup_code": "Pickup code", "location": "Location", "rows": "Rows", "amount": "Amount"}),
                 hide_index=True, width="stretch", column_config={"Amount": st.column_config.NumberColumn(format="%.2f")})

# --- excluded types ---------------------------------------------------------------------------------
st.markdown("##### Not in the grid")
ex = cb.excluded_types(slips, p0, p1)
st.dataframe(ex, hide_index=True, width="stretch",
             column_config={c: st.column_config.NumberColumn(format="%.2f") for c in ("SLIPAMOUNT", "ADJ", "RECOVEREDAMOUNT")})

# --- stores whose deposits differ from CMS ----------------------------------------------------------
diffs = cb.store_differences(slips, deposits, p0, p1)
st.markdown("##### Stores where deposits differ from CMS COL")
st.markdown(f"**{len(diffs)} store(s), net Rs {money_table(diffs['difference'].sum())}** - PTC Cash/CMS Deposit against "
            "COL slips of the same deposit dates, whatever their credit date.")
if len(diffs):
    with st.expander("Per store", expanded=False):
        st.dataframe(diffs.rename(columns={"store": "Store", "deposit": "PTC deposit", "cms": "CMS COL",
                                           "difference": "Difference"}), hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="%.2f") for c in ("PTC deposit", "CMS COL", "Difference")})

# --- date blocks for ONE month ----------------------------------------------------------------------
st.markdown("##### Date blocks")
block_month = choice if choice != "all" else month_keys[-1]
if choice == "all":
    st.caption(f"Blocks are shown one month at a time - showing {labels[block_month]}. Pick a month above to change it.")
b0, b1 = _bounds(block_month)
bf = cb.blocks_frame(model, b0, b1)
if store:
    bf = bf[bf["STORE NAME"] == store]
st.dataframe(bf, hide_index=True, width="stretch",
             column_config={"STORE NAME": st.column_config.TextColumn(pinned=True),
                            **{c: st.column_config.NumberColumn(format="%.2f") for c in bf.columns if c != "STORE NAME"}})

# --- export -----------------------------------------------------------------------------------------
st.markdown("##### Export")
if st.button("Build the Excel workbook", key="cms_build_xlsx"):
    all_rem = {}
    for mk in month_keys:
        lo, hi = _bounds(mk)
        all_rem[(lo, hi)] = {s_: v[0] for s_, v in cb.remarks_for(session, lo, hi).items()}
    data = cbx.build_workbook(
        model, d0, d1, all_rem, cb.credited_later(slips, d0, d1), cb.unresolved_rows(slips, d0, d1),
        cb.excluded_types(slips, d0, d1), cb.block_controls(model, d0, d1) + [cb.deposit_tie(session, run.id, deposits, d0, d1)],
        cb.store_differences(slips, deposits, d0, d1),
        {"run_id": run.id, "run_ts": run.started_at,
         "opening_note": f"Opening CMS outstanding assumed zero at {display_date(first)} unless configured."})
    st.session_state["cms_xlsx"] = (data, f"CMS_Reco_{d0}_{d1}.xlsx")
if "cms_xlsx" in st.session_state:
    data, name = st.session_state["cms_xlsx"]
    st.download_button(f"Download {name}", data, name,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="cms_dl_xlsx")
