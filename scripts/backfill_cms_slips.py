"""Bring an existing run forward: write the CMS slips and PTC deposits the CMS Reco page reads.

    python scripts/backfill_cms_slips.py [run_id]      (default: the latest completed run)

Reads the raw Axis CMS files (every discovered file, including credits dated after the run's
last date) and the PTC warehouse. Adds rows to two new tables only; nothing else is touched.
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import cms_blocks  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import Run, get_engine, get_session, init_db  # noqa: E402

log = logging.getLogger("backfill_cms_slips")


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = get_engine()
    init_db(engine)
    session = get_session(engine)
    if len(argv) > 1:
        run_id = int(argv[1])
    else:
        run_id = (session.query(Run.id).filter(Run.status == "completed").order_by(Run.id.desc()).first() or [None])[0]
    if run_id is None:
        log.error("no completed run")
        return 1
    report = cms_blocks.persist(session, run_id, load_config(), ROOT / "warehouse")
    log.info("run %s: %s", run_id, report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
