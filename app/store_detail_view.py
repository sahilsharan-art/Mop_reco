"""The Store-wise detail section (2026-10-26): the sale and cash sheet by business date, each date opening onto its stores and
each store onto the bills, PTC rows and CMS credits behind it. It used to sit on Summary; it now lives on the Cash position tab.
Summary keeps the cash position / cash with agency statements. Same engine as before (`store_sheet_source.load_fast`), so the
screen and the Excel export cannot disagree.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from mop_reco import store_sheet as ss
from mop_reco import store_sheet_source as src
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_table
from mop_reco.store_sheet_export import build_workbook

OPEN_DATES_KEY = "sm_open_dates"


def _cms_breakdown(session):
    """The CMS-derived notes / excluded / contribution / other-types breakdown, computed FRESH on every export - a whole-range
    breakdown (ZPO, ADJ.CR., ADJ.DB. are not store- or date-scoped) that must reflect the current CMS data, never a snapshot."""
    cms, cms_notes = src.cms_frame(session, with_notes=True)
    return {"notes": cms_notes, "excluded": ss.excluded_credit_types(cms),
            "contribution": ss.cms_contribution_by_type(cms), "other_types": ss.other_type_summary(cms)}


def _export_button(session, run, date_from, date_to, shown):
    """Build the Excel sheet (moved here from Summary 2026-10-26): the store-wise sale and cash workbook for the filtered view."""
    with st.container(key="btnrow", horizontal=True):
        build = st.button("Build the Excel sheet", type="primary", key="ss_build_sheet")
    if build:
        with st.spinner("Writing the workbook…"):
            shown_summary = ss.summarise_by_date(shown)
            checks = ss.run_controls(shown, shown_summary, None, dashboard_digital=float(shown["digital_sale"].sum()))
            cms = _cms_breakdown(session)
            st.session_state["_store_sheet_xlsx"] = build_workbook(
                shown, shown_summary, cms["excluded"], run_id=run.id, run_timestamp=run.started_at,
                date_from=date_from, date_to=date_to, checks=checks, summary_by_store=ss.summarise_by_store(shown),
                grand_total=ss.grand_total_row(shown), contribution=cms["contribution"], other_types=cms["other_types"])
    if st.session_state.get("_store_sheet_xlsx"):
        st.download_button(
            f"Download the sheet ({fmt_count(len(shown))} detail rows)", st.session_state["_store_sheet_xlsx"],
            file_name=f"store_wise_sale_and_cash_{date_from}_{date_to}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")


@st.cache_data(ttl=600, show_spinner="Reading the sheet…")
def _load(run_id, lo, hi, store_filter):
    from dashboard_common import db_session
    return src.load_fast(db_session(), run_id, lo, hi, store_filter)


# The on-screen grid's own column order (2026-09-30) — identical to the
# Excel export's layout (`store_sheet_export.COLUMN_ORDER`), minus its blank
# spacer column: a canvas-drawn `st.dataframe` grid has no concept of an
# unheaded filler column the way a real spreadsheet does, so the same
# digital/cash separation is conveyed with a words-only caption above the
# grid instead (a judgment call, stated here rather than silently guessed —
# the grid genuinely cannot merge/span a header cell across columns either,
# the same constraint already documented for the particulars matrix, §8.1).
# No letter prefixes any more (2026-09-30) — they were internal references
# and do not belong in a report, on the same instruction that removed them
# from the Excel export.
DISPLAY_COLUMNS = [
    ("store", "Store"), ("date", "Date"), ("total_sale", "Total sale"),
    ("digital_sale", "Digital sale"), ("digital_received", "Digital received"),
    ("digital_receivable", "Digital receivable"),
    ("opening_balance", "Opening balance"), ("cash_sale", "Cash sale"),
    ("cash_deposit", "Cash deposit"), ("cash_received", "Cash received"),
    ("cash_receivable", "Cash receivable"),
    ("cash_expense_income", "Cash expense/income"),
    ("closing_balance", "Closing balance"),
]
DIGITAL_RECO_GROUP = "Total sale, Digital sale, Digital received, Digital receivable"
CASH_RECO_GROUP = ("Opening balance, Cash sale, Cash deposit, Cash received, "
                   "Cash receivable, Cash expense/income, Closing balance")


def display(frame: pd.DataFrame, include_float: bool = False) -> pd.DataFrame:
    """`include_float` adds Float amount as the last column — present only
    on the expanded per-store-per-day rows, never on the consolidated
    ALL-STORES parent rows, exactly as the workbook carries it on "By store
    and date" only (2c)."""
    cols = DISPLAY_COLUMNS + ([("float_amount", "Float amount")] if include_float else [])
    out = {}
    for name, label in cols:
        if name == "date":
            out[label] = frame[name].map(display_date)
        elif name == "store":
            out[label] = frame[name]
        elif name == "cash_receivable":
            # Never shown negative anywhere on this page (2026-09-13; K's
            # polarity swapped 2026-09-17 along with column I's sign). A
            # negative K (credit exceeds deposit, at whatever grain this row
            # is — a single store-date, a consolidated ALL-STORES date row,
            # or the grand total) is a Defect B data artefact, not a
            # negative receivable — it floors to zero here and is named in
            # the Data quality expander instead, never displayed raw.
            out[label] = frame[name].map(lambda k: money_table(max(0.0, float(k))))
        else:
            out[label] = frame[name].map(money_table)
    return pd.DataFrame(out)



def render(session, run, date_from, date_to, store):
    shown, summary_by_date_shown = _load(run.id, date_from, date_to, store)
    if shown.empty:
        st.warning("Nothing loaded for this selection.")
        return
    all_dates = [d for d in summary_by_date_shown["date"] if d != "TOTAL"]
    st.session_state.setdefault(OPEN_DATES_KEY, set())
    dates = sorted(shown["date"].unique())
    last = shown[shown["date"] == dates[-1]]
    total_sale = float(shown["total_sale"].sum())
    digital_sale = float(shown["digital_sale"].sum())
    cash_sale = float(shown["cash_sale"].sum())
    digital_receivable = float(shown["digital_receivable"].sum())
    cash_deposit = float(shown["cash_deposit"].sum())
    closing_balance = float(last["closing_balance"].sum())
    receivable_total = ss.net_cash_receivable(shown)
    _export_button(session, run, date_from, date_to, shown)
    st.markdown(f"**Store-wise detail** - {fmt_count(len(shown))} rows")
    # `summary_by_date_shown` already reflects the current store filter —
    # `load()` above builds it that way regardless (2026-09-20).
    head_l, head_r = st.columns([3, 2])
    head_l.markdown("##### Store-wise sale and cash — by business date")
    with head_r:
        dea, dca = st.columns(2)
        if dea.button("Expand all", key="sm_dates_expand_all"):
            st.session_state[OPEN_DATES_KEY] = set(all_dates)
            st.rerun()
        if dca.button("Collapse all", key="sm_dates_collapse_all"):
            st.session_state[OPEN_DATES_KEY] = set()
            st.rerun()

    open_dates = st.session_state[OPEN_DATES_KEY]

    # Group headers, in words (2026-09-30) — st.dataframe's canvas-drawn
    # grid cannot merge/span a header cell across columns the way the Excel
    # export's own "Digital Reco"/"Cash Reco" band can, so the same grouping
    # is stated here instead, directly above the grid it describes.
    st.caption(f"**Digital Reco**: {DIGITAL_RECO_GROUP} &nbsp;·&nbsp; "
              f"**Cash Reco**: {CASH_RECO_GROUP}")

    # Parent (ALL STORES) rows only — build_grouped_table with an empty
    # expansion list never splices any store row into this table. No Float
    # amount here (2c) — it is a per-store-per-day figure, detail-only.
    parent_only = ss.build_grouped_table(
        shown, summary_by_date_shown, []).reset_index(drop=True)
    event = st.dataframe(
        display(parent_only), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key="sm_grouped_table",
    )
    picked = list(event.selection.rows) if event and event.selection else []
    if picked:
        clicked = parent_only.iloc[picked[0]]
        if clicked["store"] == "ALL STORES":
            d = clicked["date"]
            if d in open_dates:
                open_dates.discard(d)
            else:
                open_dates.add(d)
            st.session_state.pop("sm_grouped_table", None)
            st.rerun()

    # --- CONTROL: every parent row = the exact sum of its own child rows --
    #
    # Never silent (rule 4): the check itself always runs, over every
    # additive AND stock column (C, D, E, F, G, I, J, K, L, H, M, N),
    # regardless of which dates happen to be open on screen right now, and a
    # genuine disagreement is shown loudly, naming the date and the column.
    # Only the "all clean" case is quiet.
    date_mismatches = ss.verify_date_rows_equal_store_sums(shown, summary_by_date_shown)
    if not date_mismatches.empty:
        st.error(
            f"**{len(date_mismatches)} date/column combination(s) do not sum "
            "correctly.** The figures below cannot be relied on until this "
            "is explained."
        )
        st.dataframe(date_mismatches, hide_index=True, width="stretch")

    # --- CONTROL: sum of all parent rows = the whole-range totals ---------
    #
    # These whole-range figures used to be shown as tiles (removed
    # 2026-09-18 — see the particulars matrix above); the numbers
    # themselves are unchanged and this control still needs something
    # independently-computed to check the by-date table against. "Cash
    # receivable" is the net (sum I - sum J, floored) and "Closing balance"
    # is the LAST date's own closing — a stock, never summed across dates.
    # This asserts the SAME figures read the other way: down the parent
    # (by-date ALL STORES) rows of the table on screen.
    parent_rows = summary_by_date_shown[summary_by_date_shown["date"] != "TOTAL"]
    tile_checks = []

    def _tile_check(label, got, expected):
        diff = round(got - expected, 2)
        tile_checks.append((label, got, expected, diff, abs(diff) <= ss.TOLERANCE))

    if not parent_rows.empty:
        _tile_check("Total sale (C)", float(parent_rows["total_sale"].sum()), total_sale)
        _tile_check("Digital sale (D)", float(parent_rows["digital_sale"].sum()), digital_sale)
        _tile_check("Cash sale (E)", float(parent_rows["cash_sale"].sum()), cash_sale)
        _tile_check("Digital receivable (G)",
                   float(parent_rows["digital_receivable"].sum()), digital_receivable)
        _tile_check("Cash deposit (I)", float(parent_rows["cash_deposit"].sum()), cash_deposit)
        _tile_check("Cash receivable", ss.net_cash_receivable(parent_rows), receivable_total)
        last_parent_date = parent_rows["date"].max()
        _tile_check(
            "Closing balance (M)",
            float(parent_rows[parent_rows["date"] == last_parent_date]["closing_balance"].sum()),
            closing_balance)

    tile_mismatches = [c for c in tile_checks if not c[4]]
    if tile_mismatches:
        st.error(f"**{len(tile_mismatches)} whole-range total(s) do not match "
                 "the sum of the parent rows.**")
        st.dataframe(pd.DataFrame([
            {"Total": label, "Sum of parent rows": got, "Whole-range total": expected,
             "Difference": diff}
            for label, got, expected, diff, _ok in tile_mismatches
        ]), hide_index=True, width="stretch")

    # --- each open date's own store rows, in its own expander -------------
    #
    # A closed date is not in `open_dates`, so its store rows are never
    # sliced out of `shown`, formatted or rendered — collapse means no work,
    # not just no display.
    for d in sorted(d for d in all_dates if d in open_dates):
        block = shown[shown["date"] == d].sort_values("store").reset_index(drop=True)
        if block.empty:
            continue
        with st.expander(f"{display_date(d)} — {fmt_count(len(block))} store(s)", expanded=True):
            block_key = f"sm_store_rows_{d}"
            block_event = st.dataframe(
                display(block, include_float=True), hide_index=True, width="stretch",
                on_select="rerun", selection_mode="single-row", key=block_key,
            )
            block_picked = (list(block_event.selection.rows)
                           if block_event and block_event.selection else [])
            if block_picked:
                row = block.iloc[block_picked[0]]
                st.markdown(f"###### Behind {row['store']} on {display_date(row['date'])}")
                records = src.source_records(session, run.id, row["date"], row["store"])

                a, b = st.columns(2)
                with a:
                    st.markdown(f"**Digital sale** {money_table(row['digital_sale'])} · "
                                f"**Digital received** {money_table(row['digital_received'])} · "
                                f"**Digital receivable** {money_table(row['digital_receivable'])}")
                    if records.digital_bills is not None and not records.digital_bills.empty:
                        st.dataframe(records.digital_bills, hide_index=True, width="stretch")
                    else:
                        st.caption("No digital bills that day.")

                    st.markdown(f"**Cash sale** {money_table(row['cash_sale'])}")
                    if records.cash_bills is not None and not records.cash_bills.empty:
                        st.caption(f"{fmt_count(len(records.cash_bills))} cash bills — "
                                   "a row with no bill number is excluded, it is not a sale.")
                        st.dataframe(records.cash_bills.head(200), hide_index=True,
                                     width="stretch")
                    else:
                        st.caption("No cash bills that day.")

                with b:
                    st.markdown(f"**Cash deposit** {money_table(row['cash_deposit'])} · "
                                f"**Expense/income** {money_table(row['cash_expense_income'])}")
                    if records.ptc_rows is not None and not records.ptc_rows.empty:
                        st.dataframe(records.ptc_rows, hide_index=True, width="stretch")
                    else:
                        st.caption("No PTC rows that day.")

                    # Never shown negative (2026-09-13): a negative
                    # row['cash_receivable'] (K's polarity swapped
                    # 2026-09-17) means the credit exceeded the deposit that
                    # day — a Defect B artefact, not a receivable — and
                    # floors to zero here too.
                    row_receivable = max(0.0, float(row["cash_receivable"]))
                    st.markdown(f"**Cash received** {money_table(row['cash_received'])} · "
                                f"**Cash receivable** {money_table(row_receivable)}")
                    if records.cms_credits is not None and not records.cms_credits.empty:
                        st.dataframe(records.cms_credits, hide_index=True, width="stretch")
                        if row["cash_receivable"] < -0.005:
                            st.error(
                                "**This day's credit exceeds its deposit** — a "
                                "Defect B data artefact (a credit posted against "
                                "the wrong date, or a shared pickup code), not a "
                                "real receivable. See **Data quality** on the "
                                "**Float** page."
                            )
                        else:
                            st.caption(
                                "Credits are attributed to the **deposit date** "
                                "(DEPDATE), not the credit date. A T+1 credit shows "
                                "on the day the cash left the store, which is the "
                                "only way K means \"deposited and not yet "
                                "received\"."
                            )
                    else:
                        st.caption(
                            "No CMS credit carries this deposit date yet. If I is "
                            "non-zero, this is cash still in transit — real, not an "
                            "error."
                        )
