"""Store Task - assign items to stores, get their reply, track to closure (2026-10-14).

Three kinds of task are GENERATED from the data (open bill, CMS not done, high float) and
close themselves when the underlying fact clears; a store's reply never closes one. Head office
adds remarks, marks a task under review, or closes it with a reason. Replies arrive by bulk
upload today; every task carries its store, and every read is store-scoped, so a store login
later is a filter, not a rebuild.

Reads the `store_tasks` table only - never a bill line, never a raw file. Changes no figure
anywhere. See mop_reco/store_tasks.py for the rules.
"""

from __future__ import annotations

import io
import json
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
    latest_run,
    no_run_yet,
    page,
    sidebar,
    status_styler,
    stop_if_no_dates,
    tile,
)
from mop_reco import store_tasks as stt
from mop_reco.db import ManualMatch, StoreTask
from mop_reco.export import ExportMeta
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import display_date, money_short, money_table

CONFIG_PATH = Path(__file__).resolve().parents[2] / "app" / "config.yaml"
PAGE_SIZE = 50
BAD_BG, BAD_FG = "#FCEAEE", "#D93A5B"   # literal hex: the grid is canvas-drawn (CLAUDE.md 8.1)

page("Store Task", "Items for stores to answer, tracked to closure. Tasks are raised from the data "
                   "and close themselves when the data clears them; a reply alone never does.")

session = db_session()
run = latest_run(session)
if run is None:
    no_run_yet()

date_from, date_to, store = sidebar(session, run)
stop_if_no_dates(date_from, run)
today = date.today()

with CONFIG_PATH.open("r", encoding="utf-8") as fh:
    CONFIG = yaml.safe_load(fh)


# --- keep the task list in step with the data ------------------------------------------
# Auto-closure has to see a manual match the moment it is made, not at the next run, so the
# page re-syncs when the run or the set of active manual matches changed since this session
# last did. The sync is idempotent and reads only precomputed rows.
def _fingerprint():
    n, top = session.query(ManualMatch.id).filter(ManualMatch.lifecycle == "active").count(), \
        session.query(ManualMatch.id).filter(ManualMatch.lifecycle == "active").order_by(
            ManualMatch.id.desc()).limit(1).scalar()
    return (run.id, n, top, today.isoformat())


def _sync():
    report = stt.sync_tasks(session, run.id, today, CONFIG)
    st.session_state["st_last_sync"] = report.as_dict()
    st.session_state["st_sync_fp"] = _fingerprint()
    st.cache_data.clear()


if st.session_state.get("st_sync_fp") != _fingerprint():
    with st.spinner("Checking tasks against the data..."):
        _sync()

# --- filters (this page's own - the global ones are in the sidebar) ----------------------
def _persisted(widget, label, options, value_key, **kw):
    current = st.session_state.get(value_key)
    if kw.get("multi"):
        kw.pop("multi")
        current = [c for c in (current if current is not None else kw.pop("default")) if c in options]
        kw.pop("default", None)
        chosen = widget(label, options, default=current, key=f"w_{value_key}", **kw)
    else:
        if current not in options:
            current = options[0]
        chosen = widget(label, options, index=options.index(current), key=f"w_{value_key}", **kw)
    st.session_state[value_key] = chosen
    return chosen


STATUS_VIEWS = {"Open, Replied, Under review": [stt.OPEN, stt.REPLIED, stt.UNDER_REVIEW],
                "Open": [stt.OPEN], "Replied": [stt.REPLIED], "Under review": [stt.UNDER_REVIEW],
                "Closed": [stt.CLOSED], "All": None}

# One row of filters, side by side, all styled like the sidebar's (theme.css .st-key-st-filters).
store_choices = ["All stores"] + [x for (x,) in session.query(StoreTask.store).distinct().order_by(StoreTask.store).all()]
with st.container(key="st-filters"):
    f1, f2, f3, f4 = st.columns([1.2, 2.2, 1.5, 1.5])
    page_store = _persisted(f1.selectbox, "Store code", store_choices, "st_store")
    search = f2.text_input("Search", value=st.session_state.get("st_search", ""), key="w_st_search",
                           placeholder="Bill no, store, reply or remark")
    st.session_state["st_search"] = search
    type_pick = _persisted(f3.selectbox, "Type", ["All types"] + list(stt.TYPE_LABEL.values()), "st_type")
    status_pick = _persisted(f4.selectbox, "Status", list(STATUS_VIEWS), "st_status")

sort = "Overdue first, oldest due"
overdue_only = False
if page_store != "All stores":
    store = page_store          # the page's own store filter wins over the sidebar's

filters = dict(
    date_from=date_from, date_to=date_to, store=store,
    types=None if type_pick == "All types" else [stt.LABEL_TO_TYPE[type_pick]],
    statuses=STATUS_VIEWS[status_pick],
    overdue_only=overdue_only, search=search or None)
tile_filters = {k: filters[k] for k in ("date_from", "date_to", "store", "types", "search")}

# --- tiles ---------------------------------------------------------------------------
T = stt.tiles(session, today, **tile_filters)
# Open - raised, no store reply yet. Pending - the store has replied, awaiting head-office review.
# WIP - head office reviewing or action underway. Closed - resolved, by the data or by a person.
with st.container(key="tile-ribbon"):
    c1, c2, c3, c4 = st.columns(4)
    for col, label, key, tip in (
            (c1, "OPEN", "open", "Raised, no store reply yet"),
            (c2, "PENDING", "pending", "The store has replied; awaiting head-office review"),
            (c3, "WIP", "wip", "Head office reviewing, or action underway"),
            (c4, "CLOSED", "closed", "Resolved - by the data or by a person")):
        n, v = T[key]
        tile(col, label, fmt_count(n), note=money_short(v), tooltip=f"{tip} - Rs {money_table(v)}")

last = st.session_state.get("st_last_sync")
if not last:
    notes = {}
    try:
        notes = (json.loads(run.notes or "{}")).get("store_tasks") or {}
    except ValueError:
        notes = {}
    last = notes or None
if last and "error" not in last:
    made = sum((last.get("created") or {}).values())
    closed = sum((last.get("auto_closed") or {}).values())
    still = sum(n for n, _ in (last.get("still_open") or {}).values())
    st.caption(f"Last task check: **{fmt_count(made)}** created, **{fmt_count(closed)}** closed by the data, "
               f"**{fmt_count(still)}** still open.")
elif last:
    st.error(f"The last task check failed: {last['error']}")

by_type = stt.type_summary(session, today, **tile_filters)
shown = by_type.copy()
shown["Value"] = shown["Value"].map(money_table)
st.dataframe(shown, hide_index=True, width="stretch")

if st.button("Check tasks against the data now", key="st_resync",
             help="Re-reads the open bills, deposits and float from the latest run and closes any task "
                  "whose condition has cleared. Runs automatically when the run or a manual match changes."):
    _sync()
    st.rerun()

# --- the grid, one server-side page -----------------------------------------------------
total = stt._filtered(session, today=today, **filters).count()
fp = (run.id, date_from, date_to, store, type_pick, status_pick, search)
if st.session_state.get("st_page_fp") != fp:
    st.session_state["st_page_fp"], st.session_state["st_offset"] = fp, 0
offset = min(st.session_state.get("st_offset", 0), max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE) if total else 0)
st.session_state["st_offset"] = offset

st.markdown(f"##### Tasks - {fmt_count(total)} in this view")
if not total:
    st.info("No tasks match this view.")
else:
    p = stt.tasks_page(session, today, sort, PAGE_SIZE, offset, **filters)
    overdue_mask = p["_overdue"].tolist()
    ids = p["_id"].tolist()
    view = p.drop(columns=["_id", "_overdue"]).copy()
    view["Amount"] = view["Amount"].map(money_table)
    view = view.drop(columns=["Due by"])
    for c in ("Business date", "Raised on", "Closed on"):
        view[c] = view[c].map(lambda v: display_date(v) if v else "")
    for c in ("Store reply", "Replied by", "Replied at", "HO remark", "Closed by"):
        view[c] = view[c].fillna("")
    styler = status_styler(view, {"Status": {"Closed": "good", "Replied": "accent", "Under review": "warn"}})
    event = st.dataframe(styler, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="st_grid")

    if total > PAGE_SIZE:
        b1, b2, b3 = st.columns([1, 1, 3])
        if b1.button("Previous", disabled=offset == 0, key="st_prev"):
            st.session_state["st_offset"] = max(0, offset - PAGE_SIZE)
            st.rerun()
        if b2.button("Next", disabled=offset + PAGE_SIZE >= total, key="st_next"):
            st.session_state["st_offset"] = offset + PAGE_SIZE
            st.rerun()
        b3.markdown(f"<div class='mop-page-info'>Showing <b>{fmt_count(offset + 1)}-"
                    f"{fmt_count(min(offset + PAGE_SIZE, total))}</b> of <b>{fmt_count(total)}</b></div>",
                    unsafe_allow_html=True)

    # --- head office: remark, under review, close ---
    picked = list(event.selection.rows) if event and event.selection else []
    open_id = stt.parse_id(st.text_input("Or open a task by its id", key="st_open_id", placeholder="ST-000123",
                                         help="Select a row above, or type a task id - for example one a "
                                              "store quoted in its reply."))
    task = session.get(StoreTask, open_id) if open_id else (session.get(StoreTask, ids[picked[0]]) if picked else None)
    if open_id and task is None:
        st.warning(f"No task {stt.display_id(open_id)}.")
    if task is not None:
        st.markdown(f"###### {stt.display_id(task.id)} - {stt.TYPE_LABEL[task.task_type]} - {task.store}, "
                    f"{display_date(task.business_date)} - Rs {money_table(float(task.amount))}")
        detail = stt.detail_text(task.task_type, task.bill_no, task.mop, json.loads(task.detail or "{}"))
        st.caption(f"{detail}.  *Question to the store:* {stt.QUESTION[task.task_type]}")
        if task.store_reply:
            st.markdown(f"**Store reply** ({task.replied_by}, {task.replied_at:%d/%m/%Y %H:%M}): {task.store_reply}")
        if task.status == stt.CLOSED:
            st.caption(f"Closed on {display_date(task.closed_on)} by **{task.closed_by}** - {task.close_reason}.")
        else:
            remark = st.text_area("Head office remark", value=task.ho_remark or "", key=f"st_remark_{task.id}")
            r1, r2, r3 = st.columns([1, 1, 3])
            op = confirm_with_operator(r1.button("Save remark", key=f"st_save_{task.id}"),
                                       "recording this remark", key=f"st_op_save_{task.id}")
            if op:
                try:
                    stt.add_remark(session, task.id, remark, op)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            op = confirm_with_operator(r2.button("Mark under review", key=f"st_review_{task.id}",
                                                 disabled=task.status == stt.UNDER_REVIEW),
                                       "marking this under review", key=f"st_op_review_{task.id}")
            if op:
                stt.mark_under_review(session, task.id, op)
                st.rerun()
            reason = st.text_input("Close with a reason", key=f"st_close_reason_{task.id}",
                                   placeholder="why this can be closed now")
            op = confirm_with_operator(st.button("Close this task", key=f"st_close_{task.id}"),
                                       "closing this task", key=f"st_op_close_{task.id}")
            if op:
                try:
                    stt.close_task(session, task.id, reason, op, today)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        hist = stt.history(session, task.id)
        with st.expander(f"History - {fmt_count(len(hist))} entr{'y' if len(hist) == 1 else 'ies'}"):
            st.dataframe(hist, hide_index=True, width="stretch")

# --- export the whole grid ---------------------------------------------------------------
meta = ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=date_from, date_to=date_to,
                  filters={"store": store, "type": type_pick, "status": status_pick, "search": search or None},
                  value_column="Amount", title="Store Task")
export_all_button(lambda progress: stt.tasks_all(session, today, sort, progress=progress, **filters),
                  total, meta, key="dl_store_task", noun="tasks in this view")

# --- replies: send out, bring back -------------------------------------------------------
st.divider()
st.markdown("##### Upload store replies")
st.caption("Stores have no login yet. Send a store its open tasks, and load the completed file back. "
           "Every row is checked first - the task exists, is not closed, and the store is the task's own - "
           "and nothing is applied until you confirm.")

live_stores = [s for (s,) in session.query(StoreTask.store).filter(StoreTask.status.in_(stt.LIVE_STATUSES))
               .distinct().order_by(StoreTask.store).all()]
if live_stores:
    e1, e2 = st.columns([1, 3])
    pick = e1.selectbox("Tasks for store", live_stores,
                        index=live_stores.index(store) if store in live_stores else 0, key="st_send_store")
    sheet = stt.store_sheet(session, pick, today, date_from=date_from, date_to=date_to)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        sheet.to_excel(writer, index=False, sheet_name=pick[:31])
        ws, book = writer.sheets[pick[:31]], writer.book
        text = book.add_format({"num_format": "@"})
        ws.set_column(0, 0, 12, text)
        ws.set_column(5, 6, 60)
        ws.set_column(8, 9, 30)
    e2.download_button(f"Download {len(sheet)} open task(s) for {pick}", buf.getvalue(),
                       file_name=f"store_tasks_{pick}.xlsx", key="st_dl_store", disabled=sheet.empty,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

tbuf = io.BytesIO()
with pd.ExcelWriter(tbuf, engine="xlsxwriter") as writer:
    stt.upload_template().to_excel(writer, index=False, sheet_name="store_replies")
st.download_button("Download the reply template", tbuf.getvalue(), file_name="store_task_reply_template.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="st_dl_tpl")
st.caption(f"Columns, exactly: `{'`, `'.join(stt.TEMPLATE_COLUMNS)}`.")

uploaded = st.file_uploader("The completed file", type=["xlsx", "csv"], key="st_upload")
if uploaded is not None:
    try:
        frame = (pd.read_csv(uploaded, dtype=str) if uploaded.name.lower().endswith(".csv")
                 else pd.read_excel(uploaded, dtype=str))
        preview = stt.preview_replies(session, frame)
    except Exception as exc:  # noqa: BLE001 - show the real reason
        st.error(f"Could not use the file - {type(exc).__name__}: {exc}")
        st.stop()
    m1, m2 = st.columns(2)
    m1.metric("Would be accepted", len(preview.accepted))
    m2.metric("Rejected", len(preview.rejected))
    st.dataframe(preview.as_frame(), hide_index=True, width="stretch",
                 column_config={"Why": st.column_config.TextColumn(width="large")})
    if preview.rejected:
        st.error(f"**{len(preview.rejected)} row(s) rejected** and listed above - they are never applied.")
    op = confirm_with_operator(st.button("Apply the accepted replies", type="primary", key="st_apply",
                                         disabled=not preview.accepted),
                               "applying these replies", key="st_apply_op")
    if op:
        n = stt.apply_replies(session, preview, op)
        st.success(f"{fmt_count(n)} repl{'y' if n == 1 else 'ies'} applied.")
        st.rerun()
