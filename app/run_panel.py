"""The Run section of the Upload page (2026-10-14): what a run would read, the run history with
heartbeat, and the Run reconciliation controls. Moved here from the former Run page, unchanged.
`render()` is called once by pages/6_Upload.py, below the upload section."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import streamlit as st

from dashboard_common import confirm_with_operator, latest_run, loaded_dates
from mop_reco import queries as q
from mop_reco.formatting import count as fmt_count
from mop_reco.formatting import money_short, money_table
from mop_reco.ingest import warehouse_manifest
from mop_reco.runs import RunAlreadyInProgress, run_reconciliation


def render(config, session):
    # --- can I run, and against what -----------------------------------------
    st.markdown("##### Data available to read")
    available = pd.DataFrame([{
        "Source": status.name,
        "Files": status.file_count,
        "Location": status.configured_path or "not configured",
        "Most recent": (max(status.files, key=lambda f: f.stat().st_mtime).name
                        if status.files else "—"),
    } for status in config.all_source_statuses()])
    st.dataframe(available, hide_index=True, width="stretch")

    not_supplied = available.loc[available["Files"] == 0, "Source"].tolist()
    if not_supplied:
        st.caption(
            "Not supplied: " + ", ".join(not_supplied)
            + ". Reported rather than skipped — R3a needs the opening cash balance "
              "file, and the working-day calendar needs the holiday list."
        )

    for status in config.all_source_statuses():
        for warning in status.warnings:
            st.warning(f"**{status.name}** — {warning}")

    st.divider()

    # --- what has been run, and when -----------------------------------------
    st.markdown("##### Runs so far")
    history = q.run_history(session, limit=15)
    if history.empty:
        st.info("Nothing has been run yet.")
    else:
        # A running (or stalled) row is a black box otherwise — the operator
        # cannot tell working from hung (2026-09-24: a 5-month run sat silent
        # for 14+ minutes with nothing on screen saying so). This banner is the
        # fix: current stage, elapsed time and a last-heartbeat age that answers
        # "is this still moving" without opening a terminal.
        live = history.iloc[0]
        if live["status"] in ("running", "stalled"):
            elapsed_min = (live["duration_s"] or 0) / 60
            heartbeat_age = live["heartbeat_age_s"]
            cols = st.columns([3, 1])
            with cols[0]:
                if live["status"] == "stalled":
                    st.error(
                        f"**Run #{live['run']} looks STALLED** — last heartbeat "
                        f"{heartbeat_age/60:.1f} min ago (stalled after "
                        f"{q.STALLED_AFTER_SECONDS/60:.0f} min of silence). "
                        f"Last known stage: **{live['current_stage'] or '—'}** — "
                        f"{live['progress_detail'] or 'no detail recorded'}."
                    )
                else:
                    st.info(
                        f"**Run #{live['run']} is running** — {elapsed_min:.1f} min "
                        f"elapsed. Stage: **{live['current_stage'] or '—'}** — "
                        f"{live['progress_detail'] or 'starting…'} "
                        f"(last heartbeat {heartbeat_age:.0f}s ago)."
                    )
            with cols[1]:
                if st.button("Refresh progress", key="refresh_progress", width="stretch"):
                    st.cache_data.clear()
                    st.rerun()
                if live["status"] == "stalled":
                    op = confirm_with_operator(
                        st.button("Mark as failed", key="mark_stalled_failed", width="stretch"),
                        "marking this stalled run as failed", "mark_failed")
                    if op:
                        from mop_reco.db import Run as RunModel
                        row = session.query(RunModel).filter(RunModel.id == int(live["run"])).one()
                        row.status = "failed"
                        row.finished_at = pd.Timestamp.now().to_pydatetime()
                        row.notes = json.dumps({
                            "failure_reason": f"Marked failed by {op} — stalled, no "
                                              f"heartbeat for {heartbeat_age/60:.1f} min "
                                              f"at stage '{live['current_stage']}'.",
                        })
                        session.commit()
                        st.rerun()

        shown = history.copy()
        shown["started"] = shown["started"].dt.strftime("%d %b %Y %H:%M")
        shown["duration"] = shown["duration_s"].map(
            lambda s: f"{s/60:.1f} min" if pd.notna(s) and s >= 90
            else (f"{s:.0f}s" if pd.notna(s) else "—"))
        shown["bill_lines"] = shown["bill_lines"].map(
            lambda n: fmt_count(n) if pd.notna(n) else "—")
        shown["matched"] = shown["matched"].map(
            lambda n: fmt_count(n) if pd.notna(n) else "—")
        st.dataframe(
            shown[["run", "range", "started", "status", "duration", "bill_lines", "matched", "by"]],
            hide_index=True, width="stretch")

        # A failed run's own error, never buried — the exact thing that used to
        # require reading the log file to find out.
        failed_rows = history.loc[history["status"] == "failed"]
        if not failed_rows.empty:
            with st.expander(f"{len(failed_rows)} failed run(s) — error detail"):
                for _, row in failed_rows.iterrows():
                    st.markdown(f"**Run #{row['run']}** ({row['range']}): "
                               f"{row['failure_reason'] or 'no error recorded'}")

        # The most recent run of ANY status — deliberately NOT latest_run()
        # (completed-only, 2026-09-26), because this panel is tied to `live`
        # above it (history.iloc[0], also any status): after that fix,
        # latest_run() can point at an older completed run while a newer one
        # is running/failed, and showing THAT run's timings under a banner
        # about a different run would be its own silent-wrong-answer bug.
        from mop_reco.db import Run as RunModel
        last = session.query(RunModel).order_by(RunModel.started_at.desc()).first()
        timings = q.run_timings(last)
        if not timings.empty:
            with st.expander(f"Where run #{last.id}'s time went"):
                t = timings.copy()
                t["seconds"] = t["seconds"].map(lambda s: f"{s:,.1f}s")
                t["share"] = t["share"].map(lambda p: f"{p:.1f}%")
                st.dataframe(t, hide_index=True, width="stretch")

    st.divider()

    # --- what would be re-run, and why ---------------------------------------
    st.markdown("##### What a run would read now")
    rows = []
    for source in ("pos_bill_wise", "paytm_settlement", "axis_edc", "bajaj", "axis_cms", "ptc"):
        status = config.source_status(source)
        if not status.files:
            rows.append({"Source": source, "Files": 0, "Would read": "nothing — no file present",
                         "Why": "source not supplied"})
            continue

        chosen = max(status.files, key=lambda p: p.stat().st_mtime)
        manifest = warehouse_manifest(source, chosen) if source == "pos_bill_wise" else None
        if manifest is not None:
            rows.append({
                "Source": source, "Files": len(status.files),
                "Would read": "warehouse Parquet",
                "Why": "already ingested and unchanged since — the raw file is not opened",
            })
        else:
            rows.append({
                "Source": source, "Files": len(status.files),
                "Would read": chosen.name,
                "Why": "new or changed since the last run" if source == "pos_bill_wise"
                       else "read fresh each run (small file)",
            })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(
        "The 222 MB POS workbook costs about 22 seconds just to open before a single row "
        "is readable, so an unchanged file is read back from Parquet instead."
    )

    st.divider()

    # --- start a run ---------------------------------------------------------
    st.markdown("##### Start a run")
    st.caption(
        "This range is what gets reconciled. It is separate from the view filters in "
        "the sidebar, which only change what the other pages show."
    )
    # Defaulted from what has already been loaded, never a hardcoded month (rule 8).
    # With nothing loaded yet there is nothing to default to, so today is the seed
    # and the operator picks the real range.
    known = loaded_dates(latest_run(session).id) if latest_run(session) else []
    seed_from = date.fromisoformat(known[0]) if known else date.today()
    seed_to = date.fromisoformat(known[-1]) if known else date.today()

    c1, c2 = st.columns(2)
    date_from = c1.date_input("From business date", value=seed_from, format="DD/MM/YYYY")
    date_to = c2.date_input("To business date", value=seed_to, format="DD/MM/YYYY")

    if date_from > date_to:
        st.error("'From' is after 'To' — nothing would be reconciled.")
        st.stop()

    st.caption(
        "The range filters the result. Every discovered file is still read in full first, "
        "because two chargeback-reversals carry dates outside the range they belong to and "
        "filtering first would drop them before they can be flagged."
    )

    operator = confirm_with_operator(
        st.button("Run reconciliation", type="primary"), "running this", key="run")
    if operator:

        # Live progress: a 16-minute run with no indicator is unusable. The stages
        # are known and fixed, so each is announced before it starts and ticked
        # when the run reports its measured time.
        stages = [
            ("POS bill lines", "the 222 MB workbook, or Parquet if unchanged"),
            ("Paytm settlement", "three zip parts, concatenated and deduped"),
            ("Axis EDC", "card transactions"),
            ("Bajaj Pay", "settlement rows"),
            ("R1 matching", "every bill against every gateway"),
            ("Saving results", "store-day results and bill-line detail"),
            ("Exception register", "keeping remarks, auto-resolving what has gone"),
        ]

        progress = st.progress(0.0, text="Starting…")
        log = st.empty()
        log.info(
            "A full month takes about 16 minutes. The heaviest stages are POS ingest and "
            "R1 matching — roughly 6 minutes each. Leave this page open."
        )

        try:
            progress.progress(0.05, text="Reading sources — POS bill lines first…")
            outcome = run_reconciliation(config, session, date_from=date_from,
                                         date_to=date_to, triggered_by=operator)
            progress.progress(1.0, text="Done")
        except RunAlreadyInProgress as exc:
            # The hard lock (2026-09-24) — never queued silently, never started
            # on top of another run. A run started this way is refused BEFORE a
            # Run row is even created, so nothing here needs cleaning up.
            progress.empty()
            if exc.stalled:
                age_min = (exc.heartbeat_age_seconds / 60 if exc.heartbeat_age_seconds
                           is not None else None)
                st.error(
                    f"**Run #{exc.run_id} looks STALLED** — "
                    + (f"no heartbeat for {age_min:.1f} min. " if age_min is not None
                       else "no heartbeat was ever recorded. ")
                    + "It is blocking a new run from starting. Open the run history below, "
                      "confirm it is really dead, and mark it failed — then start this run again."
                )
            else:
                st.error(
                    f"**Run #{exc.run_id} is already in progress** "
                    f"(last heartbeat {exc.heartbeat_age_seconds:.0f}s ago) — this run was "
                    "refused, not queued. Wait for it to finish, or check its progress below."
                )
            st.stop()
        except Exception as exc:  # surfaced, never swallowed
            progress.empty()
            st.error(f"The run failed: {type(exc).__name__}: {exc}")
            st.caption("Nothing was published — the previous run's figures are untouched.")
            st.stop()

        log.empty()
        st.success(
            f"Run #{outcome['run_id']} finished in {outcome['elapsed_seconds']/60:.1f} min.")

        # Measured stage times from the run itself, not a guess.
        timings = outcome.get("timings", {})
        if timings:
            st.markdown("**Where the time went**")
            total = sum(timings.values()) or 1
            st.dataframe(pd.DataFrame([
                {"Stage": s, "Seconds": f"{v:,.1f}s", "Share": f"{v/total*100:.1f}%"}
                for s, v in sorted(timings.items(), key=lambda kv: -kv[1])
            ]), hide_index=True, width="stretch")

        r1 = outcome["r1"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Digital bill lines", fmt_count(r1["digital_bill_lines"]))
        m2.metric("Matched", fmt_count(r1["matched_total"]))
        m3.metric("Unmatched value", money_short(r1["unmatched_value"]))
        m4.metric("Exceptions", fmt_count(outcome["exceptions"]["total_candidates"]))

        st.markdown("**Control totals — what was read**")
        st.dataframe(pd.DataFrame([
            {"Source": s, "Status": r.status, "Rows read": fmt_count(r.rows_read),
             "Control total (Rs)": money_table(r.control_total),
             "Dates": (f"{min(r.business_dates)} .. {max(r.business_dates)}"
                       if r.business_dates else "—")}
            for s, r in outcome["load_reports"].items()
        ]), hide_index=True, width="stretch")

        for source, report in outcome["load_reports"].items():
            for note in report.notes or []:
                st.caption(f"**{source}** — {note}")

        # Every date the run was ASKED to reconcile that produced zero digital
        # bills — never silent (2026-09-24). Distinct from a date outside the
        # requested range entirely, which Summary now refuses to show as if it
        # were data at all (see dashboard_common / Summary.py's own range check).
        no_data_dates = outcome.get("no_data_dates") or []
        if no_data_dates:
            with st.expander(f"{len(no_data_dates)} date(s) in this run produced zero digital bills"):
                st.caption(
                    "Not necessarily wrong — a store network can genuinely have no "
                    "digital bills some day — but reported so \"nothing there\" is "
                    "never confused with \"not loaded\"."
                )
                st.write(", ".join(no_data_dates))

        # No icon= here (2026-09-22) — the Material Symbols font this app's
        # container has no network access to load, so a `:material/...:` icon
        # renders as its raw ligature name ("arrow_forward") instead of a glyph.
        # The link reads fine as plain text; see theme.css for the two places
        # (the sidebar collapse control, st.expander's triangle) where an icon
        # was load-bearing enough to need a CSS replacement instead.
        st.page_link("Summary.py", label="Open the Summary")

