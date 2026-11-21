"""The single-run lock — CLAUDE.md 6.14. Found 2026-09-24 the hard way: a
script-triggered run, an operator's own click on the Run page, and a third
attempt all ended up running concurrently, each holding a five-month
dataset in memory, and the machine ran out of RAM (one run was OS-OOM-killed
outright; another failed to allocate 1.72 MiB). Only one reconciliation may
be in progress at a time, system-wide, checked inside `run_reconciliation()`
itself so no caller — a script or the Run page — can bypass it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco.config import load_config
from mop_reco.db import Run, get_engine, get_session, init_db
from mop_reco.runs import STALE_RUN_SECONDS, RunAlreadyInProgress, run_reconciliation


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "test_reco.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def config():
    return load_config()


def _insert_running(session, heartbeat_age_seconds) -> Run:
    now = datetime.now()
    run = Run(
        started_at=now - timedelta(seconds=heartbeat_age_seconds + 60),
        heartbeat_at=now - timedelta(seconds=heartbeat_age_seconds),
        business_dates="[null, null]",
        status="running",
        current_stage="r1_match",
    )
    session.add(run)
    session.commit()
    return run


def test_a_fresh_running_run_refuses_a_new_one(config, session):
    blocking = _insert_running(session, heartbeat_age_seconds=5)

    with pytest.raises(RunAlreadyInProgress) as exc_info:
        run_reconciliation(config, session, triggered_by="second_attempt")

    assert exc_info.value.run_id == blocking.id
    assert exc_info.value.stalled is False
    # Refused BEFORE any new Run row is created — the history shows only
    # the one blocking run, not a second failed attempt.
    assert session.query(Run).count() == 1


def test_a_stale_running_run_still_refuses_but_is_flagged_stalled(config, session):
    blocking = _insert_running(session, heartbeat_age_seconds=STALE_RUN_SECONDS + 30)

    with pytest.raises(RunAlreadyInProgress) as exc_info:
        run_reconciliation(config, session, triggered_by="second_attempt")

    assert exc_info.value.run_id == blocking.id
    assert exc_info.value.stalled is True, (
        "a stale heartbeat must still BLOCK — it is surfaced as stalled, "
        "never silently bypassed; only an operator marking it failed clears it")
    assert session.query(Run).count() == 1


def test_a_run_with_no_heartbeat_at_all_is_treated_as_stalled(config, session):
    """A `running` row that somehow never got a first heartbeat (e.g. a
    crash between the Run insert and the first `_stage()` call) must not
    be trusted as fresh just because `heartbeat_at` is NULL."""
    blocking = _insert_running(session, heartbeat_age_seconds=0)
    blocking.heartbeat_at = None
    session.commit()

    with pytest.raises(RunAlreadyInProgress) as exc_info:
        run_reconciliation(config, session, triggered_by="second_attempt")

    assert exc_info.value.stalled is True
    assert exc_info.value.heartbeat_age_seconds is None


def test_no_blocking_run_once_the_stale_one_is_marked_failed(config, session, monkeypatch):
    """The confirm step: once the operator marks a stalled run failed, the
    NEXT attempt's lock check must pass. Ingestion itself is stubbed to
    raise immediately — this test only needs to prove the LOCK clears, not
    exercise real file I/O against whatever happens to be configured."""
    blocking = _insert_running(session, heartbeat_age_seconds=STALE_RUN_SECONDS + 30)
    blocking.status = "failed"
    session.commit()

    class _PastTheLock(Exception):
        pass

    import mop_reco.runs as runs_module

    def _stub(*a, **k):
        raise _PastTheLock("lock check passed; ingestion stubbed out")

    monkeypatch.setattr(runs_module.ingest_mod, "ingest_pos_bill_wise", _stub)
    # The masters pre-flight (tests/test_masters_preflight.py) is the first stage after the
    # lock; this test is about the lock only, so it must not depend on whatever master
    # files happen to be on disk today.
    monkeypatch.setattr(runs_module.masters_mod, "preflight_masters",
                        lambda config, session=None, raise_on_failure=True, use_stored=None: [])

    with pytest.raises(_PastTheLock):
        run_reconciliation(config, session, triggered_by="second_attempt")
    # A new Run row WAS created this time (the lock let it through) — two
    # rows total: the marked-failed blocker, plus this new attempt.
    assert session.query(Run).count() == 2


# --- a failing stage must neither hide its own error nor leave the run 'running' (run #28, 2026-11-20) ---------------

def test_stage_failure_with_a_broken_session_still_reports_the_original_error(tmp_path):
    from datetime import datetime
    from mop_reco import runs
    from mop_reco.db import Run, get_engine, get_session, init_db
    engine = init_db(get_engine(tmp_path / "t.sqlite"))
    session = get_session(engine)
    run = Run(started_at=datetime.now(), business_dates="[]", status="running")
    session.add(run); session.commit()

    class Boom(Exception):
        pass

    timings = {}
    with pytest.raises(Boom):
        with runs._stage(session, run, timings, "manual_overlay"):
            # leave the Session unusable exactly as a mid-query MemoryError did
            session.commit()
            session.get_transaction().close() if session.get_transaction() else None
            raise Boom("the real problem")
    assert "manual_overlay" in timings


def test_manual_overlay_reads_only_the_bills_it_needs(tmp_path):
    """reconcile_after_run used to load every bill line of the run. It must query only the matched bills."""
    from datetime import datetime
    from mop_reco import manual_match as mm
    from mop_reco.db import ManualMatch, R1BillLine, Run, get_engine, get_session, init_db
    engine = init_db(get_engine(tmp_path / "t.sqlite"))
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime.now(), business_dates="[]", status="completed")); s.commit()
    src = open(mm.__file__, encoding="utf-8").read()
    body = src[src.index("def reconcile_after_run"):src.index("def _transition")]
    assert "R1BillLine.bill_no.in_(" in body, "reconcile_after_run must filter bill lines by the matched bill numbers"
    assert ".filter(R1BillLine.run_id == run_id).all()" not in body.replace("\n", "").replace("                 ", "")
