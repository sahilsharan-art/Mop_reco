"""Exceptions - the work queue (2026-10-14).

Two sections on one tab. EXCEPTIONS: bills with no matching transaction and transactions with
no matching bill (moved here from Summary unchanged, plus candidate transactions found for each
open bill - mop_reco/exception_pairing.py). MANUAL MATCHING: By bill / By transaction matching,
the bulk upload, the refund upload and the manual-match report and register (moved here from the
former Manual matches page, function unchanged). Nothing recorded there is lost: it is read from
the same tables, and every match and refund stays in force.
"""

from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_common import (
    confirm_with_operator,
    db_session,
    export_all_button,
    frame_export_button,
    latest_run,
    no_run_yet,
    page,
    require_operator,
    sidebar,
    status_styler,
    stop_if_no_dates,
    tile,
)
from mop_reco import exception_pairing as ep
from mop_reco import exception_tables as et
from mop_reco import exception_remarks as er
from mop_reco import exceptions as exc_mod
from mop_reco import gateway_txns as gt
from mop_reco import io_
from mop_reco import manual_match as mm
from mop_reco import queries as q
from mop_reco import refunds as rf
from mop_reco.exceptions import bill_exception_key, txn_exception_key
from mop_reco.export import ExportMeta
from mop_reco.formatting import blank
from mop_reco.formatting import blank as fmt_blank
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_short, money_table

CONFIG_PATH = Path(__file__).resolve().parents[2] / "app" / "config.yaml"

page("Exceptions", "Bills with no matching transaction, transactions with no matching bill, and the "
                   "matching and refund tools that clear them.")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)

with CONFIG_PATH.open("r", encoding="utf-8") as _fh:
    CONFIG = yaml.safe_load(_fh)

SECTION_DEFAULTS = {"store_detail": False, "matched": False, "exceptions": True,
                    "closed_exceptions": False}


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



# ===========================================================================
# Section open/closed state - plain session keys (as on Summary before the move)
# ===========================================================================
# ===========================================================================
# Section: Exceptions — expanded by default (2026-09-17). Two tables, never
# merged into one count: bills with no matching transaction, and
# transactions with no matching bill. Plain tables, no drill-through — the
# rich grouped/detail Exceptions page is gone; this is the work queue.
# ===========================================================================
BILL_EXCEPTION_TYPES = q.NOT_RECEIVED_TYPES
TXN_EXCEPTION_TYPES = q.RECEIVED_NOT_BILLED_TYPES + ("original_txn_not_found", "store_ambiguous")

BILL_EXC_COLUMNS = et.BILL_BASE_COLUMNS
TXN_EXC_COLUMNS = et.TXN_COLUMNS

# Narrower id/remark columns (2026-09-29) — with bill_time/created_by/
# transaction_time added, 10 columns no longer fit this grid's own
# container at 1366px without its internal horizontal scrollbar
# appearing. Rather than drop a column, the two free-text columns that
# never need much width to stay readable (bill_no/transaction_id are
# short codes, remark/remark_by are usually empty or a short phrase) give
# theirs back — every column stays, none gets dropped. remark_at
# (2026-09-30) gets the same treatment.
BILL_EXC_COLUMN_CONFIG = {
    "bill_no": st.column_config.TextColumn(width="small"),
    "remark": st.column_config.TextColumn(width="small", help="Click to edit — saves on Enter."),
    "remark_by": st.column_config.TextColumn(width="small"),
    "remark_at": st.column_config.TextColumn(width="small"),
}
TXN_EXC_COLUMN_CONFIG = {
    "transaction_id": st.column_config.TextColumn(width="small"),
    "remark": st.column_config.TextColumn(width="small", help="Click to edit — saves on Enter."),
    "remark_by": st.column_config.TextColumn(width="small"),
    "remark_at": st.column_config.TextColumn(width="small"),
}


def _bill_exception_frame(page_df):
    """One page of bill exceptions - the shared builder (mop_reco/exception_tables.py)."""
    return et.bill_frame(session, run.id, page_df)


def _txn_exception_frame(page_df):
    return et.txn_frame(session, page_df)


EXCEPTION_STATUS_TINT = {"open": "warn", "under_investigation": "accent",
                         "closed": "good", "auto_resolved": "good"}


def _format_exceptions(df):
    """The plain (unstyled) display frame — amount/date formatted, nothing
    else. Used directly by the editable grid (2026-09-30), since
    `st.data_editor` cannot render a pandas Styler the way `st.dataframe`
    can (no equivalent hook — the same canvas-grid constraint already
    documented for the particulars matrix, §8.1)."""
    if df.empty:
        return df
    out = df.copy()
    out["amount"] = out["amount"].map(money_table)
    if "date" in out.columns:
        out["date"] = out["date"].map(display_date)
    return out


def _display_exceptions(df):
    """The READ-ONLY, tinted display — used only by the "Closed and
    auto-resolved" register at the bottom, which stays st.dataframe."""
    out = _format_exceptions(df)
    if not out.empty and "status" in out.columns:
        return status_styler(out, {"status": EXCEPTION_STATUS_TINT})
    return out


def _editable_exceptions(df, column_list, column_config, table_key, id_col, label_prefix, pairing=None):
    """Inline-editable remark column (2026-09-30) — every OTHER column is
    `disabled=True`; a remark never changes a figure, a status or a match
    (rule 4). The operator's name is asked inline, at the point of the
    action (never up front), via the same `require_operator()` pattern
    every other write in this app already uses. A detected change writes
    through `exceptions.add_remark()` — append-only, a brand new `Remark`
    row every time, never an overwrite — the instant the name is known;
    until then the typed text stays in the widget with a caption saying it
    is not yet saved.

    History: `st.data_editor`'s canvas grid has no per-row expander or
    tooltip hook (the same constraint already documented for the
    particulars matrix, §8.1), so "prior versions in a tooltip or a small
    expander on the row" becomes the practical equivalent here — a picker
    naming every row that carries a remark, showing that one row's full
    history, oldest first, the instant it's picked."""
    if df.empty:
        st.caption("Nothing in this view.")
        return
    shown = _format_exceptions(df).reset_index(drop=True)
    baseline = shown["remark"].fillna("").copy()

    edited = st.data_editor(
        shown, hide_index=True, width="stretch", key=table_key,
        column_order=column_list,
        disabled=[c for c in shown.columns if c not in (("remark", "Txn 1", "Txn 2", "Match") if pairing else ("remark",))],
        column_config=column_config,
    )
    if pairing:
        _handle_pairing(edited, shown, df, pairing, table_key)

    changed = edited.index[edited["remark"].fillna("") != baseline]
    if len(changed):
        # Asked only now — at the point of the action, never up front —
        # the same `require_operator()` pattern used everywhere else in
        # this app. The typed remark stays in the widget (Streamlit keeps
        # it keyed to `table_key`) until a name is given, then saves on
        # the very next rerun.
        operator = require_operator("adding a remark", key=f"{table_key}_op")
        if not operator:
            st.warning(f"{len(changed)} remark(s) typed but not yet saved — "
                       "give your name above to save them.")
        else:
            for idx in changed:
                text = str(edited.loc[idx, "remark"]).strip()
                if text:
                    exc_mod.add_remark(session, shown.loc[idx, "exception_key"], text, operator)
            st.session_state.pop(table_key, None)
            st.rerun()

    have_remark = shown[shown["remark"].astype(str).str.strip() != ""]
    if not have_remark.empty:
        options = ["—"] + [f"{label_prefix} {v}" for v in have_remark[id_col]]
        picked = st.selectbox("View remark history for", options,
                              key=f"{table_key}_history_pick")
        if picked != "—":
            picked_id = picked[len(label_prefix) + 1:]
            hist_row = have_remark[have_remark[id_col].astype(str) == picked_id].iloc[0]
            history = exc_mod.remarks_for(session, hist_row["exception_key"])
            st.dataframe(pd.DataFrame([
                {"When": r.created_at.strftime("%d/%m/%Y %H:%M"),
                 "Who": r.created_by or "", "Remark": r.remark_text}
                for r in history
            ]), hide_index=True, width="stretch")


def _bulk_remark_upload(label, validate_fn, template_bytes, key_prefix):
    """Validate-then-preview-then-confirm, all-or-nothing on apply — the
    same shape `manual_match.py`'s and `refunds.py`'s own bulk uploads
    already use. Nothing is written until the operator explicitly clicks
    Apply, after seeing exactly which rows were accepted and why any
    others were not."""
    with st.expander(f"Bulk upload remarks — {label}"):
        st.download_button(
            "Download template", template_bytes,
            file_name=f"{key_prefix}_remarks_template.csv", mime="text/csv",
            key=f"{key_prefix}_template_dl")
        uploaded = st.file_uploader(
            "Upload a filled template (.csv or .xlsx)",
            type=["csv", "xlsx"], key=f"{key_prefix}_upload")
        if uploaded is None:
            return
        raw_df = er.read_upload(uploaded.getvalue(), uploaded.name)
        results = validate_fn(session, raw_df)
        preview = er.preview_frame(results)
        accepted_n = sum(1 for r in results if r.ok)
        rejected_n = len(results) - accepted_n
        st.markdown(f"**{fmt_count(accepted_n)} accepted, {fmt_count(rejected_n)} rejected**")
        st.dataframe(preview, hide_index=True, width="stretch")
        if accepted_n == 0:
            st.caption("Nothing to apply.")
            return
        operator = require_operator("applying this bulk upload", key=f"{key_prefix}_bulk_op")
        if st.button(f"Apply {accepted_n} accepted remark(s)", key=f"{key_prefix}_bulk_apply",
                    disabled=not operator):
            accepted_rows = [r for r in results if r.ok]
            n = er.apply_bulk_remarks(session, accepted_rows, operator)
            st.success(f"Applied {n} remark(s).")
            st.session_state.pop(f"{key_prefix}_upload", None)
            st.rerun()


# Everything below is SERVER-SIDE (2026-10-13): the header counts and values are SQL
# aggregates; a table reads ONE page (sorted and filtered in the query); a collapsed
# section reads nothing but its count; the full population is fetched only by an export
# button, in chunks. Section state, the Show/Sort choices and each pager's offset live in
# plain session keys, so leaving the page and coming back does not reset them or
# recompute anything that is not on screen.
EXC_PAGE = 50
CLOSED_STATUSES = ("closed", "auto_resolved")


def _persisted_select(label, options, value_key, widget_key, container=None):
    """A selectbox whose CHOICE survives navigation. A widget's own state is dropped by
    Streamlit when its page is not rendered, so the choice is also kept under a plain
    key and used as the widget's starting index on the next visit."""
    current = st.session_state.get(value_key, options[0])
    if current not in options:
        current = options[0]
    chosen = (container or st).selectbox(label, options, index=options.index(current), key=widget_key)
    st.session_state[value_key] = chosen
    return chosen


def _pager(name: str, total: int, fingerprint) -> int:
    """Previous/Next for a server-side page. The offset is a plain session key (survives
    navigation) and resets when the filters behind it change."""
    off_key, fp_key = f"sm_{name}_offset", f"sm_{name}_fp"
    st.session_state.setdefault(off_key, 0)
    if st.session_state.get(fp_key) != fingerprint:
        st.session_state[fp_key] = fingerprint
        st.session_state[off_key] = 0
    last_start = max(0, (total - 1) // EXC_PAGE * EXC_PAGE) if total else 0
    offset = min(st.session_state[off_key], last_start)
    st.session_state[off_key] = offset
    if total > EXC_PAGE:
        p1, p2, p3 = st.columns([1, 1, 3])
        if p1.button("Previous", disabled=offset == 0, key=f"{name}_prev"):
            st.session_state[off_key] = max(0, offset - EXC_PAGE)
            st.rerun()
        if p2.button("Next", disabled=offset + EXC_PAGE >= total, key=f"{name}_next"):
            st.session_state[off_key] = offset + EXC_PAGE
            st.rerun()
        p3.markdown(
            f"<div class='mop-page-info'>Showing "
            f"<b>{fmt_count(offset + 1)}-{fmt_count(min(offset + EXC_PAGE, total))}</b> "
            f"of <b>{fmt_count(total)}</b></div>", unsafe_allow_html=True)
    return offset


# ===========================================================================
# Pairing (2026-10-14): each open bill is shown with the unmapped transactions the system
# found for it - same store, exact amount, within +/- PAIR_MINUTES then +/- PAIR_DAYS, nearest in
# time first. Txn 1 / Txn 2 are also editable (paste an id yourself). Match performs the manual
# match on the id then showing in Txn 1 or Txn 2, with full validation and the same cascade as
# every other manual match.
# ===========================================================================
PAIR_MINUTES, PAIR_DAYS = ep.window_settings(CONFIG)
MATCH_CHOICES = ["", "Match Txn 1", "Match Txn 2"]
BILL_EXC_PAIR_COLUMNS = et.bill_columns(pairing=True, screen=True)
PAIR_COLUMN_CONFIG = {
    "Txn 1": st.column_config.TextColumn("Txn 1", width="medium", help="Found by the system - or paste an id."),
    "Txn 2": st.column_config.TextColumn("Txn 2", width="medium", help="Found by the system - or paste an id."),
    "Amount 1": st.column_config.TextColumn("Amount 1", width="small"),
    "Amount 2": st.column_config.TextColumn("Amount 2", width="small"),
    "More": st.column_config.TextColumn("More", width="medium", help="Ambiguous cluster, or a split payment."),
    "Match": st.column_config.SelectboxColumn("Match", options=MATCH_CHOICES, width="small",
                                              help="Choose which transaction to match this bill to."),
}


def _pairing_fp():
    """What the cached global assignment depends on. Anything that can change which bills are open or
    which transactions are unmapped must be in here, or accepting a match leaves stale suggestions:
      * the run (a new run rebuilds the exceptions),
      * the manual-match state - how many are ACTIVE (a reversal changes it) and the newest id,
      * the refund count (a refunded transaction leaves the pool),
      * the open exception population itself - its size and its newest row - which moves when a bill
        or transaction is closed, auto-resolved or re-raised by any route.
    There is deliberately NO date/store filter: the assignment must see the WHOLE open population,
    or the same transaction could be suggested to different bills in different filtered views."""
    from sqlalchemy import func

    from mop_reco.db import Exception_
    n = session.query(mm.ManualMatch.id).filter(mm.ManualMatch.lifecycle == "active").count()
    top = session.query(mm.ManualMatch.id).order_by(mm.ManualMatch.id.desc()).limit(1).scalar()
    refunds = session.query(rf.TransactionRefund.id).count()
    live = (session.query(func.count(Exception_.id), func.max(Exception_.id))
            .filter(Exception_.type.in_(tuple(q.NOT_RECEIVED_TYPES) + ("unmapped_gateway_transaction",)),
                    Exception_.status.in_(("open", "under_investigation"))).one())
    return (run.id, n, top, refunds, int(live[0] or 0), live[1])


@st.cache_data(ttl=300, show_spinner=False)
def _cached_assignment(fp):
    """The candidate set for the WHOLE open population, as one global assignment (a transaction is
    suggested to at most one bill). Cached on the manual-match / refund state; never computed from a page."""
    sess = db_session()
    pool = ep.unmapped_pool(sess)
    bills = ep.open_population(sess, run.id)
    return pool, bills, ep.assign(bills, pool, PAIR_MINUTES, PAIR_DAYS, ep.mop_family(CONFIG))


def _add_pairing(frame):
    """The screen's view: the shared pairing, with the candidate amounts formatted for display."""
    _pool, _bills, assignment = _cached_assignment(_pairing_fp())
    out, cands = et.add_pairing(frame, assignment)
    for n in (1, 2):
        out[f"Amount {n}"] = out[f"Amount {n}"].map(lambda v: money_table(v) if pd.notna(v) else "")
    return out, cands


def _handle_pairing(edited, shown, raw, pairing, table_key):
    """Act on a Match choice, and validate any id the person pasted into Txn 1 / Txn 2."""
    cands = pairing
    for i in edited.index:
        row = raw.iloc[i]
        bill = {"business_date": row["date"], "store": row["store"], "bill_no": row["bill_no"],
                "mop": row["mop"], "amount": float(row["amount"])}
        choice = str(edited.at[i, "Match"] or "")
        if choice:
            n = 1 if choice.endswith("1") else 2
            ref = io_.clean_ref(edited.at[i, f"Txn {n}"]) or ""
            _do_match(bill, ref, cands.get(row["exception_key"]) or [], table_key)
            return
        for n in (1, 2):
            if str(edited.at[i, f"Txn {n}"] or "") != str(shown.at[i, f"Txn {n}"] or ""):
                ref = io_.clean_ref(edited.at[i, f"Txn {n}"]) or ""
                if ref:
                    v = _validate_ref(bill, ref, cands.get(row["exception_key"]) or [])
                    if v.ok:
                        st.success(f"Bill {bill['bill_no']}: {ref} found in {v.gateway.upper()} - amount and store "
                                   f"agree. Choose **Match Txn {n}** in the Match column to apply it.")
                    else:
                        st.error(f"Bill {bill['bill_no']}: {ref} cannot be matched - " + " | ".join(v.problems))


def _validate_ref(bill, ref, cand_list):
    """Validate one transaction id for one bill. A system-found candidate is checked against its
    own known row (fast); a pasted id is searched across the whole warehouse."""
    hit = next((c for c in cand_list if c.txn_id == ref), None)
    if hit is not None:
        pool = pd.DataFrame([{"gateway": hit.gateway, "txn_ref": hit.txn_id, "business_date": hit.date,
                              "store": bill["store"], "amount": hit.amount, "status": "SUCCESS",
                              "txn_type": "ACQUIRING", "payment_mode": None}])
    else:
        pool = mm.targeted_pool(ref)
    return mm.validate_by_id(session, bill, ref, gateway_pool=pool)


def _do_match(bill, ref, cand_list, table_key):
    if not ref:
        st.error(f"Bill {bill['bill_no']}: Match was chosen but that Txn cell is empty.")
        return
    suggested = next((c for c in cand_list if c.txn_id == ref), None)
    if suggested is not None:
        if len(cand_list) == 2 and abs(suggested.amount - bill["amount"]) > 0.005:
            st.warning(f"Bill {bill['bill_no']} looks like a SPLIT payment: Txn 1 + Txn 2 add up to the bill "
                       f"(Rs {money_table(bill['amount'])}). A manual match holds one transaction per bill, so it is "
                       "not matched with one click - decide it by hand (By bill, with an override reason).")
            return
        # A suggestion is only a suggestion: re-read the world now. Another bill may have taken this
        # transaction since the page was built (a manual match, a refund, a closed exception).
        fresh = ep.unmapped_pool(session)
        if ref not in set(fresh["txn_id"]):
            holder = session.query(mm.ManualMatch).filter(mm.ManualMatch.transaction_ref == ref,
                                                          mm.ManualMatch.lifecycle == "active").first()
            who = (f" - already matched to bill {holder.bill_no} ({holder.store}, {holder.business_date})"
                   if holder else " - it has since been matched, refunded or closed")
            st.error(f"Bill {bill['bill_no']}: refused - transaction {ref} is no longer an open unmapped "
                     f"transaction{who}.")
            st.cache_data.clear()
            return
    v = _validate_ref(bill, ref, cand_list)
    if not v.ok:
        st.error(f"Bill {bill['bill_no']}: refused - " + " | ".join(v.problems))
        return
    operator = require_operator("matching this bill", key=f"{table_key}_match_op")
    if not operator:
        st.warning("Give your name above to record this match.")
        return
    try:
        mm.record(session, bill, v.gateway, ref, "manual_match_by_bill", operator, v)
    except (ValueError, LookupError) as exc:
        st.error(f"Bill {bill['bill_no']}: refused - {exc}")
        return
    others = [c for c in cand_list if c.txn_id != ref]
    note = ""
    if others:
        note = (f" **Second transaction still open:** {len(others)} other unmapped transaction(s) with the same "
                f"store and amount ({', '.join(c.txn_id for c in others[:3])}{'...' if len(others) > 3 else ''}). "
                "The bill stays closed on the one you matched - decide whether the other is a refund (Refund "
                "upload, below) or belongs to a second bill.")
    st.session_state["_pair_msg"] = f"Matched {bill['bill_no']} to {ref} ({v.gateway}).{note}"
    st.session_state.pop(table_key, None)
    st.cache_data.clear()
    st.rerun()


def _cluster_resolver(frame):
    """Ambiguous clusters on this page: N bills and M transactions of one store and amount that cannot
    be separated. Nothing is suggested for them; they are offered here to be resolved as a SET. When
    N == M every bill can be paired (one transaction each, in time order) - amounts are equal, so the
    pairing moves no figure - with one button; otherwise the cluster is listed for a decision."""
    _pool, bills_pop, assignment = _cached_assignment(_pairing_fp())
    on_page = [k for k in frame["exception_key"] if k in assignment.ambiguous]
    if not on_page:
        return
    bn = dict(zip(frame["exception_key"], frame["bill_no"]))
    labels = {}
    for k in on_page:
        labels.setdefault(assignment.ambiguous[k], f"{bn[k]} - {assignment.flag(k)}")
    pick = st.selectbox("Resolve an ambiguous cluster", ["-"] + list(labels.values()),
                        key="pair_cluster_pick")
    if pick == "-":
        return
    cid = next(c for c, lab in labels.items() if lab == pick)
    cl = assignment.clusters[cid]
    pool = _pool[_pool["txn_id"].isin(cl["txns"])].sort_values(["time", "txn_id"])
    b = bills_pop[bills_pop["bill_key"].isin(cl["bills"])].sort_values(["time", "bill_no"])
    st.markdown(f"**{len(b)} bills and {len(pool)} transactions** of store {b['store'].iloc[0]} for "
                f"Rs {money_table(float(b['amount'].iloc[0]))} cannot be told apart.")
    c1, c2 = st.columns(2)
    c1.dataframe(b[["bill_no", "date", "time", "mop"]].assign(date=lambda d: d["date"].map(display_date)),
                 hide_index=True, width="stretch")
    c2.dataframe(pool[["txn_id", "gateway", "date", "time"]].assign(date=lambda d: d["date"].map(display_date)),
                 hide_index=True, width="stretch")
    if len(b) == len(pool):
        operator = confirm_with_operator(
            st.button(f"Pair all {len(b)} in time order", key=f"pair_cluster_go_{cid}"),
            "recording these matches", key=f"pair_cluster_op_{cid}")
        if operator:
            done, failed = 0, []
            for brow, trow in zip(b.itertuples(), pool.itertuples()):
                bill = {"business_date": brow.date, "store": brow.store, "bill_no": brow.bill_no, "mop": brow.mop,
                        "amount": float(brow.amount)}
                v = mm.validate_by_id(session, bill, trow.txn_id, gateway_pool=pd.DataFrame([{
                    "gateway": trow.gateway, "txn_ref": trow.txn_id, "business_date": trow.date, "store": trow.store,
                    "amount": trow.amount, "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None}]))
                if not v.ok:
                    failed.append((brow.bill_no, " | ".join(v.problems)))
                    continue
                mm.record(session, bill, v.gateway, trow.txn_id, "manual_match_by_bill", operator, v)
                done += 1
            st.cache_data.clear()
            st.session_state["_pair_msg"] = f"Paired {done} of {len(b)}." + (f" Refused: {failed}" if failed else "")
            st.rerun()
    else:
        st.caption("The counts differ, so this cannot be paired as a set - match each bill by hand (By bill), or "
                   "refund the extra transactions.")


def _exception_table_block(kind: str, statuses, scope_label: str, editable: bool, sort_label: str):
    """One exception table (bills or transactions) for the given statuses: header with the
    SQL count/value, one server-side page, its export. `kind` is "bills" or "txns"."""
    bills = kind == "bills"
    types = BILL_EXCEPTION_TYPES if bills else TXN_EXCEPTION_TYPES
    n, value = (_exc_counts_for(types, statuses))
    noun = "bills" if bills else "txns"
    st.markdown(f"**Exception {'bills' if bills else 'transactions'}** {fmt_count(n)} {noun} · "
               f"{money_short(value)}")
    if editable:
        st.caption(("Bills with no matching transaction. " if bills else
                    "Transactions with no matching bill. ")
                   + "The remark cell is editable — click it, type, Enter or click away to save.")
    if n == 0:
        st.caption("Nothing in this view." if editable
                   else "Nothing closed or auto-resolved in this range yet.")
        return
    name = f"exc_{kind}_{'open' if editable else 'closed'}"
    offset = _pager(name, n, (str(date_from), str(date_to), store, scope_label, sort_label, run.id))
    page_df, _, _ = q.exception_page(session, date_from, date_to, store, types, statuses,
                                     limit=EXC_PAGE, offset=offset, sort=sort_label)
    frame = (_bill_exception_frame if bills else _txn_exception_frame)(page_df)
    columns = BILL_EXC_COLUMNS if bills else TXN_EXC_COLUMNS
    config = BILL_EXC_COLUMN_CONFIG if bills else TXN_EXC_COLUMN_CONFIG
    pairing = None
    if bills and editable:
        if st.session_state.get("_pair_msg"):
            st.success(st.session_state.pop("_pair_msg"))
        frame, pairing = _add_pairing(frame)
        columns = BILL_EXC_PAIR_COLUMNS
        config = {**BILL_EXC_COLUMN_CONFIG, **PAIR_COLUMN_CONFIG}
    if editable:
        _editable_exceptions(frame, columns, config,
                             f"sm_exc_{kind}_editor_{scope_label}_{offset}",
                             "bill_no" if bills else "transaction_id", "Bill" if bills else "Txn",
                             pairing=pairing)
        if pairing is not None:
            _cluster_resolver(frame)
    else:
        st.dataframe(_display_exceptions(frame), hide_index=True, width="stretch",
                     column_config=config)
    if not bills and (frame["gateway"] == "axis").any():
        st.caption("Axis does not supply a transaction time.")

    title = ("Exception bills" if bills else "Exception transactions") + ("" if editable else " (closed)")
    def _fetch_export(progress):
        what = ("Exception bills" if bills else "Exception transactions") + ("" if editable else " (closed)")
        if bills and editable:
            # ONE global assignment over the whole open population (fresh, not the page cache), so the
            # file carries exactly the suggestions the screen does and no transaction twice.
            _p = ep.unmapped_pool(session)
            _b = ep.open_population(session, run.id)
            _assignment = ep.assign(_b, _p, PAIR_MINUTES, PAIR_DAYS, ep.mop_family(CONFIG))
            cols = et.bill_columns(pairing=True, screen=False)
            shape = lambda f: et.add_pairing(_bill_exception_frame(f), _assignment)[0]  # noqa: E731
        elif bills:
            cols = et.bill_columns(pairing=False, screen=False)
            shape = _bill_exception_frame
        else:
            cols = et.TXN_COLUMNS
            shape = _txn_exception_frame
        frame = q.exception_export(session, date_from, date_to, store, types, statuses, sort=sort_label,
                                   progress=progress, transform=shape)
        return et.export_view(frame, cols, what)

    export_all_button(
        _fetch_export,
        n,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                  date_from=date_from, date_to=date_to,
                  filters={"store": store, "show": scope_label}, value_column="amount",
                  title=title),
        key=f"dl_exc_{kind}{'' if editable else '_closed'}",
        noun=("exception bills" if bills else "exception transactions") + ("" if editable else " (closed)"),
    )


def _exc_counts_for(types, statuses):
    """(count, value) of the filtered exceptions of these types/statuses - one SQL aggregate."""
    if types is BILL_EXCEPTION_TYPES:
        n, v, _, _ = q.exception_counts(session, date_from, date_to, store, types, (), statuses=statuses)
    else:
        _, _, n, v = q.exception_counts(session, date_from, date_to, store, (), types, statuses=statuses)
    return n, v


# The header count uses a cheap COUNT+SUM aggregate, never a per-row build.
_preview_bill_n, _preview_bill_v, _preview_txn_n, _preview_txn_v = q.exception_counts(
    session, date_from, date_to, store, BILL_EXCEPTION_TYPES, TXN_EXCEPTION_TYPES,
    statuses=("open",))

exceptions_open = _section_toggle(
    "exceptions", "Exceptions",
    f"{fmt_count(_preview_bill_n)} bill(s), "
    f"{fmt_count(_preview_txn_n)} transaction(s) open")

if exceptions_open:
    f1, f2, _f3 = st.columns([1, 1.4, 2.6])
    scope = _persisted_select("Show", ["Open", "Under investigation"], "sm_exc_scope_value",
                              "sm_exc_scope_w", container=f1)
    sort_label = _persisted_select("Sort by", list(q.EXCEPTION_SORTS), "sm_exc_sort_value",
                                   "sm_exc_sort_w", container=f2)
    status_wanted = ("open",) if scope == "Open" else ("under_investigation",)

    _exception_table_block("bills", status_wanted, scope, True, sort_label)
    _bulk_remark_upload("Exception bills", er.validate_bill_remarks,
                       er.bill_template_csv(), "sm_exc_bills_bulk")

    # A bill a person matched that STILL has another unmapped transaction of the same store and
    # amount nearby: a genuine split or duplicate payment. The bill stays closed, the second
    # transaction stays open, and the decision (refund, or a second bill) is left to a person.
    _second = ep.second_transaction_still_open(session, _cached_assignment(_pairing_fp())[0], PAIR_DAYS)
    if not _second.empty:
        st.markdown(f"**Second transaction still open** {fmt_count(len(_second))} - bills matched by a person "
                    "that have another unmapped transaction of the same store and amount.")
        _show2 = _second.copy()
        _show2["bill_date"] = _show2["bill_date"].map(display_date)
        _show2["second_date"] = _show2["second_date"].map(display_date)
        _show2["amount"] = _show2["amount"].map(money_table)
        st.dataframe(_show2, hide_index=True, width="stretch")
        st.caption("Decide each: a refund (Refund upload below) or a second bill. Nothing here is forced to 1:1.")

    st.divider()

    _exception_table_block("txns", status_wanted, scope, True, sort_label)
    _bulk_remark_upload("Exception transactions", er.validate_txn_remarks,
                       er.txn_template_csv(), "sm_exc_txns_bulk")

    # --- closed and auto-resolved — never made unreachable, but only READ when opened ----
    closed_bill_n, _cbv, closed_txn_n, _ctv = q.exception_counts(
        session, date_from, date_to, store, BILL_EXCEPTION_TYPES, TXN_EXCEPTION_TYPES,
        statuses=CLOSED_STATUSES)
    st.divider()
    closed_open = _section_toggle(
        "closed_exceptions", "Closed and auto-resolved",
        f"{fmt_count(closed_bill_n)} bill(s), {fmt_count(closed_txn_n)} transaction(s)")
    if closed_open:
        _exception_table_block("bills", CLOSED_STATUSES, "closed", False, sort_label)
        _exception_table_block("txns", CLOSED_STATUSES, "closed", False, sort_label)


# ===========================================================================
# MANUAL MATCHING - moved here from the former Manual matches page (2026-10-14)
# ===========================================================================
st.divider()
st.header("Manual matching")


def manual_matching_section(session, run, date_from, date_to, store):
    LIFECYCLE_HELP = q.MANUAL_LIFECYCLE_LABEL
    PAGE_SIZE = 50

    # ===========================================================================
    # Both two-sided controls, always visible regardless of which mode is open —
    # bills: total = matched + unmatched; transactions: total = mapped +
    # refunded + unmapped (rules, Request B).
    # ===========================================================================
    bill_summary = q.bill_match_summary(session, run.id, date_from, date_to, store)
    txn_summary = q.transaction_resolution_summary(session, date_from, date_to, store,
                                                    run_id=run.id)

    with st.container(key="mm-open-ribbon"):
        st.markdown("**Bills**")
        r1, r2, r3 = st.columns(3)
        tile(r1, "OPEN BILLS", fmt_count(bill_summary["open"]["count"]),
             state="attention" if bill_summary["open"]["count"] else "clean",
             note=money_short(bill_summary["open"]["value"]),
             tooltip=f"Rs {money_table(bill_summary['open']['value'])} — no transaction matched yet")
        tile(r2, "CLOSED BILLS", fmt_count(bill_summary["closed"]["count"]), state="clean",
             note=money_short(bill_summary["closed"]["value"]),
             tooltip=f"Rs {money_table(bill_summary['closed']['value'])} — matched, by the "
                     "engine or by a person.")
        tile(r3, "TOTAL BILLS", fmt_count(bill_summary["total"]["count"]), state="neutral",
             note=f"{fmt_count(bill_summary['open']['count'])} open + "
                  f"{fmt_count(bill_summary['closed']['count'])} closed",
             tooltip=f"Rs {money_table(bill_summary['total']['value'])}")

        if (bill_summary["open"]["count"] + bill_summary["closed"]["count"]
                == bill_summary["total"]["count"]):
            st.caption(
                f"✓ open ({fmt_count(bill_summary['open']['count'])}) + closed "
                f"({fmt_count(bill_summary['closed']['count'])}) = total "
                f"({fmt_count(bill_summary['total']['count'])})."
            )
        else:
            st.error(
                f"**MISMATCH**: open ({fmt_count(bill_summary['open']['count'])}) + "
                f"closed ({fmt_count(bill_summary['closed']['count'])}) ≠ total "
                f"({fmt_count(bill_summary['total']['count'])})."
            )

        st.markdown("**Transactions**")
        t1, t2, t3, t4 = st.columns(4)
        tile(t1, "MAPPED TO BILL", fmt_count(txn_summary.mapped_n), state="clean",
             note=money_short(txn_summary.mapped_v))
        tile(t2, "REFUNDED TO CUSTOMER", fmt_count(txn_summary.refunded_n),
             state="neutral", note=money_short(txn_summary.refunded_v),
             tooltip=f"Pending {fmt_count(txn_summary.refund_pending_n)} · "
                     f"Released {fmt_count(txn_summary.refund_released_n)} — "
                     "never merged.")
        tile(t3, "UNMAPPED (OPEN)", fmt_count(txn_summary.unmapped_n),
             state="attention" if txn_summary.unmapped_n else "clean",
             note=money_short(txn_summary.unmapped_v))
        tile(t4, "TOTAL TRANSACTIONS", fmt_count(txn_summary.total_n), state="neutral",
             note=money_short(txn_summary.total_v))
        st.caption(
            f"Refund pending {fmt_count(txn_summary.refund_pending_n)} · "
            f"refund released {fmt_count(txn_summary.refund_released_n)} — "
            "both visible, never merged into one figure."
        )
        if txn_summary.ok:
            st.caption(
                f"✓ mapped ({fmt_count(txn_summary.mapped_n)}) + refunded "
                f"({fmt_count(txn_summary.refunded_n)}) + unmapped "
                f"({fmt_count(txn_summary.unmapped_n)}) = total "
                f"({fmt_count(txn_summary.total_n)})."
            )
        else:
            st.error(
                f"**MISMATCH**: mapped + refunded + unmapped differs from total "
                f"by {txn_summary.difference}."
            )

    # --- date-lag matches: information, never an exception (2026-09-13) -------
    # Computed only on request: finding these is a scan of every matched line (37 s for five
    # months), which must not be paid on every load of a work-queue page.
    _want_lag = st.toggle("Show bills matched to a transaction dated on a different day (loads on demand)",
                          value=False, key="mm_show_lag")
    lag_matches = q.date_lag_matches(session, run.id, date_from, date_to, store) if _want_lag else pd.DataFrame()
    if not lag_matches.empty:
        with st.expander(
            f"📅 {fmt_count(len(lag_matches))} bill(s) matched to a transaction "
            "dated on a different day"):
            st.caption(
                "Information, not an exception — the reference, store and amount "
                "all agree; only the gateway's own transaction date differs from "
                "the bill's. Revenue recognition stays on the bill's date "
                "regardless."
            )
            shown_lag = lag_matches.rename(columns={
                "bill_no": "Bill no", "business_date": "Bill date",
                "txn_date": "Txn date", "lag_days": "Lag (days)",
                "amount": "Amount (Rs)", "gateway": "Gateway",
                "match_note": "Note",
            }).copy()
            shown_lag["Bill date"] = shown_lag["Bill date"].map(display_date)
            shown_lag["Txn date"] = shown_lag["Txn date"].map(display_date)
            shown_lag["Amount (Rs)"] = shown_lag["Amount (Rs)"].map(money_table)
            st.dataframe(shown_lag, hide_index=True, width="stretch")

    st.divider()

    # ===========================================================================
    # Segmented toggle — [By bill] [By transaction]. Two buttons, not tabs.
    # ===========================================================================
    st.session_state.setdefault("mm_mode", "by_bill")
    m1, m2, _ = st.columns([1, 1, 4])
    if m1.button("By bill", type="primary" if st.session_state["mm_mode"] == "by_bill"
                 else "secondary", width="stretch", key="mm_mode_bill"):
        st.session_state["mm_mode"] = "by_bill"
        st.rerun()
    if m2.button("By transaction", type="primary" if st.session_state["mm_mode"] == "by_transaction"
                 else "secondary", width="stretch", key="mm_mode_txn"):
        st.session_state["mm_mode"] = "by_transaction"
        st.rerun()

    mode = st.session_state["mm_mode"]

    # ===========================================================================
    # BY BILL — open bills, editable "Matched txn", validate on entry, batch save
    # ===========================================================================
    if mode == "by_bill":
        st.markdown("##### By bill — open bills, no transaction matched yet")

        f1, f2, f3 = st.columns([1.5, 1.15, 1.15])
        search = f1.text_input("Find a bill or transaction", placeholder="bill no, txn id or RRN",
                               help="Searches bill number and the Paytm, Axis and "
                                    "Bajaj references.")
        status_pick = f2.selectbox("Status", ["Any"] + sorted(q.status_options(session, run.id)))
        mop_pick = f3.selectbox("MOP", ["Any"] + sorted(q.mop_options(session, run.id)))

        state_key = "mm_open_offset"
        st.session_state.setdefault(state_key, 0)
        fingerprint = (str(date_from), str(date_to), store, search, status_pick, mop_pick)
        if st.session_state.get("mm_open_fingerprint") != fingerprint:
            st.session_state["mm_open_fingerprint"] = fingerprint
            st.session_state[state_key] = 0

        df, total = q.bill_match_page(
            session, run.id, date_from, date_to, store, show="open",
            status=None if status_pick == "Any" else status_pick,
            mop=None if mop_pick == "Any" else mop_pick,
            search=search or None,
            limit=PAGE_SIZE, offset=st.session_state[state_key],
        )

        if total == 0:
            st.success("No open bills match this selection.")
        else:
            offset = min(st.session_state[state_key], max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE))
            first_row, last_row = offset + 1, min(offset + PAGE_SIZE, total)
            pages = max(1, -(-total // PAGE_SIZE))
            current_page = offset // PAGE_SIZE + 1

            p1, p2, p3, p4, p5 = st.columns([0.9, 0.9, 2.2, 1.1, 1.4])
            if p1.button("Previous", disabled=offset == 0, width="stretch", key="mm_open_prev"):
                st.session_state[state_key] = max(0, offset - PAGE_SIZE)
                st.rerun()
            if p2.button("Next", disabled=last_row >= total, width="stretch", key="mm_open_next"):
                st.session_state[state_key] = offset + PAGE_SIZE
                st.rerun()
            p3.markdown(
                f"<div class='mop-page-info'>Showing <b>{fmt_count(first_row)}-{fmt_count(last_row)}</b> "
                f"of <b>{fmt_count(total)}</b> · {money_short(float(df['amount'].sum()))} on this page"
                f"</div>", unsafe_allow_html=True)
            jump = p4.number_input("Page", min_value=1, max_value=pages, value=current_page,
                                   step=1, label_visibility="collapsed", key="mm_open_jump")
            if p5.button(f"Go to page (of {fmt_count(pages)})", width="stretch", key="mm_open_go"):
                st.session_state[state_key] = (int(jump) - 1) * PAGE_SIZE
                st.rerun()

            remark_keys = [bill_exception_key(r.date, r.store, r.bill_no, r.mop)
                          for r in df.itertuples()]
            remarks = q.latest_remarks(session, remark_keys)

            st.session_state.setdefault("bybill_pending", {})
            pending = st.session_state["bybill_pending"]

            shown = pd.DataFrame({
                "Date": [display_date(r.date) for r in df.itertuples()],
                "Store": df["store"],
                "Bill no": df["bill_no"],
                "MOP": df["mop"],
                "Amount (Rs)": df["amount"].map(money_table),
                "Status": df["status"],
                "Matched txn": [
                    pending.get(bill_exception_key(r.date, r.store, r.bill_no, r.mop), {})
                           .get("ref", "")
                    for r in df.itertuples()
                ],
                "Remark": [fmt_blank(remarks.get(k, (None, None))[0]) for k in remark_keys],
            })
            bill_keys = list(remark_keys)

            st.caption(
                "Paste a transaction id or RRN into **Matched txn** and press enter. "
                "The gateway is worked out from the id — you are not asked for it. "
                "It validates immediately below; nothing is written until you "
                "press **Save matches**."
            )

            edited = st.data_editor(
                shown, hide_index=True, width="stretch",
                disabled=[c for c in shown.columns if c != "Matched txn"],
                key=f"mm_bybill_editor_{st.session_state[state_key]}",
                column_config={
                    "Matched txn": st.column_config.TextColumn(
                        "Matched txn", width="medium",
                        help="Paste the id exactly as the gateway shows it, then press enter."),
                },
            )

            changed = shown["Matched txn"] != edited["Matched txn"]
            if changed.any():
                idx = changed.idxmax()
                key = bill_keys[idx]
                pasted = io_.clean_ref(edited["Matched txn"].iloc[idx]) or ""
                if not pasted:
                    pending.pop(key, None)
                else:
                    bill = {"business_date": df["date"].iloc[idx], "store": df["store"].iloc[idx],
                            "bill_no": df["bill_no"].iloc[idx], "mop": df["mop"].iloc[idx],
                            "amount": float(df["amount"].iloc[idx])}
                    validation = mm.validate_by_id(session, bill, pasted)
                    pending[key] = {"bill": bill, "ref": pasted, "validation": validation}
                st.rerun()

            this_page_pending = {k: v for k, v in pending.items() if k in bill_keys}
            if this_page_pending:
                st.markdown("###### Pending — validated, not yet saved")
                rows = []
                for key, entry in this_page_pending.items():
                    v = entry["validation"]
                    verdict = "Accept" if v.ok else "Reject"
                    reason = "Clean match" if v.ok else " | ".join(v.problems)
                    lag = f" · lag {v.date_lag_days:+d}d" if v.date_lag_days else ""
                    rows.append({
                        "Bill no": entry["bill"]["bill_no"], "Transaction": entry["ref"],
                        "Gateway": v.gateway or "—", "Verdict": verdict,
                        "Reason": reason + lag,
                    })
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                             column_config={"Reason": st.column_config.TextColumn(width="large")})

                acceptable = {k: v for k, v in this_page_pending.items() if v["validation"].ok}
                operator = confirm_with_operator(
                    st.button(f"Save matches ({fmt_count(len(acceptable))} accepted)",
                             type="primary", key="mm_bybill_save",
                             disabled=not acceptable),
                    "saving these matches", key="mm_bybill_save_op")
                if operator:
                    saved, failed = [], []
                    for key, entry in list(acceptable.items()):
                        try:
                            mm.record(session, entry["bill"], entry["validation"].gateway,
                                     entry["ref"], "manual_match_by_bill", operator,
                                     entry["validation"])
                            saved.append(entry["bill"]["bill_no"])
                            pending.pop(key, None)
                        except (ValueError, LookupError) as exc:
                            failed.append((entry["bill"]["bill_no"], str(exc)))
                    st.success(f"Saved {len(saved)} match(es).")
                    for bill_no, why in failed:
                        st.warning(f"{bill_no} — {why}")
                    st.rerun()

            export_all_button(
                lambda progress: q.bill_match_export(
                    session, run.id, date_from, date_to, store, show="open",
                    status=None if status_pick == "Any" else status_pick,
                    mop=None if mop_pick == "Any" else mop_pick,
                    search=search or None, progress=progress,
                ),
                bill_summary["open"]["count"],
                ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                          date_from=date_from, date_to=date_to,
                          filters={"store": store, "status": status_pick,
                                  "mop": mop_pick, "search": search or None},
                          value_column="amount", title="Open bills"),
                key="dl_mm_open", noun="open bills",
            )

        with st.expander("Bulk upload — bill number → transaction id"):
            st.caption(
                "Clear many open bills at once from a spreadsheet. Every row is "
                "validated first and **nothing is applied** until you choose — "
                "the same global reference search the inline paste uses, no "
                "gateway column (worked out from the id), one batch id so it "
                "reverses as a unit."
            )
            template = mm.upload_template()
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
                template.to_excel(writer, index=False, sheet_name="manual_matches")
            st.download_button("Download the template", buffer.getvalue(),
                               file_name="manual_match_template.xlsx",
                               mime="application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet")
            st.caption(f"Columns, exactly: `{'`, `'.join(mm.UPLOAD_COLUMNS)}`.")

            uploaded = st.file_uploader("The completed template",
                                       type=["xlsx", "csv"], key="mm_upload")

            if uploaded is not None:
                try:
                    frame = (pd.read_csv(uploaded, dtype=str) if uploaded.name.endswith(".csv")
                             else pd.read_excel(uploaded, dtype=str))
                except Exception as exc:  # noqa: BLE001 — show the real reason
                    st.error(f"Could not read the file — {type(exc).__name__}: {exc}")
                    st.stop()

                st.caption(f"{fmt_count(len(frame))} row(s) read from **{uploaded.name}**.")
                pool = mm.gateway_pool_for_date()

                try:
                    preview = mm.preview_upload(session, frame, pool, run.id)
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()

                p1, p2, p3 = st.columns(3)
                p1.metric("Would be accepted", len(preview.accepted))
                p2.metric("Carry warnings", len(preview.warned))
                p3.metric("Rejected", len(preview.rejected))

                st.dataframe(preview.as_frame(), hide_index=True, width="stretch",
                             column_config={"why": st.column_config.TextColumn(width="large")})

                if preview.rejected:
                    st.error(f"**{len(preview.rejected)} row(s) rejected and listed above.**")

                include = st.checkbox(
                    f"Also apply the {len(preview.warned)} row(s) with warnings",
                    value=False, disabled=not preview.warned)
                override = st.text_area(
                    "Override reason — required to apply warned rows",
                    key="mm_batch_override") if include else None

                operator = confirm_with_operator(
                    st.button("Apply this upload", type="primary", key="mm_apply"),
                    "applying this upload", key="mm_apply_op")
                if operator:
                    try:
                        batch, applied, failed = mm.apply_upload(
                            session, preview, operator, include_warnings=include,
                            override_reason=override, file_name=uploaded.name)
                    except ValueError as exc:
                        st.error(f"Refused — {exc}")
                        st.stop()
                    st.success(
                        f"Batch #{batch.id}: {len(applied)} applied, {len(failed)} failed, "
                        f"{len(preview.rejected)} rejected."
                    )
                    for row, why in failed:
                        st.warning(f"Row {row.row_number} ({row.bill.get('bill_no')}) — {why}")

    # ===========================================================================
    # BY TRANSACTION — unmapped transactions, editable "Matched bill no",
    # a status/filter "Resolution" (see note below), and the refund upload.
    # ===========================================================================
    else:
        st.markdown("##### By transaction")
        st.caption(
            "**Resolution** is shown as the transaction's current state, plus a "
            "**Show** filter above the table — it is not a free-standing dropdown "
            "you set directly, because recording a customer refund needs more "
            "than one field (a Zoho number, the customer's name); that goes "
            "through the Refund upload below. Matching to a bill is still done "
            "inline, in **Matched bill no**."
        )

        resolution_pick = st.selectbox(
            "Show", ["Unmapped (open)", "Matched to bill", "Refunded to customer", "All"])
        _RES_MAP = {"Unmapped (open)": "unmapped", "Matched to bill": "mapped",
                   "Refunded to customer": "refunded", "All": None}

        txn_frame = gt.gateway_transactions_with_resolution(
            session, date_from, date_to) if date_from and date_to else pd.DataFrame()
        if not txn_frame.empty:
            # The engine's own pool shape (gateway/txn_ref/business_date/...) —
            # renamed to the names this page's table/pending-edit code uses,
            # the same rename q.unmapped_transactions_frame() applies.
            txn_frame = txn_frame.rename(columns={"txn_ref": "transaction_id",
                                                  "business_date": "date"})
        if store and not txn_frame.empty:
            txn_frame = txn_frame[txn_frame["store"] == store]
        wanted = _RES_MAP[resolution_pick]
        if wanted and not txn_frame.empty:
            txn_frame = txn_frame[txn_frame["resolution"] == wanted]
        txn_frame = txn_frame.reset_index(drop=True)

        state_key = "mm_txn_offset"
        st.session_state.setdefault(state_key, 0)
        fingerprint = (str(date_from), str(date_to), store, resolution_pick)
        if st.session_state.get("mm_txn_fingerprint") != fingerprint:
            st.session_state["mm_txn_fingerprint"] = fingerprint
            st.session_state[state_key] = 0

        total = len(txn_frame)
        if total == 0:
            st.success("No transactions match this selection.")
        else:
            offset = min(st.session_state[state_key], max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE))
            page_df = txn_frame.iloc[offset:offset + PAGE_SIZE].reset_index(drop=True)
            first_row, last_row = offset + 1, min(offset + PAGE_SIZE, total)
            pages = max(1, -(-total // PAGE_SIZE))
            current_page = offset // PAGE_SIZE + 1

            p1, p2, p3, p4, p5 = st.columns([0.9, 0.9, 2.2, 1.1, 1.4])
            if p1.button("Previous", disabled=offset == 0, width="stretch", key="mm_txn_prev"):
                st.session_state[state_key] = max(0, offset - PAGE_SIZE)
                st.rerun()
            if p2.button("Next", disabled=last_row >= total, width="stretch", key="mm_txn_next"):
                st.session_state[state_key] = offset + PAGE_SIZE
                st.rerun()
            p3.markdown(
                f"<div class='mop-page-info'>Showing <b>{fmt_count(first_row)}-{fmt_count(last_row)}</b> "
                f"of <b>{fmt_count(total)}</b> · "
                f"{money_short(float(pd.to_numeric(page_df['amount'], errors='coerce').sum()))} on this page"
                f"</div>", unsafe_allow_html=True)
            jump = p4.number_input("Page", min_value=1, max_value=pages, value=current_page,
                                   step=1, label_visibility="collapsed", key="mm_txn_jump")
            if p5.button(f"Go to page (of {fmt_count(pages)})", width="stretch", key="mm_txn_go"):
                st.session_state[state_key] = (int(jump) - 1) * PAGE_SIZE
                st.rerun()

            remark_keys = [txn_exception_key(r.gateway, r.transaction_id)
                          for r in page_df.itertuples()]
            remarks = q.latest_remarks(session, remark_keys)

            st.session_state.setdefault("bytxn_pending", {})
            pending = st.session_state["bytxn_pending"]

            _RES_LABEL = {"mapped": "Matched to bill", "refunded": "Refunded to customer",
                         "unmapped": "Open"}
            can_edit = wanted == "unmapped"

            shown = pd.DataFrame({
                "Gateway": page_df["gateway"],
                "Transaction id": page_df["transaction_id"],
                "Date": [display_date(d) for d in page_df["date"]],
                "Store": page_df["store"],
                "Amount (Rs)": page_df["amount"].map(money_table),
                "Payment mode": page_df["payment_mode"].map(fmt_blank),
                "Matched bill no": [
                    pending.get(f"{r.gateway}|{r.transaction_id}", {}).get("bill_no",
                        fmt_blank(r.matched_bill_no))
                    for r in page_df.itertuples()
                ] if can_edit else page_df["matched_bill_no"].map(fmt_blank),
                "Resolution": page_df["resolution"].map(_RES_LABEL),
                "Remark": [fmt_blank(remarks.get(k, (None, None))[0]) for k in remark_keys],
            })
            txn_keys = [f"{r.gateway}|{r.transaction_id}" for r in page_df.itertuples()]

            st.caption(
                "Paste a bill number into **Matched bill no** and press enter — "
                "validated the same way, in reverse. Nothing is written until "
                "you press **Save matches**." if can_edit else
                "Read-only — switch **Show** to \"Unmapped (open)\" to match one."
            )

            edited = st.data_editor(
                shown, hide_index=True, width="stretch",
                disabled=[c for c in shown.columns if c != "Matched bill no"] if can_edit
                         else list(shown.columns),
                key=f"mm_bytxn_editor_{st.session_state[state_key]}_{wanted}",
                column_config={
                    "Transaction id": st.column_config.TextColumn(width="medium"),
                    "Matched bill no": st.column_config.TextColumn(
                        "Matched bill no", width="medium",
                        help="Paste the bill number exactly, then press enter."),
                },
            )

            if can_edit:
                changed = shown["Matched bill no"] != edited["Matched bill no"]
                if changed.any():
                    idx = changed.idxmax()
                    key = txn_keys[idx]
                    raw_bill_no = edited["Matched bill no"].iloc[idx]
                    typed_bill_no = "" if raw_bill_no is None else str(raw_bill_no).strip()
                    if not typed_bill_no:
                        pending.pop(key, None)
                    else:
                        txn_row = page_df.iloc[idx]
                        bill, is_open, why = mm.find_bill_for_match(session, run.id, typed_bill_no)
                        if bill is None or not is_open:
                            validation = mm.Validation("b", False, "asserted_unverified",
                                                       [why or "bill not found"])
                        else:
                            gateway_pool = mm.gateway_pool_for_date()
                            validation = mm.validate(session, bill, str(txn_row["gateway"]),
                                                     str(txn_row["transaction_id"]), gateway_pool)
                        pending[key] = {"bill": bill, "bill_no": typed_bill_no,
                                        "gateway": str(txn_row["gateway"]),
                                        "ref": str(txn_row["transaction_id"]),
                                        "validation": validation}
                    st.rerun()

                this_page_pending = {k: v for k, v in pending.items() if k in txn_keys}
                if this_page_pending:
                    st.markdown("###### Pending — validated, not yet saved")
                    rows = []
                    for key, entry in this_page_pending.items():
                        v = entry["validation"]
                        verdict = "Accept" if v.ok else "Reject"
                        reason = "Clean match" if v.ok else " | ".join(v.problems)
                        rows.append({"Transaction": entry["ref"], "Bill no": entry["bill_no"],
                                    "Verdict": verdict, "Reason": reason})
                    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                                 column_config={"Reason": st.column_config.TextColumn(width="large")})

                    acceptable = {k: v for k, v in this_page_pending.items() if v["validation"].ok}
                    operator = confirm_with_operator(
                        st.button(f"Save matches ({fmt_count(len(acceptable))} accepted)",
                                 type="primary", key="mm_bytxn_save",
                                 disabled=not acceptable),
                        "saving these matches", key="mm_bytxn_save_op")
                    if operator:
                        saved, failed = [], []
                        for key, entry in list(acceptable.items()):
                            try:
                                mm.record(session, entry["bill"], entry["gateway"], entry["ref"],
                                         "manual_match_by_transaction", operator,
                                         entry["validation"])
                                saved.append(entry["bill_no"])
                                pending.pop(key, None)
                            except (ValueError, LookupError) as exc:
                                failed.append((entry["bill_no"], str(exc)))
                        st.success(f"Saved {len(saved)} match(es).")
                        for bill_no, why in failed:
                            st.warning(f"{bill_no} — {why}")
                        st.rerun()

            export_all_button(
                lambda progress: q.unmapped_transactions_export(session, date_from, date_to, store)
                                 if wanted == "unmapped" else txn_frame,
                total,
                ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                          date_from=date_from, date_to=date_to,
                          filters={"store": store, "show": resolution_pick},
                          value_column="amount", title="Transactions"),
                key="dl_mm_txn", noun="transactions",
            )

        # -----------------------------------------------------------------
        # Customer refund resolution (§ Request B part 3) — a Zoho upload,
        # never a bare dropdown selection: it needs zoho_no and customer_name,
        # and every row is validated before anything is applied.
        # -----------------------------------------------------------------
        with st.expander("Refund upload — mark unmapped transactions as refunded to the customer"):
            st.caption(
                "A refund is NOT a bill match. The transaction moves to "
                "`resolution = customer_refund`; it is never counted as mapped "
                "to a bill and never closes one. All rows are validated first "
                "and applied all-or-nothing."
            )
            template = rf.upload_template()
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
                template.to_excel(writer, index=False, sheet_name="refunds")
            st.download_button("Download the refund template", buffer.getvalue(),
                               file_name="refund_template.xlsx",
                               mime="application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet", key="dl_refund_template")
            st.caption(f"Columns, exactly: `{'`, `'.join(rf.UPLOAD_COLUMNS)}`. "
                      "`status` is `pending` (refundable, not yet paid) or "
                      "`released`/`paid` (already paid back).")

            refund_upload = st.file_uploader("The completed refund template",
                                            type=["xlsx", "csv"], key="mm_refund_upload")
            if refund_upload is not None:
                try:
                    rframe = (pd.read_csv(refund_upload, dtype=str) if refund_upload.name.endswith(".csv")
                             else pd.read_excel(refund_upload, dtype=str))
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not read the file — {type(exc).__name__}: {exc}")
                    st.stop()

                st.caption(f"{fmt_count(len(rframe))} row(s) read from **{refund_upload.name}**.")
                try:
                    rpreview = rf.preview_upload(session, rframe)
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()

                rp1, rp2 = st.columns(2)
                rp1.metric("Would be accepted", len(rpreview.accepted))
                rp2.metric("Rejected", len(rpreview.rejected))
                st.dataframe(rpreview.as_frame(), hide_index=True, width="stretch",
                             column_config={"why": st.column_config.TextColumn(width="large")})

                if rpreview.rejected:
                    st.error(
                        f"**{len(rpreview.rejected)} row(s) rejected and listed above.** "
                        "Applied all-or-nothing — only the accepted rows are written, "
                        "never partially."
                    )

                operator = confirm_with_operator(
                    st.button("Apply refunds", type="primary", key="mm_refund_apply",
                             disabled=not rpreview.accepted),
                    "applying these refunds", key="mm_refund_apply_op")
                if operator:
                    try:
                        applied, rejected = rf.apply_upload(session, rpreview, operator,
                                                            file_name=refund_upload.name)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Refused — {exc}")
                        st.stop()
                    st.success(f"Applied {len(applied)} refund(s). "
                              f"{len(rejected)} row(s) were not applied (see above).")
                    st.rerun()

        # -----------------------------------------------------------------
        # Refund report — its own table and export.
        # -----------------------------------------------------------------
        st.markdown("##### Refund report")
        refund_rows = rf.refund_records(session)
        if refund_rows.empty:
            st.caption("No refunds recorded yet.")
        else:
            rshown = refund_rows.copy()
            rshown["refund_date"] = rshown["refund_date"].map(display_date)
            rshown["uploaded_at"] = pd.to_datetime(rshown["uploaded_at"]).dt.strftime("%d/%m/%Y %H:%M")
            rshown["amount"] = rshown["amount"].map(money_table)
            rshown["customer_name"] = rshown["customer_name"].map(fmt_blank)
            rshown.columns = ["Refund date", "Store", "Gateway", "Transaction id", "Amount (Rs)",
                              "Zoho no", "Customer name", "Status", "Uploaded by", "Uploaded at"]
            st.dataframe(rshown, hide_index=True, width="stretch",
                         column_config={"Transaction id": st.column_config.TextColumn(width="medium")})
            frame_export_button(
                refund_rows,
                ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                          date_from=date_from, date_to=date_to,
                          value_column="amount", title="Refund report"),
                key="dl_refund_report", noun="refunds",
            )

    # ===========================================================================
    # Manual match bill report — below both modes, its own export.
    # ===========================================================================
    st.divider()
    st.markdown("##### Manual match bill report")
    report = q.manual_match_bill_report(session, date_from, date_to, store)
    if report.empty:
        st.caption("No manual matches recorded for this selection.")
    else:
        rshown = report.copy()
        rshown["date"] = rshown["date"].map(display_date)
        rshown["txn_date"] = rshown["txn_date"].map(display_date)
        rshown["matched_at"] = pd.to_datetime(rshown["matched_at"]).dt.strftime("%d/%m/%Y %H:%M")
        rshown["amount"] = rshown["amount"].map(money_table)
        rshown.columns = ["Date", "Store", "Bill no", "MOP", "Amount (Rs)", "Gateway",
                          "Transaction id", "Txn date", "Lag", "Matched by",
                          "Matched at", "Reason", "Status"]
        st.dataframe(rshown, hide_index=True, width="stretch",
                     column_config={"Transaction id": st.column_config.TextColumn(width="medium")})
        frame_export_button(
            report,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                      date_from=date_from, date_to=date_to,
                      filters={"store": store},
                      value_column="amount", title="Manual match bill report"),
            key="dl_manual_bill_report", noun="manual matches",
        )

    # ===========================================================================
    # The register — every manual match ever recorded, in force or not. The
    # "Open one" detail panel (dropdown, Bill/Amount/Transaction/Status block,
    # Lifecycle line, Reason code line, Audit trail expander, "Reverse this
    # match") is REMOVED from display, per instruction — the underlying audit
    # data (manual_match_events) and mm.reverse()/mm.reverse_batch() are
    # untouched; only this page stopped rendering them.
    # ===========================================================================
    st.divider()
    st.markdown("##### The register")
    st.caption(
        "The first thing an auditor asks to see, so nothing is hidden by "
        "default: reversed, superseded, conflicted and orphaned records are all "
        "listed alongside the ones in force."
    )

    split = q.match_split(session, run.id, date_from, date_to, store)

    c1, c2, c3 = st.columns(3)
    c1.metric("Matched by system", fmt_count(split.system),
              help="The engine's own result. The only rate comparable across runs.")
    c1.caption(f"{split.system_rate * 100:.3f}% · {money_short(split.system_value)}")
    c2.metric("Matched manually", fmt_count(split.manual),
              help="A person matched it to a transaction that was actually found.")
    c2.caption(money_short(split.manual_value))
    c3.metric("Asserted, unverified", fmt_count(split.asserted),
              help="Asserted with no gateway row behind it. NOT a match, and "
                   "not counted in any rate.")
    c3.caption(money_short(split.asserted_value))

    st.caption(
        "These are never blended. A single rate that counts human assertions as "
        "matches is a number that flatters itself."
    )

    st.divider()

    f1, f2, f3, f4 = st.columns(4)
    operators = q.manual_match_options(session, "operator")
    codes = q.manual_match_options(session, "reason_code")
    lifecycles = q.manual_match_options(session, "lifecycle")
    who = f1.selectbox("Operator", ["Any"] + operators)
    code = f2.selectbox("Reason code", ["Any"] + codes)
    life = f3.selectbox("Lifecycle", ["Any"] + lifecycles)
    only_overrides = f4.checkbox("Overrides only", value=False,
                                 help="Rows recorded despite a validation "
                                      "problem (case b).")

    rows = q.manual_matches(
        session, date_from, date_to, store,
        operator=None if who == "Any" else who,
        reason_code=None if code == "Any" else code,
        lifecycle=None if life == "Any" else life,
    )
    if not rows.empty and only_overrides:
        rows = rows[rows["override_used"]]

    if rows.empty:
        st.info("No manual matches recorded for this selection.")
    else:
        conflicted = rows[rows["lifecycle"] == "conflicted"]
        if not conflicted.empty:
            st.error(
                f"**{len(conflicted)} conflict(s) need resolving.** The engine "
                "matched these bills to a different transaction than the manual "
                "match names. Both are kept; neither was overwritten."
            )

        reg_shown = rows.copy()
        reg_shown["Amount (Rs)"] = reg_shown["amount"].map(money_table)
        reg_shown["Date"] = reg_shown["business_date"].map(display_date)
        reg_shown["When"] = pd.to_datetime(reg_shown["created_at"]).dt.strftime("%d/%m/%Y %H:%M")
        reg_shown["Override"] = reg_shown["override_used"].map({True: "yes", False: "—"})
        reg_shown["reason_code"] = reg_shown["reason_code"].map(fmt_blank)
        reg_shown["lifecycle_note"] = reg_shown["lifecycle_note"].map(fmt_blank)
        reg_display = reg_shown[["id", "Date", "store", "bill_no", "mop",
                                 "Amount (Rs)", "gateway", "transaction_id", "status",
                                 "case", "Override", "reason_code", "operator", "When",
                                 "lifecycle", "batch_id"]]
        reg_display.columns = ["#", "Date", "Store", "Bill no", "MOP", "Amount (Rs)",
                               "Gateway", "Transaction id", "Status", "Case",
                               "Override", "Reason", "By", "When", "Lifecycle",
                               "Batch"]
        # Status pills (2026-09-18 restyle) — a pandas Styler tint, the value
        # itself untouched, so sorting/export both still read the plain word.
        reg_styled = status_styler(reg_display, {
            "Status": {"matched_manual": "good", "asserted_unverified": "warn"},
            "Lifecycle": {"active": "good", "reversed": "warn", "orphaned": "warn",
                          "superseded": "accent", "conflicted": "bad"},
        })
        st.dataframe(reg_styled, hide_index=True, width="stretch",
                     column_config={"Transaction id":
                                    st.column_config.TextColumn(width="medium")})

        frame_export_button(
            rows,
            ExportMeta(run_id=run.id, run_timestamp=run.started_at,
                       date_from=date_from, date_to=date_to,
                       filters={"store": store, "operator": who,
                                "reason_code": code, "lifecycle": life},
                       value_column="amount", title="Manual matches"),
            key="dl_manual", noun="manual matches",
        )



manual_matching_section(session, run, date_from, date_to, store)
