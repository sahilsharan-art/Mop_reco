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


def period_message(run_id: int, first: date, last: date, date_from: date, date_to: date, statuses=None):
    """The one sentence, or None when the whole period is inside the run. It never invites a run: whether a month CAN
    be run is `month_message`'s job. With `statuses` (month_statuses), months that hold some source data are left to
    their own message and only the months nothing has arrived for are named here."""
    months = uncovered_months(first, last, date_from, date_to)
    if statuses is not None:
        have = {s.month for s in statuses}
        months = [m for m in months if m not in have]
    if not months:
        return None
    several_years = len({m.year for m in months}) > 1
    names = [m.strftime("%B %Y") if several_years else calendar.month_name[m.month] for m in months]
    verb = "is" if len(names) == 1 else "are"
    return (f"Run #{run_id} covers {first:%d/%m} to {last:%d/%m}"
            f"{'' if first.year == last.year else f' ({first:%Y} - {last:%Y})'}. "
            f"{_join(names)} {verb} not reconciled"
            + (" - no source files for it have arrived." if statuses is not None and len(names) == 1
               else " - no source files for them have arrived." if statuses is not None else "."))


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


def unread_inside_run(config, unread: list, first: date, last: date) -> list:
    """Of the unread files, those holding any date INSIDE the run's range - a re-dump or a correction. A file whose
    every date lies after the run belongs to a month `month_message` already describes, so it is not repeated."""
    out = []
    for source, f in unread:
        try:
            days = probe_dates(config, source, f)
        except Exception:  # noqa: BLE001 - unknown dates: say so rather than hide the file
            out.append((source, f))
            continue
        if not days or any(first <= d <= last for d in days):
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
            "They cover dates the run already reconciled, so the figures may have moved - go to Upload and run it.")
import logging  # noqa: E402


# ---------------------------------------------------------------------------
# Month readiness (2026-10-19): never invite a run of a month that cannot reconcile.
#
# Incident: October's Paytm and Bajaj files arrived first. The banner said "go to Upload and run it" regardless,
# but with no October POS file every gateway transaction would have had no bill to match and become a false
# unmapped exception. A month is READY only when every source a reconciliation needs holds data for it; the dates are
# read from the data, never from a folder or file name (CLAUDE.md 4 and 6.10).
# ---------------------------------------------------------------------------

import json  # noqa: E402
import zipfile  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402

# A month cannot reconcile without ALL of these. Bajaj is a gateway that exists only for some months (April and May
# genuinely have no Bajaj file), so its absence is reported but never blocks. Opening cash is needed once, at the start.
REQUIRED_SOURCES = ("pos_bill_wise", "paytm_settlement", "axis_edc", "axis_cms", "ptc")
OPTIONAL_SOURCES = ("bajaj",)
SOURCE_LABEL.update({"axis_cms": "CMS", "ptc": "petty cash"})
CHECK_ORDER = ("pos_bill_wise", "paytm_settlement", "axis_edc", "bajaj", "axis_cms", "ptc")

# What goes wrong if a month is run with a source missing - said in the message, so it is a reason, not a rule.
CONSEQUENCE = {
    "pos_bill_wise": "every gateway transaction would have no bill to match and become a false unmapped exception",
    "paytm_settlement": "every Paytm bill would have no transaction and become a false open bill",
    "axis_edc": "every card bill would have no transaction and become a false open bill",
    "axis_cms": "the cash credited by the agency would be missing from the cash receivable",
    "ptc": "deposits and petty cash would be missing, so every store's cash balance would be wrong",
}


@dataclass
class MonthStatus:
    month: date
    present: dict = field(default_factory=dict)      # source -> {"first", "last", "n", "gaps": [(a, b)]}
    missing: list = field(default_factory=list)      # REQUIRED sources with no data in the month
    optional_missing: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.missing and bool(self.present)

    @property
    def common_span(self):
        """The dates every required source covers (latest first-date .. earliest last-date), else None."""
        if not self.ready:
            return None
        a = max(self.present[s]["first"] for s in REQUIRED_SOURCES)
        b = min(self.present[s]["last"] for s in REQUIRED_SOURCES)
        return (a, b) if a <= b else None


def _gaps(days: set) -> list:
    """Runs of calendar days missing between the first and last date held."""
    if not days:
        return []
    ordered = sorted(days)
    out, cur, end = [], ordered[0], ordered[-1]
    have, run_start = set(ordered), None
    while cur <= end:
        if cur not in have:
            run_start = run_start or cur
        elif run_start is not None:
            out.append((run_start, date.fromordinal(cur.toordinal() - 1)))
            run_start = None
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def month_statuses(dates_by_source: dict, first: date, last: date) -> list:
    """One MonthStatus per calendar month that holds data in ANY source and lies outside the run's own range
    [first, last] - the months not reconciled. Pure: `dates_by_source` is {source: set(date)}."""
    outside = {}
    for source, days in dates_by_source.items():
        for d in days:
            if d > last:        # months AFTER the run; stray earlier rows inside a neighbouring file are not a month
                outside.setdefault(date(d.year, d.month, 1), {}).setdefault(source, set()).add(d)
    result = []
    for month in sorted(outside):
        st = MonthStatus(month)
        for source in CHECK_ORDER:
            if outside[month].get(source):
                full = {d for d in dates_by_source.get(source, set()) if d.year == month.year and d.month == month.month}
                st.present[source] = {"first": min(full), "last": max(full), "n": len(full), "gaps": _gaps(full)}
            elif source in REQUIRED_SOURCES:
                st.missing.append(source)
            else:
                st.optional_missing.append(source)
        result.append(st)
    return result


def _span(d1: date, d2: date) -> str:
    return f"{d1:%d/%m}" if d1 == d2 else f"{d1:%d/%m}-{d2:%d/%m}"


def _labels(sources) -> str:
    return _join([SOURCE_LABEL[s] for s in sources])


def month_message(st: MonthStatus):
    """(level, text) for one month: "success" when ready to run, "warning" when incomplete."""
    name = calendar.month_name[st.month.month]
    spans = []
    for s in CHECK_ORDER:
        if s in st.present:
            p = st.present[s]
            gap = f", no data {', '.join(_span(a, b) for a, b in p['gaps'])}" if p["gaps"] else ""
            spans.append(f"{SOURCE_LABEL[s]} {_span(p['first'], p['last'])}{gap}")
    if st.ready:
        c = st.common_span
        tail = f" for {_span(*c)}" if c else ""
        extra = f" Bajaj is not here, so {name}'s Bajaj bills would be open items." if st.optional_missing else ""
        return ("success", f"{name} is ready to run{tail}: " + "; ".join(spans) + "." + extra + " Go to Upload and run it.")
    here = [s for s in CHECK_ORDER if s in st.present]
    if st.missing == ["axis_edc"]:
        others = [s for s in REQUIRED_SOURCES if s != "axis_edc"]
        a = max(st.present[s]["first"] for s in others)
        b = min(st.present[s]["last"] for s in others)
        return ("warning",
                f"{name} can be run PROVISIONALLY for {_span(a, b)} (Axis is outstanding): " + "; ".join(spans) +
                ". Bills that can only settle through Axis are parked as 'Awaiting Axis - not an exception' - not "
                "opened, not counted in receivable or store tasks - and the period is shown as provisional. Choose "
                "the end date no later than the last day every other source covers; do not run past it.")
    why = "; ".join(dict.fromkeys(CONSEQUENCE[s] for s in st.missing))
    return ("warning",
            f"{name} is incomplete: {_labels(here)} {'is' if len(here) == 1 else 'are'} here "
            f"({'; '.join(spans)}); {_labels(st.missing)} {'is' if len(st.missing) == 1 else 'are'} not. "
            f"Running now would create false exceptions - {why}. Do not run {name} until they arrive.")


# --- where each source's dates come from --------------------------------------------------------------------------

def _warehouse_root() -> Path:
    return Path(__file__).resolve().parents[2] / "warehouse"


def warehouse_dates(source: str, root: Path = None) -> tuple:
    """(set of business dates already ingested for this source, newest partition mtime) from the per-date parquet
    partitions a run writes. These are the dates as the reconciliation read them."""
    folder = (root or _warehouse_root()) / source
    days, newest = set(), 0.0
    if folder.exists():
        for p in folder.glob("*.parquet"):
            try:
                days.add(date.fromisoformat(p.stem))
            except ValueError:
                continue
            newest = max(newest, p.stat().st_mtime)
    return days, newest


def _probe_cache_path(root: Path = None) -> Path:
    return (root or _warehouse_root()) / "_coverage_probe.json"


def _read_probe_cache(root: Path = None) -> dict:
    try:
        return json.loads(_probe_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pos_dates(path: Path) -> set:
    import fastexcel
    wb = fastexcel.read_excel(str(path))
    days = set()
    for name in wb.sheet_names:
        col = wb.load_sheet(name, use_columns=["BILLDATE"]).to_polars()["BILLDATE"].drop_nulls()
        days |= set(col.dt.date().unique().to_list())
    return days


def _paytm_dates(path: Path) -> set:
    import pandas as pd
    days = set()
    with zipfile.ZipFile(path) as z:
        for name in (n for n in z.namelist() if n.lower().endswith(".csv")):
            with z.open(name) as f:
                header = pd.read_csv(f, nrows=0).columns.tolist()
            if "transaction_date" not in header:
                continue
            with z.open(name) as f:
                s = pd.read_csv(f, usecols=[header.index("transaction_date")], index_col=False,
                                dtype=str)["transaction_date"]
            d = pd.to_datetime(s.str.strip("'"), dayfirst=True, errors="coerce").dropna()
            days |= set(d.dt.date.unique())
    return days


def probe_dates(config, source: str, path: Path) -> set:
    """The business dates INSIDE one file that has not been ingested yet, read with the project's own readers (a column
    or a small file, never a reconciliation). Cached by path + mtime + size so a file is opened once."""
    from . import ingest

    stat = path.stat()
    key = f"{path}|{stat.st_mtime_ns}|{stat.st_size}"
    cache = _read_probe_cache()
    if key in cache:
        return {date.fromisoformat(d) for d in cache[key]}
    mtime = datetime.fromtimestamp(stat.st_mtime)
    if source == "pos_bill_wise":
        days = _pos_dates(path)
    elif source == "paytm_settlement":
        days = _paytm_dates(path)
    elif source == "axis_edc":
        days = set(ingest._read_axis_edc_file(path, mtime)["business_date"].dropna())
    elif source == "bajaj":
        days = set(ingest._read_bajaj_file(path, mtime)["business_date"].dropna())
    elif source == "axis_cms":
        days = set(ingest._read_axis_cms_file(config, path, mtime)["business_date"].dropna())
    elif source == "ptc":
        days = set(ingest._read_ptc_file(path, config.encoding_for("ptc") or "cp1252")["business_date"].dropna())
    else:
        days = set()
    days = {d.date() if isinstance(d, datetime) else d for d in days}
    cache[key] = sorted(d.isoformat() for d in days)
    try:
        _probe_cache_path().write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return days


# The sources read from files that someone uploads. POS and petty cash are not here: the ERP supplies them and their dates are the
# warehouse's own.
UPLOAD_ONLY_DATE_SOURCES = ("paytm_settlement", "axis_edc", "bajaj", "axis_cms")


def source_dates(config, source: str, root: Path = None) -> set:
    """Every business date this source holds: the dates already ingested (warehouse) plus the dates inside any file
    newer than the last ingest - the files nobody has run yet. A file that cannot be read is skipped, never fatal."""
    days, newest = warehouse_dates(source, root)
    uploads_only = getattr(config, "uploads_only", False) and source in UPLOAD_ONLY_DATE_SOURCES
    if uploads_only:
        # The numbered folders are no longer the way in: a date counts only if an UPLOADED file holds it. Partitions left in the
        # warehouse by the folder-based runs are history (and the comparison baseline), not evidence that anything was supplied.
        days, newest = set(), 0.0
    try:
        files = config.source_status(source).files
    except Exception:  # noqa: BLE001 - a source that is not configured holds nothing
        return days
    for f in files:
        try:
            if f.stat().st_mtime > newest + 2:
                days |= probe_dates(config, source, f)
        except Exception:  # noqa: BLE001
            logging.getLogger("mop_reco").warning("coverage probe of %s failed", f, exc_info=True)
    return days


def all_source_dates(config) -> dict:
    return {s: source_dates(config, s) for s in CHECK_ORDER}


def readiness_for_run(config, first: date, last: date) -> list:
    """The month statuses a page shows: every month outside the latest run's range that any source holds data for."""
    return month_statuses(all_source_dates(config), first, last)


def range_problems(dates_by_source: dict, date_from: date, date_to: date) -> list:
    """For the Run page: months of [date_from, date_to] in which a REQUIRED source holds NO data at all - running that
    range would reconcile the month against nothing. [(month, [missing sources], [sources that are present])]"""
    out = []
    for month in _months_between(date_from, date_to):
        lo = max(date_from, month)
        hi = min(date_to, date(month.year, month.month, calendar.monthrange(month.year, month.month)[1]))
        held = lambda s: any(lo <= d <= hi for d in dates_by_source.get(s, set()))  # noqa: E731
        missing = [s for s in REQUIRED_SOURCES if not held(s)]
        # Axis alone missing is handled by parking (CLAUDE.md 6.28), not a reason to refuse the range.
        if missing == ["axis_edc"]:
            continue
        if missing:
            out.append((month, missing, [s for s in CHECK_ORDER if held(s)]))
    return out


def range_warning(month: date, missing: list, present: list) -> str:
    name = f"{calendar.month_name[month.month]} {month.year}"
    why = "; ".join(dict.fromkeys(CONSEQUENCE[s] for s in missing))
    have = f"{_labels(present)} {'is' if len(present) == 1 else 'are'} here" if present else "nothing is here"
    return f"{name} has no {_labels(missing)} data ({have}). Running it would create false exceptions - {why}."


def axis_parking_note(dates_by_source: dict, date_from: date, date_to: date):
    """For the Run page: the days of the range that no Axis file covers (after Axis's last date), where Axis-only
    bills will be parked rather than opened. None when Axis covers the whole range."""
    axis = dates_by_source.get("axis_edc", set())
    if not axis:
        return None
    last = max(axis)
    if date_to <= last:
        return None
    first_uncovered = max(date_from, date.fromordinal(last.toordinal() + 1))
    return (f"No Axis file after {last:%d/%m/%Y}. Bills dated {first_uncovered:%d/%m/%Y} to {date_to:%d/%m/%Y} that can "
            "only settle through Axis will be parked as 'Awaiting Axis - not an exception' and the period shown as "
            "provisional.")
