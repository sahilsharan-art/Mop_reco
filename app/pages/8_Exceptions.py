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

import match_strip
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
from mop_reco import bulk_accept as ba
from mop_reco import exception_pairing as ep
from mop_reco import exception_tables as et
from mop_reco import exceptions as exc_mod
from mop_reco import gateway_txns as gt
from mop_reco import io_
from mop_reco import manual_match as mm
from mop_reco import queries as q
from mop_reco import refunds as rf
from mop_reco import table_upload as tu
from mop_reco.exceptions import bill_exception_key, txn_exception_key
from mop_reco.export import ExportMeta, build_export
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
    "matched_bill": st.column_config.TextColumn(
        width="small", help="Type the bill this transaction paid, then Save. Validated like any match."),
    "customer_name": st.column_config.TextColumn(width="small", help="Refund: all three of name, Zoho no and date."),
    "zoho_no": st.column_config.TextColumn(width="small", help="Refund: all three of name, Zoho no and date."),
    "refund_date": st.column_config.TextColumn(width="small", help="Refund: DD/MM/YYYY."),
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


TXN_EDITABLE = ("remark",) + tuple(et.TXN_ACTION_COLUMNS)


def _editable_exceptions(df, column_list, column_config, table_key, id_col, label_prefix, pairing=None,
                         txn_actions=False):
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
        disabled=[c for c in shown.columns if c not in (
            ("remark", "Txn 1", "Txn 2", "Match") if pairing else (TXN_EDITABLE if txn_actions else ("remark",)))],
        column_config=column_config,
    )
    if pairing:
        _handle_pairing(edited, shown, df, pairing, table_key)
    if txn_actions:
        _handle_txn_actions(edited, shown, df, table_key)

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
        mm.record(session, bill, v.gateway, ref, "manual_match_by_bill", operator, v, source="inline")
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
                mm.record(session, bill, v.gateway, trow.txn_id, "manual_match_by_bill", operator, v, source="inline")
                done += 1
            st.cache_data.clear()
            st.session_state["_pair_msg"] = f"Paired {done} of {len(b)}." + (f" Refused: {failed}" if failed else "")
            st.rerun()
    else:
        st.caption("The counts differ, so this cannot be paired as a set - match each bill by hand (By bill), or "
                   "refund the extra transactions.")


def _handle_txn_actions(edited, shown, raw, table_key):
    """A person closes an exception transaction by typing on its row: the BILL it paid (matched_bill) or the
    three refund fields (customer_name, zoho_no, refund_date - all required). Each is validated here, at
    once, exactly as an uploaded row is; nothing is written until Save."""
    pending = []
    for i in edited.index:
        changed = [c for c in et.TXN_ACTION_COLUMNS
                   if str(edited.at[i, c] or "").strip() != str(shown.at[i, c] or "").strip()]
        if not changed:
            continue
        row = raw.iloc[i]
        gateway, ref = str(row["gateway"]).lower(), str(row["transaction_id"])
        bill_no = str(edited.at[i, "matched_bill"] or "").strip()
        name = str(edited.at[i, "customer_name"] or "").strip()
        zoho = str(edited.at[i, "zoho_no"] or "").strip()
        rdate = str(edited.at[i, "refund_date"] or "").strip()
        txn = {"gateway": gateway, "txn_ref": ref, "business_date": row["date"], "store": row["store"],
               "amount": float(row["amount"]), "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None}
        entry = {"Transaction": ref, "gateway": gateway, "txn": txn, "ok": False, "problems": [],
                 "action": "", "against": "", "bill": None, "validation": None,
                 "name": name, "zoho": zoho, "rdate": rdate}
        if bill_no and (name or zoho or rdate):
            entry["problems"].append("a transaction is either matched to a bill or refunded to a customer - not both")
        elif bill_no:
            entry["action"], entry["against"] = "match to bill", bill_no
            bill, is_open, why = mm.find_bill_for_match(session, run.id, bill_no)
            if bill is None or not is_open:
                entry["problems"].append(why)
            else:
                v = mm.validate(session, bill, gateway, ref, pd.DataFrame([{
                    "gateway": gateway, "txn_ref": ref, "business_date": txn["business_date"],
                    "store": txn["store"], "amount": txn["amount"], "status": "SUCCESS",
                    "txn_type": "ACQUIRING", "payment_mode": None}]))
                entry["bill"], entry["validation"] = bill, v
                entry["problems"].extend(v.problems)
        elif name or zoho or rdate:
            entry["action"], entry["against"] = "customer refund", zoho
            entry["problems"].extend(rf.validate_refund(session, txn, name, zoho, rdate))
        else:
            continue
        entry["ok"] = not entry["problems"]
        pending.append(entry)
    if not pending:
        return
    st.markdown("###### Pending - validated, not yet saved")
    st.dataframe(pd.DataFrame([{
        "Transaction": e["Transaction"], "Action": e["action"], "Against": e["against"],
        "Verdict": "Accept" if e["ok"] else "Reject",
        "Reason": "Clean" if e["ok"] else " | ".join(e["problems"])} for e in pending]),
        hide_index=True, width="stretch", column_config={"Reason": st.column_config.TextColumn(width="large")})
    good = [e for e in pending if e["ok"]]
    operator = confirm_with_operator(
        st.button(f"Save {fmt_count(len(good))} accepted", type="primary", key=f"{table_key}_save_actions",
                  disabled=not good),
        "saving these", key=f"{table_key}_actions_op")
    if operator:
        done, failed = 0, []
        for e in good:
            try:
                if e["action"] == "match to bill":
                    mm.record(session, e["bill"], e["gateway"], e["Transaction"], "manual_match_by_transaction",
                              operator, e["validation"], source="inline")
                else:
                    rf.record_refund(session, e["txn"], e["name"], e["zoho"], e["rdate"], operator, source="inline")
                done += 1
            except (ValueError, LookupError) as exc:
                session.rollback()
                failed.append((e["Transaction"], str(exc)))
        st.session_state["_pair_msg"] = f"Saved {done}." + (f" Refused: {failed}" if failed else "")
        st.session_state.pop(table_key, None)
        st.cache_data.clear()
        st.rerun()


def _table_upload(kind: str, title: str, expected_cols: list):
    """Upload the SAME file the table exports, filled in. One column definition drives the table, its
    export, this template and this reader (mop_reco/exception_tables.py)."""
    with st.expander(f"Upload {title}"):
        tmpl = build_export(
            tu.template_frame(kind),
            ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=date_from, date_to=date_to,
                       filters={"template": "blank"}, value_column=None, title=f"{title} template"))
        st.download_button(
            "Download the template (the table's own columns)", tmpl.data, file_name=tmpl.filename,
            key=f"tu_tpl_{kind}",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if tmpl.fmt == "xlsx" else "text/csv")
        up = st.file_uploader("The filled file (.xlsx or .csv) - the table's export works as it is",
                              type=["xlsx", "csv"], key=f"tu_file_{kind}")
        if up is None:
            return
        blob = up.getvalue()
        sig = (up.name, len(blob), hash(blob), session.query(mm.ManualMatch.id).count())
        cache_key = f"tu_prev_{kind}"
        if st.session_state.get(cache_key, (None, None))[0] != sig:
            try:
                df = tu.read_table_file(blob, up.name, expected_cols)
            except ValueError as exc:
                st.error(str(exc))
                return
            bar = st.progress(0.0, text=f"Read {fmt_count(len(df))} row(s)...")
            try:
                prev = (tu.preview_bills if kind == "bills" else tu.preview_txns)(
                    session, run.id, df, progress=lambda f, m: bar.progress(f, text=m))
            finally:
                bar.empty()
            st.session_state[cache_key] = (sig, prev)
        prev = st.session_state[cache_key][1]
        st.caption(prev.summary_line("transaction" if kind == "bills" else "change"))
        st.markdown(f"**{fmt_count(len(prev.accepted))} accepted, {fmt_count(len(prev.rejected))} rejected, "
                    f"{fmt_count(len(prev.ignored))} with no change.**")
        shown = prev.as_frame()
        shown = shown[shown["verdict"] != "ignore"]
        if not shown.empty:
            st.dataframe(shown, hide_index=True, width="stretch",
                         column_config={"why": st.column_config.TextColumn(width="large")})
        if prev.rejected:
            st.error(f"{len(prev.rejected)} row(s) rejected - listed above with the reason. Nothing is applied "
                     "until the file has none: fix them and upload again.")
        operator = confirm_with_operator(
            st.button(f"Apply {fmt_count(len(prev.accepted))} row(s)", type="primary", key=f"tu_apply_{kind}",
                      disabled=not prev.can_apply),
            "applying this upload", key=f"tu_op_{kind}")
        if operator:
            try:
                counts = tu.apply_preview(session, prev, operator, file_name=up.name)
            except Exception as exc:  # noqa: BLE001 - shown with its reason; the whole file rolled back
                st.error(f"Nothing was applied: {exc}")
                return
            st.session_state.pop(cache_key, None)
            st.session_state["_pair_msg"] = (f"Applied: {counts['matched']} matched, {counts['refunded']} refunded, "
                                             f"{counts['remarks']} remark(s).")
            st.cache_data.clear()
            st.rerun()


def _exception_table_block(kind: str, statuses, scope_label: str, editable: bool, sort_label: str):
    """One exception table (bills or transactions) for the given statuses: one server-side page and its
    export. `kind` is "bills" or "txns". Editable only while the work is open; a closed view is read-only."""
    bills = kind == "bills"
    types = BILL_EXCEPTION_TYPES if bills else TXN_EXCEPTION_TYPES
    n, value = _exc_counts_for(types, statuses)
    st.markdown(f"**Exception {'bills' if bills else 'transactions'}**")
    if n == 0:
        st.caption("Nothing in this view.")
        return
    name = f"exc_{kind}_{scope_label}"
    offset = _pager(name, n, (str(date_from), str(date_to), store, scope_label, sort_label, run.id))
    page_df, _, _ = q.exception_page(session, date_from, date_to, store, types, statuses,
                                     limit=EXC_PAGE, offset=offset, sort=sort_label)
    frame = (_bill_exception_frame if bills else _txn_exception_frame)(page_df)
    columns = BILL_EXC_COLUMNS if bills else TXN_EXC_COLUMNS
    config = BILL_EXC_COLUMN_CONFIG if bills else TXN_EXC_COLUMN_CONFIG
    pairing = None
    if st.session_state.get("_pair_msg") and bills:
        st.success(st.session_state.pop("_pair_msg"))
    if bills and editable:
        frame, pairing = _add_pairing(frame)
        columns = BILL_EXC_PAIR_COLUMNS
        config = {**BILL_EXC_COLUMN_CONFIG, **PAIR_COLUMN_CONFIG}
    if editable:
        _editable_exceptions(frame, columns, config,
                             f"sm_exc_{kind}_editor_{scope_label}_{offset}",
                             "bill_no" if bills else "transaction_id", "Bill" if bills else "Txn",
                             pairing=pairing, txn_actions=not bills)
        if pairing is not None:
            _cluster_resolver(frame)
    else:
        st.dataframe(_display_exceptions(frame), hide_index=True, width="stretch",
                     column_order=columns if not bills else BILL_EXC_COLUMNS, column_config=config)
    if not bills and (frame["gateway"] == "axis").any():
        st.caption("Axis does not supply a transaction time.")

    what = ("Exception bills" if bills else "Exception transactions") + ("" if scope_label in ("Open", "Under investigation") else f" ({scope_label.lower()})")

    def _fetch_export(progress):
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
        _fetch_export, n,
        ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=date_from, date_to=date_to,
                   filters={"store": store, "show": scope_label}, value_column="amount", title=what),
        key=f"dl_exc_{kind}_{scope_label.replace(' ', '_')}", noun=what.lower())
    if editable:
        if bills:
            _table_upload("bills", "Exception bills", et.bill_columns(pairing=True, screen=False))
        else:
            _table_upload("txns", "Exception transactions", list(et.TXN_COLUMNS))


def _exc_counts_for(types, statuses):
    """(count, value) of the filtered exceptions of these types/statuses - one SQL aggregate."""
    if types is BILL_EXCEPTION_TYPES:
        n, v, _, _ = q.exception_counts(session, date_from, date_to, store, types, (), statuses=statuses)
    else:
        _, _, n, v = q.exception_counts(session, date_from, date_to, store, (), types, statuses=statuses)
    return n, v


def _bulk_accept_block():
    """Two buttons above the Exception bills table: exact single-candidate matches, and split payments. Each
    previews its full list (exportable), then applies as manual matches after a confirmation. Nothing is
    applied from the cached suggestion: `bulk_accept.apply` validates every row again at that moment."""
    fp = _pairing_fp()
    pool, bills_pop, assignment = _cached_assignment(fp)
    if st.session_state.get("_bulk_fp") != fp:
        st.session_state["_bulk_set"] = ba.build(bills_pop, pool, assignment)
        st.session_state["_bulk_fp"] = fp
    bulk = st.session_state["_bulk_set"]

    done = st.session_state.pop("_bulk_result", None)
    if done is not None:
        rep, ov = done
        st.success(f"Bulk accept ({rep.kind}) applied: {fmt_count(rep.bills_closed)} bills closed "
                   f"({money_table(rep.value)}), {fmt_count(rep.transactions_consumed)} transactions consumed, "
                   f"{fmt_count(rep.tasks_closed)} store tasks closed, in {rep.seconds:.0f}s. Digital receivable "
                   f"{money_table(rep.open_value_before)} -> {money_table(rep.open_value_after)}. Batch #{rep.batch_id} "
                   "- reverse it below if needed.")
        st.caption("Two-sided controls after this accept: bills open + system + manual = total "
                   + ("holds" if ov.bills_ok else f"BREAKS by {ov.bills_difference}") + "; transactions system + manual + "
                   "refunded + unmapped = total " + ("holds" if ov.txns_ok else f"BREAKS by {ov.txns_difference}") + ".")
        if rep.refused:
            st.error(f"{len(rep.refused)} row(s) were refused at the moment of applying and stay in the table:")
            st.dataframe(pd.DataFrame(rep.refused), hide_index=True, width="stretch")

    c1, c2, _c3 = st.columns([1.5, 1.5, 2])
    pick_exact = c1.button(f"Accept all exact matches - {fmt_count(bulk.exact_n)} bills, {money_table(bulk.exact_value)}",
                           key="bulk_btn_exact", disabled=bulk.exact_n == 0,
                           help="One candidate, amount equal to the bill to the paisa, same store, still unmapped.")
    pick_split = c2.button(f"Accept all split matches - {fmt_count(bulk.split_n)} bills, {money_table(bulk.split_value)}",
                           key="bulk_btn_split", disabled=bulk.split_n == 0,
                           help="Amount 1 + Amount 2 equal the bill exactly; both transactions are consumed.")
    if pick_exact:
        st.session_state["_bulk_preview"] = "exact"
    if pick_split:
        st.session_state["_bulk_preview"] = "split"
    if bulk.left:
        why = "; ".join(f"{v['bills']:,} ({money_table(v['value'])}) {k}" for k, v in bulk.left.items())
        st.caption(f"After both buttons {fmt_count(bulk.left_n)} bills ({money_table(bulk.left_value)}) stay for you "
                   f"to decide one by one: {why}.")

    kind = st.session_state.get("_bulk_preview")
    if kind:
        rows = bulk.exact if kind == "exact" else bulk.split
        st.markdown(f"**Preview - {fmt_count(len(rows))} {kind} matches, {money_table(sum(r.amount for r in rows))}.** "
                    "Nothing is applied until you confirm.")
        prev = ba.preview_frame(rows)
        shown = prev.copy()
        shown["date"] = shown["date"].map(display_date)
        shown["txn_date"] = shown["txn_date"].map(display_date)
        for col in ("amount", "Amount 1", "Amount 2"):
            shown[col] = shown[col].map(lambda v: money_table(v) if pd.notna(v) else "")
        st.dataframe(shown, hide_index=True, width="stretch", height=320)
        frame_export_button(prev, ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=date_from,
                                             date_to=date_to, filters={"bulk_accept": kind}, value_column="amount",
                                             title=f"Bulk accept preview - {kind}"),
                            key=f"bulk_prev_dl_{kind}", noun=f"{kind} matches")
        a1, a2, _a3 = st.columns([1.4, 1, 3])
        operator = confirm_with_operator(
            a1.button(f"Apply {fmt_count(len(rows))} {kind} matches", type="primary", key=f"bulk_apply_{kind}"),
            "applying this bulk accept", key=f"bulk_op_{kind}")
        if a2.button("Cancel", key="bulk_cancel"):
            st.session_state.pop("_bulk_preview", None)
            st.rerun()
        if operator:
            bar = st.progress(0.0, text="Checking every row again against the bills and transactions as they are now...")
            try:
                rep = ba.apply(session, run.id, rows, operator, kind, progress=lambda f, m: bar.progress(f, text=m))
            except Exception as exc:  # noqa: BLE001 - shown with its reason; nothing was applied
                bar.empty()
                st.error(f"Nothing was applied: {exc}")
                return
            bar.empty()
            st.session_state["_bulk_result"] = (rep, q.match_overview(session, run.id, date_from, date_to, store))
            st.session_state.pop("_bulk_preview", None)
            st.session_state.pop("_bulk_fp", None)
            st.cache_data.clear()
            st.rerun()

    batches = session.query(mm.ManualMatchBatch).filter(mm.ManualMatchBatch.source == ba.BULK_SOURCE) \
        .order_by(mm.ManualMatchBatch.id.desc()).all()
    if batches:
        with st.expander(f"Bulk accept batches ({len(batches)}) - reverse one"):
            live = {b.id: session.query(mm.ManualMatch).filter(mm.ManualMatch.batch_id == b.id,
                                                              mm.ManualMatch.lifecycle == "active").count()
                    for b in batches}
            st.dataframe(pd.DataFrame([{"batch": b.id, "when": b.created_at.strftime("%d/%m/%Y %H:%M"),
                                        "by": b.operator, "applied": b.rows_accepted, "refused": b.rows_rejected,
                                        "still active": live[b.id], "rule": (b.note or "")[:60]} for b in batches]),
                         hide_index=True, width="stretch")
            pick = st.selectbox("Batch to reverse", [b.id for b in batches if live[b.id]], key="bulk_rev_pick")
            reason = st.text_input("Reason for reversing (kept in the history)", key="bulk_rev_reason")
            who = confirm_with_operator(
                st.button("Reverse this batch", key="bulk_rev_btn", disabled=not (pick and reason.strip())),
                "reversing this batch", key="bulk_rev_op")
            if who and pick and reason.strip():
                with st.spinner("Reversing - bills and transactions return to the exception tables..."):
                    n = mm.reverse_batch(session, int(pick), who, reason.strip())
                st.session_state.pop("_bulk_fp", None)
                st.cache_data.clear()
                st.success(f"Reversed {n} match(es). The records and their history are kept.")
                st.rerun()


# --- the tiles: bills and transactions split by who matched them (one overview, shared with Summary) ---
_overview = q.match_overview(session, run.id, date_from, date_to, store)
match_strip.render_tiles(_overview)

st.divider()

# --- Exceptions: ONE header line carrying both populations, and the filters beside it -----------
f1, f2, f3, _f4 = st.columns([1, 1.2, 1, 2.8])
scope = _persisted_select("Show", ["Open", "Under investigation"], "sm_exc_scope_value", "sm_exc_scope_w", container=f1)
sort_label = _persisted_select("Sort by", list(q.EXCEPTION_SORTS), "sm_exc_sort_value", "sm_exc_sort_w",
                               container=f2)
status_view = _persisted_select("Status", ["Open", "Closed", "All"], "sm_exc_status_value", "sm_exc_status_w",
                                container=f3)
_live = ("open",) if scope == "Open" else ("under_investigation",)
status_wanted = {"Open": _live, "Closed": CLOSED_STATUSES,
                 "All": ("open", "under_investigation") + CLOSED_STATUSES}[status_view]
scope_label = {"Open": scope, "Closed": "Closed", "All": "All"}[status_view]

_hb_n, _hb_v = _exc_counts_for(BILL_EXCEPTION_TYPES, status_wanted)
_ht_n, _ht_v = _exc_counts_for(TXN_EXCEPTION_TYPES, status_wanted)
st.markdown(f"**Exception bills {fmt_count(_hb_n)} · {money_short(_hb_v)}**&nbsp;&nbsp;|&nbsp;&nbsp;"
            f"**Exception transactions {fmt_count(_ht_n)} · {money_short(_ht_v)}**")

if status_view == "Open":
    _bulk_accept_block()
_exception_table_block("bills", status_wanted, scope_label, status_view == "Open", sort_label)

# A bill a person matched that STILL has another unmapped transaction of the same store and
# amount nearby: a genuine split or duplicate payment. The bill stays closed, the second
# transaction stays open, and the decision (refund, or a second bill) is left to a person.
if status_view == "Open":
    _second = ep.second_transaction_still_open(session, _cached_assignment(_pairing_fp())[0], PAIR_DAYS)
    if not _second.empty:
        st.markdown(f"**Second transaction still open** {fmt_count(len(_second))} - bills matched by a person "
                    "that have another unmapped transaction of the same store and amount.")
        _show2 = _second.copy()
        _show2["bill_date"] = _show2["bill_date"].map(display_date)
        _show2["second_date"] = _show2["second_date"].map(display_date)
        _show2["amount"] = _show2["amount"].map(money_table)
        st.dataframe(_show2, hide_index=True, width="stretch")

st.divider()

_exception_table_block("txns", status_wanted, scope_label, status_view == "Open", sort_label)


# ===========================================================================
# CLOSED BY A PERSON (2026-10-16). When a bill and a transaction are matched - inline or by upload - both
# leave the exception tables and appear here, in tables with EXACTLY the exception tables' columns plus
# who, when and how. A customer refund appears in Manual match transactions, marked "customer refund".
# Either can be reversed (who and when recorded, the record kept): both return to the exception tables
# and the bill's store task is raised again.
# ===========================================================================
st.divider()
if st.session_state.get("_rev_msg"):
    st.success(st.session_state.pop("_rev_msg"))


# A new key after every reversal, so the row that was selected is not left selected over a different row.
_REV_GEN = st.session_state.setdefault("_rev_gen", 0)


def _matched_view(frame: pd.DataFrame, columns: list, what: str, money_cols=("amount", "Amount 1", "Amount 2")):
    out = frame[columns].copy()
    if "date" in out.columns:
        out["date"] = out["date"].map(display_date)
    for c in money_cols:
        if c in out.columns:
            out[c] = out[c].map(lambda v: money_table(v) if pd.notna(v) and v != "" else "")
    return out


def _reverse_panel(frame: pd.DataFrame, picked: list, key: str, describe, reverse_fn):
    if not picked:
        st.caption("Select a row to reverse it.")
        return
    row = frame.iloc[picked[0]]
    st.markdown(f"**Reverse:** {describe(row)}")
    reason = st.text_input("Reason for the reversal (required)", key=f"{key}_reason_{int(row['id'])}_{row.get('kind', '')}")
    operator = confirm_with_operator(
        st.button("Reverse - return both to Exceptions", key=f"{key}_go", disabled=not reason.strip()),
        "reversing this", key=f"{key}_op")
    if operator:
        try:
            reverse_fn(row, operator, reason)
        except (ValueError, LookupError) as exc:
            session.rollback()
            st.error(f"Refused: {exc}")
            return
        st.session_state["_rev_msg"] = f"Reversed - {describe(row)}. Both are back in the exception tables."
        st.session_state["_rev_gen"] = _REV_GEN + 1
        st.cache_data.clear()
        st.rerun()


# --- Manual match bills ---------------------------------------------------------------------------
mb_frame = et.manual_bill_frame(session, run.id, date_from, date_to, store)
st.markdown(f"**Manual match bills {fmt_count(len(mb_frame))} · "
            f"{money_short(float(mb_frame['amount'].sum()) if len(mb_frame) else 0.0)}**")
if mb_frame.empty:
    st.caption("No bill has been matched by a person in this view.")
else:
    ev = st.dataframe(_matched_view(mb_frame, et.bill_matched_columns(), "Manual match bills"), hide_index=True,
                      width="stretch", on_select="rerun", selection_mode="single-row", key=f"mb_table_{_REV_GEN}",
                      column_config={"Txn 1": st.column_config.TextColumn(width="medium"),
                                     "matched_txn": st.column_config.TextColumn(width="medium")})
    frame_export_button(
        et.export_view(mb_frame, et.bill_matched_columns(), "Manual match bills"),
        ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=date_from, date_to=date_to,
                   filters={"store": store}, value_column="amount", title="Manual match bills"),
        key="dl_manual_bills", noun="manual match bills")
    _reverse_panel(
        mb_frame, ev.selection.rows if ev and ev.selection else [], "mb_rev",
        lambda r: f"bill {r['bill_no']} <-> transaction {r['matched_txn']}",
        lambda r, who, why: mm.reverse(session, int(r["id"]), who, why))

st.divider()

# --- Manual match transactions --------------------------------------------------------------------
mt_frame = et.manual_txn_frame(session, date_from, date_to, store)
st.markdown(f"**Manual match transactions {fmt_count(len(mt_frame))} · "
            f"{money_short(float(mt_frame['amount'].sum()) if len(mt_frame) else 0.0)}**")
if mt_frame.empty:
    st.caption("No transaction has been matched or refunded by a person in this view.")
else:
    ev2 = st.dataframe(_matched_view(mt_frame, et.txn_matched_columns(), "Manual match transactions"),
                       hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                       key=f"mt_table_{_REV_GEN}", column_config={"transaction_id": st.column_config.TextColumn(width="medium")})
    frame_export_button(
        et.export_view(mt_frame, et.txn_matched_columns(), "Manual match transactions"),
        ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=date_from, date_to=date_to,
                   filters={"store": store}, value_column="amount", title="Manual match transactions"),
        key="dl_manual_txns", noun="manual match transactions")

    def _reverse_txn(r, who, why):
        if r["kind"] == "refund":
            rf.reverse_refund(session, int(r["id"]), who, why)
        else:
            mm.reverse(session, int(r["id"]), who, why)

    _reverse_panel(
        mt_frame, ev2.selection.rows if ev2 and ev2.selection else [], "mt_rev",
        lambda r: (f"customer refund of transaction {r['transaction_id']} (Zoho {r['zoho_no']})"
                   if r["kind"] == "refund" else f"transaction {r['transaction_id']} <-> bill {r['matched_bill']}"),
        _reverse_txn)

# --- The register: every match and refund ever recorded, reversed ones included --------------------
st.divider()
with st.expander("The register - every manual match and refund ever recorded, reversed ones included"):
    reg = q.manual_matches(session, date_from, date_to, store)
    if reg.empty:
        st.caption("No manual match recorded for this selection.")
    else:
        view = reg.copy()
        view["Date"] = view["business_date"].map(display_date)
        view["When"] = pd.to_datetime(view["created_at"]).dt.strftime("%d/%m/%Y %H:%M")
        view["Amount (Rs)"] = view["amount"].map(money_table)
        view["lifecycle_note"] = view["lifecycle_note"].map(fmt_blank)
        st.dataframe(status_styler(view[["id", "Date", "store", "bill_no", "mop", "Amount (Rs)", "gateway",
                                         "transaction_id", "status", "operator", "When", "lifecycle",
                                         "lifecycle_note", "source" if "source" in view.columns else "batch_id"]]
                                   .rename(columns={"id": "#", "store": "Store", "bill_no": "Bill no", "mop": "MOP",
                                                    "gateway": "Gateway", "transaction_id": "Transaction id",
                                                    "status": "Status", "operator": "By", "lifecycle": "Lifecycle",
                                                    "lifecycle_note": "Reversal / note"}),
                                   {"Lifecycle": {"active": "good", "reversed": "warn", "orphaned": "warn",
                                                  "superseded": "accent", "conflicted": "bad"}}),
                     hide_index=True, width="stretch",
                     column_config={"Transaction id": st.column_config.TextColumn(width="medium")})
    events = rf.refund_register(session)
    if not events.empty:
        st.markdown("**Refund events**")
        ev_view = events.copy()
        ev_view["at"] = pd.to_datetime(ev_view["at"]).dt.strftime("%d/%m/%Y %H:%M")
        ev_view["amount"] = ev_view["amount"].map(money_table)
        ev_view["refund_date"] = ev_view["refund_date"].map(display_date)
        st.dataframe(ev_view, hide_index=True, width="stretch",
                     column_config={"transaction_id": st.column_config.TextColumn(width="medium")})
