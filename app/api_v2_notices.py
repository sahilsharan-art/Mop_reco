"""The loud warnings the Streamlit dashboard showed on every page, for the V2 screens (CLAUDE.md 6.24, 6.28, 6.37, 6.23, 6.9d).

Without these a user sees tidy rupee figures while the POS file is cut off, a month is unreconciled, Axis is parked or a control has broken.
Pure helpers over the engines already in `mop_reco` (no Streamlit): each notice is {key, level (error | warning | info), title, text}.
A notice must never take a page down: every source is wrapped, and a failure to compute one becomes an info notice that says so,
never silence."""
from __future__ import annotations

import json
import logging
import time
from datetime import date

from mop_reco import pos_completeness, queries as q, run_coverage as rc
from mop_reco import store_sheet_source as source

LOG = logging.getLogger(__name__)
_READINESS_TTL = 120.0
_readiness_cache: dict = {}


def _fmt(d) -> str:
    return d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)


def _notes(run) -> dict:
    try:
        return json.loads(run.notes or "{}")
    except (ValueError, TypeError):
        return {}


def run_dates(run) -> tuple:
    dates = sorted(json.loads(run.business_dates or "[]"))
    return (date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])) if dates else (None, None)


def _readiness(config, run_id: int, first: date, last: date):
    """Which months after the run hold which sources - listed from the warehouse and the source folders, cached two minutes (it opens a new file once)."""
    key = (run_id, first, last)
    hit = _readiness_cache.get(key)
    if hit and time.monotonic() - hit[0] < _READINESS_TTL:
        return hit[1]
    value = rc.readiness_for_run(config, first, last)
    _readiness_cache[key] = (time.monotonic(), value)
    return value


def build(session, run, date_from: date, date_to: date, store: str | None, config) -> list:
    out: list = []
    notes = _notes(run)
    first, last = run_dates(run)

    # 1. a POS export that looks cut off (the November incident: 209,757 of 1,187,732 bill numbers missing)
    completeness = notes.get("source_completeness") or {}
    if completeness and completeness.get("ok") is False:
        out.append({"key": "pos_incomplete", "level": "error", "title": "The POS export looks INCOMPLETE",
                    "text": "Treat the affected figures as provisional. " + " ".join(pos_completeness.warning_lines(completeness))})

    # 2. bills parked while a gateway file has not arrived (CLAUDE.md 6.28)
    parked = notes.get("awaiting_source") or {}
    if parked.get("lines"):
        dates = parked.get("dates") or ["", ""]
        out.append({"key": "parked_axis", "level": "warning", "title": "Provisional - Axis is outstanding",
                    "text": f"No Axis file covers {_fmt(dates[0])} to {_fmt(dates[1])}. {parked.get('bills', 0):,} bill(s) worth Rs {float(parked.get('value', 0)):,.2f} "
                            "can only settle through Axis, so they are parked as 'Awaiting Axis - not an exception' and are not in the exceptions, store tasks, "
                            "digital sale or digital receivable until the Axis file arrives and the period is re-run."})

    # 3. a period the run does not cover, and months whose files exist but are not reconciled
    if first and last:
        try:
            statuses = None
            try:
                statuses = _readiness(config, run.id, first, last)
            except Exception:  # noqa: BLE001
                LOG.warning("month readiness check failed", exc_info=True)
            message = rc.period_message(run.id, first, last, date_from, date_to, statuses)
            if message:
                out.append({"key": "period_outside_run", "level": "warning", "title": "Part of this period is not reconciled", "text": message})
            for status in statuses or []:
                if (status.month.year, status.month.month) == (last.year, last.month):
                    continue
                level, text = rc.month_message(status)
                out.append({"key": f"month_{status.month:%Y_%m}", "level": "info" if level == "success" else "warning",
                            "title": f"{status.month:%B %Y}", "text": text})
            try:
                unread = rc.unread_inside_run(config, rc.unread_source_files(session, config, run.id), first, last)
                note = rc.source_message(run.id, unread)
                if note:
                    out.append({"key": "unread_files", "level": "info", "title": "Source files the run has not read", "text": note})
            except Exception:  # noqa: BLE001
                LOG.warning("source coverage check failed", exc_info=True)
        except Exception:  # noqa: BLE001
            LOG.warning("coverage notices failed", exc_info=True)
            out.append({"key": "coverage_unavailable", "level": "info", "title": "Coverage not checked",
                        "text": "The check of which months are reconciled could not be completed; do not assume the whole period is covered."})

    # 4. the store-sheet controls the run stored (a-h, the estate identity, date = sum of stores): loud only when one fails
    try:
        static = source.load_static(run)
        for key, what, holds, detail in (static or {}).get("controls", []):
            if not holds:
                out.append({"key": f"control_{key}", "level": "error", "title": f"Control {key} FAILED", "text": f"{what}. {detail}"})
    except Exception:  # noqa: BLE001
        LOG.warning("control read failed", exc_info=True)
        out.append({"key": "controls_unavailable", "level": "info", "title": "Controls not read", "text": "The run's stored controls could not be read."})

    # 5. the two-sided bills / transactions controls, each computed from two independent sources
    try:
        ov = q.match_overview(session, run.id, date_from, date_to, store or None)
        if not ov.bills_ok:
            out.append({"key": "bills_mismatch", "level": "error", "title": "MISMATCH (bills)",
                        "text": f"open + engine-closed + manual-closed differs from the total by {ov.bills_difference:,} bill line(s)."})
        if not ov.bills_agree_with_exceptions:
            out.append({"key": "bills_vs_exceptions", "level": "error", "title": "MISMATCH (bills against exceptions)",
                        "text": f"{ov.bills['open']['count']:,} open bill lines but {ov.exception_bills_n:,} exception bills (+{ov.merged_bill_lines:,} folded lines)."})
        if not ov.txns_ok:
            out.append({"key": "txns_mismatch", "level": "error", "title": "MISMATCH (transactions)",
                        "text": f"auto + manual + refunded + unmapped differs from the total by {ov.txns_difference:,} transaction(s)."})
        if not ov.unmapped_agrees_with_exceptions:
            out.append({"key": "txns_vs_exceptions", "level": "error", "title": "MISMATCH (transactions against exceptions)",
                        "text": f"{ov.txns.unmapped_n:,} unmapped transactions but {ov.exception_txns_n:,} exception transactions (+{ov.merged_txn_rows:,} folded rows)."})
    except Exception:  # noqa: BLE001
        LOG.warning("match overview failed", exc_info=True)
        out.append({"key": "match_overview_unavailable", "level": "info", "title": "Bills / transactions controls not checked",
                    "text": "The two-sided bills and transactions controls could not be computed for this selection."})
    return out
