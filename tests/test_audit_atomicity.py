"""Audit Phase 3/6: a bulk operation that fails half way leaves NOTHING behind (all-or-nothing).

Bulk accept and the shared-payment match record many matches in one database transaction (`commit=False` per row, one commit at the end, a rollback
on any error). Nothing tested that. These force a failure on the third match and check the whole state afterwards: no manual match, no batch, every
bill and transaction exception still open, every store task still open, and the next attempt works."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import test_bulk_accept as tb  # noqa: E402  (its fixtures and helpers)
from mop_reco import bulk_accept as ba  # noqa: E402
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco.db import Exception_, ManualMatch, ManualMatchBatch, StoreTask  # noqa: E402
from mop_reco.exceptions import bill_exception_key, txn_exception_key  # noqa: E402

session = tb.session


def _five(s):
    for i in range(1, 6):
        tb.add_bill(s, f"B{i}", 100.0 + i)
        tb.add_txn(s, f"T{i}", 100.0 + i)


def _state(s):
    return dict(
        matches=s.query(ManualMatch).count(),
        batches=s.query(ManualMatchBatch).count(),
        open_exceptions=s.query(Exception_).filter(Exception_.status == "open").count(),
        open_tasks=s.query(StoreTask).filter(StoreTask.status == "open").count(),
        consumed=len(mm.consumed_transactions(s)),
    )


def test_a_failure_on_the_third_match_leaves_nothing_behind(session, monkeypatch):
    _five(session)
    before = _state(session)
    assert before == dict(matches=0, batches=0, open_exceptions=10, open_tasks=5, consumed=0)
    bills, pool, a = tb.suggestions(session)
    s = ba.build(bills, pool, a)
    assert len(s.exact) == 5

    real = mm.record
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("simulated failure on the third match")
        return real(*args, **kwargs)

    monkeypatch.setattr(mm, "record", flaky)
    with pytest.raises(RuntimeError, match="third match"):
        ba.apply(session, 1, s.exact, "Aditi", "exact")
    assert len(calls) == 3, "two matches had been written (uncommitted) when it failed"

    assert _state(session) == before, "everything rolled back: no match, no batch, nothing closed"
    for i in range(1, 6):
        assert tb.status(session, bill_exception_key(tb.DAY, "ABD", f"B{i}", "PAYTM EDC")) == "open"
        assert tb.status(session, txn_exception_key("paytm", f"T{i}")) == "open"


def test_the_same_operation_succeeds_afterwards_and_applies_all_five(session, monkeypatch):
    _five(session)
    bills, pool, a = tb.suggestions(session)
    s = ba.build(bills, pool, a)
    real = mm.record
    n = []
    monkeypatch.setattr(mm, "record", lambda *a, **k: (n.append(1), (_ for _ in ()).throw(RuntimeError("boom")) if len(n) == 2 else real(*a, **k))[1])
    with pytest.raises(RuntimeError):
        ba.apply(session, 1, s.exact, "Aditi", "exact")
    monkeypatch.setattr(mm, "record", real)
    rep = ba.apply(session, 1, s.exact, "Aditi", "exact")
    assert (rep.applied, rep.bills_closed, rep.transactions_consumed) == (5, 5, 5)
    assert _state(session) == dict(matches=5, batches=1, open_exceptions=0, open_tasks=0, consumed=5)


def test_a_shared_payment_that_fails_on_its_second_bill_records_neither(session, monkeypatch):
    tb.add_bill(session, "S1", 60.0)
    tb.add_bill(session, "S2", 40.0)
    tb.add_txn(session, "TS", 100.0)
    before = _state(session)
    bills = [mm.find_bill_for_match(session, 1, "S1")[0], mm.find_bill_for_match(session, 1, "S2")[0]]
    import pandas as pd
    pool = pd.DataFrame([("paytm", "TS", tb.DAY, "ABD", 100.0, "SUCCESS", "ACQUIRING")],
                        columns=["gateway", "txn_ref", "business_date", "store", "amount", "status", "txn_type"])
    v = mm.validate_shared(session, bills, "paytm", "TS", pool)
    assert v.ok, v.problems
    real = mm.record
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("simulated failure on the second bill")
        return real(*args, **kwargs)

    monkeypatch.setattr(mm, "record", flaky)
    with pytest.raises(RuntimeError, match="second bill"):
        mm.record_shared(session, bills, "paytm", "TS", "gateway_confirmed_by_email", "Aditi", v)
    assert _state(session) == before, "neither bill is matched and the transaction is still free"


# --- the upload path: matches, refunds and remarks are one transaction ---------------------------------
import test_lifecycle as tl  # noqa: E402
from mop_reco import table_upload as tu  # noqa: E402
from mop_reco.db import Remark, TransactionRefund  # noqa: E402

lifecycle_session = tl.session


def test_an_upload_that_fails_on_its_third_row_applies_nothing_not_even_remarks(lifecycle_session, monkeypatch):
    s = lifecycle_session
    for i in range(1, 5):
        tl.make_pair(s, bill_no=f"SB1/000{i}", txn=f"TXN-{i}", amount=500.0 + i)
    rows = [tl.row_for(f"SB1/000{i}", **{"Txn 1": f"TXN-{i}", "amount": f"{500.0 + i:.2f}", "remark": f"checked {i}"}) for i in range(1, 5)]
    preview = tu.preview_bills(s, 1, tl.bills_file(rows))
    assert preview.can_apply and len(preview.accepted) == 4, [r.why for r in preview.rejected]
    before = (s.query(ManualMatch).count(), s.query(Remark).count(), s.query(Exception_).filter(Exception_.status == "open").count())

    real, calls = mm.record, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("simulated failure on the third row")
        return real(*a, **k)

    monkeypatch.setattr(mm, "record", flaky)
    with pytest.raises(RuntimeError, match="third row"):
        tu.apply_preview(s, preview, "Aditi")
    after = (s.query(ManualMatch).count(), s.query(Remark).count(), s.query(Exception_).filter(Exception_.status == "open").count())
    assert after == before, f"state changed by a failed upload: {before} -> {after}"
    monkeypatch.setattr(mm, "record", real)
    counts = tu.apply_preview(s, tu.preview_bills(s, 1, tl.bills_file(rows)), "Aditi")
    assert counts["matched"] == 4 and counts["remarks"] == 4, "the same file applies cleanly once the fault is gone"
