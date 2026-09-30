"""Summary — the daily store-wise sale and cash sheet, columns A..N in the
exact format, plus (2026-09-17) the bill/transaction match and exception
picture that used to live on four separate pages: Digital sale, Bill match,
Transactions and Exceptions. Those pages are gone — their content is here as
collapsible sections, never as tabs, so the whole picture reads top to
bottom on one page. Manual matches stays its own page: it is data entry, not
a report.

Every major section (Store-wise detail, Matched, Exceptions) carries its own
+/- toggle (2026-09-19): the header always shows the row count so the size
is visible without opening it, but a COLLAPSED section skips its own
expensive work — the grouped/detail table build, the paged bill query, the
per-row remark join — never just hiding it visually. Open/closed state
persists in plain (non-widget) session keys, the same pattern the sidebar's
own filters use, so it survives a filter change or a page switch.

The table here and the Excel export come from the same engine
(`mop_reco.store_sheet`), so the screen and the file cannot disagree.
"""

from __future__ import annotations

import json
import sys
from datetime import date as date_cls
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_common import (
    db_session,
    export_all_button,
    frame_export_button,
    latest_run,
    no_run_yet,
    page,
    sidebar,
    status_styler,
    stmt_row,
    stmt_table,
    stop_if_no_dates,
    tile,
)
from mop_reco import queries as q
from mop_reco import store_sheet as ss
from mop_reco import store_sheet_source as src
from mop_reco.export import ExportMeta
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import blank, display_date, money_cr, money_short, money_table
from mop_reco.store_sheet_export import build_workbook

page("MOP RECO_Store Wise")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)

# The run's own REQUESTED range, never guessed — a run that was only ever
# asked to reconcile July cannot have August, and the page must say so
# plainly rather than let a store-day query for August quietly return
# nothing and read as "August is all zero" (2026-09-24 — this exact
# confusion happened live: run #4 covered July only, selecting August in
# the sidebar showed a grid of Rs 0.00 Cr with no explanation why).
try:
    _requested = json.loads(run.business_dates) if run.business_dates else [None, None]
except (ValueError, TypeError):
    _requested = [None, None]
_run_from = date_cls.fromisoformat(_requested[0]) if _requested[0] else None
_run_to = date_cls.fromisoformat(_requested[1]) if _requested[1] else None
if _run_from and _run_to and (date_to < _run_from or date_from > _run_to):
    st.warning(
        f"**{date_from} to {date_to} is not covered by run #{run.id}**, which "
        f"reconciled {_run_from} to {_run_to}. This is not \"no data\" — it is "
        "\"not run yet\". Open **Run** to reconcile this period, or change the "
        "filter back to a range this run covered."
    )
    st.stop()


@st.cache_data(ttl=600, show_spinner="Reading the sheet…")
def load(run_id, lo, hi, store_filter):
    """Cached per run, range AND store (2026-09-20 — store is now part of
    the cache key, since the store filter is pushed into the SQL query
    itself rather than sliced from an already-cached whole-range frame).

    Reads `store_day_agg`/`date_total_agg` — precomputed once when the run
    itself completed (`runs.run_reconciliation()`) — via an indexed SELECT,
    joining in F (digital received) live so a manual match still shows up
    immediately with no re-run (CLAUDE.md 6.9b-ii). This used to rebuild
    the whole cash book from PTC/CMS/opening-balance/POS on every call;
    measured on one month, that cost 31-34s and 486 MB peak — see
    docs/PERF_BASELINE.md. This is the one thing that made a full
    financial year (12x this) unusable, so it is the one thing this rework
    changed on the page's own hot path.

    "Refresh" in the sidebar clears this cache (and every other page's) so
    the view re-reads the database without re-running the reconciliation.
    """
    return src.load_fast(session, run_id, lo, hi, store_filter)


shown, summary_by_date_shown = load(run.id, date_from, date_to, store)

if shown.empty:
    st.warning("Nothing loaded for this selection.")
    st.stop()

# --- controls: ALWAYS live, scoped to the CURRENTLY SHOWN sheet -----------
#
# Found 2026-09-24: `load_static()` used to be read here — it plays back a
# JSON snapshot computed ONCE, at persist time, for the RUN'S OWN ORIGINAL
# requested range — e.g. run #4 was
# persisted for July only, so its snapshot's controls are July's controls,
# forever, no matter what date range a viewer later selects in the sidebar.
# Selecting August (which run #4 never covered) still showed July's own
# "62 store-day(s) hold negative cash" banner, because nothing here ever
# re-ran the check against August's (empty) data. A manual match genuinely
# cannot move a control result — that part of the old reasoning was right —
# but the SIDEBAR FILTER changing `shown` to a completely different sheet
# absolutely can and must. `run_controls()` is a pure, cheap function over
# whatever sheet is already in memory (no query, no file read) — the export
# button below already recomputed it live for exactly this reason; the
# on-screen banner just never matched it. Recomputed the same way here now.
controls = [(c.key, c.what, c.holds, c.detail) for c in ss.run_controls(
    shown, summary_by_date_shown, None,
    dashboard_digital=float(shown["digital_sale"].sum())).controls]


def _cms_breakdown():
    """The CMS-derived notes/excluded/contribution/other_types/unresolved
    breakdown, computed FRESH every call — never a static snapshot. This is
    a whole-loaded-range breakdown by design (ZPO/ADJ.CR./ADJ.DB. are not
    store- or date-scoped), but it must reflect the CURRENT CMS data, not
    whatever happened to be true when some earlier run was persisted — the
    same staleness that hit the controls above would otherwise hit this
    too. Only called from the Data notes expander and the export button
    (both already-deliberate, occasional actions), never eagerly on every
    page load — cms_frame() re-ingests the real CMS files, which CLAUDE.md
    documents as small and fast but not literally free."""
    cms, cms_notes = src.cms_frame(session, with_notes=True)
    unresolved = pd.DataFrame()
    if not cms.empty:
        frame = cms.copy()
        frame["value"] = pd.to_numeric(frame["SLIPAMOUNT"], errors="coerce").fillna(0.0)
        unresolved_rows = frame[frame["store"].isna() | frame["store"].astype(str).eq("None")]
        if not unresolved_rows.empty:
            unresolved = (unresolved_rows.groupby(["PICKUPPOINTCODE", "TYPE"], as_index=False)
                         .agg(rows=("value", "size"), value=("value", "sum")))
    return {
        "notes": cms_notes,
        "excluded": ss.excluded_credit_types(cms),
        "contribution": ss.cms_contribution_by_type(cms),
        "other_types": ss.other_type_summary(cms),
        "unresolved_pickups": unresolved,
    }

# --- the controls — quiet when clean, loud when not (2026-09-12) ---------
#
# All 8 holding is the normal case and was noise as a banner every time; a
# failure must never be quiet, so that alone gets a red banner naming which
# control failed and by how much. The full controls table and the run's data
# notes (opening date, unmapped cost centres, grand total check) move to a
# collapsed "Data notes" expander at the BOTTOM of the page — findable, not
# in the way.
failed = [c for c in controls if not c[2]]
if failed:
    st.error(f"**{len(failed)} control(s) do not hold.** The figures below "
             "cannot be relied on until this is explained.")
    for key, what, _, detail in failed:
        st.markdown(f"- **({key})** {what} — {detail}")

# --- whole-range totals (2026-09-18) ---------------------------------------
#
# The seven-tile ribbon that used to sit here is gone (2026-09-18, on
# instruction) — replaced by the particulars matrix below. These variables
# are kept: they are not "a figure on the page" in their own right, they
# feed the existing "sum of parent rows = the whole-range totals" control
# further down (§ Store-wise detail), which still needs to compare the
# by-date table against SOMETHING independently-computed, tile or no tile.
dates = sorted(shown["date"].unique())
first = shown[shown["date"] == dates[0]]
last = shown[shown["date"] == dates[-1]]

total_sale = float(shown["total_sale"].sum())
digital_sale = float(shown["digital_sale"].sum())
cash_sale = float(shown["cash_sale"].sum())
digital_receivable = float(shown["digital_receivable"].sum())
cash_deposit = float(shown["cash_deposit"].sum())
closing_balance = float(last["closing_balance"].sum())

# There is no such thing as "excess credit" in this business — the agency
# never credits more than was deposited (2026-09-13, supersedes the two-tile
# "Cash receivable" / "Excess credited" split shipped a few hours earlier the
# same day). A store-date where K = I - J is NEGATIVE (K's polarity swapped
# 2026-09-17 when column I's own sign was corrected to positive) is a data
# artefact — Defect B: a credit posted against the wrong date, or a pickup
# code shared with another store — never a real balance. It is never shown
# as its own tile and never left inside a negative receivable; it is netted
# at the level being displayed (ss.net_cash_receivable — sum I and sum J
# FIRST, floor at zero LAST) and every row that made the floor bite is
# surfaced in the Data quality expander below instead of vanishing silently.
receivable_total = ss.net_cash_receivable(shown)

# ===========================================================================
# The particulars matrix (2026-09-18) — Sale/Collected/Receivable for Total
# sale, UPI, Card, Total digital and Total cash, plus Gift Voucher Redeemed
# / Credit Note Issued / Credit Note Received as their own Total-sale-only
# lines. See `store_sheet_source.mop_particulars_matrix()` for the full
# computation and the judgment calls it states explicitly (Gift Voucher
# self-collected on instruction; Credit Notes flow into Total sale's own
# Receivable, since neither has a "collection" event of its own and only
# Gift Voucher carried an explicit "receivable must be zero" instruction;
# open bills default to UPI, provisionally, until matched — see
# `mop_matrix.PROVISIONAL_DIGITAL_MODE` and the footnote below the matrix).
#
# **The hero band (Total sale / Collected / Receivable in one card, above
# the matrix) is REMOVED entirely (2026-09-22, on instruction) — not just
# its contents, the whole block, so the page starts with the matrix
# directly under the title.** Those three figures are not lost: they are
# the matrix's own Total sale column (Sale/Collected/Receivable rows),
# confirmed identical before deleting the band — the band was reading the
# SAME `matrix.total_sale` fields this code still computes, never a
# separate source. `_hero_cells`/`hero_html`/`st.container(key="hero-
# band")` and the band's own %-of-sale calculations are deleted outright,
# not hidden — nothing else on the page used them. `range_label` (used
# only for the band's Total-sale sub-line) is no longer imported here.
#
# **This matrix shows Rs Cr, not full rupees (2026-09-21, on
# instruction).** `formatting.money_cr()` is the one function that decides
# it: two decimals, ALWAYS crores (never switches to Lakh for a small cell,
# unlike `money_short`) so every figure in the table stays one consistent
# unit. Nowhere else on this page changed — the store-wise detail table
# (columns A-N), the cash equation strip, every export and the Float page
# all keep full rupees via `money_table`, untouched. Rounding is display
# only: every Cr figure is formatted straight from the same unrounded float
# `mop_particulars_matrix()` already computed — nothing here sums already-
# rounded Cr strings into a total.
# ===========================================================================
matrix = src.mop_particulars_matrix(session, run.id, date_from, date_to, store)

# --- the matrix, restyled as a plain report (hairlines only, no box grid) -
MATRIX_COLUMNS = ["Total sale", "UPI", "Card", "Total digital", "Total cash"]
_matrix_cols = [matrix.total_sale, matrix.upi, matrix.card,
               matrix.total_digital, matrix.total_cash]


def _adjustment_row(label: str, value: float, first: bool = False, colour: str = None) -> str:
    cls = "mop-matrix-row mop-matrix-row--adjustment"
    if first:
        cls += " mop-matrix-row--first-adjustment"
    style = f" style='color:{colour}'" if colour else ""
    return (
        f"<tr class='{cls}'>"
        f"<td class='mop-matrix-rowlabel'>{label}</td>"
        f"<td class='mop-matrix-cell'>"
        f"<div class='mop-matrix-figure'{style}>{money_cr(value)}</div></td>"
        f"<td colspan='4'></td></tr>"
    )


_row_sale = "<tr class='mop-matrix-row mop-matrix-row--sale'><td class='mop-matrix-rowlabel'>Sale</td>"
_row_collected = "<tr class='mop-matrix-row'><td class='mop-matrix-rowlabel'>Collected</td>"
_row_receivable = "<tr class='mop-matrix-row'><td class='mop-matrix-rowlabel'>Receivable</td>"
for i, col in enumerate(_matrix_cols):
    pct_html = (f"<div class='mop-matrix-pct'>{col.pct:.1f}%</div>"
               if col.pct is not None else "")
    _row_sale += (f"<td class='mop-matrix-cell'>"
                 f"<div class='mop-matrix-figure mop-matrix-figure--strong'>"
                 f"{money_cr(col.sale)}</div>{pct_html}</td>")
    _row_collected += (f"<td class='mop-matrix-cell'>"
                       f"<div class='mop-matrix-figure'>{money_cr(col.collected)}</div></td>")
    warn = abs(col.receivable) > 0.005
    warn_cls = " mop-matrix-cell--warn" if warn else ""
    _row_receivable += (f"<td class='mop-matrix-cell{warn_cls}'>"
                        f"<div class='mop-matrix-figure'>{money_cr(col.receivable)}</div></td>")
_row_sale += "</tr>"
_row_collected += "</tr>"
_row_receivable += "</tr>"

matrix_html = f"""
<div class='mop-matrix-scroll'>
<table class='mop-matrix'>
  <thead>
    <tr class='mop-matrix-grouprow'>
      <th></th><th></th>
      <th colspan='3' class='mop-matrix-group'>DIGITAL</th>
      <th class='mop-matrix-group'>CASH</th>
    </tr>
    <tr class='mop-matrix-headrow'>
      <th class='mop-matrix-rowlabel-head'></th>
      <th class='mop-matrix-colhead'>Total sale</th>
      <th class='mop-matrix-colhead'>UPI</th>
      <th class='mop-matrix-colhead'>Card</th>
      <th class='mop-matrix-colhead'>Total digital</th>
      <th class='mop-matrix-colhead'>Total cash</th>
    </tr>
  </thead>
  <tbody>
    {_row_sale}
    {_row_collected}
    {_row_receivable}
    <tr class='mop-matrix-spacer'><td colspan='6'></td></tr>
    {_adjustment_row("Gift Voucher Redeemed", matrix.gift_voucher_redeemed, first=True)}
    {_adjustment_row("Credit Note Issued", matrix.credit_note_issued, colour="var(--bad)")}
    {_adjustment_row("Credit Note Received", matrix.credit_note_received)}
  </tbody>
</table>
</div>
"""

with st.container(key="matrix-card"):
    st.markdown(matrix_html, unsafe_allow_html=True)

# The provisional-UPI footnote sentence is removed from the page face
# (2026-09-28, on instruction — display only, the rule itself is untouched:
# an open bill still counts as UPI, inside Total digital and Total sale,
# until matched). The count and value stay traceable in the Data notes
# expander at the bottom of the page instead of sitting on every load.

_matrix_failed = [c for c in matrix.controls if not c[1]]
if _matrix_failed:
    st.error(f"**{len(_matrix_failed)} particulars-matrix control(s) do not "
             "hold.** The matrix above cannot be relied on until this is "
             "explained.")
    for label, _holds, detail in _matrix_failed:
        st.markdown(f"- **{label}** — {detail}")

# ===========================================================================
# Section open/closed state — plain session keys (2026-09-19), not widget
# keys, so a filter change or a page switch never resets which sections are
# open. Default: Exceptions open, everything else closed.
# ===========================================================================
SECTION_DEFAULTS = {"store_detail": False, "matched": False, "exceptions": True}


def _section_key(name: str) -> str:
    return f"sm_open_{name}"


def _section_open(name: str) -> bool:
    key = _section_key(name)
    if key not in st.session_state:
        st.session_state[key] = SECTION_DEFAULTS[name]
    return st.session_state[key]


def _section_toggle(name: str, title: str, meta: str) -> bool:
    """One header row: a compact +/- button, the title, and the row
    count/value — shown even while collapsed, so the size is visible
    without opening it.
    """
    is_open = _section_open(name)
    c1, c2 = st.columns([0.05, 0.95])
    if c1.button("−" if is_open else "+", key=f"toggle_{name}",
                help=("Collapse " if is_open else "Expand ") + title):
        st.session_state[_section_key(name)] = not is_open
        st.rerun()
    c2.markdown(f"**{title}**  \n{meta}")
    return _section_open(name)


# --- Expand all / Collapse all — page-level, drive the three SECTION
# toggles only (2026-09-20: the per-date table expansion got its own local
# Expand all/Collapse all next to the table's own heading, below, since it
# is a different control over a different thing). ------------------------
all_dates = [d for d in summary_by_date_shown["date"] if d != "TOTAL"]
OPEN_DATES_KEY = "sm_open_dates"
st.session_state.setdefault(OPEN_DATES_KEY, set())

ea, ca, _sp = st.columns([1, 1, 4])
if ea.button("Expand all", key="ss_expand_all"):
    for name in SECTION_DEFAULTS:
        st.session_state[_section_key(name)] = True
    st.rerun()
if ca.button("Collapse all", key="ss_collapse_all"):
    for name in SECTION_DEFAULTS:
        st.session_state[_section_key(name)] = False
    st.rerun()

# --- the export -----------------------------------------------------------
#
# The "Data quality" section (credits exceeding deposits — Defect B) that
# used to live here is REMOVED from Summary (2026-09-20, on instruction) —
# never from the underlying data: `src.defect_b_summary()` and
# `src.data_quality_rows()` are untouched and now read from the Float page
# instead, merged under Float's own existing deposit-vs-credit data quality
# heading (see app/pages/1_Float.py) so there is exactly one place to look.
if st.button("Build the Excel sheet", type="primary"):
    with st.spinner("Writing the workbook…"):
        shown_summary = ss.summarise_by_date(shown)
        shown_by_store = ss.summarise_by_store(shown)
        shown_grand_total = ss.grand_total_row(shown)
        # other_types (ADJ.CR./ADJ.DB./ZPO) is a whole-range CMS breakdown,
        # like contribution and excluded — not store-scoped, so it is passed
        # through unchanged regardless of the store filter. Computed FRESH
        # here (_cms_breakdown(), 2026-09-24) rather than read from a static
        # per-run snapshot, for the same reason the controls above are now
        # live — an export built while viewing August must not silently
        # carry July's own CMS breakdown.
        bundle_checks = ss.run_controls(
            shown, shown_summary, None,
            dashboard_digital=float(shown["digital_sale"].sum()))
        _cms = _cms_breakdown()
        data = build_workbook(
            shown, shown_summary, _cms["excluded"],
            run_id=run.id, run_timestamp=run.started_at,
            date_from=date_from, date_to=date_to, checks=bundle_checks,
            summary_by_store=shown_by_store, grand_total=shown_grand_total,
            contribution=_cms["contribution"], other_types=_cms["other_types"])
    st.session_state["_store_sheet_xlsx"] = data

if st.session_state.get("_store_sheet_xlsx"):
    st.download_button(
        f"Download the sheet ({fmt_count(len(shown))} detail rows)",
        st.session_state["_store_sheet_xlsx"],
        file_name=f"store_wise_sale_and_cash_{date_from}_{date_to}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch")

st.divider()


def display(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({"A Date": frame["date"].map(display_date),
                        "B Store": frame["store"]})
    for letter, name, label in ss.COLUMNS[2:]:
        if name == "cash_receivable":
            # Never shown negative anywhere on this page (2026-09-13; K's
            # polarity swapped 2026-09-17 along with column I's sign). A
            # negative K (credit exceeds deposit, at whatever grain this row
            # is — a single store-date, a consolidated ALL-STORES date row,
            # or the grand total) is a Defect B data artefact, not a
            # negative receivable — it floors to zero here and is named in
            # the Data quality expander instead, never displayed raw.
            out[f"{letter} {label}"] = frame[name].map(
                lambda k: money_table(max(0.0, float(k))))
        else:
            out[f"{letter} {label}"] = frame[name].map(money_table)
    return out


# ===========================================================================
# Section: Store-wise detail — collapsed by default. ONE parent table
# (2026-09-20, revised same day) — the by-date consolidated table and the
# separate flat store-wise detail table are still merged into one concept,
# but a date's own store rows are NEVER spliced into the same dataframe any
# more. The earlier "+"/"−" glyph prefixed onto the Date cell rendered as a
# floating box that overlapped the header and the first data row in the
# browser — a real rendering glitch, not a cosmetic quibble — so it is gone
# entirely, on instruction: "working and plain beats clever and broken."
# The parent (ALL STORES) rows are the table; a store's own rows for an
# open date render in a plain st.expander beneath the table instead —
# Streamlit's dataframe grid has no clean way to expand a row in place at
# this scale, so this does not try to fake one with a styled cell.
# ===========================================================================
# --- Part 2 (2026-09-18, rebuilt as an equation 2026-09-20): the store
# cash figures read left to right as a calculation ending in a result —
# Opening + Cash sale − Cash deposit + Expense/income = Closing — rather
# than five loose tiles. The header ("Store-wise detail" / row count / its
# +/- toggle) and the equation now share ONE card
# (`st.container(key="store-cash-card")`, styled via `.st-key-store-cash-
# card` in theme.css), with a 1px `--border` hairline between them, so
# there is never a grey page gap between a header and a separate white
# band. H, E, I, L, M read straight from the precomputed store_day_agg
# (never re-derived), so this can never disagree with the detail table
# below it. See `store_sheet_source.store_cash_summary()`'s own docstring
# for exactly what "Opening" means here (the FIRST filtered date's own H,
# summed across stores — never summed across dates, which would double-
# count a carried-forward balance) and what the mismatch check compares
# against (the LAST filtered date's own M, the same grain the old Closing
# balance tile always used). Shown regardless of the section's own open/
# closed state — cheap enough (a handful of indexed SUMs) that gating it
# behind "open" would save nothing worth the inconsistency.
# _stmt_row/_stmt_table moved to dashboard_common.py (2026-09-30) as
# stmt_row/stmt_table — CMS Reco reuses them verbatim ("same statement-style
# formatting as the cash block"), so the pattern lives in one shared place
# rather than a second, subtly different copy. Aliased here, unchanged in
# behaviour, so nothing below this line needed to change.
_stmt_row = stmt_row
_stmt_table = stmt_table


def _store_cash_card():
    cash = src.store_cash_summary(session, run.id, date_from, date_to, store)
    mismatch = not cash["matches"]
    period = f"{display_date(date_from)} to {display_date(date_to)}"

    # Expense & income is the one figure whose SIGN carries real meaning
    # (net expense vs. net income, §6.6) — the only row whose label is
    # chosen rather than fixed. Arithmetic is unchanged; only which word
    # ("Add"/"Less") appears flips with the sign.
    expense_income = cash["cash_expense_income"]
    expense_label = ("Less: Net cash expense" if expense_income < 0
                     else "Add: Net cash income")

    left_rows = (
        _stmt_row("Opening balance", cash["opening_balance"])
        + _stmt_row("Add: Cash sale", cash["cash_sale"])
        + _stmt_row("Less: Cash deposit", cash["cash_deposit"])
        + _stmt_row(expense_label, expense_income)
    )
    result_bad = mismatch or cash["closing_at_store"] < 0
    left_html = _stmt_table(
        "CASH POSITION", period, left_rows,
        "Closing balance at store", cash["closing_at_store"], result_bad=result_bad,
    )

    receivable_mismatch = not cash["matches_receivable"]
    receivable_nonzero = abs(cash["cash_receivable_net"]) > 0.005
    right_rows = (
        _stmt_row("Cash deposited", cash["cash_deposit"])
        + _stmt_row("Less: Cash credited by bank", cash["cash_received"])
    )
    right_html = _stmt_table(
        "CASH WITH AGENCY", period, right_rows,
        "Cash receivable", cash["cash_receivable_net"],
        result_bad=receivable_mismatch, result_warn=receivable_nonzero,
    )

    st.markdown(
        f"<div class='mop-stmt-grid'>{left_html}{right_html}</div>",
        unsafe_allow_html=True,
    )

    # UNCHANGED (2026-09-28 rebuild is display-only): both assertions read
    # the exact same store_day_agg-derived figures as before — Closing at
    # store = H + E - I + L = sum of column M over the filter; Cash
    # receivable = sum(I) - sum(J), netted then floored at zero. A
    # disagreement still shows in --bad on the total row above AND as an
    # explicit error naming the difference, never silently.
    if mismatch:
        st.error(
            f"**MISMATCH** — Closing balance at store ({money_table(cash['closing_at_store'])}) "
            f"≠ sum of column M for the last filtered date "
            f"({money_table(cash['check_closing'])})."
        )
    if receivable_mismatch:
        st.error(
            f"**MISMATCH** — Cash receivable "
            f"({money_table(cash['cash_receivable_net'])}) does not agree with "
            f"the store-day aggregate's own I − J for this filter "
            f"(sum of column K reads {money_table(cash['check_receivable'])})."
        )


with st.container(key="store-cash-card"):
    store_detail_open = _section_toggle(
        "store_detail", "Store-wise detail", f"{fmt_count(len(shown))} rows")
    st.markdown("<hr class='mop-cash-hr'>", unsafe_allow_html=True)
    _store_cash_card()

if store_detail_open:
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

    # Parent (ALL STORES) rows only — build_grouped_table with an empty
    # expansion list never splices any store row into this table.
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
                display(block), hide_index=True, width="stretch",
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
                    st.markdown(f"**D Digital sale** {money_table(row['digital_sale'])} · "
                                f"**F received** {money_table(row['digital_received'])} · "
                                f"**G receivable** {money_table(row['digital_receivable'])}")
                    if records.digital_bills is not None and not records.digital_bills.empty:
                        st.dataframe(records.digital_bills, hide_index=True, width="stretch")
                    else:
                        st.caption("No digital bills that day.")

                    st.markdown(f"**E Cash sale** {money_table(row['cash_sale'])}")
                    if records.cash_bills is not None and not records.cash_bills.empty:
                        st.caption(f"{fmt_count(len(records.cash_bills))} cash bills — "
                                   "a row with no bill number is excluded, it is not a sale.")
                        st.dataframe(records.cash_bills.head(200), hide_index=True,
                                     width="stretch")
                    else:
                        st.caption("No cash bills that day.")

                with b:
                    st.markdown(f"**I Cash deposit** {money_table(row['cash_deposit'])} · "
                                f"**L expense/income** {money_table(row['cash_expense_income'])}")
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
                    st.markdown(f"**J Cash received** {money_table(row['cash_received'])} · "
                                f"**K receivable** {money_table(row_receivable)}")
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

# ===========================================================================
# Bills and transactions — the two-sided controls made visible (2026-09-19,
# extended 2026-09-20 with a third TRANSACTIONS line for customer refunds).
# Every "Total" row is the ARITHMETIC SUM of the lines above it; each is
# also checked against its own independently-queried total, and a
# disagreement shows as MISMATCH in red rather than a total that merely
# looks right. Sits below "Store-wise sale and cash" and above Exceptions,
# on instruction. Nothing here is cached behind a re-run: both summaries
# query fresh every render, so a manual match or a refund shows up
# immediately with no re-run.
# ===========================================================================
bill_summary = q.bill_match_summary(session, run.id, date_from, date_to, store)
txn_summary = q.transaction_resolution_summary(session, date_from, date_to, store)

bills_closed_n, bills_closed_v = bill_summary["closed"]["count"], bill_summary["closed"]["value"]
bills_open_n, bills_open_v = bill_summary["open"]["count"], bill_summary["open"]["value"]
bills_sum_n, bills_sum_v = bills_closed_n + bills_open_n, bills_closed_v + bills_open_v
bills_ok = (bills_sum_n == bill_summary["total"]["count"]
           and abs(bills_sum_v - bill_summary["total"]["value"]) <= 0.01)

st.markdown("##### Bills and transactions")
bcol, tcol = st.columns(2)
with bcol:
    st.markdown("**BILLS**")
    st.dataframe(pd.DataFrame([
        {"": "Closed (matched)", "Count": fmt_count(bills_closed_n),
         "Value (Rs)": f"Rs {money_table(bills_closed_v)}"},
        {"": "Open (unmatched)", "Count": fmt_count(bills_open_n),
         "Value (Rs)": f"Rs {money_table(bills_open_v)}"},
        {"": "Total", "Count": fmt_count(bills_sum_n),
         "Value (Rs)": f"Rs {money_table(bills_sum_v)}"},
    ]), hide_index=True, width="stretch")
    if not bills_ok:
        st.error(
            f"**MISMATCH** — closed + open = {fmt_count(bills_sum_n)} "
            f"(Rs {money_table(bills_sum_v)}) but the independently-queried "
            f"total is {fmt_count(bill_summary['total']['count'])} "
            f"(Rs {money_table(bill_summary['total']['value'])})."
        )
with tcol:
    st.markdown("**TRANSACTIONS**")
    st.dataframe(pd.DataFrame([
        {"": "Mapped to bill", "Count": fmt_count(txn_summary.mapped_n),
         "Value (Rs)": f"Rs {money_table(txn_summary.mapped_v)}"},
        {"": "Refunded to customer", "Count": fmt_count(txn_summary.refunded_n),
         "Value (Rs)": f"Rs {money_table(txn_summary.refunded_v)}"},
        {"": "Unmapped (open)", "Count": fmt_count(txn_summary.unmapped_n),
         "Value (Rs)": f"Rs {money_table(txn_summary.unmapped_v)}"},
        {"": "Total", "Count": fmt_count(txn_summary.total_n),
         "Value (Rs)": f"Rs {money_table(txn_summary.mapped_v + txn_summary.refunded_v + txn_summary.unmapped_v)}"},
    ]), hide_index=True, width="stretch")
    st.caption(
        f"Of the refunded total: pending {fmt_count(txn_summary.refund_pending_n)} "
        f"({money_short(txn_summary.refund_pending_v)}) · released "
        f"{fmt_count(txn_summary.refund_released_n)} "
        f"({money_short(txn_summary.refund_released_v)}) — never merged. "
        "A refund is never a bill match and never counted as revenue; see "
        "Manual matches → By transaction → Refund upload."
    )
    if not txn_summary.ok:
        st.error(
            f"**MISMATCH** — mapped + refunded + unmapped differs from the "
            f"independently-queried total by {txn_summary.difference}."
        )

st.divider()

# ===========================================================================
# Section: Matched — collapsed by default. Matched BILLS only (2026-09-19 —
# the matched-transactions table is gone from here; it never belonged next
# to a bill-side table, and "Exception transactions" on the Exceptions
# section below is untouched, its own count/value/export, never merged with
# this one). Reformatted for readability: a fixed, sortable, frozen-header
# table with the transaction id shown in full, never truncated.
# ===========================================================================
matched_open = _section_toggle(
    "matched", "Matched", f"{fmt_count(bills_closed_n)} bill(s)")

if matched_open:
    st.caption("A bill is matched by the engine or by a person — either way "
              "it leaves the exception list the instant it happens.")

    MB_PAGE = 50
    mb_key = "sm_matched_bills_offset"
    st.session_state.setdefault(mb_key, 0)
    mb_fingerprint = (str(date_from), str(date_to), store)
    if st.session_state.get("sm_matched_bills_fp") != mb_fingerprint:
        st.session_state["sm_matched_bills_fp"] = mb_fingerprint
        st.session_state[mb_key] = 0

    mb_total = bills_closed_n
    if mb_total == 0:
        st.caption("No matched bills in this range.")
    else:
        mb_offset = min(st.session_state[mb_key],
                        max(0, (mb_total - 1) // MB_PAGE * MB_PAGE))
        mb_df, _ = q.matched_bills_page(session, run.id, date_from, date_to, store,
                                        limit=MB_PAGE, offset=mb_offset)
        mp1, mp2, mp3 = st.columns([1, 1, 3])
        if mp1.button("Previous", disabled=mb_offset == 0, key="mb_prev"):
            st.session_state[mb_key] = max(0, mb_offset - MB_PAGE)
            st.rerun()
        if mp2.button("Next", disabled=mb_offset + MB_PAGE >= mb_total, key="mb_next"):
            st.session_state[mb_key] = mb_offset + MB_PAGE
            st.rerun()
        mp3.markdown(
            f"<div class='mop-page-info'>Showing "
            f"<b>{fmt_count(mb_offset + 1)}-{fmt_count(min(mb_offset + MB_PAGE, mb_total))}</b> "
            f"of <b>{fmt_count(mb_total)}</b></div>", unsafe_allow_html=True)

        # Date/Txn date as DD/MM/YYYY; Amount Indian-grouped; Lag flagged
        # with a "⚠" prefix when non-zero (the same never-hide-a-flag
        # pattern the Float page uses for a negative float, rather than a
        # per-cell background colour Streamlit's dataframe grid has no
        # supported hook for). Sortable on every column and frozen-header
        # scrolling are the grid's own default behaviour — nothing extra to
        # configure for either.
        matched_display = pd.DataFrame({
            "Date": mb_df["date"].map(display_date),
            "Store": mb_df["store"],
            "Bill no": mb_df["bill_no"],
            "MOP": mb_df["mop"],
            "Amount": mb_df["amount"].map(money_table),
            "Gateway": mb_df["gateway"],
            "Transaction id": mb_df["transaction_id"],
            "Txn date": mb_df["txn_date"].map(display_date),
            "Lag": mb_df["lag"].map(
                lambda n: "0" if n == 0 else f"⚠ {n:+d}"),
            "Match source": mb_df["match_source"],
        })
        st.dataframe(
            matched_display, hide_index=True, width="stretch",
            column_config={
                "Amount": st.column_config.TextColumn("Amount", width="small"),
                "Transaction id": st.column_config.TextColumn(
                    "Transaction id", width="large",
                    help="Shown in full — never truncated."),
            },
        )

    export_all_button(
        lambda progress: q.matched_bills_export(
            session, run.id, date_from, date_to, store, progress=progress),
        mb_total,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                  date_from=date_from, date_to=date_to,
                  filters={"store": store}, value_column="amount",
                  title="Matched bills"),
        key="dl_matched_bills", noun="matched bills",
    )

# ===========================================================================
# Section: Exceptions — expanded by default (2026-09-17). Two tables, never
# merged into one count: bills with no matching transaction, and
# transactions with no matching bill. Plain tables, no drill-through — the
# rich grouped/detail Exceptions page is gone; this is the work queue.
# ===========================================================================
BILL_EXCEPTION_TYPES = q.NOT_RECEIVED_TYPES
TXN_EXCEPTION_TYPES = q.RECEIVED_NOT_BILLED_TYPES + ("original_txn_not_found", "store_ambiguous")

BILL_EXC_COLUMNS = ["date", "time", "store", "bill_no", "mop", "amount",
                    "created_by", "status", "remark", "remark_by"]
TXN_EXC_COLUMNS = ["gateway", "transaction_id", "date", "time", "store",
                   "amount", "payment_mode", "status", "remark", "remark_by"]

# Narrower id/remark columns (2026-09-29) — with bill_time/created_by/
# transaction_time added, 10 columns no longer fit this grid's own
# container at 1366px without its internal horizontal scrollbar
# appearing. Rather than drop a column, the two free-text columns that
# never need much width to stay readable (bill_no/transaction_id are
# short codes, remark/remark_by are usually empty or a short phrase) give
# theirs back — every column stays, none gets dropped.
BILL_EXC_COLUMN_CONFIG = {
    "bill_no": st.column_config.TextColumn(width="small"),
    "remark": st.column_config.TextColumn(width="small"),
    "remark_by": st.column_config.TextColumn(width="small"),
}
TXN_EXC_COLUMN_CONFIG = {
    "transaction_id": st.column_config.TextColumn(width="small"),
    "remark": st.column_config.TextColumn(width="small"),
    "remark_by": st.column_config.TextColumn(width="small"),
}


def _with_remarks(session, df, key_col="exception_key"):
    if df.empty:
        return df
    remarks = q.latest_remarks(session, df[key_col].tolist())
    df = df.copy()
    df["remark"] = df[key_col].map(lambda k: remarks.get(k, ("", ""))[0])
    df["remark_by"] = df[key_col].map(lambda k: remarks.get(k, ("", ""))[1] or "")
    return df


def _bill_exception_frame(all_df, statuses):
    if all_df.empty:
        return pd.DataFrame(columns=BILL_EXC_COLUMNS)
    df = all_df[all_df["type"].isin(BILL_EXCEPTION_TYPES)
               & all_df["status"].isin(statuses)].copy()
    if df.empty:
        return pd.DataFrame(columns=BILL_EXC_COLUMNS)
    df = _with_remarks(session, df)
    df["bill_no"] = df["detail"].map(lambda d: d.get("bill_no") or "—")
    df["mop"] = df["detail"].map(lambda d: d.get("mop") or "—")
    # BILL_TIME / CREATEBY (2026-09-29) — real R1BillLine columns, joined by
    # the bill's own deterministic key (business_date, store, bill_no, mop).
    # Exception_ itself never carries these two; see
    # queries.unmatched_bill_facts()'s own docstring for why the join lives
    # here rather than duplicating them into `detail`. Genuinely blank,
    # never "nan"/"None", on the rare row a source file predates these two
    # columns for.
    bill_facts = q.unmatched_bill_facts(session, run.id)
    facts = df.apply(
        lambda r: bill_facts.get((str(r["date"]), str(r["store"]), str(r["bill_no"]), str(r["mop"]))),
        axis=1)
    df["time"] = facts.map(lambda f: blank(f[0]) if f else "")
    df["created_by"] = facts.map(lambda f: blank(f[1]) if f else "")
    return df[BILL_EXC_COLUMNS].sort_values("amount", ascending=False).reset_index(drop=True)


def _txn_exception_frame(all_df, statuses):
    if all_df.empty:
        return pd.DataFrame(columns=TXN_EXC_COLUMNS)
    df = all_df[all_df["type"].isin(TXN_EXCEPTION_TYPES)
               & all_df["status"].isin(statuses)].copy()
    if df.empty:
        return pd.DataFrame(columns=TXN_EXC_COLUMNS)
    df = _with_remarks(session, df)
    df["gateway"] = df["detail"].map(lambda d: (d.get("gateway") or "—"))
    df["transaction_id"] = df["detail"].map(
        lambda d: d.get("transaction_id") or d.get("bill_no") or "—")
    df["payment_mode"] = df["detail"].map(lambda d: d.get("payment_mode") or "—")
    # transaction_time (2026-09-29) — a real Exception_ column, not inside
    # `detail`. Genuinely blank for Axis (its own TRAN_TIME field is a
    # constant placeholder, never a real time; see
    # r1_bill_txn.build_gateway_pool) and for any row whose time failed to
    # parse. Never "nan"/"None".
    df["time"] = df["transaction_time"].map(blank)
    return df[TXN_EXC_COLUMNS].sort_values("amount", ascending=False).reset_index(drop=True)


EXCEPTION_STATUS_TINT = {"open": "warn", "under_investigation": "accent",
                         "closed": "good", "auto_resolved": "good"}


def _display_exceptions(df):
    if df.empty:
        return df
    out = df.copy()
    out["amount"] = out["amount"].map(money_table)
    if "date" in out.columns:
        out["date"] = out["date"].map(display_date)
    if "status" in out.columns:
        return status_styler(out, {"status": EXCEPTION_STATUS_TINT})
    return out


# The header count uses a cheap COUNT+SUM aggregate (2026-09-19), never the
# full per-row build `exception_rows()` does (JSON-parsed detail, severity,
# age for every row, then the bulk remarks join) — that only happens once
# the section is actually open, below.
_preview_bill_n, _preview_bill_v, _preview_txn_n, _preview_txn_v = q.exception_counts(
    session, date_from, date_to, store, BILL_EXCEPTION_TYPES, TXN_EXCEPTION_TYPES,
    statuses=("open",))

exceptions_open = _section_toggle(
    "exceptions", "Exceptions",
    f"{fmt_count(_preview_bill_n)} bill(s), "
    f"{fmt_count(_preview_txn_n)} transaction(s) open")

if exceptions_open:
    scope = st.selectbox("Show", ["Open", "Under investigation"], key="sm_exc_scope")
    status_wanted = ("open",) if scope == "Open" else ("under_investigation",)

    all_exc = q.exception_rows(
        session, date_from, date_to, store, open_only=False,
        types=BILL_EXCEPTION_TYPES + TXN_EXCEPTION_TYPES)

    bill_exc = _bill_exception_frame(all_exc, status_wanted)
    txn_exc = _txn_exception_frame(all_exc, status_wanted)

    st.markdown(f"**Exception bills** {fmt_count(len(bill_exc))} bills · "
               f"{money_short(float(bill_exc['amount'].sum()) if not bill_exc.empty else 0.0)}")
    st.caption("Bills with no matching transaction.")
    if bill_exc.empty:
        st.caption("Nothing in this view.")
    else:
        st.dataframe(_display_exceptions(bill_exc), hide_index=True, width="stretch",
                    column_config=BILL_EXC_COLUMN_CONFIG)
    frame_export_button(
        bill_exc,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                  date_from=date_from, date_to=date_to,
                  filters={"store": store, "show": scope}, value_column="amount",
                  title="Exception bills"),
        key="dl_exc_bills", noun="exception bills",
    )

    st.divider()

    st.markdown(f"**Exception transactions** {fmt_count(len(txn_exc))} txns · "
               f"{money_short(float(txn_exc['amount'].sum()) if not txn_exc.empty else 0.0)}")
    st.caption("Transactions with no matching bill.")
    if txn_exc.empty:
        st.caption("Nothing in this view.")
    else:
        st.dataframe(_display_exceptions(txn_exc), hide_index=True, width="stretch",
                    column_config=TXN_EXC_COLUMN_CONFIG)
    if not txn_exc.empty and (txn_exc["gateway"] == "axis").any():
        st.caption("Axis does not supply a transaction time.")
    frame_export_button(
        txn_exc,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                  date_from=date_from, date_to=date_to,
                  filters={"store": store, "show": scope}, value_column="amount",
                  title="Exception transactions"),
        key="dl_exc_txns", noun="exception transactions",
    )

    # --- closed and auto-resolved — never made unreachable ------------------
    closed_statuses = ("closed", "auto_resolved")
    closed_bills = _bill_exception_frame(all_exc, closed_statuses)
    closed_txns = _txn_exception_frame(all_exc, closed_statuses)
    st.divider()
    with st.expander(f"Closed and auto-resolved — {fmt_count(len(closed_bills))} bill(s), "
                     f"{fmt_count(len(closed_txns))} transaction(s)"):
        st.markdown(f"**Exception bills** {fmt_count(len(closed_bills))} · "
                   f"{money_short(float(closed_bills['amount'].sum()) if not closed_bills.empty else 0.0)}")
        if closed_bills.empty:
            st.caption("Nothing closed or auto-resolved in this range yet.")
        else:
            st.dataframe(_display_exceptions(closed_bills), hide_index=True, width="stretch",
                        column_config=BILL_EXC_COLUMN_CONFIG)
        frame_export_button(
            closed_bills,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store}, value_column="amount",
                      title="Closed exception bills"),
            key="dl_exc_bills_closed", noun="closed exception bills",
        )

        st.markdown(f"**Exception transactions** {fmt_count(len(closed_txns))} · "
                   f"{money_short(float(closed_txns['amount'].sum()) if not closed_txns.empty else 0.0)}")
        if closed_txns.empty:
            st.caption("Nothing closed or auto-resolved in this range yet.")
        else:
            st.dataframe(_display_exceptions(closed_txns), hide_index=True, width="stretch",
                        column_config=TXN_EXC_COLUMN_CONFIG)
        frame_export_button(
            closed_txns,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store}, value_column="amount",
                      title="Closed exception transactions"),
            key="dl_exc_txns_closed", noun="closed exception transactions",
        )

# --- data notes, collapsed, at the bottom (2026-09-12) --------------------
#
# Everything here used to sit at the TOP unconditionally — the controls
# table and every run note, whether or not anything needed attention. A
# failing control now gets its own loud banner above; this is for when
# someone asks "where did H come from" or "which cost centres didn't map",
# findable but out of the way otherwise. The pickup-code-resolution warning
# (moved here 2026-09-17, alongside the Column J / ZPO tables it used to sit
# next to being removed as part of the same page consolidation) stays
# visible rather than being dropped, per rule 5.
with st.expander("Data notes"):
    _cms = _cms_breakdown()
    unresolved = _cms["unresolved_pickups"]
    st.markdown(f"**The {len(controls)} controls in full**")
    st.dataframe(pd.DataFrame([
        {"": key, "Control": what, "Result": "HOLDS" if holds else "FAILS",
         "Detail": detail} for key, what, holds, detail in controls
    ]), hide_index=True, width="stretch",
        column_config={"Detail": st.column_config.TextColumn(width="large")})
    for note in _cms["notes"]:
        st.caption(note)

    if matrix.provisional_upi_bills > 0:
        st.markdown("**Provisional UPI**")
        st.caption(
            f"{money_table(matrix.provisional_upi_amount)} of UPI sale "
            f"({fmt_count(matrix.provisional_upi_bills)} open bills) is "
            "provisional — these bills have no matched transaction yet and "
            "reclassify on match. Already counted inside UPI, Total digital "
            "and Total sale above; this line only makes the count traceable, "
            "it changes nothing."
        )

    if unresolved is not None and not unresolved.empty:
        st.markdown("**Pickup codes that do not resolve to a store**")
        st.caption("Reported by code, never guessed (rule 5). ZPO rows carry "
                  "a single pseudo pickup code and no deposit date, so they "
                  "can be attributed neither to a store nor to a business "
                  "date — consistent with §6.7 excluding them from credits.")
        st.dataframe(unresolved.assign(
            value=unresolved["value"].map(money_table)), hide_index=True,
            width="stretch")
