"""CMS Reco — the cash-with-agency reconciliation (2026-09-30).

What stores deposited, what the bank credited, and what is still
outstanding — a RUNNING LEDGER per store, never netted per day. A deposit
made on day D and credited on D+1 clears the OPENING balance on D+1; it
does not create an "excess" on D+1 and a "shortfall" on D. See
mop_reco/cms_reco.py for the full reasoning.

Sits directly below Float in the nav, on instruction. Reads only the
precomputed store_day_agg aggregate — never a raw file, never a bill-line
scan (CLAUDE.md 6.14).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import (
    db_session,
    frame_export_button,
    latest_run,
    no_run_yet,
    page,
    sidebar,
    stmt_row,
    stmt_table,
    stop_if_no_dates,
    tile,
)
from mop_reco import cms_reco as cr
from mop_reco import store_sheet_source as src
from mop_reco.export import ExportMeta
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_short, money_table

page("CMS Reco", "Cash handed to the agency, credited by the bank, and what "
                "is still outstanding — a running ledger per store, not a "
                "per-day net.")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)


@st.cache_data(ttl=600, show_spinner="Building the CMS ledger…")
def _load_ledger(run_id: int):
    # Always the FULL loaded range — never the sidebar's date filter — so
    # narrowing the date window never resets a store's carried receivable
    # to zero at the window's own start (mop_reco.cms_reco's own docstring).
    # No opening-receivable file has ever been supplied, so every store
    # opens at exactly zero on its first loaded date (stated on the page).
    return cr.build_ledger(session, run_id, store=None, opening_receivable=None)


ledger_all = _load_ledger(run.id)
if ledger_all.empty:
    st.warning("Nothing loaded for this selection.")
    st.stop()

ledger_store = ledger_all if not store else ledger_all[ledger_all["store"] == store]
date_from_s, date_to_s = str(date_from), str(date_to)
ledger_window = ledger_store[(ledger_store["business_date"] >= date_from_s)
                             & (ledger_store["business_date"] <= date_to_s)]

st.caption("No opening-receivable file has ever been supplied — every store's "
          "ledger opens at exactly zero on its own first loaded date.")

# ===========================================================================
# Controls — asserted and shown, quiet only when clean (2026-09-12 pattern)
# ===========================================================================
checks = cr.run_checks(ledger_store)
failed = [c for c in checks.controls if not c.holds]

# The live parity check against the store sheet — SAME filter (date range
# AND store) as the sidebar currently holds, never the full range this
# ledger is otherwise built over. CMS Reco and the store sheet must never
# disagree (the operator's own instruction).
parity = src.store_cash_summary(session, run.id, date_from, date_to, store)
sum_i_ledger = float(ledger_window["cash_deposit"].sum())
sum_j_ledger = float(ledger_window["cash_received"].sum())
parity_i_diff = round(sum_i_ledger - parity["cash_deposit"], 2)
parity_j_diff = round(sum_j_ledger - parity["cash_received"], 2)
parity_ok = abs(parity_i_diff) <= 0.005 and abs(parity_j_diff) <= 0.005

if failed or not parity_ok:
    st.error(f"**{len(failed) + (0 if parity_ok else 1)} control(s) do not hold.** "
            "The figures below cannot be relied on until this is explained.")
    for c in failed:
        st.markdown(f"- **({c.key})** {c.what} — {c.detail}")
    if not parity_ok:
        st.markdown(
            f"- **(parity)** this page's sum(I)/sum(J) must equal the store "
            f"sheet's own totals for the same filter — got I {sum_i_ledger:,.2f} "
            f"vs {parity['cash_deposit']:,.2f} (diff {parity_i_diff:,.2f}), "
            f"J {sum_j_ledger:,.2f} vs {parity['cash_received']:,.2f} "
            f"(diff {parity_j_diff:,.2f})."
        )

# ===========================================================================
# Tiles
# ===========================================================================
outstanding_floored = max(0.0, sum_i_ledger - sum_j_ledger)
over_2_days = ledger_store[
    (ledger_store["business_date"] == ledger_store["business_date"].max())
    & (ledger_store["days_outstanding"] > 2)]

with st.container(key="cms-reco-ribbon"):
    r1, r2, r3, r4 = st.columns(4)
    tile(r1, "CASH DEPOSITED (PERIOD)", money_short(sum_i_ledger),
        tooltip=f"Rs {money_table(sum_i_ledger)}")
    tile(r2, "CASH CREDITED (PERIOD)", money_short(sum_j_ledger),
        tooltip=f"Rs {money_table(sum_j_ledger)}")
    tile(r3, "OUTSTANDING RECEIVABLE", money_short(outstanding_floored),
        state="attention" if outstanding_floored > 0.005 else "clean",
        note="closing, floored at zero", tooltip=f"Rs {money_table(outstanding_floored)}")
    tile(r4, "STORES > 2 DAYS OUTSTANDING", fmt_count(over_2_days["store"].nunique()),
        state="action" if len(over_2_days) else "clean", note="the work queue")

# Same statement-style presentation as Summary's own cash block (the
# operator's own instruction) — labels carry the operation, every figure
# stays positive. Opening/closing are STOCKS (the first/last filtered
# date's own value, summed across stores — never summed across dates,
# which would double-count a carried balance, the same discipline
# store_cash_summary() already applies to column H); deposited/credited are
# FLOWS, summed across the whole filtered window.
_period_opening = float(
    ledger_window[ledger_window["business_date"] == date_from_s]["opening_receivable"].sum()
) if not ledger_window.empty else 0.0
_period_closing = float(
    ledger_window[ledger_window["business_date"] == date_to_s]["closing_receivable"].sum()
) if not ledger_window.empty else 0.0
_stmt_rows = (
    stmt_row("Opening receivable", _period_opening)
    + stmt_row("Add: Cash deposited", sum_i_ledger)
    + stmt_row("Less: Cash credited", sum_j_ledger)
)
st.markdown(
    f"<div class='mop-stmt-grid'>"
    f"{stmt_table('CASH WITH AGENCY (running)', f'{display_date(date_from)} to {display_date(date_to)}', _stmt_rows, 'Closing receivable', _period_closing, result_bad=not parity_ok)}"
    f"</div>", unsafe_allow_html=True)

st.caption(
    "Ageing is indicative until Defect B (slip-to-voucher date attribution) is "
    "resolved — a slip whose DEPDATE is the agency's banking date rather than "
    "the store's handover date will still age wrongly. The running ledger "
    "absorbs most of the day-shift noise, but not that one."
)

# ===========================================================================
# Negative balances — never floored at the ledger level (rule 4)
# ===========================================================================
st.markdown("##### Data quality")
negative = cr.negative_closing_rows(ledger_store)
if not negative.empty:
    neg_total = float(negative["closing_receivable"].sum())
    st.warning(
        f"⚠ **{fmt_count(len(negative))} store-day(s) show a negative closing "
        f"receivable** (credits exceeded deposits) — combined **{money_short(neg_total)}**."
    )
    with st.expander(f"Negative closing receivable — {fmt_count(len(negative))} store-day(s)"):
        neg_table = negative.rename(columns={
            "business_date": "Date", "store": "Store",
            "opening_receivable": "Opening receivable", "cash_deposit": "Cash deposit",
            "cash_received": "Cash received", "closing_receivable": "Closing receivable",
            "days_outstanding": "Days outstanding",
        }).copy()
        neg_table["Date"] = neg_table["Date"].map(display_date)
        for column in ("Opening receivable", "Cash deposit", "Cash received", "Closing receivable"):
            neg_table[column] = neg_table[column].map(money_table)
        st.dataframe(neg_table, hide_index=True, width="stretch")
        frame_export_button(
            negative,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store}, value_column="closing_receivable",
                      title="CMS Reco — negative closing receivable"),
            key="dl_cms_negative", noun="negative closing rows",
        )
else:
    st.caption("No negative closing receivable in this filter.")

# ===========================================================================
# The grid — date rows, expandable to store rows, like the Summary grid
# ===========================================================================
st.divider()
all_dates = sorted(ledger_window["business_date"].unique())
OPEN_DATES_KEY = "cms_reco_open_dates"
st.session_state.setdefault(OPEN_DATES_KEY, set())

head_l, head_r = st.columns([3, 2])
head_l.markdown("##### Cash-with-agency ledger — by business date")
with head_r:
    dea, dca = st.columns(2)
    if dea.button("Expand all", key="cms_dates_expand_all"):
        st.session_state[OPEN_DATES_KEY] = set(all_dates)
    if dca.button("Collapse all", key="cms_dates_collapse_all"):
        st.session_state[OPEN_DATES_KEY] = set()

grouped = cr.build_grouped_table(ledger_store, date_from_s, date_to_s,
                                 st.session_state[OPEN_DATES_KEY])
display_grouped = grouped.copy()
display_grouped["business_date"] = display_grouped["business_date"].map(
    lambda d: display_date(d) if d not in ("", "TOTAL") else d)
for column in ("opening_receivable", "cash_deposit", "cash_received", "closing_receivable"):
    display_grouped[column] = display_grouped[column].map(money_table)
display_grouped = display_grouped.rename(columns={
    "business_date": "Date", "store": "Store", "opening_receivable": "Opening receivable",
    "cash_deposit": "Cash deposit (I)", "cash_received": "Cash received (J)",
    "closing_receivable": "Closing receivable", "days_outstanding": "Days outstanding",
})

event = st.dataframe(
    display_grouped, hide_index=True, width="stretch",
    on_select="rerun", selection_mode="single-row", key="cms_reco_grouped_table",
)
selected_rows = event.selection.rows if event and event.selection else []
if selected_rows:
    parent_rows = [d for d in all_dates]
    # Only a genuine "ALL STORES" parent row (not the TOTAL row, not an
    # already-spliced-in store row) can be toggled — child rows carry a
    # blank date and are skipped naturally since they are not in `all_dates`.
    clicked_idx = selected_rows[0]
    clicked_date_label = grouped.iloc[clicked_idx]["business_date"] if clicked_idx < len(grouped) else None
    if clicked_date_label in all_dates:
        opened = st.session_state[OPEN_DATES_KEY]
        if clicked_date_label in opened:
            opened.discard(clicked_date_label)
        else:
            opened.add(clicked_date_label)
        st.rerun()

frame_export_button(
    ledger_window,
    ExportMeta(run_id=run.id, run_timestamp=run.started_at,
              date_from=date_from, date_to=date_to,
              filters={"store": store}, value_column="closing_receivable",
              title="CMS Reco ledger"),
    key="dl_cms_reco_ledger", noun="CMS Reco ledger rows",
)

# ===========================================================================
# Ageing profile
# ===========================================================================
st.divider()
st.markdown("##### Ageing")
profile = cr.ageing_profile(ledger_store)
ap1, ap2, ap3, ap4 = st.columns(4)
labels = [("1_day", "1 day", ap1), ("2_days", "2 days", ap2),
         ("3_to_5_days", "3-5 days", ap3), ("over_5_days", "Over 5 days", ap4)]
for k, label, col in labels:
    n, v = profile[k]
    tile(col, label.upper(), fmt_count(n), state="attention" if n else "clean",
        tooltip=f"Rs {money_table(v)}", note=money_short(v))
