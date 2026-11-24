"""Audit Phase 2/3: two operators acting at the same moment cannot both win the same transaction (CLAUDE.md 6.9b).

`record()` checks that a transaction is unused, then inserts. Two sessions can both pass the check before either commits, so the
database's partial unique index (ux_manual_txn_active) is the real guard. This proves it with real concurrent sessions on a
file database: exactly one match is recorded, the other is refused, and the loser leaves no half-written state behind.
"""
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import manual_match as mm  # noqa: E402
from mop_reco.db import Exception_, ManualMatch, R1BillLine, Run, get_engine, get_session, init_db  # noqa: E402

POOL = pd.DataFrame([("paytm", "TXN-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING")],
                    columns=["gateway", "txn_ref", "business_date", "store", "amount", "status", "txn_type"])


@pytest.fixture
def engine(tmp_path):
    eng = get_engine(tmp_path / "conc.sqlite")
    init_db(eng)
    s = get_session(eng)
    run = Run(started_at=datetime(2026, 9, 11, 10, 0), status="completed", business_dates="2026-07-20")
    s.add(run)
    s.flush()
    for no in ("B1", "B2"):
        s.add(R1BillLine(run_id=run.id, business_date="2026-07-20", store="ABD", bill_no=no, line_seq=1, mop="PAYTM EDC", mop_class="digital",
                         amount=500.0, matched=False, status="Not found in gateway"))
    s.commit()
    s.close()
    return eng


def _bill(no):
    return {"business_date": "2026-07-20", "store": "ABD", "bill_no": no, "mop": "PAYTM EDC", "amount": 500.0}


def test_two_operators_matching_the_same_transaction_at_once_only_one_wins(engine):
    barrier = threading.Barrier(2)
    outcome = {}

    def operator(no, who):
        s = get_session(engine)
        try:
            bill = _bill(no)
            v = mm.validate(s, bill, "paytm", "TXN-1", POOL)
            assert v.ok and v.case == mm.CASE_CLEAN, "both operators see a clean match: neither has recorded yet"
            barrier.wait(timeout=20)                                     # both are past validation before either writes
            mm.record(s, bill, "paytm", "TXN-1", "gateway_confirmed_by_email", who, v)
            outcome[who] = "recorded"
        except Exception as exc:                                           # a refusal of any kind is acceptable for the loser
            s.rollback()
            outcome[who] = f"refused: {type(exc).__name__}"
        finally:
            s.close()

    threads = [threading.Thread(target=operator, args=("B1", "anna")), threading.Thread(target=operator, args=("B2", "ben"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(v.split(":")[0] for v in outcome.values()) == ["recorded", "refused"], outcome
    s = get_session(engine)
    active = s.query(ManualMatch).filter(ManualMatch.lifecycle == "active", ManualMatch.transaction_ref == "TXN-1").all()
    assert len(active) == 1, "exactly one active match holds the transaction"
    winner_bill = active[0].bill_key.split("|")[2]
    loser_bill = "B2" if winner_bill == "B1" else "B1"
    assert s.query(ManualMatch).filter(ManualMatch.bill_key.like(f"%|{loser_bill}|%")).count() == 0, "the loser left no half-written row"
    s.close()


def test_the_database_refuses_a_second_active_match_even_if_the_check_is_skipped(engine):
    """The backstop on its own: write two active rows for one transaction directly, bypassing every check."""
    from sqlalchemy.exc import IntegrityError
    s = get_session(engine)
    base = dict(gateway="paytm", transaction_ref="TXN-9", business_date="2026-07-20", store="ABD", mop="PAYTM EDC", amount=500.0, lifecycle="active",
                validation_case="a", status="matched_manual", operator="x", reason_code="r", created_at=datetime.now())
    def row(no):
        return ManualMatch(**{**base, "bill_key": f"2026-07-20|ABD|{no}|PAYTM EDC", "bill_no": no})
    s.add(row("B1"))
    s.commit()
    s.add(row("B2"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    assert s.query(ManualMatch).filter(ManualMatch.transaction_ref == "TXN-9").count() == 1
    s.close()
