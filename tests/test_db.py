"""The engine/session lifecycle — a live regression guard for the class of
bug that put the dashboard down: a Session cached across Streamlit reruns
(via st.cache_resource) is bound to whichever thread first created it, and
SQLite refuses to let a different thread touch that same connection
(`sqlite3.InterfaceError: bad parameter or other API misuse`). A test that
only ever touches one thread cannot catch this — every test here that
matters runs the query from more than one real OS thread.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from datetime import datetime

from mop_reco.db import Run, get_engine, get_session, init_db  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    return init_db(get_engine(tmp_path / "concurrency_test.sqlite"))


def test_engine_is_usable_from_two_threads_concurrently(engine):
    """The exact shape of the reported bug: two threads, one shared engine,
    each opening its OWN session and running a real query. Before the fix
    (a Session cached and reused across threads), this either raised
    InterfaceError or corrupted results; after it (check_same_thread=False +
    NullPool, a fresh Session per thread), both threads succeed cleanly."""
    errors = []
    results = []

    def worker():
        try:
            session = get_session(engine)
            try:
                run = session.query(Run).order_by(Run.started_at.desc()).first()
                results.append(run)
            finally:
                session.close()
        except Exception as e:  # noqa: BLE001 — capture anything, this is the assertion
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"query raised in a worker thread: {errors}"
    assert len(results) == 8
    assert all(r is None for r in results)  # empty runs table — see the next test


def test_latest_run_handles_an_empty_runs_table(engine):
    """No run yet must return None, quietly — never raise. Every page that
    needs a run checks this and calls no_run_yet(); Run and Masters call
    sidebar() with a possibly-None run on purpose, since Run is where the
    first one gets created."""
    from dashboard_common import latest_run

    session = get_session(engine)
    try:
        assert latest_run(session) is None
    finally:
        session.close()


def test_latest_run_only_returns_completed_runs(engine):
    """Found 2026-09-26: a run that is running, stalled or failed used to
    hide a finished run's data outright — a completed 5-month run (#11)
    went dark the moment a NEWER run (#12) got stuck in ingest_paytm for
    22+ hours, because latest_run() picked the newest row by started_at
    regardless of status. A run in progress (or one that failed) must
    never blank a page that has real, completed data to show."""
    from dashboard_common import latest_run

    session = get_session(engine)
    try:
        completed = Run(started_at=datetime(2026, 9, 24, 18, 0),
                        finished_at=datetime(2026, 9, 25, 10, 0),
                        business_dates="[]", status="completed")
        session.add(completed)
        session.commit()

        stuck = Run(started_at=datetime(2026, 9, 25, 11, 0),
                    business_dates="[]", status="running")
        session.add(stuck)
        session.commit()

        found = latest_run(session)
        assert found is not None
        assert found.id == completed.id, (
            "latest_run() must skip a newer non-completed run and return "
            "the latest COMPLETED one")

        stuck.status = "failed"
        stuck.finished_at = datetime(2026, 9, 26, 9, 0)
        session.commit()
        assert latest_run(session).id == completed.id, (
            "a failed run must not be picked over a completed one either")
    finally:
        session.close()


def test_dashboard_common_never_caches_a_live_session(monkeypatch, tmp_path):
    """The actual fix, asserted structurally: dashboard_common.db_session()
    must return a NEW Session object every call — never the same one twice
    — which is what makes it safe to call from any thread on any rerun.
    Only the ENGINE underneath may be shared."""
    monkeypatch.setattr(
        "mop_reco.db.DEFAULT_DB_PATH", tmp_path / "test_reco.sqlite", raising=False)
    import dashboard_common

    # db_session() reads its engine via the module's own cached _engine();
    # cache_resource's cache lives across this test process, so force a
    # fresh entry pointed at our tmp path rather than the real project db.
    dashboard_common._engine.clear()
    monkeypatch.setattr(dashboard_common, "init_db",
                         lambda: init_db(get_engine(tmp_path / "test_reco.sqlite")))

    s1 = dashboard_common.db_session()
    s2 = dashboard_common.db_session()
    try:
        assert s1 is not s2, "db_session() returned the SAME Session twice — it must never be cached"
    finally:
        s1.close()
        s2.close()
