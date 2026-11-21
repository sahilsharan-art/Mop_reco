"""Health check of the database the application is pointed at (MOP_DATABASE_URL, else the local SQLite file).

Prints a few plain lines and exits 0 when healthy, 1 otherwise. Used by deploy/health_check.ps1.
Checks: it connects; the masters are in the database (masters_source = database) with all six present; the schema
is current; and how many runs there are and the latest completed one.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import masters_db  # noqa: E402
from mop_reco.db import Run, get_engine, get_session, init_db, is_postgres  # noqa: E402


def main() -> int:
    engine = get_engine()
    init_db(engine)                       # adds anything missing; never drops
    session = get_session(engine)
    problems = []
    kind = "PostgreSQL" if is_postgres(engine) else "SQLite"
    host = engine.url.render_as_string(hide_password=True)
    print(f"{kind}: connected to {host}")
    source = masters_db.get_setting(session, masters_db.SETTING_MASTERS_SOURCE)
    print(f"masters_source = {source}")
    if source != masters_db.SOURCE_DATABASE:
        problems.append("masters are not in the database yet - run scripts/import_masters_snapshot.py")
    for name in masters_db.MASTER_FIELDS:
        v = masters_db.current_version(session, name)
        if v is None or not v.row_count:
            problems.append(f"master '{name}' has no rows")
    runs = session.query(Run).count()
    last = session.query(Run).filter(Run.status == "completed").order_by(Run.id.desc()).first()
    print(f"runs: {runs}; latest completed: " + (f"#{last.id} finished {last.finished_at:%d/%m/%Y %H:%M}" if last else "none yet"))
    session.close()
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
