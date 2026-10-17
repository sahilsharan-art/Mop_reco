"""What a run covers, and what is NOT in it - so "nothing is there" is never mistaken for "zero" (2026-10-17).

Two incidents had the same shape: September's files were in the folders, nobody had run September, and every page
showed an empty grid or zeros - which read as a real answer ("no September sales") rather than "not reconciled yet".
These are the two plain statements every page now makes:

  * the period on screen reaches outside the run's range  -> "Run #11 covers 01/04 to 31/08. September is not
    reconciled - go to Upload and run it."
  * source files sit in the folders that the run never read -> "Run #11 has not read 6 source file(s) ... "

Pure functions of dates, a session and the config (CLAUDE.md section 8). No Streamlit.
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime

from .db import RunFile

# The sources a run records file-by-file (`runs._record_files`): POS, Paytm, Axis EDC and Bajaj. PTC, CMS and the
# masters are read at their own stages and are not listed per run, so a "not read" statement cannot be made for them.
RECORDED_SOURCES = ("pos_bill_wise", "paytm_settlement", "axis_edc", "bajaj")
SOURCE_LABEL = {"pos_bill_wise": "POS", "paytm_settlement": "Paytm", "axis_edc": "Axis", "bajaj": "Bajaj"}


def _months_between(start: date, end: date) -> list:
    """First-of-month dates for every calendar month touched by [start, end]."""
    if start > end:
        return []
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def uncovered_months(first: date, last: date, date_from: date, date_to: date) -> list:
    """Months of the filtered period that fall outside the run's own range [first, last]."""
    months = []
    if date_to > last:
        months += _months_between(max(date_from, date.fromordinal(last.toordinal() + 1)), date_to)
    if date_from < first:
        months += _months_between(date_from, min(date_to, date.fromordinal(first.toordinal() - 1)))
    seen, out = set(), []
    for m in months:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return sorted(out)


def entirely_outside(first: date, last: date, date_from: date, date_to: date) -> bool:
    """True when none of the filtered period lies inside the run - there is nothing to show."""
    return date_from > last or date_to < first


def _join(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def period_message(run_id: int, first: date, last: date, date_from: date, date_to: date):
    """The one sentence, or None when the whole period is inside the run."""
    months = uncovered_months(first, last, date_from, date_to)
    if not months:
        return None
    several_years = len({m.year for m in months}) > 1
    names = [m.strftime("%B %Y") if several_years else calendar.month_name[m.month] for m in months]
    verb = "is" if len(names) == 1 else "are"
    return (f"Run #{run_id} covers {first:%d/%m} to {last:%d/%m}"
            f"{'' if first.year == last.year else f' ({first:%Y} - {last:%Y})'}. "
            f"{_join(names)} {verb} not reconciled - go to Upload and run it.")


def unread_source_files(session, config, run_id: int) -> list:
    """[(source, Path)] of files now in the source folders that this run never read (by path and modified time).
    Only the four sources a run records per file are checked."""
    known = {}
    for r in session.query(RunFile.file_path, RunFile.file_mtime).filter(RunFile.run_id == run_id).all():
        known.setdefault(str(r.file_path), []).append(r.file_mtime)
    out = []
    for source in RECORDED_SOURCES:
        try:
            status = config.source_status(source)
        except Exception:  # noqa: BLE001 - a source that is not configured has nothing to check
            continue
        for f in status.files:
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            hit = any(m is not None and abs((m - mtime).total_seconds()) < 2 for m in known.get(str(f), []))
            if not hit:
                out.append((source, f))
    return out


def source_message(run_id: int, unread: list):
    """"Run #11 has not read 6 source file(s) that are now in the folders - POS: 2026-09.xlsb; Paytm: ...", or None."""
    if not unread:
        return None
    by_source = {}
    for source, f in unread:
        by_source.setdefault(source, []).append(f.name)
    parts = "; ".join(f"{SOURCE_LABEL[s]}: {', '.join(names)}" for s, names in by_source.items())
    return (f"Run #{run_id} has not read {len(unread)} source file(s) that are now in the folders - {parts}. "
            "Whatever they hold is not reconciled yet - go to Upload and run it.")
