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

    with pytest.raises(_PastTheLock):
        run_reconciliation(config, session, triggered_by="second_attempt")
    # A new Run row WAS created this time (the lock let it through) — two
    # rows total: the marked-failed blocker, plus this new attempt.
    assert session.query(Run).count() == 2
