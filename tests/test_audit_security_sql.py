"""Audit Phase 3/5: the string-built SQL flagged by bandit (upload_prefetch._in) cannot be injected into (candidate C-005).

The placeholders it emits are always ':<prefix><n>' and every value travels as a bound parameter. This proves it with hostile input
against a real (throw-away) database: nothing is dropped, nothing is returned for the hostile value, and the table is intact afterwards.
"""
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import upload_prefetch as up  # noqa: E402
from mop_reco.db import R1BillLine, Run, get_engine, get_session, init_db  # noqa: E402

HOSTILE = ["'; DROP TABLE r1_bill_lines; --", "x' OR '1'='1", "B1\"); DELETE FROM runs; --", "%", "_", "\\", "Robert'); DROP TABLE exceptions;--", "\x00", "é☃", ""]


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "sec.sqlite")
    init_db(engine)
    s = get_session(engine)
    from datetime import datetime
    run = Run(started_at=datetime.now(), business_dates='["2026-07-01"]', status="completed")
    s.add(run)
    s.flush()
    s.add(R1BillLine(run_id=run.id, business_date="2026-07-01", store="AAA", bill_no="B1", line_seq=1, mop="PAYTM EDC", mop_class="digital",
                     amount=100, matched=False, status="open"))
    s.commit()
    s.run_id = run.id
    yield s
    s.close()


def test_in_only_ever_emits_bound_placeholders():
    for values in (HOSTILE, ["a"], [], list("abcdefghij" * 30)):
        marks, params = up._in("b", values)
        assert re.fullmatch(r"(:b\d+(, :b\d+)*)?", marks), marks
        assert list(params.values()) == values, "values travel as parameters, unchanged"
        assert all(re.fullmatch(r"b\d+", k) for k in params)


def test_hostile_bill_numbers_and_references_change_nothing(session):
    with up.prefetch(session, session.run_id, bill_nos=HOSTILE + ["B1"], refs=HOSTILE) as pf:
        assert set(pf.bills) == {"B1"}, "only the real bill is found; the hostile strings match nothing"
        assert pf.engine_owner == {}
    # the schema and the data are intact
    assert session.execute(text("select count(*) from r1_bill_lines")).scalar() == 1
    assert session.execute(text("select count(*) from runs")).scalar() == 1
    assert session.execute(text("select count(*) from exceptions")).scalar() == 0


def test_like_wildcards_are_not_treated_as_wildcards(session):
    with up.prefetch(session, session.run_id, bill_nos=["%", "B_", "_1"]) as pf:
        assert pf.bills == {}, "'%' and '_' are literal characters in an IN list, never patterns"
