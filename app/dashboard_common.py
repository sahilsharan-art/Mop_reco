"""Shared chrome for the console: operator identity, the filter bar, exports.

Views read precomputed rows only (CLAUDE.md 6.14) — nothing here opens a raw
file or runs a reconciliation.
"""

from __future__ import annotations

import base64
import calendar
import html
import io
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import selfcheck

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mop_reco.db import Exception_, R1BillLine, Run, get_session, init_db
from mop_reco.export import XLSX_ROW_LIMIT, ExportMeta, build_export
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_cr, money_short, money_table

# --- session keys ----------------------------------------------------------
K_OPERATOR = "operator"
K_DATE_FROM = "flt_date_from"
K_DATE_TO = "flt_date_to"
K_STORE = "flt_store"


# What this system reconciles TODAY. R3a and R3b (cash) are blocked, so the
# name does not claim them — see the note under the title on the landing page.
APP_NAME = "MOP Reconciliation"


THEME_CSS_PATH = Path(__file__).resolve().parent / "assets" / "theme.css"


@st.cache_data
def _theme_css() -> str:
    return THEME_CSS_PATH.read_text(encoding="utf-8")


def inject_theme():
    """The ONE stylesheet (app/assets/theme.css), injected once, here, and
    nowhere else — no page carries its own ad hoc <style> block any more.
    To change the accent colour (or anything else visual), edit theme.css;
    nothing in this function or any page needs to change.

    Cached so the file is read from disk once per process, not once per
    rerun — restyling must not cost render time (per the operator's own
    instruction: "if any change adds render time, drop it").
    """
    st.markdown(f"<style>{_theme_css()}</style>", unsafe_allow_html=True)


def page(title: str, purpose: str = "", icon: str = None, nav: bool = True):
    """Standard page opening: a title and, when given, ONE line saying what
    it is for — written for the person using it, never a build-stage label.

    `purpose` is optional (2026-09-12): the Summary page's heading carries no
    subtitle at all, and an empty caption still occupies a line of vertical
    space even with nothing in it, so a falsy purpose skips st.caption
    entirely rather than rendering a blank line.

    The active date range is written beside the title by `sidebar()` once it
    knows what it is — see `page_range_slot`.
    """
    # The landing page's title IS the app name; don't repeat it in the tab.
    tab = title if title == APP_NAME else f"{title} — {APP_NAME}"
    st.set_page_config(page_title=tab, layout="wide")
    inject_theme()

    # Every page load checks that the functions it calls exist in the modules
    # actually loaded, and that this process is not serving a module older than
    # the file on disk. A failure is named here rather than thrown as a
    # traceback halfway down the page — see app/selfcheck.py for the incident
    # that made this necessary.
    selfcheck.mark_process_start()
    if not selfcheck.render(st):
        st.stop()

    heading, slot = st.columns([3, 2])
    heading.markdown(_title_html(title), unsafe_allow_html=True)
    st.session_state["_range_slot"] = slot
    if purpose:
        st.caption(purpose)
    # The status pill sits directly under the title on every page; sidebar() fills it.
    st.session_state["_pill_slot"] = st.empty()
    # The navigation bar. Summary places it itself, BELOW its summary matrix
    # (`nav=False` here, then `top_nav()` after the matrix); every other page has no
    # matrix, so the bar goes straight under the heading - the same place relative to the
    # page's own opening block, so the layout does not jump when switching pages.
    if nav:
        top_nav(NAV_LABEL_BY_TITLE.get(title))


LOGO_PATH = Path(__file__).resolve().parent / "assets" / "citykart-logo.png"
LOGO_HEIGHT_PX = 32


@st.cache_data(show_spinner=False)
def _logo_data_uri(mtime: float):
    """The logo as a data: URI (read once per file version), or None when the file is
    missing or unreadable - never a broken image."""
    try:
        raw = LOGO_PATH.read_bytes()
    except OSError:
        return None
    if raw[:4] != bytes([137]) + b"PNG":
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def logo_missing() -> bool:
    return not LOGO_PATH.is_file()


def _title_html(title: str) -> str:
    """Logo (32px high, aspect ratio kept) at the top left, the title to its right. With
    no logo file the title stands alone and a warning is logged once - no broken-image icon."""
    uri = _logo_data_uri(LOGO_PATH.stat().st_mtime if LOGO_PATH.is_file() else 0.0)
    if uri is None:
        if not st.session_state.get("_logo_warned"):
            st.session_state["_logo_warned"] = True
            logging.getLogger(__name__).warning(
                "Logo file missing or not a PNG: %s - showing the title alone", LOGO_PATH)
        return f"<div class='mop-titlebar'><h1>{html.escape(title)}</h1></div>"
    return (f"<div class='mop-titlebar'><img class='mop-logo' src='{uri}' alt='Citykart' "
            f"style='height:{LOGO_HEIGHT_PX}px;width:auto'><h1>{html.escape(title)}</h1></div>")


# The pages, left to right (2026-10-13). The bar is `st.page_link`s - the separate page
# files stay separate pages, and Streamlit still runs ONLY the page that is opened. It is
# deliberately NOT `st.tabs`: that renders every tab's content on every run, hidden ones
# included, which would undo the lazy page loading (CLAUDE.md 6.14) and make the dashboard
# slower. The native sidebar list is hidden (theme.css + .streamlit/config.toml).
NAV_PAGES = [
    ("Summary.py", "Summary"),
    ("pages/8_Exceptions.py", "Exceptions"),
    ("pages/2_CMS_Reco.py", "CMS Reco"),
    ("pages/1_Float.py", "Float"),
    ("pages/7_Store_Task.py", "Store Task"),
    ("pages/6_Upload.py", "Upload"),
    ("pages/5_Masters.py", "Masters"),
]


# Page title -> nav item, so the bar can mark the open page (Streamlit 1.57 sets no
# aria-current on a page_link; the active link differs only by a generated class name,
# which must not be styled against).
NAV_LABEL_BY_TITLE = {
    APP_NAME: "Summary", "MOP RECO_Store Wise": "Summary", "Exceptions": "Exceptions", "CMS Reco": "CMS Reco",
    "Float": "Float", "Store Task": "Store Task", "Upload": "Upload", "Master data": "Masters",
}


def top_nav(active: str = None):
    """The horizontal navigation bar across the top of every page, and - under it - the
    status pill's slot, which `sidebar()` fills once it knows the run and the filters.
    The open page (`active`) is drawn as a plain label with the accent underline; every
    other page is a link."""
    with st.container(key="topnav"):
        cols = st.columns([len(label) + 3 for _, label in NAV_PAGES] + [14], gap="small")
        for col, (path, label) in zip(cols, NAV_PAGES):
            if label == active:
                col.markdown(f"<div class='mop-nav-active'>{label}</div>", unsafe_allow_html=True)
            else:
                col.page_link(path, label=label)


def build_stamp() -> str:
    """When THIS PROCESS loaded its code - the answer to "which build am I on" (CLAUDE.md 9a)."""
    boot = getattr(sys, "_mop_reco_started_at", None)
    return datetime.fromtimestamp(boot).strftime("%d %b %Y, %H:%M:%S") if boot else "unknown"


def _write_status_pill(run, date_from, date_to, store=None):
    """"Live . run #11 . 01/04/2026 to 31/08/2026 . all stores" - which run is on screen and
    what filter is active, directly under the navigation bar on every page. Called from
    `sidebar()` once the range is settled."""
    slot = st.session_state.pop("_pill_slot", None)
    if slot is None or run is None:
        return
    label = range_label_ddmmyyyy(date_from, date_to)
    store_words = f"{store} only" if store else "all stores"
    slot.markdown(
        f"<div class='mop-pill' title='Build: code loaded {build_stamp()}'><span class='mop-pill-dot'></span>"
        f"<span class='mop-pill-live'>Live</span>"
        f"<span class='mop-pill-sep'>&middot;</span>run #{run.id}"
        f"<span class='mop-pill-sep'>&middot;</span>{label}"
        f"<span class='mop-pill-sep'>&middot;</span>{store_words}</div>",
        unsafe_allow_html=True)


def range_label_ddmmyyyy(date_from, date_to) -> str:
    """"01/04/2026 to 30/09/2026" - the pill always spells the range out as dates, never
    the prose of `range_label`."""
    if date_from is None or date_to is None:
        return "all loaded dates"
    return f"{display_date(date_from)} to {display_date(date_to)}"


def _write_range_label(date_from, date_to, store=None):
    """The period is no longer written beside the title (2026-10-14): the Live pill under it
    already shows the range and the store. The slot is still consumed so nothing leaks."""
    st.session_state.pop("_range_slot", None)


@st.cache_resource
def _engine():
    """The ENGINE only is cached — never a Session or a Connection.

    Streamlit runs each rerun in its own script thread, and a raw SQLite
    connection is bound to the thread that opened it. `db_session()` used to
    be cached here directly, so every rerun after the first reused the SAME
    live Session — and therefore the same underlying sqlite3 connection —
    from whatever thread happened to run first, which is exactly what threw
    `sqlite3.InterfaceError: bad parameter or other API misuse` the moment a
    later rerun landed on a different thread. `mop_reco.db.get_engine()` is
    safe to share this way: it sets `check_same_thread=False` and
    `NullPool`, so no connection is ever held open and handed to a
    different thread than the one that opened it — every checkout is a
    fresh connection.
    """
    return init_db()


def db_session():
    """A brand-new Session bound to the shared, thread-safe engine — never
    cached. Safe to hold for the duration of one page's script run (a
    single Streamlit rerun executes single-threaded, top to bottom), but it
    must never be stashed anywhere that survives past that run (no
    `st.session_state`, no `st.cache_resource`, no module-level global) —
    that is exactly how the old caching bug happened."""
    return get_session(_engine())


# --- operator: asked at the point of the action, never up front ------------
#
# The name is recorded against every remark, status change, upload and run. It
# is asked ONCE, inline, the first time an action actually needs it — not as a
# form standing in the sidebar demanding an answer before anything can be
# looked at. After that it is only the "Working as" line at the foot of the
# sidebar.

def operator() -> str:
    """The current operator, or "" if not yet given. Never prompts."""
    return st.session_state.get(K_OPERATOR, "")


def require_operator(action: str = "record this", key: str = "op") -> str:
    """The operator name, prompting inline if it is not yet known.

    Returns "" when still unknown, so the caller does not act. Put this
    immediately beside the button it guards — the prompt appears where the
    action is, which is the only place the question is relevant.
    """
    current = st.session_state.get(K_OPERATOR)
    if current:
        return current

    st.caption(f"Your name is recorded against this. Who is {action}?")
    cols = st.columns([2, 1])
    name = cols[0].text_input("Your name", key=f"{key}_name",
                              placeholder="your name",
                              label_visibility="collapsed")
    # Not type="primary": red is reserved for critical state in this app.
    if cols[1].button("Save", key=f"{key}_save", width="stretch"):
        if name.strip():
            st.session_state[K_OPERATOR] = name.strip()
            st.rerun()
        else:
            st.warning("Type a name first.")
    return ""


# --- the run everything is read from --------------------------------------

def latest_run(session):
    """The latest run whose results are actually safe to show — the latest
    COMPLETED one, never the latest run of any status.

    Found 2026-09-26: a run that is running, stalled or failed used to hide
    a finished run's data outright — Run #11 completed a full five-month
    reconciliation, then Run #12 (started the next day, stuck in
    ingest_paytm for over 22 hours before being caught) made every page
    show nothing, because this function returned #12 regardless of its
    status. A run in progress must never blank a page that has real data
    to show — see `sidebar()`'s "newer run" note for how that state is
    surfaced instead, alongside the completed run's own figures, not in
    place of them."""
    return (session.query(Run).filter(Run.status == "completed")
            .order_by(Run.started_at.desc()).first())


def run_notes(run) -> dict:
    if run is None or not run.notes:
        return {}
    try:
        return json.loads(run.notes)
    except (ValueError, TypeError):
        return {}


@st.cache_data(ttl=300)
def loaded_dates(run_id: int) -> list:
    session = db_session()
    return [d[0] for d in session.query(R1BillLine.business_date)
            .filter(R1BillLine.run_id == run_id).distinct()
            .order_by(R1BillLine.business_date).all()]


@st.cache_data(ttl=300)
def loaded_stores(run_id: int) -> list:
    session = db_session()
    return [s[0] for s in session.query(R1BillLine.store)
            .filter(R1BillLine.run_id == run_id).distinct()
            .order_by(R1BillLine.store).all() if s[0]]


def confirm_with_operator(clicked: bool, action: str, key: str) -> str:
    """Returns the operator name when the action may proceed, else "".

    On the first click with no name known, this renders the inline prompt
    instead of acting, and remembers the click — so the action runs as soon as
    the name is given, rather than making the operator press the button twice.
    Nothing is asked until a button is actually pressed.
    """
    flag = f"_pending_{key}"
    if clicked:
        st.session_state[flag] = True
    if not st.session_state.get(flag):
        return ""
    name = require_operator(action, key=key)
    if name:
        st.session_state.pop(flag, None)
    return name


# --- the sidebar: the three GLOBAL filters, and nothing page-specific -----
#
# Only date-from, date-to and store live here, because only those three mean
# the same thing on every page. A filter that belongs to one view — status,
# MOP, exception type, gateway, the search box — stays on that view, on one
# horizontal row above its table. Putting a page-specific filter here would
# make it look global and silently apply nowhere else.

SIDEBAR_CSS = """
<style>
/* Lay the sidebar out as a column so the operator line can sit at its foot
   rather than immediately under the filters. */
section[data-testid="stSidebar"] div[data-testid="stSidebarUserContent"] {
    display: flex;
    flex-direction: column;
    min-height: 78vh;
}
section[data-testid="stSidebar"] .op-footer { margin-top: auto; }
</style>
"""


# --- the statement-table pattern (2026-09-28, shared 2026-09-30) -----------
#
# One line of a "proper statement" — label left, figure right, always
# POSITIVE, the operation carried by the label text ("Add:"/"Less:") never
# by a minus sign. First built for Summary's own cash block; CMS Reco reuses
# it verbatim ("same statement-style formatting as the cash block" — the
# operator's own instruction) rather than a second, subtly different copy.

def stmt_row(label, value, use_cr: bool = False) -> str:
    """One line of a statement table — label left, figure right, always
    shown POSITIVE: the operation is carried by the row's own label text
    ("Add:"/"Less:"), never by a minus sign, which is the whole point of a
    proper statement over a calculator tape. A figure that is structurally
    non-negative (a deposit, a sale total) uses a FIXED label regardless of
    sign; only a figure that can genuinely be either (net expense/income)
    needs the CALLER to choose the label from its own sign before calling
    this — see Summary.py's `_store_cash_card()` for that one case.

    `use_cr` (2026-09-30) switches the figure to `money_cr()` — always
    crores, two decimals, never rounds the stored value or sums already-
    rounded parts — for callers that want one consistent unit across a
    whole table (Summary's Cash Position/Cash With Agency). Defaults to
    the original full-rupee `money_table()` so every other caller (CMS
    Reco's own statement table) is unaffected."""
    figure = money_cr(abs(value)) if use_cr else money_table(abs(value))
    return (f"<tr class='mop-stmt-row'>"
            f"<td class='mop-stmt-rowlabel'>{label}</td>"
            f"<td class='mop-stmt-figure'>{figure}</td></tr>")


def stmt_table(title, period, rows_html, result_label, result_value,
               result_bad=False, result_warn=False, use_cr: bool = False) -> str:
    warn_cls = " mop-stmt-cell--warn" if result_warn else ""
    bad_cls = " mop-stmt-figure--bad" if result_bad else ""
    result_figure = money_cr(abs(result_value)) if use_cr else money_table(abs(result_value))
    total_row = (
        f"<tr class='mop-stmt-row mop-stmt-total{warn_cls}'>"
        f"<td class='mop-stmt-rowlabel'>{result_label}</td>"
        f"<td class='mop-stmt-figure{bad_cls}'>{result_figure}</td></tr>"
    )
    note_html = ""
    return f"""
<div class='mop-stmt'>
  <div class='mop-stmt-title'>{title}</div>
  <div class='mop-stmt-period'>{period}</div>
  <table class='mop-stmt-table'>
    <thead><tr class='mop-stmt-headrow'><th></th><th>Amount (Rs)</th></tr></thead>
    <tbody>
      {rows_html}
      {total_row}
    </tbody>
  </table>
  {note_html}
</div>"""


def range_label(date_from, date_to) -> str:
    """The active range in words — "31 July 2026" or "1-31 July 2026".

    Shown next to the page title so it is never a guess what is on screen.
    """
    if date_from is None or date_to is None:
        return ""
    if date_from == date_to:
        return f"{date_from.day} {date_from:%B %Y}"
    if (date_from.year, date_from.month) == (date_to.year, date_to.month):
        return f"{date_from.day}-{date_to.day} {date_to:%B %Y}"
    if date_from.year == date_to.year:
        return f"{date_from.day} {date_from:%B} - {date_to.day} {date_to:%B %Y}"
    return f"{date_from.day} {date_from:%B %Y} - {date_to.day} {date_to:%B %Y}"


# --- Year/Month quick pickers (2026-09-21) ---------------------------------
#
# ONE state with From/To, on purpose: the button grid never stores its own
# "which year/month is selected" flag. A button's selected/enabled state is
# DERIVED, every run, from the current From/To values against the loaded
# date range — so a button and a hand-typed date can never disagree (the
# operator's own explicit requirement). Clicking a button just writes
# From/To (both the canonical and widget-backing keys) like any other
# filter change; the date_input widgets rendered further down in the SAME
# run then show the new value directly, no extra rerun needed.

MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _year_span(year: int, first: date, last: date):
    """That year's own date span, CLAMPED to what is actually loaded — never
    the full calendar year when only part of it is loaded, since setting an
    out-of-range From/To would otherwise trip `sidebar()`'s own "remembered
    date outside this run" reset (turning a year click into "every loaded
    date" instead of that year). None when the year has no overlap at all
    and it is NOT entirely after the loaded range — the caller uses that to
    disable the button, never to guess a range.

    A year entirely AFTER `last` is the one deliberate exception
    (2026-09-22, on instruction): `_quick_date_picker` only ever offers ONE
    such year (`loaded_years[-1] + 1`, "the next calendar year"), so a
    future year is selectable before its data arrives — there is nothing
    loaded to clamp against, so the plain calendar span is the honest
    answer, never None/disabled. `sidebar()` widens its own reset window to
    match (see `_padding_upper_bound`), or this span would be silently
    dropped back to "every loaded date" the next rerun.
    """
    y_from, y_to = date(year, 1, 1), date(year, 12, 31)
    if y_from > last:
        return (y_from, y_to)
    y_from, y_to = max(y_from, first), min(y_to, last)
    return (y_from, y_to) if y_from <= y_to else None


def _month_span(year: int, month: int, first: date, last: date):
    """Same clamping as `_year_span`, for one calendar month — including the
    same "entirely after `last`" exception, so every month of the one
    padding year is selectable too, once that year is the active one."""
    last_day = calendar.monthrange(year, month)[1]
    m_from, m_to = date(year, month, 1), date(year, month, last_day)
    if m_from > last:
        return (m_from, m_to)
    m_from, m_to = max(m_from, first), min(m_to, last)
    return (m_from, m_to) if m_from <= m_to else None


def _padding_upper_bound(last: date) -> date:
    """The end of the one padding year `_quick_date_picker` offers beyond
    the loaded range — 31 December of `last.year + 1`. `sidebar()` uses
    this instead of `last` for the widgets' own `max_value` and for its
    "remembered date outside this run" reset, so clicking the padding year
    (or typing a date inside it by hand) survives the next rerun instead of
    being silently dropped back to "every loaded date"."""
    return date(last.year + 1, 12, 31)


def _quick_date_picker(dates: list[str], first: date, last: date):
    """Year row, then a 3-column month grid, both reading/writing the same
    From/To the date_input widgets below use. Scoped inside
    `st.container(key="qp")` so the CSS overrides for selected/unselected/
    disabled buttons apply ONLY here — the app's other primary/secondary
    buttons elsewhere are untouched.
    """
    resolved_from = st.session_state.get(K_DATE_FROM) or first
    resolved_to = st.session_state.get(K_DATE_TO) or last
    current = (resolved_from, resolved_to)

    # The year row is built from the DATA, never a fixed list (2026-09-22, on
    # instruction) — every year that actually has a loaded row, PLUS the
    # next calendar year (`loaded_years[-1] + 1`) so a year is selectable
    # before its data arrives — never a hardcoded pair, so this stays right
    # as the years roll on. Derived from every loaded date's own year, not
    # just first.year..last.year, so a gap in the loaded range (a skipped
    # year) is never shown as if it had data.
    loaded_years = sorted({date.fromisoformat(d).year for d in dates})
    padding_year = loaded_years[-1] + 1
    years = loaded_years + [padding_year]
    # Which year's months to show — follows the current From date whenever
    # it names one of the years on the row, so typing a date (or picking a
    # year) always keeps the month grid in step; never stored independently.
    active_year = resolved_from.year if resolved_from.year in years else last.year

    def _set_range(d_from: date, d_to: date):
        st.session_state[K_DATE_FROM] = d_from
        st.session_state[K_DATE_TO] = d_to
        st.session_state["w_flt_from"] = d_from
        st.session_state["w_flt_to"] = d_to

    with st.container(key="qp"):
        st.markdown("<div class='mop-qp-label'>Year</div>", unsafe_allow_html=True)
        year_cols = st.columns(len(years))
        for col, year in zip(year_cols, years):
            span = _year_span(year, first, last)
            selected = span is not None and span == current
            button_kwargs = dict(
                key=f"qp_y_{year}", width="stretch",
                disabled=span is None,
                type="primary" if selected else "secondary",
                on_click=(lambda s=span: _set_range(*s)) if span else None,
            )
            if year == padding_year:
                # Enabled, not disabled (rule 5 does not apply here — this
                # is a real, selectable future range, just an empty one
                # today) — only visibly DIMMED, via the same st.container
                # (key=...) CSS-scoping pattern this app already uses
                # everywhere else, a fixed key so no hardcoded year ever
                # lands in the CSS itself.
                with col:
                    with st.container(key="qp_pad"):
                        st.button(str(year), **button_kwargs)
            else:
                col.button(str(year), **button_kwargs)

        st.markdown("<div class='mop-qp-label'>Month</div>", unsafe_allow_html=True)
        is_padding_year = active_year == padding_year
        for row_start in range(0, 12, 3):
            month_cols = st.columns(3)
            for col, month in zip(month_cols, range(row_start + 1, row_start + 4)):
                span = _month_span(active_year, month, first, last)
                selected = span is not None and span == current
                target = col
                if is_padding_year:
                    target = col.container(key=f"qp_pad_m_{month}")
                target.button(
                    MONTH_LABELS[month - 1], key=f"qp_m_{active_year}_{month}",
                    width="stretch", disabled=span is None,
                    type="primary" if selected else "secondary",
                    on_click=(lambda s=span: _set_range(*s)) if span else None,
                )


def sidebar(session, run, show_store: bool = True):
    """The global filters, the run stamp, and the operator footer.

    Called once per page, before the page body, so the page can lead with its
    answer instead of a row of controls. Returns (date_from, date_to, store).

    **Summary briefly (2026-09-20) hid its own From/To fields, on the
    instruction that its merged store-wise table showed every loaded date as
    its own expandable row anyway. That instruction was withdrawn the same
    day** — From/To/Store/Clear filters/Refresh apply to every page,
    including Summary, with no exception. `show_dates` is gone; do not
    re-add a per-page opt-out of the global filters without a fresh,
    explicit instruction to do so.

    The selection lives in session_state under stable keys, so setting July on
    Summary and opening Transactions keeps July. Changing a widget here reruns
    the page through Streamlit's own mechanism — this function never calls
    st.rerun() on its own account, which would reset the page and lose the
    scroll position (Clear filters and Refresh are the deliberate exceptions:
    both replace the whole selection, so there is no scroll position worth
    protecting).

    **No default date (rule 8 — no hardcoded dates).** From and To open
    EMPTY. Empty means every reconciled date that has been loaded — resolved
    here to the actual first/last loaded date so every caller gets concrete
    dates to query with — but the WIDGETS themselves stay visibly empty
    rather than silently pre-filled with a date the operator never chose.
    What "empty" resolved to is written beside the page title instead
    (`_write_range_label`), so it is never a guess what is on screen.
    """
    st.markdown(SIDEBAR_CSS, unsafe_allow_html=True)

    # Clear filters is handled HERE, before any of this function's widgets
    # are instantiated below — Streamlit refuses to write to a widget's own
    # session_state key once that widget has been created in the current
    # run ("cannot be modified after the widget ... is instantiated"). The
    # button itself (further down) only sets this flag and reruns; the
    # actual reset happens on the NEXT run, before the widgets exist yet.
    if st.session_state.pop("_pending_clear_filters", False):
        st.session_state["w_flt_from"] = None
        st.session_state["w_flt_to"] = None
        st.session_state["w_flt_store"] = "All stores"
        st.session_state[K_DATE_FROM] = None
        st.session_state[K_DATE_TO] = None
        st.session_state[K_STORE] = "All stores"

    dates = loaded_dates(run.id) if run else []
    with st.sidebar:
        st.divider()
        if not dates:
            st.caption("No reconciled dates yet — filters appear after a run.")
            _operator_footer()
            return None, None, None

        first, last = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        st.session_state.setdefault(K_STORE, "All stores")

        # The quick-picker offers one year beyond the loaded range (the
        # "next calendar year", so a year is selectable before its data
        # arrives — see `_quick_date_picker`) — the upper bound everything
        # below uses must match that, or a click into the padding year (or
        # a hand-typed date inside it) is silently dropped straight back to
        # "every loaded date" on the very next rerun.
        picker_max = _padding_upper_bound(last)

        # A remembered date outside what THIS run loaded (or the one
        # padding year beyond it) is dropped back to empty ("every loaded
        # date") rather than left dangling out of range.
        if (st.session_state.get(K_DATE_FROM) is not None
                and not (first <= st.session_state[K_DATE_FROM] <= picker_max)):
            st.session_state[K_DATE_FROM] = None
        if (st.session_state.get(K_DATE_TO) is not None
                and not (first <= st.session_state[K_DATE_TO] <= picker_max)):
            st.session_state[K_DATE_TO] = None

        st.markdown("**Filters**")

        _quick_date_picker(dates, first, last)

        # The canonical values live in PLAIN session keys, and the widgets
        # carry their own separate keys seeded from them. Streamlit
        # garbage-collects the state of a widget that is not rendered in
        # the current run, so a widget key alone does NOT survive a page
        # switch: setting July on Summary and opening Transactions
        # silently reverted to the default. Writing back to the plain key
        # is what makes the filter global. `max_value` is `picker_max`, not
        # `last` — a hand-typed date inside the one padding year must be
        # accepted by the widget itself, not just by the reset check above.
        st.session_state[K_DATE_FROM] = st.date_input(
            "From", value=st.session_state.get(K_DATE_FROM),
            min_value=first, max_value=picker_max, key="w_flt_from",
            format="DD/MM/YYYY")
        st.session_state[K_DATE_TO] = st.date_input(
            "To", value=st.session_state.get(K_DATE_TO),
            min_value=first, max_value=picker_max, key="w_flt_to",
            format="DD/MM/YYYY")

        if show_store:
            options = ["All stores"] + (loaded_stores(run.id) if run else [])
            if st.session_state[K_STORE] not in options:
                st.session_state[K_STORE] = "All stores"
            st.session_state[K_STORE] = st.selectbox(
                "Store", options, index=options.index(st.session_state[K_STORE]),
                key="w_flt_store")

        cols = st.columns(2)
        if cols[0].button("Clear filters", key="flt_clear", width="stretch"):
            # The actual reset happens at the top of this function, on the
            # NEXT run — see the comment there for why it cannot happen here.
            st.session_state["_pending_clear_filters"] = True
            st.rerun()
        if cols[1].button(
                "Refresh", key="flt_refresh", width="stretch",
                help="Re-reads the current results from the database and "
                     "redraws the page. This refreshes the VIEW, not the "
                     "run — it does not re-reconcile anything. Open Run "
                     "for that."):
            st.cache_data.clear()
            st.rerun()

        # The run stamp, directly under the filters: what a view shows is as of
        # the last run, not as of now (§6.14), so it is never buried. `run` is
        # None on Run/Masters before the very first reconciliation — a page
        # that needs a run at all already calls no_run_yet() and stops before
        # reaching sidebar()'s caller, but this stamp itself must not assume
        # that guard already ran (Run.py and Masters.py call sidebar() with a
        # possibly-None run on purpose, since that is where the first run is
        # created).
        if run:
            finished = (f"{run.finished_at:%d %b}" if run.finished_at
                       else "in progress")
            st.markdown(
                f"<div class='mop-sidebar-stamp'>"
                f"Showing run #{run.id} — {first:%d/%m/%Y} to {last:%d/%m/%Y} — "
                f"completed {finished}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div class='mop-sidebar-stamp'>No run yet</div>",
                unsafe_allow_html=True,
            )

        # The build stamp is no longer page text (2026-10-14): it lives in the Live pill's
        # tooltip (`build_stamp()`, `_write_status_pill`) so "which build am I on" is still
        # answerable, and in the code (`sys._mop_reco_started_at`, selfcheck).

        _operator_footer()

    date_from = st.session_state.get(K_DATE_FROM) or first
    date_to = st.session_state.get(K_DATE_TO) or last
    if date_from > date_to:
        st.error("'From' is after 'To' — no dates are selected. Fix it in the sidebar.")
        st.stop()

    store = st.session_state[K_STORE]
    _write_range_label(date_from, date_to, None if store == "All stores" else store)
    _write_status_pill(run, date_from, date_to, None if store == "All stores" else store)
    return date_from, date_to, (None if store == "All stores" else store)


def _operator_footer():
    """"Working as: <name>" at the foot of the sidebar, with a change link.

    Nothing is asked here. If no name has been given yet the line simply says
    so — the question is put at the point of the action instead
    (see require_operator).
    """
    name = st.session_state.get(K_OPERATOR)
    st.markdown("<div class='op-footer'></div>", unsafe_allow_html=True)
    with st.container():
        if name:
            left, right = st.columns([2, 1])
            left.markdown(
                f"<div class='mop-sidebar-stamp'>"
                f"Working as: <b>{name}</b></div>", unsafe_allow_html=True)
            if right.button("Change", key="op_change"):
                st.session_state.pop(K_OPERATOR, None)
                st.rerun()
        # Not signed in: nothing is shown - the name is asked when something is first recorded.


def no_run_yet():
    st.info(
        "No reconciliation has been run yet. Open **Upload** to reconcile a date range — "
        "a full month takes about 16 minutes."
    )
    st.stop()


def stop_if_no_dates(date_from, run):
    """A run row EXISTS (latest_run() didn't return None) but has produced
    no reconciled dates yet — sidebar() returns (None, None, None) in that
    case (CLAUDE.md rule 8: never fall back to a fake date). Distinct from
    no_run_yet(): this is a database with a run, not a database with no run
    at all.

    Since latest_run() became completed-only (2026-09-26), the run this
    guards against is never the "still running" case any more — a
    completed run with zero dates means it genuinely reconciled an empty
    range (every requested date came back a no_data_date). The message
    below still points at Run rather than guessing further, since that is
    where the actual date range and any no_data_dates are shown.
    Every page that calls sidebar() and then uses date_from/date_to must
    call this right after, or a None date reaches a query expecting a real
    one (e.g. `date_from.isoformat()`) and raises AttributeError instead of
    showing something sensible."""
    if date_from is not None:
        return
    st.info(
        f"Run #{run.id} completed but produced no reconciled dates for this "
        "range. Open **Upload** to check what it actually loaded."
    )
    st.stop()


# --- exports ---------------------------------------------------------------

MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


def export_all_button(fetch_all, total: int, meta: ExportMeta, key: str,
                      noun: str = "matching rows"):
    """One button: 'Export all N matching rows', where N is the FILTERED count.

    `fetch_all` is a callable taking a `progress(done, total)` callback and
    returning every matching row — never the page on screen. The build runs on
    click, so opening the page never costs a full-month query.
    """
    if not total:
        return

    label = f"Export all {fmt_count(total)} {noun}"
    fmt = "CSV" if total > XLSX_ROW_LIMIT else "Excel"
    if st.button(label, key=key, width="stretch",
                 help=f"{fmt_count(total)} rows match the current filters. "
                      f"Will produce {fmt} "
                      f"({'above' if total > XLSX_ROW_LIMIT else 'within'} the "
                      f"{fmt_count(XLSX_ROW_LIMIT)}-row Excel threshold)."):
        # Three honest phases: COLLECTING (rows are read and shaped, up to 60% - the file's own
        # columns are checked on the first chunk, so a schema error surfaces here, not at the end),
        # BUILDING (the workbook or CSV is written, 60-95%), READY. The bar never reaches 100% until
        # the file exists; any failure is shown as an error with its reason, not as a bar that
        # filled and then threw.
        status = st.status(f"Collecting {fmt_count(total)} rows…", expanded=True)
        bar = status.progress(0.0)

        def fetched(done, of):
            bar.progress(min(0.6, 0.6 * done / max(1, of)))
            status.update(label=f"Collecting rows - {fmt_count(done)} of {fmt_count(of)}…")

        def writing(fraction, message):
            bar.progress(0.6 + 0.35 * fraction)
            status.update(label=f"Building the file - {message}")

        st.session_state.pop(f"{key}_result", None)
        try:
            df = fetch_all(fetched)
            status.update(label=f"Collected {fmt_count(len(df))} rows - building the file…")
            result = build_export(df, meta, progress=writing)
        except Exception as exc:  # noqa: BLE001 - shown with its reason, never swallowed
            bar.empty()
            status.update(label=f"Export failed - {type(exc).__name__}", state="error", expanded=True)
            st.error(f"The export could not be built: {exc}")
        else:
            bar.progress(1.0)
            status.update(label=f"Ready - {result.filename} ({fmt_count(result.rows)} rows). {result.note}",
                          state="complete")
            st.session_state[f"{key}_result"] = result

    result = st.session_state.get(f"{key}_result")
    if result is not None:
        st.download_button(
            f"Download {result.filename} ({fmt_count(result.rows)} rows, "
            f"{result.fmt.upper()})",
            result.data, file_name=result.filename, key=f"{key}_dl",
            mime=MIME[result.fmt], width="stretch",
        )
        st.caption(
            f"{result.fmt.upper()} — {result.note} Reference ids are written as text, "
            "so no id is truncated or shown in scientific notation. The file opens "
            "with a header block: run, run timestamp, date range, filters applied, "
            "row count and total value."
        )


def frame_export_button(df: pd.DataFrame, meta: ExportMeta, key: str,
                        noun: str = "matching rows"):
    """Same button for a table already fully in memory (no paging behind it)."""
    if df is None or df.empty:
        return
    export_all_button(lambda _progress: df, len(df), meta, key, noun)


# --- presentation helpers -------------------------------------------------

# Kept for backward compatibility with every existing tile() call site, which
# passes a semantic `state` (does this number need a look?). The 2026-09-18
# restyle deliberately stopped using that to colour the tile: the operator's
# own brief reserves the left accent bar for exactly two named tiles (Cash
# receivable, Digital receivable — see `accent=`) and genuine break/mismatch
# tiles, "everything else — no accent bar", so five tiles stop competing for
# attention. `state` is still accepted (nothing calling tile() needed to
# change) but no longer drives any colour; only `accent` does.
STATE_COLOUR = {"clean": "good", "attention": "warn", "action": "bad",
                "neutral": None}

_ACCENT_CLASS = {"warn": "mop-tile--warn", "bad": "mop-tile--bad", None: ""}


def tile(container, label: str, value: str, state: str = "neutral", note: str = "",
        tooltip: str = "", accent: str = None, feature: bool = False):
    """A tile — white surface, 1px border, radius 16px (theme.css `.mop-tile`).

    `accent` puts a 3px left border on the tile — "warn" or "bad" — and
    nothing else does; colour is never a filled background and never the
    only signal (the word itself is always in `note`/`tooltip` too).
    `feature=True` is for the ONE navy, white-text tile (Total sale) — every
    other tile stays white; do not set it on more than one tile per page.

    A cut-off figure on a financial screen is worse than no figure — never
    truncated. `min-width` (in theme.css) keeps the value from being clipped
    by a narrow column at any window size, and `tooltip` (defaulting to
    `note` when not given) puts the exact figure on hover via the native
    title attribute, on top of whatever is already shown in the note line
    beneath the value.
    """
    hover = (tooltip or note).replace('"', "&quot;")
    classes = ["mop-tile"]
    if feature:
        classes.append("mop-tile--feature")
    elif accent:
        classes.append(_ACCENT_CLASS.get(accent, ""))
    sub_class = "mop-tile-sub"
    if accent and accent in ("warn", "bad", "good") and not feature:
        sub_class += f" mop-tile-sub--{accent}"

    container.markdown(
        f"""<div class="{' '.join(c for c in classes if c)}" title="{hover}">
              <div class="mop-tile-label">{label}</div>
              <div class="mop-tile-figure mop-num">{value}</div>
              <div class="{sub_class}">{note}</div>
            </div>""",
        unsafe_allow_html=True,
    )


# Status "pills" for a table (2026-09-18 restyle, brief section 4). A pandas
# Styler applied to a plain text/string column, read by st.dataframe's own
# Styler support (background_color/color via `.map`) — never an HTML string
# baked into the cell value. The value st.dataframe sorts, filters and the
# frame our own export.py writes are both the untouched original DataFrame;
# the Styler is display-only and is never passed to build_export().
#
# Literal hex values, NOT `var(--good-tint)` etc: st.dataframe's grid
# (glide-data-grid) draws cell colours on an HTML canvas, and a canvas
# `fillStyle` cannot resolve a CSS custom property — passing "var(...)"
# renders the cell blank instead of tinted (found live, 2026-09-18: the
# Status/Lifecycle columns went empty, not merely uncoloured, until this was
# switched to literal hex). Kept in sync with theme.css's :root values by
# hand; there is no way to share one definition across a CSS file and a
# canvas-consumed Python dict.
STATUS_TINT = {
    "good": ("#E7F6EF", "#15A66B"),
    "warn": ("#FDF2E3", "#E8962E"),
    "bad":  ("#FCEAEE", "#D93A5B"),
    "accent": ("#E6F6FA", "#16A5C4"),
}


def status_styler(df: pd.DataFrame, columns: dict):
    """`df.style` with one or more columns' cells tinted per their own
    mapping — `{"Status column": {"open": "warn", "closed": "good", ...}}`
    — for `st.dataframe(status_styler(df, columns))`. Multiple columns in
    one call so callers chain nothing themselves.

    The column's own text is untouched — the word is still what a reader
    (or a black-and-white printout) reads; colour is additional, never the
    only signal, and never baked into the value itself.
    """
    styler = df.style
    for column, mapping in columns.items():
        def _tint(value, mapping=mapping):
            key = mapping.get(value)
            if not key:
                return ""
            bg, fg = STATUS_TINT[key]
            return f"background-color: {bg}; color: {fg}"
        styler = styler.map(_tint, subset=[column])
    return styler


def money_column_config(*columns) -> dict:
    """Right-aligned, tabular, Indian-grouped money columns for st.dataframe.

    Streamlit's NumberColumn cannot do lakh/crore grouping, so money is passed
    in pre-formatted as text and right-aligned here.
    """
    return {c: st.column_config.TextColumn(c, width="small") for c in columns}


def blocked_panel(title: str, missing: str, detail: str = ""):
    with st.container(border=True):
        st.markdown(f"**{title}** — cannot run")
        st.markdown(f"Missing: **{missing}**")
        if detail:
            st.caption(detail)
