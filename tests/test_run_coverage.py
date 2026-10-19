"""A period outside the run, or source files the run never read, is SAID - never an empty grid or zeros."""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import run_coverage as rc  # noqa: E402
from mop_reco.db import Run, RunFile, get_engine, get_session, init_db  # noqa: E402

FIRST, LAST = date(2026, 4, 1), date(2026, 8, 31)


def test_a_period_inside_the_run_says_nothing():
    assert rc.period_message(11, FIRST, LAST, date(2026, 4, 1), date(2026, 8, 31)) is None
    assert rc.period_message(11, FIRST, LAST, date(2026, 7, 1), date(2026, 7, 31)) is None


def test_september_outside_a_run_ending_in_august_is_named():
    msg = rc.period_message(11, FIRST, LAST, date(2026, 9, 1), date(2026, 9, 30))
    assert msg == "Run #11 covers 01/04 to 31/08. September is not reconciled."
    assert rc.entirely_outside(FIRST, LAST, date(2026, 9, 1), date(2026, 9, 30))


def test_a_period_that_only_partly_overlaps_names_just_the_missing_months():
    msg = rc.period_message(11, FIRST, LAST, date(2026, 4, 1), date(2026, 10, 15))
    assert "September and October are not reconciled" in msg
    assert not rc.entirely_outside(FIRST, LAST, date(2026, 4, 1), date(2026, 10, 15))


def test_before_the_run_is_named_too():
    msg = rc.period_message(11, FIRST, LAST, date(2026, 3, 10), date(2026, 5, 1))
    assert "March is not reconciled" in msg


def test_unread_source_files_are_found_by_path_and_modified_time(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    read = tmp_path / "aug.xlsb"
    new = tmp_path / "sep.xlsb"
    read.write_bytes(b"x")
    new.write_bytes(b"y")
    s.add(RunFile(run_id=1, source="pos_bill_wise", file_path=str(read),
                  file_mtime=datetime.fromtimestamp(read.stat().st_mtime), row_count=1))
    s.commit()

    class Status:
        files = (read, new)

    class Config:
        def source_status(self, name):
            return Status() if name == "pos_bill_wise" else type("E", (), {"files": ()})()

    unread = rc.unread_source_files(s, Config(), 1)
    assert [(src, f.name) for src, f in unread] == [("pos_bill_wise", "sep.xlsb")]
    assert "has not read 1 source file(s)" in rc.source_message(1, unread) and "may have moved" in rc.source_message(1, unread)
    assert rc.source_message(1, []) is None


# --- month readiness (2026-10-19) ---------------------------------------------------------------------------------

def _days(a, b):
    from datetime import timedelta
    return {a + timedelta(days=i) for i in range((b - a).days + 1)}


def _oct(**overrides):
    base = {s: set() for s in rc.CHECK_ORDER}
    base["pos_bill_wise"] = _days(date(2026, 4, 1), date(2026, 9, 30))
    base.update(overrides)
    return base


def test_october_with_only_gateways_cms_and_petty_cash_is_incomplete_and_never_invites_a_run():
    dates = _oct(paytm_settlement=_days(date(2026, 10, 10), date(2026, 10, 17)) | {date(2026, 10, 1)},
                 bajaj=_days(date(2026, 10, 12), date(2026, 10, 18)),
                 ptc=_days(date(2026, 10, 1), date(2026, 10, 18)),
                 axis_cms=_days(date(2026, 10, 1), date(2026, 10, 17)))
    (st,) = rc.month_statuses(dates, date(2026, 4, 1), date(2026, 9, 30))
    assert not st.ready and st.missing == ["pos_bill_wise", "axis_edc"] and st.month == date(2026, 10, 1)
    level, text = rc.month_message(st)
    assert level == "warning" and text.startswith("October is incomplete: Paytm, Bajaj, CMS and petty cash are here")
    assert "POS and Axis are not" in text and "false exceptions" in text and "go to Upload" not in text
    assert "no data 02/10-09/10" in text            # the Paytm gap inside October is named too


def test_a_month_with_every_required_source_is_ready_and_names_the_common_dates():
    dates = {s: _days(date(2026, 10, 1), date(2026, 10, 18)) for s in rc.CHECK_ORDER}
    dates["paytm_settlement"] = _days(date(2026, 10, 1), date(2026, 10, 17))
    dates["bajaj"] = set()                           # Bajaj is optional: it never blocks
    (st,) = rc.month_statuses(dates, date(2026, 4, 1), date(2026, 9, 30))
    assert st.ready and st.common_span == (date(2026, 10, 1), date(2026, 10, 17))
    level, text = rc.month_message(st)
    assert level == "success" and text.startswith("October is ready to run for 01/10-17/10") and "Upload" in text
    assert "Bajaj is not here" in text


def test_only_months_after_the_run_are_reported_and_stray_earlier_rows_are_ignored():
    dates = _oct(paytm_settlement={date(2026, 3, 6), date(2026, 9, 15)}, ptc={date(2026, 8, 1)})
    assert rc.month_statuses(dates, date(2026, 4, 1), date(2026, 9, 30)) == []


def test_a_month_nothing_has_arrived_for_is_said_without_inviting_a_run():
    msg = rc.period_message(15, date(2026, 4, 1), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 5), statuses=[])
    assert msg == ("Run #15 covers 01/04 to 30/09. October is not reconciled - no source files for it have arrived.")


def test_the_run_page_guard_names_the_months_that_lack_a_required_source():
    dates = _oct(paytm_settlement=_days(date(2026, 10, 10), date(2026, 10, 17)))
    problems = rc.range_problems(dates, date(2026, 9, 1), date(2026, 10, 18))
    months = {m: (miss, present) for m, miss, present in problems}
    assert date(2026, 9, 1) not in months or "pos_bill_wise" not in months[date(2026, 9, 1)][0]
    miss, present = months[date(2026, 10, 1)]
    assert "pos_bill_wise" in miss and "axis_edc" in miss and present == ["paytm_settlement"]
    warning = rc.range_warning(date(2026, 10, 1), ["pos_bill_wise", "axis_edc"], present)
    assert warning.startswith("October 2026 has no POS and Axis data (Paytm is here)") and "false exceptions" in warning
    assert rc.range_problems(_oct(**{s: _days(date(2026, 4, 1), date(2026, 9, 30)) for s in rc.CHECK_ORDER}),
                             date(2026, 4, 1), date(2026, 9, 30)) == []
