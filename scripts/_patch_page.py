from pathlib import Path
p = Path('app/pages/4_Store_sheet.py')
s = p.read_text(encoding='utf-8')

# --- 1. load() must carry the new bundle fields ---------------------------
old_load = '''@st.cache_data(ttl=600, show_spinner="Building the sheet…")
def load(run_id, lo, hi):
    """Cached per run and range. The sheet walks the cash book from 1 July
    whatever range is asked for, so it is worth not rebuilding per click."""
    bundle = src.build(session, run_id, lo, hi)
    return (bundle.sheet, bundle.summary, bundle.excluded,
            bundle.cms_type_totals, bundle.unresolved_pickups, bundle.notes,
            [(c.key, c.what, c.holds, c.detail) for c in bundle.checks.controls])


sheet, summary, excluded, type_totals, unresolved, notes, controls = load(
    run.id, date_from, date_to)'''

new_load = '''@st.cache_data(ttl=600, show_spinner="Building the sheet…")
def load(run_id, lo, hi):
    """Cached per run and range. The sheet walks the cash book from 1 July
    whatever range is asked for, so it is worth not rebuilding per click."""
    bundle = src.build(session, run_id, lo, hi)
    return (bundle.sheet, bundle.summary, bundle.summary_by_store,
            bundle.grand_total, bundle.excluded, bundle.contribution,
            bundle.cms_type_totals, bundle.unresolved_pickups, bundle.notes,
            [(c.key, c.what, c.holds, c.detail) for c in bundle.checks.controls])


(sheet, summary, summary_by_store, grand_total, excluded, contribution,
 type_totals, unresolved, notes, controls) = load(run.id, date_from, date_to)'''

assert old_load in s, "load() anchor"
s = s.replace(old_load, new_load, 1)

# --- 2. the export button must pass the three blocks + contribution -------
old_export = '''# --- the export -----------------------------------------------------------
if st.button("Build the Excel sheet", type="primary"):
    with st.spinner("Writing the workbook…"):
        bundle_checks = ss.run_controls(
            shown, ss.summarise_by_date(shown), None,
            dashboard_digital=float(shown["digital_sale"].sum()))
        data = build_workbook(
            shown, ss.summarise_by_date(shown), excluded,
            run_id=run.id, run_timestamp=run.started_at,
            date_from=date_from, date_to=date_to, checks=bundle_checks)
    st.session_state["_store_sheet_xlsx"] = data

if st.session_state.get("_store_sheet_xlsx"):
    st.download_button(
        f"Download the sheet ({fmt_count(len(shown))} detail rows)",
        st.session_state["_store_sheet_xlsx"],
        file_name=f"store_wise_sale_and_cash_{date_from}_{date_to}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch")
    st.caption(
        "One tab: the summary block above the detail, in the same column "
        "order. C, G, K, M and N are live Excel formulas, not pasted values, "
        "so any cell can be traced. Indian number grouping throughout."
    )'''

new_export = '''# --- the export -----------------------------------------------------------
if st.button("Build the Excel sheet", type="primary"):
    with st.spinner("Writing the workbook…"):
        shown_summary = ss.summarise_by_date(shown)
        shown_by_store = ss.summarise_by_store(shown)
        shown_grand_total = ss.grand_total_row(shown)
        bundle_checks = ss.run_controls(
            shown, shown_summary, None,
            dashboard_digital=float(shown["digital_sale"].sum()))
        data = build_workbook(
            shown, shown_summary, excluded,
            run_id=run.id, run_timestamp=run.started_at,
            date_from=date_from, date_to=date_to, checks=bundle_checks,
            summary_by_store=shown_by_store, grand_total=shown_grand_total,
            contribution=contribution)
    st.session_state["_store_sheet_xlsx"] = data

if st.session_state.get("_store_sheet_xlsx"):
    st.download_button(
        f"Download the sheet ({fmt_count(len(shown))} detail rows)",
        st.session_state["_store_sheet_xlsx"],
        file_name=f"store_wise_sale_and_cash_{date_from}_{date_to}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch")
    st.caption(
        "One tab, three summary blocks above the detail — by date, by store, "
        "and a grand total whose formulas point straight at the detail rows. "
        "C, G, K, M and N are live Excel formulas, not pasted values, so any "
        "cell can be traced. Indian number grouping throughout."
    )'''

assert old_export in s, "export button anchor"
s = s.replace(old_export, new_export, 1)

# --- 3. the on-screen summary becomes three blocks -------------------------
old_summary = '''st.divider()

# --- the summary block ----------------------------------------------------
st.markdown("##### Summary — by business date, all stores")


def display(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({"A Date": frame["date"], "B Store": frame["store"]})
    for letter, name, label in ss.COLUMNS[2:]:
        out[f"{letter} {label}"] = frame[name].map(money_table)
    return out


summary_shown = (summary if store is None
                 else ss.summarise_by_date(shown))
st.dataframe(display(summary_shown), hide_index=True, width="stretch")
st.caption(
    "The month's opening is the FIRST date's opening and its closing the LAST "
    "date's closing — summing either down the column would count the "
    "carry-forward once per day."
)'''

new_summary = '''st.divider()


def display(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({"A Date": frame["date"], "B Store": frame["store"]})
    for letter, name, label in ss.COLUMNS[2:]:
        out[f"{letter} {label}"] = frame[name].map(money_table)
    return out


# When a store is filtered, all three blocks are rebuilt over just that
# store's rows, so the screen and the export never disagree with the filter.
if store is None:
    summary_by_date_shown = summary
    summary_by_store_shown = summary_by_store
    grand_total_shown = grand_total
else:
    summary_by_date_shown = ss.summarise_by_date(shown)
    summary_by_store_shown = ss.summarise_by_store(shown)
    grand_total_shown = ss.grand_total_row(shown)

st.markdown("##### Summary 1 — by business date, all stores")
st.dataframe(display(summary_by_date_shown), hide_index=True, width="stretch")

st.markdown("##### Summary 2 — by store, whole range")
st.dataframe(display(summary_by_store_shown), hide_index=True, width="stretch")
st.caption(
    "A store's opening here is its FIRST date's opening and its closing its "
    "LAST date's closing — not a sum of balances, which would double-count "
    "the carry-forward."
)

st.markdown("##### Summary 3 — grand total")
st.dataframe(display(grand_total_shown), hide_index=True, width="stretch")'''

assert old_summary in s, "summary block anchor"
s = s.replace(old_summary, new_summary, 1)

# --- 4. the CMS-type section: rebuild for the new column-source rule -------
old_cms = s[s.index('# --- what is NOT in column J ---------------------------------------------'):
           s.index('if unresolved is not None and not unresolved.empty:')]

new_cms = '''# --- what is in column J, and from which column -------------------------
st.divider()
st.markdown("##### Column J — contribution by CMS type")
st.caption(
    "J is not COL alone. COL is credited from **SLIPAMOUNT**. ADJ.CR. is "
    "credited from the **ADJ column**, NOT SLIPAMOUNT — on an ADJ.CR. row "
    "SLIPAMOUNT is the ORIGINAL slip being adjusted, and using it would book "
    "that slip a second time. ADJ.DB. is the bank recovering an earlier "
    "overcredit, so its **RECOVEREDAMOUNT is subtracted**. TYPE = ZPO is "
    "excluded entirely, on instruction — see below."
)
if contribution is not None and not contribution.empty:
    st.dataframe(contribution.assign(
        value=contribution["value"].map(money_table)).rename(
        columns={"type": "Type", "source_column": "Source column",
                "rows": "Rows", "value": "Contribution to J (Rs)"}),
        hide_index=True, width="stretch")
    st.caption(
        f"Total contribution to J: "
        f"**{money_short(float(contribution['value'].sum()))}**."
    )

st.markdown("##### TYPE = ZPO — excluded from column J")
st.caption(
    "Excluded entirely on the operator's instruction. ZPO rows also carry no "
    "deposit date and a single pseudo pickup code, so they could not be "
    "attributed to a business date or a store even if included. Reported "
    "here rather than dropped without a trace (rule 4/5)."
)
if excluded is not None and not excluded.empty:
    st.dataframe(excluded.assign(
        value=excluded["value"].map(money_table)).rename(
        columns={"type": "Type", "date": "Date", "store": "Store",
                "rows": "Rows", "value": "Value (Rs)"}),
        hide_index=True, width="stretch")
    st.caption(
        f"**{money_short(float(excluded['value'].sum()))} across "
        f"{fmt_count(int(excluded['rows'].sum()))} row(s)** excluded from J."
    )

'''

assert old_cms in s, "cms section anchor"
s = s.replace(old_cms, new_cms, 1)

p.write_text(s, encoding='utf-8')
print("page updated")
