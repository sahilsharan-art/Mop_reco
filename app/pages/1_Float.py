"""Float — store cash float, and whether it is sitting too high (2026-09-13,
formula and threshold corrected 2026-09-15).

Float = opening balance LESS the cash deposited that business date — it is
NOT the closing balance, which the original build wrongly displayed under
this name. This page reads the store sheet's own `float_amount` column
(Summary's column N, `= H + I`) and never recomputes it, and adds only a
threshold, a high/low flag, a consecutive-day streak, a negative-float
drill-down and a weekend/holiday explanation on top.

Sits directly below Summary in the nav, on instruction.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import (
    confirm_with_operator,
    db_session,
    frame_export_button,
    latest_run,
    no_run_yet,
    page,
    sidebar,
    stop_if_no_dates,
    tile,
)
from mop_reco import float_tracker as ft
from mop_reco import store_sheet_source as src
from mop_reco.export import ExportMeta
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_short, money_table

CONFIG_PATH = Path(__file__).resolve().parents[2] / "app" / "config.yaml"

page("Float", "Float is the opening cash balance less the cash deposited "
             "that day — what the store held back instead of banking. "
             "Threshold Rs 10,000 per store unless overridden.")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)


def _load_config_raw() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@st.cache_data(ttl=600, show_spinner="Building the float grid…")
def _load_grid(run_id, lo, hi):
    bundle = src.build(session, run_id, lo, hi)
    # The raw store-day sheet is returned alongside the float grid — the
    # Data quality section below (credit exceeds deposit, merged in from
    # Summary 2026-09-20) needs `cash_receivable`, which `build_float_grid`
    # does not carry over into its own narrower column set.
    return ft.build_float_grid(bundle.sheet, _load_config_raw()), bundle.sheet


grid_all, raw_sheet = _load_grid(run.id, date_from, date_to)
if grid_all.empty:
    st.warning("Nothing loaded for this selection.")
    st.stop()

grid = grid_all if store is None else grid_all[grid_all["store"] == store]

remarks = ft.float_remarks_for_range(session, date_from, date_to, store)
full = ft.with_remarks(grid, remarks)

# --- tiles ------------------------------------------------------------------
#
# "Today" is the last loaded business date in the active range — there is no
# other candidate for "today" on a system that only ever looks at business
# dates (rule 8: no hardcoded dates). The unremarked count and the longest
# run are whole-range figures, not just today's, since those are about the
# backlog of explaining, not a single day's snapshot.
last_date = sorted(full["business_date"].unique())[-1]
today_rows = full[full["business_date"] == last_date]
today_high = today_rows[today_rows["high_float"]]

unremarked = full[(full["high_float"]) & (full["remark"] == "")]

longest_idx = full["days_held_high"].idxmax() if not full.empty else None
longest_row = full.loc[longest_idx] if longest_idx is not None else None

with st.container(key="float-ribbon"):
    last_date_shown = display_date(last_date)
    r1, r2, r3, r4 = st.columns(4)
    tile(r1, "STORES ABOVE THRESHOLD TODAY", fmt_count(today_high["store"].nunique()),
        state="attention" if len(today_high) else "clean",
        note=f"as of {last_date_shown}", tooltip=f"as of {last_date_shown}")

    total_today = float(today_high["float_amount"].sum())
    tile(r2, "TOTAL FLOAT ABOVE THRESHOLD", money_short(total_today),
        state="attention" if total_today > 0.005 else "clean",
        note=f"as of {last_date_shown}",
        tooltip=f"Rs {money_table(total_today)} as of {last_date_shown}")

    tile(r3, "HIGH FLOAT, NO REMARK", fmt_count(len(unremarked)),
        state="action" if len(unremarked) else "clean",
        note="the work queue", tooltip="high-float store-days with no "
                                       "remark at all, auto or manual")

    if longest_row is not None and longest_row["days_held_high"] > 0:
        longest_date_shown = display_date(longest_row["business_date"])
        tile(r4, "LONGEST HIGH-FLOAT RUN",
            f"{fmt_count(int(longest_row['days_held_high']))} day(s)",
            state="attention",
            note=f"{longest_row['store']}, to {longest_date_shown}",
            tooltip=f"{longest_row['store']} — "
                    f"{int(longest_row['days_held_high'])} consecutive day(s) "
                    f"through {longest_date_shown}")
    else:
        tile(r4, "LONGEST HIGH-FLOAT RUN", "0 days", state="clean")

st.caption(
    "Float = opening balance − cash deposit for the business date, read "
    "from **Summary**'s own column N — it is NOT the closing balance. This "
    "page adds only a threshold, a high/low flag, a consecutive-day streak, "
    "a negative-float check and a weekend/holiday explanation on top."
)

# ===========================================================================
# Data quality — ONE heading, two different anomalies, never hidden
# (2026-09-20: merged here from Summary, which used to carry a separate
# "Data quality" expander for credits exceeding deposits — REMOVED from
# Summary on instruction, its underlying data untouched, now read here
# instead so there is exactly one place to look).
#   - Negative float: deposit > opening (catch-up or misdated deposit).
#   - Credits exceeding deposits (Defect B): a CMS credit posted against the
#     wrong date, or a pickup code shared with another store.
# Neither is floored or excluded anywhere else on this page or Summary —
# these are purely the drill-downs (rule 4).
# ===========================================================================
st.markdown("##### Data quality")

negative_rows = ft.negative_float_rows(full)
if not negative_rows.empty:
    negative_total = float(negative_rows["float_amount"].sum())
    st.warning(
        f"⚠ **{fmt_count(len(negative_rows))} store-day(s) show a deposit "
        f"larger than the opening balance** (catch-up or misdated deposit) "
        f"— combined float **{money_short(negative_total)}**."
    )
    with st.expander(f"Negative float — {fmt_count(len(negative_rows))} "
                     "store-day(s)"):
        neg_table = negative_rows.rename(columns={
            "store": "Store", "business_date": "Business date",
            "opening_balance": "Opening", "cash_deposit": "Deposit",
            "float_amount": "Float",
        }).copy()
        neg_table["Business date"] = neg_table["Business date"].map(display_date)
        for column in ("Opening", "Deposit", "Float"):
            neg_table[column] = neg_table[column].map(money_table)
        st.dataframe(neg_table, hide_index=True, width="stretch")
        frame_export_button(
            negative_rows,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store}, value_column="float_amount",
                      title="Negative float"),
            key="dl_negative_float", noun="negative float rows",
        )

dq_count, dq_total = src.defect_b_summary(session, run.id, date_from, date_to, store)
if dq_count:
    st.warning(
        f"⚠ **{fmt_count(dq_count)} store-day(s) show credits exceeding "
        f"deposits** (Defect B) — combined **{money_short(dq_total)}**."
    )
    with st.expander(f"Credits exceeding deposits (Defect B) — "
                     f"{fmt_count(dq_count)} store-day(s)"):
        st.caption(
            "These rows indicate a credit posted against the wrong date or "
            "a shared pickup code, not money owed to the agency. Open item: "
            "Defect B (CMS date attribution)."
        )
        cms_for_dq = src.cms_frame(session)
        sheet_for_dq = raw_sheet if not store else raw_sheet[raw_sheet["store"] == store]
        dq_shown = src.data_quality_rows(sheet_for_dq, cms_for_dq)
        dq_table = dq_shown.rename(columns={
            "store": "Store", "business_date": "Date", "I": "I (deposit)",
            "J": "J (credit)", "J_minus_I": "J - I",
            "pickup_code": "Pickup code", "slip_no": "Slip no",
        }).copy()
        dq_table["Date"] = dq_table["Date"].map(display_date)
        for column in ("I (deposit)", "J (credit)", "J - I"):
            dq_table[column] = dq_table[column].map(money_table)
        st.dataframe(dq_table, hide_index=True, width="stretch")
        st.caption(f"Total: {fmt_count(len(dq_shown))} store-day(s), "
                  f"{money_short(dq_total)}.")
        frame_export_button(
            dq_shown,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store}, value_column="J_minus_I",
                      title="Data quality"),
            key="dl_data_quality", noun="data quality rows",
        )

if negative_rows.empty and not dq_count:
    st.caption("No data-quality issues in this range.")

# --- filters, one horizontal row --------------------------------------------
f1, _sp = st.columns([1.5, 3.5])
view = f1.selectbox(
    "Show", ["Unremarked only", "High float only", "Weekend-explained only", "All"],
    help="Unremarked only is the default work queue: high-float store-days "
        "that carry no explanation at all, auto or manual.")

if view == "High float only":
    view_df = full[full["high_float"]]
elif view == "Unremarked only":
    view_df = full[(full["high_float"]) & (full["remark"] == "")]
elif view == "Weekend-explained only":
    view_df = full[full["is_auto"]]
else:
    view_df = full

view_df = view_df.sort_values("business_date", ascending=False).reset_index(drop=True)

st.caption(f"{fmt_count(len(view_df))} row(s) in this view.")


def _float_cell(row) -> str:
    # Flagged, never hidden — a negative float is a real number (catch-up or
    # misdated deposit), not an error to floor away (2026-09-15).
    text = money_table(row["float_amount"])
    return f"⚠ {text}" if row["negative_float"] else text


def _display(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "Store": frame["store"],
        "Business date": frame["business_date"].map(display_date),
        "Day": frame["day_name"],
        "Opening": frame["opening_balance"].map(money_table),
        "Cash sale": frame["cash_sale"].map(money_table),
        "Deposit": frame["cash_deposit"].map(money_table),
        "Float": frame.apply(_float_cell, axis=1),
        "Float threshold": frame["float_threshold"].map(money_table),
        "High float?": frame["high_float"].map({True: "Yes", False: ""}),
        "Days held high": frame["days_held_high"],
        "Cash received": frame["cash_received"].map(money_table),
        "Closing balance": frame["closing_balance"].map(money_table),
        "Remark": frame["remark"],
        "Remark by": frame["remark_by"],
        "Remark at": frame["remark_at"].map(
            lambda t: "" if pd.isna(t) else pd.Timestamp(t).strftime("%d %b %Y, %H:%M")),
    })


event = st.dataframe(
    _display(view_df), hide_index=True, width="stretch",
    on_select="rerun", selection_mode="single-row",
)

# --- remark editor: pick a row, then explain it -----------------------------
picked = list(event.selection.rows) if event and event.selection else []
if picked:
    row = view_df.iloc[picked[0]]
    st.markdown(f"###### {row['store']} on {display_date(row['business_date'])} "
               f"({row['day_name']}) — float {money_table(row['float_amount'])}, "
               f"threshold {money_table(row['float_threshold'])} "
               f"(closing balance {money_table(row['closing_balance'])})")

    if row["is_auto"]:
        st.caption(
            f"Currently auto-explained as **{row['remark']}** by the system. "
            "An auto-remark is never blank, but you can overwrite it with "
            "your own text — once you do, it becomes yours and the system "
            "never overwrites it again on a later run."
        )
    elif row["remark"]:
        st.caption(f"Currently remarked by **{row['remark_by']}**"
                   + (f" at {pd.Timestamp(row['remark_at']):%d %b %Y, %H:%M}"
                      if pd.notna(row["remark_at"]) else "") + ".")
    else:
        st.caption("No remark on record yet.")

    new_remark = st.text_area(
        "Remark", value="" if row["is_auto"] else row["remark"],
        key=f"float_remark_{row['store']}_{row['business_date']}")

    operator = confirm_with_operator(
        st.button("Save remark", key=f"float_save_{row['store']}_{row['business_date']}"),
        "recording this remark", key=f"float_op_{row['store']}_{row['business_date']}")
    if operator:
        try:
            ft.set_float_remark(session, row["store"], row["business_date"],
                                new_remark, operator)
            st.success("Remark saved.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    history = ft.float_remark_history(session, row["store"], row["business_date"])
    if not history.empty:
        with st.expander(f"Remark history — {fmt_count(len(history))} entr"
                         f"{'y' if len(history) == 1 else 'ies'}"):
            shown_hist = history.copy()
            shown_hist["remark_at"] = shown_hist["remark_at"].map(
                lambda t: pd.Timestamp(t).strftime("%d %b %Y, %H:%M"))
            st.dataframe(
                shown_hist.rename(columns={"remark_at": "When", "remark_by": "Who",
                                          "remark": "Remark"}),
                hide_index=True, width="stretch")

# --- bulk upload -------------------------------------------------------------
st.divider()
st.markdown("##### Bulk upload")
st.caption(
    "Explain many high-float store-days at once from a spreadsheet. Every "
    "row is validated first and **nothing is applied** until you choose — a "
    "row is rejected when its store or date is outside the loaded range, or "
    "the store-date is not actually flagged high float."
)

template = ft.upload_template()
buffer = io.BytesIO()
with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
    template.to_excel(writer, index=False, sheet_name="float_remarks")
st.download_button("Download the template", buffer.getvalue(),
                   file_name="float_remark_template.xlsx",
                   mime="application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")
st.caption(f"Columns, exactly: `{'`, `'.join(ft.UPLOAD_COLUMNS)}`.")

uploaded = st.file_uploader("The completed template",
                           type=["xlsx", "csv"], key="float_upload")

if uploaded is not None:
    try:
        frame = (pd.read_csv(uploaded, dtype=str) if uploaded.name.endswith(".csv")
                 else pd.read_excel(uploaded, dtype=str))
    except Exception as exc:  # noqa: BLE001 — show the real reason
        st.error(f"Could not read the file — {type(exc).__name__}: {exc}")
        st.stop()

    st.caption(f"{fmt_count(len(frame))} row(s) read from **{uploaded.name}**.")

    try:
        preview = ft.preview_upload(frame, grid)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    p1, p2 = st.columns(2)
    p1.metric("Would be accepted", len(preview.accepted))
    p2.metric("Rejected", len(preview.rejected))

    st.dataframe(preview.as_frame(), hide_index=True, width="stretch",
                column_config={"why": st.column_config.TextColumn(width="large")})

    if preview.rejected:
        st.error(f"**{len(preview.rejected)} row(s) rejected and listed above.** "
                "Nothing is applied until you confirm below, and rejected "
                "rows are never applied regardless.")

    operator = confirm_with_operator(
        st.button("Apply this upload", type="primary", key="float_apply",
                 disabled=not preview.accepted),
        "applying this upload", key="float_apply_op")
    if operator:
        applied, failed = ft.apply_upload(session, preview, operator)
        st.success(f"{len(applied)} remark(s) applied, {len(failed)} failed.")
        for row_outcome, why in failed:
            st.warning(f"Row {row_outcome.row_number} "
                      f"({row_outcome.store}/{row_outcome.business_date}) — {why}")
        if applied:
            st.rerun()

# --- export: the whole grid, respecting the current filter, never a page ---
st.divider()
st.markdown("##### Export")
frame_export_button(
    _display(view_df), ExportMeta(
        run_id=run.id, run_timestamp=run.started_at,
        date_from=date_from, date_to=date_to,
        filters={"store": store, "show": view},
        title="Float"),
    key="dl_float", noun="rows in this view",
)
