"""The console's chrome: which filters are global, how they persist, and where
the operator is asked.

Streamlit widgets need a script run to exercise, so what is checked here is the
contract every page must obey — read statically from the page sources — plus the
pure helpers. That is deliberately narrower than "the UI works", and it is the
part that regressed before: a page importing a helper that no longer exists
crashes on open, and Upload did exactly that.
"""

from __future__ import annotations

import re

import ast
import sys
from datetime import date
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

PAGES = [APP / "Summary.py"] + sorted((APP / "pages").glob("*.py"))
COMMON = APP / "dashboard_common.py"


def module_names(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))}


def imported_from_common(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dashboard_common":
            names |= {alias.name for alias in node.names}
    return names


# ---------------------------------------------------------------------------
# No default date (2026-09-11) — rule 8, no hardcoded dates anywhere
# ---------------------------------------------------------------------------

def test_there_is_no_default_range_helper_any_more():
    """Replaced outright, not wrapped: From/To now open empty and "empty"
    resolves to every loaded date inside sidebar() itself, rather than being
    pre-seeded with the latest reconciled date before the widgets render."""
    names = module_names(COMMON)
    assert "_default_range" not in names
    assert "_preset_range" not in names


def test_the_date_widgets_are_seeded_from_get_not_a_forced_default():
    """`.get(K_DATE_FROM)` returns None until the operator picks a date,
    which is what makes `st.date_input(value=None)` render empty. Seeding
    from the plain key directly (`st.session_state[K_DATE_FROM]`) would
    KeyError the first time the page is opened, since nothing has set it."""
    source = COMMON.read_text(encoding="utf-8")
    sidebar_src = source[source.index("def sidebar("):source.index("def _operator_footer(")]
    assert 'value=st.session_state.get(K_DATE_FROM)' in sidebar_src
    assert 'value=st.session_state.get(K_DATE_TO)' in sidebar_src


def test_empty_dates_resolve_to_the_full_loaded_range():
    """The widgets stay visibly empty, but sidebar()'s return value always
    carries concrete dates — every caller queries with real dates, it is
    only the ON-SCREEN control that shows nothing until the operator types
    one."""
    source = COMMON.read_text(encoding="utf-8")
    tail = source[source.index("date_from = st.session_state.get(K_DATE_FROM)"):]
    assert "date_from = st.session_state.get(K_DATE_FROM) or first" in tail
    assert "date_to = st.session_state.get(K_DATE_TO) or last" in tail


# ---------------------------------------------------------------------------
# Clear filters and Refresh (2026-09-11)
# ---------------------------------------------------------------------------

def test_clear_filters_resets_dates_and_store_to_empty():
    """The actual reset lives at the TOP of sidebar(), before its widgets are
    instantiated — Streamlit refuses `st.session_state[key] = ...` for a
    widget key once that widget has been created in the current run. The
    button itself only sets a pending flag and reruns; see the next test."""
    source = COMMON.read_text(encoding="utf-8")
    sidebar_src = source[source.index("def sidebar("):source.index("def _operator_footer(")]
    assert '"_pending_clear_filters"' in sidebar_src
    pending_block = sidebar_src[:sidebar_src.index("dates = loaded_dates(")]
    assert 'st.session_state["w_flt_from"] = None' in pending_block
    assert 'st.session_state["w_flt_to"] = None' in pending_block
    assert 'st.session_state["w_flt_store"] = "All stores"' in pending_block
    assert 'st.session_state[K_DATE_FROM] = None' in pending_block
    assert 'st.session_state[K_DATE_TO] = None' in pending_block
    assert 'st.session_state[K_STORE] = "All stores"' in pending_block


def test_clear_filters_button_only_sets_the_pending_flag():
    """The button click happens AFTER the date/store widgets are already
    instantiated in the same run, so it must not touch their session_state
    keys directly — that is exactly what crashed with
    "cannot be modified after the widget ... is instantiated" when it did."""
    source = COMMON.read_text(encoding="utf-8")
    sidebar_src = source[source.index("def sidebar("):source.index("def _operator_footer(")]
    assert '"Clear filters"' in sidebar_src
    button_block = sidebar_src[sidebar_src.index('"Clear filters"'):
                               sidebar_src.index('"Refresh"')]
    assert 'st.session_state["_pending_clear_filters"] = True' in button_block
    assert "st.rerun()" in button_block
    assert 'st.session_state["w_flt_from"] = None' not in button_block
    assert 'st.session_state[K_DATE_FROM] = None' not in button_block


def test_refresh_re_reads_the_database_without_touching_the_run():
    """Refresh clears Streamlit's own cache and redraws — it must never call
    anything that re-runs the reconciliation (that lives on the Run page)."""
    source = COMMON.read_text(encoding="utf-8")
    sidebar_src = source[source.index("def sidebar("):source.index("def _operator_footer(")]
    assert '"Refresh"' in sidebar_src
    refresh_block = sidebar_src[sidebar_src.index('"Refresh"'):]
    assert "st.cache_data.clear()" in refresh_block
    assert "does not re-run the" in refresh_block or "does not re-reconcile" in refresh_block


# ---------------------------------------------------------------------------
# The API every page depends on
# ---------------------------------------------------------------------------

def test_the_removed_helpers_are_really_gone():
    """`filter_bar` put the global filters in the page body and `operator_gate`
    blocked the sidebar with a form. Both are replaced, not wrapped."""
    names = module_names(COMMON)
    assert "filter_bar" not in names
    assert "operator_gate" not in names


def test_the_new_helpers_exist():
    names = module_names(COMMON)
    for helper in ("sidebar", "require_operator", "confirm_with_operator", "operator"):
        assert helper in names, helper


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_page_imports_a_helper_that_does_not_exist(page):
    """Upload imported `get_session_cached` and `require_operator` when neither
    existed, so the page crashed the moment it was opened. Nothing catches that
    except opening every page — or this test."""
    available = module_names(COMMON) | {"APP_NAME", "SIDEBAR_CSS", "MIME",
                                        "K_OPERATOR", "K_DATE_FROM", "K_DATE_TO",
                                        "K_STORE", "STATE_COLOUR"}
    missing = imported_from_common(page) - available
    assert not missing, f"{page.name} imports {sorted(missing)} from dashboard_common"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_page_uses_the_old_filter_bar(page):
    source = page.read_text(encoding="utf-8")
    assert "filter_bar" not in source
    assert "operator_gate" not in source


# ---------------------------------------------------------------------------
# Which filters are global, and which stay on their page
# ---------------------------------------------------------------------------

GLOBAL_FILTER_LABELS = ('"From"', '"To"', '"Store"')

# These belong to one view each. Putting one in the sidebar would make it look
# global and silently apply nowhere else.
PAGE_LEVEL_LABELS = ('"Status"', '"MOP"', '"Show"', '"Find a bill or transaction"')


def test_only_the_three_global_filters_live_in_the_sidebar():
    source = COMMON.read_text(encoding="utf-8")
    sidebar_src = source[source.index("def sidebar("):source.index("def _operator_footer(")]
    for label in GLOBAL_FILTER_LABELS:
        assert label in sidebar_src, f"{label} should be a sidebar filter"
    for label in PAGE_LEVEL_LABELS:
        assert label not in sidebar_src, f"{label} is page-level, not global"


def test_page_level_filters_stayed_on_their_page():
    """Status/MOP moved to Manual matches (2026-09-17, with the open-bills
    table that used to live on the now-removed Bill match page); Show moved
    to Summary's own Exceptions section."""
    manual_matches = (APP / "pages" / "8_Exceptions.py").read_text(encoding="utf-8")
    for label in ('"Status"', '"MOP"'):
        assert label in manual_matches
    assert '"Show"' in manual_matches   # the Exceptions tab carries both (2026-10-14)


def test_the_filters_persist_in_plain_keys_not_widget_keys():
    """Streamlit garbage-collects the state of a widget that is not rendered in
    the current run, so a widget key alone does NOT survive a page switch —
    setting July on Summary and opening Transactions reverted to the default.
    The canonical value must be written back to a plain key."""
    source = COMMON.read_text(encoding="utf-8")
    assert 'key="w_flt_from"' in source and 'key="w_flt_to"' in source
    assert "st.session_state[K_DATE_FROM] = st.date_input(" in source
    assert "st.session_state[K_DATE_TO] = st.date_input(" in source


# ---------------------------------------------------------------------------
# The operator, and the colour rule
# ---------------------------------------------------------------------------

def test_the_operator_is_never_asked_before_an_action():
    """No name is requested until a button is actually pressed."""
    source = COMMON.read_text(encoding="utf-8")
    confirm = source[source.index("def confirm_with_operator("):
                     source.index("# --- the sidebar:")]
    assert "if not st.session_state.get(flag):" in confirm
    assert "return \"\"" in confirm


def test_the_sidebar_footer_asks_nothing():
    source = COMMON.read_text(encoding="utf-8")
    footer = source[source.index("def _operator_footer("):]
    assert "Working as" in footer
    assert "text_input" not in footer, "the footer must not contain a prompt"


def test_no_red_continue_button_anywhere():
    """Red is reserved for critical state. The old sidebar had a bright red
    Continue button for a routine action."""
    for path in PAGES + [COMMON]:
        source = path.read_text(encoding="utf-8")
        assert '"Continue"' not in source, f"{path.name} still has a Continue button"


def test_the_operator_save_button_is_not_primary():
    """Match the button call itself, not the comment above it — a comment
    explaining why the button is not primary would otherwise fail this."""
    source = COMMON.read_text(encoding="utf-8")
    lines = [l for l in source.splitlines()
             if '.button("Save"' in l and not l.lstrip().startswith("#")]
    assert len(lines) == 1, lines
    assert "type=" not in lines[0]


# ---------------------------------------------------------------------------
# The range label — no presets any more (2026-09-11), just the words
# ---------------------------------------------------------------------------

from dashboard_common import range_label  # noqa: E402


@pytest.mark.parametrize("start,end,expected", [
    ((2026, 7, 31), (2026, 7, 31), "31 July 2026"),
    ((2026, 7, 1), (2026, 7, 31), "1-31 July 2026"),
    ((2026, 6, 15), (2026, 7, 31), "15 June - 31 July 2026"),
    ((2025, 12, 30), (2026, 1, 2), "30 December 2025 - 2 January 2026"),
])
def test_the_range_is_written_in_words(start, end, expected):
    assert range_label(date(*start), date(*end)) == expected


def test_no_range_label_when_nothing_is_selected():
    assert range_label(None, None) == ""


def test_the_range_label_includes_the_store_or_says_all_stores():
    """"1-31 July 2026, all stores" — the operator asked to always know what
    is on screen, and with no default date any more that includes the store
    filter, not only the date span."""
    source = COMMON.read_text(encoding="utf-8")
    # The period beside the title was removed (2026-10-14); the Live pill carries the range
    # and the store, so that is where "all stores" / "ABD only" must still be said.
    body = source[source.index("def _write_status_pill("):
                 source.index("def range_label_ddmmyyyy(")]
    assert 'store_words = f"{store} only" if store else "all stores"' in body


# ---------------------------------------------------------------------------
# The quick-picker's year/month spans, and the one padding year beyond the
# loaded range (2026-09-22) — a year/month entirely after `last` is enabled
# (a full, unclamped calendar span), never disabled like a genuine gap.
# ---------------------------------------------------------------------------

from dashboard_common import _month_span, _padding_upper_bound, _year_span  # noqa: E402

FIRST, LAST = date(2026, 7, 1), date(2026, 7, 31)


def test_year_span_clamps_to_the_loaded_range():
    assert _year_span(2026, FIRST, LAST) == (FIRST, LAST)


def test_year_span_is_none_for_a_year_entirely_before_the_loaded_range():
    """A genuine gap (not the one padding year) stays disabled."""
    assert _year_span(2024, FIRST, LAST) is None


def test_year_span_is_the_full_calendar_year_for_the_one_padding_year():
    """The year right after `last` is never clamped/disabled — there is
    nothing loaded to clamp against, so the plain calendar span is the
    honest answer, and the page shows an empty state for it downstream."""
    assert _year_span(2027, FIRST, LAST) == (date(2027, 1, 1), date(2027, 12, 31))


def test_month_span_is_the_full_month_for_a_month_in_the_padding_year():
    assert _month_span(2027, 3, FIRST, LAST) == (date(2027, 3, 1), date(2027, 3, 31))


def test_month_span_is_none_for_a_month_entirely_before_the_loaded_range():
    assert _month_span(2026, 1, FIRST, LAST) is None


def test_padding_upper_bound_is_the_end_of_the_year_after_last():
    assert _padding_upper_bound(LAST) == date(2027, 12, 31)


# ---------------------------------------------------------------------------
# Widget keys and changing option sets
# ---------------------------------------------------------------------------

def test_clear_filters_also_writes_the_date_widget_keys():
    """Streamlit prefers existing widget state over a `value=` argument, so
    clearing that only reset the canonical keys would have the date inputs
    write the old dates straight back on the same run — the same trap that
    used to apply to the (now-removed) range presets. The write now happens
    in the deferred pending-flag block (see
    test_clear_filters_resets_dates_and_store_to_empty), not inside the
    button's own click branch."""
    source = COMMON.read_text(encoding="utf-8")
    pending_block = source[source.index('"_pending_clear_filters", False)'):
                           source.index("dates = loaded_dates(")]
    assert 'st.session_state["w_flt_from"] = None' in pending_block
    assert 'st.session_state["w_flt_to"] = None' in pending_block


def test_the_date_expansion_state_is_filtered_against_the_current_option_set():
    """The merged store-wise table (2026-09-20) replaced a date-expansion
    MULTISELECT — a widget whose key had to be tied to the option set, or a
    changed range left it showing a stale selection (the same trap this
    originally caught on the now-gone Digital sale page's store selector) —
    with a plain (non-widget) session key, `OPEN_DATES_KEY`, that persists
    across page navigation and re-runs. It cannot go stale the same way: a
    date no longer in range is simply filtered out of `all_dates` membership
    before its expander is built, rather than needing a fresh widget
    instance. Revised again the same day: the per-date table itself no
    longer splices child rows into one dataframe (a "+"/"−" glyph on the
    date cell rendered as a floating box that overlapped the header and the
    first row) — each open date's own store rows render in their own
    st.expander below the parent-only table instead."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert 'OPEN_DATES_KEY = "sm_open_dates"' in source
    assert "for d in sorted(d for d in all_dates if d in open_dates):" in source
    # The glyph is gone for good — no "+ "/"− " prefix on the date cell.
    assert '"− " if' not in source
    assert '"+ " if' not in source


# ---------------------------------------------------------------------------
# Digital sale (formerly "Summary", renamed 2026-09-12) was itself removed
# 2026-09-17: its verdict tiles, risk-split call-out and by-store cut are
# superseded by Summary's plain Exception bills / Exception transactions
# tables and the tile ribbon's own Digital sale/receivable figures. There is
# no replacement for the verdict sentence, the risk-split tiles or the
# by-store grouping specifically — those tests are gone with the page they
# tested, not rewritten against a non-existent equivalent.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tiles never truncate a figure (2026-09-11)
# ---------------------------------------------------------------------------

def test_tile_sets_a_minimum_width_so_a_value_cannot_be_clipped():
    """2026-09-18 restyle: tile() emits CSS classes (`.mop-tile`,
    `.mop-tile-figure`) instead of inline styles — the actual min-width/
    nowrap rules that stop a value being clipped now live in the ONE
    stylesheet, theme.css, per the restyle's own "one stylesheet, no
    per-page/per-call inline styles" instruction."""
    tile_src = COMMON.read_text(encoding="utf-8")
    assert 'classes = ["mop-tile"]' in tile_src
    assert "mop-tile-figure" in tile_src
    css = (APP / "assets" / "theme.css").read_text(encoding="utf-8")
    assert "min-width: 150px" in css
    assert "white-space: nowrap" in css


def test_tile_carries_the_exact_figure_as_a_hover_tooltip():
    """A short-form value like "Rs 2.90 Cr" loses precision; the exact rupee
    figure must be reachable without leaving the tile — a native title
    attribute, not a separate control."""
    source = COMMON.read_text(encoding="utf-8")
    tile_src = source[source.index("def tile("):source.index("def money_column_config(")]
    assert 'title="{hover}"' in tile_src
    assert "tooltip or note" in tile_src


# ---------------------------------------------------------------------------
# The page swap (2026-09-12): "Summary" now IS the store-wise sheet
# ---------------------------------------------------------------------------
#
# Old "Summary" (digital-sale/exceptions view) -> pages/6_Digital_sale.py,
# itself removed 2026-09-17 in the page consolidation (see the note further
# down where its own tests used to be).
# Old "Store sheet" (pages/4_Store_sheet.py)   -> app/Summary.py, the
# entrypoint. Both moves are file moves, not new pages — asserted together so
# a future rename can't reintroduce a stray duplicate of either.

def test_the_old_store_sheet_filename_is_gone():
    assert not (APP / "pages" / "4_Store_sheet.py").exists()


def test_summary_py_is_now_the_store_wise_sheet():
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert "mop_reco import store_sheet" in source
    assert "MOP RECO_Store Wise" in source


def test_no_page_still_calls_itself_store_wise_sale_and_cash():
    """The old page() title string — if this still appears anywhere, a copy
    of the old heading survived the rewrite."""
    for path in PAGES:
        source = path.read_text(encoding="utf-8")
        assert 'page("Store-wise sale and cash"' not in source


# ---------------------------------------------------------------------------
# Summary (formerly Store sheet): tiles, ribbon, quiet controls (2026-09-12)
# ---------------------------------------------------------------------------

def test_the_seven_tile_ribbon_is_gone_replaced_by_the_particulars_matrix():
    """2026-09-18: the seven-tile ribbon (Total sale/Digital sale/Cash sale/
    Digital receivable/Cash deposit/Cash receivable/Closing balance) is
    REMOVED from Summary, on instruction, replaced by one particulars
    matrix table (Sale/%/Collected/Receivable × Total sale/UPI/Card/Total
    digital/Total cash, plus Gift Voucher Redeemed/Credit Note Issued/
    Credit Note Received as their own Total-sale-only lines).

    Was seven tiles, 4 then 3, using dashboard_common.tile() throughout,
    from 2026-09-11 (never more than four tiles in a row — a cut-off figure
    is worse than no figure) through an "Excess credited" tile tried and
    reverted the same day 2026-09-13 (there is no such thing as excess
    credit in this business). All of that machinery is gone from this page
    now; `tile()` itself is unchanged and still used elsewhere (the Bills/
    Transactions strip, Manual matches, Float)."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert 'st.container(key="tile-ribbon")' not in source
    assert "tile(row1[" not in source
    assert "tile(row2[" not in source
    assert "MATRIX_COLUMNS = " in source
    assert "matrix = src.mop_particulars_matrix(" in source


def test_the_particulars_matrix_has_all_its_rows():
    """2026-09-21: '% to total' is no longer its own row — restyled as a
    supporting sub-line sitting tight under Sale's own figure
    (`.mop-matrix-pct`), on instruction, "with no full-height row of its
    own." The other six labels are each still their own row."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    matrix_src = source[source.index("MATRIX_COLUMNS = "):source.index("_matrix_failed = ")]
    for row in ("Sale", "Collected", "Receivable",
               "Gift Voucher Redeemed", "Credit Note Issued", "Credit Note Received"):
        assert f'>{row}<' in matrix_src or f'"{row}"' in matrix_src, row
    assert "mop-matrix-pct" in matrix_src


def test_percent_to_total_is_a_direct_complement_not_independently_rounded():
    """The operator's own rule (carried over from the old tile ribbon's
    digital/cash split, 2026-09-11): a % column must be computed FROM the
    same Sale figures the matrix already holds, never re-derived so that
    two percentages could fail to add up. `MatrixColumn.pct` is computed as
    `sale / pct_base * 100`, one formula, applied to every column against
    the SAME Total sale denominator — so UPI% + Card% + (the rest) can
    never independently drift the way two separately-rounded figures could."""
    source = (Path(__file__).resolve().parents[1] / "app" / "mop_reco"
             / "store_sheet_source.py").read_text(encoding="utf-8")
    assert "pct = round(sale / pct_base * 100, 1) if pct_base else None" in source


def test_no_display_side_sign_flip_of_cash_deposit_survives():
    """Column I is stored positive (2026-09-17) — a deposit is a positive
    amount of money. This guarantee predates the tile ribbon and must
    survive its removal: no display-side flip of the DataFrame column
    itself anywhere on the page."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert 'shown["cash_deposit"] = shown["cash_deposit"].abs()' not in source
    assert "money_short(abs(cash_deposit))" not in source


def test_cash_receivable_is_still_net_and_floored_never_a_separate_excess_figure():
    """There is no such thing as excess credit in this business — the agency
    never credits more than was deposited (2026-09-13). The NET figure
    (sum I - sum J over the filtered range, floored at zero) is still
    computed via `ss.net_cash_receivable()` — now feeding only the
    "whole-range totals" control (the tile it used to feed is gone) — and
    no separate "Excess credited" gross figure has been reintroduced."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert "receivable_total = ss.net_cash_receivable(shown)" in source
    assert 'tile(row2[2], "Excess credited"' not in source
    assert "excess_credited_total" not in source


def test_no_excess_credited_label_tooltip_or_column_survives_on_the_summary_page():
    """A code comment recording WHY the tile was removed is fine (this
    codebase documents its own history in comments throughout); a label,
    tooltip, column header or export column a user would actually see is
    not. Checked by excluding comment lines, not by banning the phrase
    outright — the instruction was to delete user-facing wording, not
    history."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    live_lines = [line for line in source.splitlines()
                 if not line.lstrip().startswith("#")]
    assert not any("excess credit" in line.lower() for line in live_lines)


def test_data_quality_is_removed_from_summary_and_merged_onto_float():
    """2026-09-20 (Request B, part 5c): the Summary page's own "Data quality"
    toggle section (credits exceeding deposits, Defect B) is REMOVED from
    Summary, on instruction — never the underlying data, which is unchanged
    and now read from the Float page instead, merged under Float's own
    existing deposit-vs-credit data quality heading so there is exactly one
    place to look."""
    summary_source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert '_section_toggle(\n    "data_quality", "Data quality"' not in summary_source
    assert '"data_quality": False' not in summary_source

    float_source = (APP / "pages" / "1_Float.py").read_text(encoding="utf-8")
    assert "src.defect_b_summary(session, run.id, date_from, date_to, store)" in float_source
    assert "Defect B" in float_source
    assert "not money owed to the agency" in float_source
    assert "##### Data quality" in float_source
    # A raw, unprotected `to_csv()` download button used to sit here — no
    # text/quoting for pickup_code/slip_no, no header block. It now goes
    # through the standard export button, like every other export on this
    # page (2026-09-20).
    assert "dq_shown.to_csv(" not in float_source
    assert 'key="dl_data_quality"' in float_source


def test_the_detail_table_never_displays_cash_receivable_negative():
    """K = I - J since 2026-09-17 (polarity swapped with column I's sign) —
    a negative K is the Defect-B floor now, not a positive one."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    display_fn = source[source.index("def display("):
                        source.index("# ===", source.index("def display("))]
    assert 'max(0.0, float(k))' in display_fn


def test_the_ribbon_tint_rule_still_lives_in_theme_css_only():
    """2026-09-18 restyle: the per-page `<style>.st-key-tile-ribbon{...}`
    block Summary.py used to inject itself is gone — every page's ribbon
    tint is ONE rule in theme.css, keyed on st.container(key=) hooks
    (tile-ribbon, mm-open-ribbon, float-ribbon), so there is exactly one
    place to change it. Summary.py no longer has a `tile-ribbon` container
    of its own at all (the same-day particulars-matrix change removed the
    tile ribbon it used to tint) — the rule stays in theme.css regardless,
    since Manual matches' and Float's own ribbons still use the same class."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert "<style>.st-key-tile-ribbon" not in source
    css = (APP / "assets" / "theme.css").read_text(encoding="utf-8")
    assert ".st-key-tile-ribbon" in css
    assert "var(--surface-alt)" in css


def test_controls_never_banner_on_the_page_face_any_more():
    """The old page said "All 8 controls hold" every single time — noise,
    since that is the normal case — and later grew a red top-of-page banner
    for a failure instead. Both are gone (2026-09-30, on instruction: "move
    it, do not delete it") — a failing control is a real finding (292
    negative-closing store-days) but it now only shows inside the Data
    notes expander, never as a banner across the page on load."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert "All {len(controls)} controls hold" not in source
    assert 'st.error(f"**{len(failed)} control(s) do not hold.**' not in source
    assert 'st.error(f"**{len(_matrix_failed)}' in source  # the matrix's own
                                                           # separate banner
                                                           # is untouched


def test_the_data_notes_expander_header_shows_a_failing_count():
    """Moved out of a banner, the failing-count must still be visible at a
    glance without opening the expander (2026-09-30)."""
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert '_controls_failed = [c for c in controls if not c.holds]' in source
    assert 'with st.expander(_notes_label):' in source
    assert 'f"Data notes — {len(_controls_failed)} control' in source


# ---------------------------------------------------------------------------
# Exceptions — the Show filter simplified to Open / Under investigation
# (2026-09-12): the page is a working list, not history. Closed and
# auto-resolved rows are never made unreachable — they move to a collapsed
# expander at the bottom with their own export.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The date-lag note must be VISIBLE at the point of accepting a match
# (2026-09-12) — never only inside the audit record. A transaction found one
# day over is still accepted, but the operator must be shown that it happened.
# ---------------------------------------------------------------------------

def test_inline_match_surfaces_the_date_lag_note_on_a_clean_accept():
    source = (APP / "inline_match.py").read_text(encoding="utf-8")
    clean_case = source[source.index("def _clean_case("):source.index("def _discrepant_case(")]
    assert "validation.date_lag_note" in clean_case


def test_inline_match_surfaces_the_date_lag_note_on_a_discrepant_case_too():
    source = (APP / "inline_match.py").read_text(encoding="utf-8")
    discrepant_case = source[source.index("def _discrepant_case("):
                             source.index("def _not_found_case(")]
    assert "validation.date_lag_note" in discrepant_case


def test_exceptions_show_dropdown_offers_only_open_and_under_investigation():
    """The standalone Exceptions page is gone (2026-09-17) - this is Summary's own Exceptions
    section. Since 2026-10-13 the choice is a persisted select (it survives navigation)."""
    source = (APP / "pages" / "8_Exceptions.py").read_text(encoding="utf-8")
    section = source[source.index('_section_toggle(\n    "exceptions"'):]
    assert '_persisted_select("Show", ["Open", "Under investigation"]' in section
    assert '"Everything"' not in section
    assert '"Auto-resolved"' not in section
    assert '"Closed"' not in section


def test_exceptions_section_keeps_closed_and_auto_resolved_reachable_but_lazy():
    """Bills and transactions are never merged into one count (2026-09-17). Closed and
    auto-resolved stay reachable (rule 4) - since 2026-10-13 behind their own lazy +/- section,
    read only when opened - as two separate tables with two separate exports."""
    source = (APP / "pages" / "8_Exceptions.py").read_text(encoding="utf-8")
    section = source[source.index('_section_toggle(\n    "exceptions"'):]
    assert '_section_toggle(\n        "closed_exceptions", "Closed and auto-resolved"' in section
    assert 'CLOSED_STATUSES = ("closed", "auto_resolved")' in source
    assert '"closed_exceptions": False' in source          # collapsed by default: nothing is fetched
    # one block per kind (bills / txns), each with its own export key
    assert '_exception_table_block("bills", CLOSED_STATUSES' in section
    assert '_exception_table_block("txns", CLOSED_STATUSES' in section
    assert "key=f\"dl_exc_{kind}{'' if editable else '_closed'}\"" in source


def test_exception_tables_are_paged_server_side_and_never_load_the_whole_list():
    source = (APP / "pages" / "8_Exceptions.py").read_text(encoding="utf-8")
    section = source[source.index('_section_toggle(\n    "exceptions"'):]
    assert "q.exception_page(" in source and "q.exception_export(" in source
    assert "q.exception_rows(" not in section          # the load-everything call is gone from the page
    assert "_pager(" in source


def test_the_controls_table_and_notes_moved_into_a_bottom_expander():
    source = (APP / "Summary.py").read_text(encoding="utf-8")
    assert 'with st.expander(_notes_label):' in source
    head, tail = source.split('with st.expander(_notes_label):', 1)
    # "controls in full" only appears inside the bottom expander, not above it.
    assert "controls in full" not in head
    assert "controls in full" in tail
    # A failing control's own offending rows get their own count/value and
    # CSV export inside the same expander (2026-09-30).
    assert "_control_value" in tail
    assert 'noun=f"control ({c.key}) rows"' in tail
    # Notes come from a freshly-computed CMS breakdown now (2026-09-24), not
    # a static per-run snapshot read once at persist time — the underlying
    # bug this rework fixed (a stale run's controls/notes echoing back
    # regardless of the sidebar's current filter).
    assert 'for note in _cms["notes"]:' in tail


# ---------------------------------------------------------------------------
# The 2026-09-18 restyle — one stylesheet, tabular figures, status pills that
# actually render (a real bug found live: `var(--good-tint)` renders BLANK
# inside st.dataframe's canvas-drawn grid, since a canvas fillStyle cannot
# resolve a CSS custom property — literal hex is required there).
# ---------------------------------------------------------------------------

import pandas as pd  # noqa: E402

from dashboard_common import STATUS_TINT, status_styler  # noqa: E402


def test_theme_css_exists_and_defines_the_palette():
    css = (APP / "assets" / "theme.css").read_text(encoding="utf-8")
    for var in ("--bg", "--surface", "--border", "--ink", "--ink-soft",
               "--accent", "--accent-deep", "--good", "--warn", "--bad"):
        assert f"{var}:" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "Plus Jakarta Sans" in css
    assert "Inter" in css


def test_theme_is_injected_from_one_place_only():
    """No page carries its own <style> block any more — dashboard_common.
    inject_theme() (called from page()) is the only place theme.css is read
    and injected."""
    assert "inject_theme" in COMMON.read_text(encoding="utf-8")
    page_src = (APP / "Summary.py").read_text(encoding="utf-8")
    assert "<style>" not in page_src
    for p in (APP / "pages").glob("*.py"):
        assert "<style>" not in p.read_text(encoding="utf-8"), p.name


def test_status_tint_uses_literal_colours_never_css_variables():
    """The bug: `var(--good-tint)` is invisible inside st.dataframe's grid,
    because that grid draws cells on an HTML canvas and a canvas fillStyle
    cannot resolve a CSS custom property — the cell renders blank, not
    merely uncoloured. STATUS_TINT must use literal hex so the Styler-driven
    background/text colour actually appears."""
    for state, (bg, fg) in STATUS_TINT.items():
        assert bg.startswith("#"), f"{state} background must be literal hex, got {bg!r}"
        assert fg.startswith("#"), f"{state} text colour must be literal hex, got {fg!r}"


def test_status_styler_never_changes_the_underlying_values():
    """A Styler is display-only. Sorting, filtering and export all read the
    plain DataFrame — status_styler() must never mutate it, and the value a
    reader sees printed (in black and white) is exactly the original word."""
    df = pd.DataFrame({"Status": ["open", "closed", "mismatch"], "Amount": [1, 2, 3]})
    styled = status_styler(df, {"Status": {"open": "warn", "closed": "good",
                                           "mismatch": "bad"}})
    assert styled.data is df  # the Styler wraps the SAME frame, nothing copied-and-altered
    pd.testing.assert_frame_equal(styled.data, df)


def test_status_styler_tints_only_the_given_column():
    df = pd.DataFrame({"Status": ["open", "closed"], "Other": ["x", "y"]})
    styled = status_styler(df, {"Status": {"open": "warn", "closed": "good"}})
    computed = styled._compute()
    # ._todict()/.ctx is pandas' own internal Styler state — asserting through
    # the public render path instead, which is what st.dataframe consumes.
    html = computed.to_html()
    assert "background-color: #FDF2E3" in html  # warn tint, "open"
    assert "background-color: #E7F6EF" in html  # good tint, "closed"


def test_status_styler_handles_an_unmapped_value_without_colour():
    """A status word with no entry in the mapping gets no tint at all — it
    is never guessed at (rule 5) and never crashes the page."""
    df = pd.DataFrame({"Status": ["something_unexpected"]})
    styled = status_styler(df, {"Status": {"open": "warn"}})
    html = styled._compute().to_html()
    assert "something_unexpected" in html


def test_summary_no_longer_carries_the_exceptions_section_and_the_page_does():
    summary = (APP / "Summary.py").read_text(encoding="utf-8")
    assert "_exception_table_block" not in summary and "exception_page" not in summary
    page = (APP / "pages" / "8_Exceptions.py").read_text(encoding="utf-8")
    assert "_exception_table_block" in page and "manual_matching_section" in page


def test_navigation_order_and_removed_tabs():
    source = COMMON.read_text(encoding="utf-8")
    start = source.index("NAV_PAGES = [")
    block = source[start:source.index(chr(93), start + len("NAV_PAGES = ["))]
    labels = re.findall(r'"([A-Za-z ]+)"\)', block)
    assert labels == ["Summary", "Exceptions", "CMS Reco", "Float", "Store Task", "Upload", "Masters"]
    assert not (APP / "pages" / "3_Manual_matches.py").exists()
    assert not (APP / "pages" / "4_Run.py").exists()


def test_the_removed_page_texts_are_gone_but_the_build_stamp_is_in_the_pill_tooltip():
    source = COMMON.read_text(encoding="utf-8")
    for gone in ("Code loaded:", "Not signed in", "A newer run", "All figures in Rs Cr"):
        assert gone not in source.replace("# Not signed in:", "")
    assert "title='Build: code loaded" in source
