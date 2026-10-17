"""Bring the exception register in line with the manual matches and refunds already in force.

    python scripts/sync_closures.py

A transaction a person matched to a bill or refunded to a customer is closed in the register (it left
Exception transactions the moment it was matched; matches recorded before that rule existed did not).
Changes exception STATUS only; no bill, match, refund or figure is touched. Idempotent.
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import lifecycle  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
engine = init_db(get_engine())
print(lifecycle.sync_closures(get_session(engine)))
