"""The V2 performance fast paths must give EXACTLY the figures the original line-level queries give (CLAUDE.md 6.43, 2026-11-25).

 * `queries.verdict` / `queries.daily_trend` read the run's saved per-(date, store) R1 results instead of counting every bill line.
 * `store_sheet_source.load_fast` computes F as  digital_sale - engine_unmatched + manual_only  instead of summing every matched line.
Each is checked against the original on a small random-looking population, including manual matches, a reversed match, a NULL store and a store
filter, in whole paise / exact counts. The same comparison runs on the full 5.6M-line run in scripts/audit_fastpath_equivalence.py."""
from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q, store_sheet_source as src  # noqa: E402
from mop_reco.db import DateTotalAgg, ManualMatch, R1BillLine, ReconciliationResult, Run, StoreDayAgg, get_engine, get_session, init_db  # noqa: E402


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "fast.sqlite")
    init_db(engine)
    s = get_session(engine)
    yield s
    s.close()


def _paise(x):
    return int(round(float(x) * 100))


def _populate(session, with_results=True, seed=7):
    """~3 days x 4 stores (one NULL store) of digital lines, about 15% unmatched; store-day aggregates and R1 results consistent with them."""
    rnd = random.Random(seed)
    run = Run(started_at=datetime(2026, 11, 25, 9, 0), business_dates=json.dumps(["2026-07-01", "2026-07-03"]),
              status="completed", triggered_by="test")
    session.add(run)
    session.commit()
    stores = ["AAA", "BBB", "CCC", None]
    n = 0
    open_bills = []
    day_total = {}
    for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
        for st in stores:
            lines = []
            for _ in range(rnd.randint(3, 9)):
                n += 1
                amount = round(rnd.uniform(10, 900), 2)
                matched = rnd.random() > 0.15
                lines.append((f"B{n}", amount, matched))
                session.add(R1BillLine(run_id=run.id, business_date=d, store=st, bill_no=f"B{n}", line_seq=1, mop="PAYTM EDC",
                                       mop_class="digital", amount=amount, matched=matched, status="matched" if matched else "open"))
                if not matched:
                    open_bills.append((d, st, f"B{n}", amount))
            total = round(sum(a for _, a, _ in lines), 2)
            mval = round(sum(a for _, a, m in lines if m), 2)
            uval = round(sum(a for _, a, m in lines if not m), 2)
            if with_results:
                session.add(ReconciliationResult(run_id=run.id, reconciliation="R1", business_date=d, store=st, created_at=datetime.now(),
                                                 payload=json.dumps({"digital_bill_lines": len(lines), "matched": sum(1 for *_, m in lines if m),
                                                                     "unmatched": sum(1 for *_, m in lines if not m), "digital_value": total,
                                                                     "matched_value": mval, "unmatched_value": uval})))
            if st is not None:
                kw = {c: 0.0 for c in src.AGG_COLUMNS}
                kw["digital_sale"] = total
                session.add(StoreDayAgg(run_id=run.id, business_date=d, store=st, **kw))
                day_total[d] = round(day_total.get(d, 0.0) + total, 2)
    for d, total in day_total.items():
        kw = {c: 0.0 for c in src.AGG_COLUMNS}
        kw["digital_sale"] = total
        session.add(DateTotalAgg(run_id=run.id, business_date=d, **kw))
    session.commit()
    return run, open_bills


def _manual(session, bill):
    d, st, bill_no, amount = bill
    session.add(ManualMatch(bill_key=f"{d}|{st}|{bill_no}|PAYTM EDC", business_date=d, store=st, bill_no=bill_no, mop="PAYTM EDC", amount=amount,
                            gateway="paytm", transaction_ref="T" + bill_no, status="matched_manual", validation_case="a", reason_code="clean",
                            operator="t", created_at=datetime(2026, 11, 25, 9, 5), lifecycle="active"))


def _verdict_tuple(v):
    return (v.digital_lines, v.matched, v.unmatched, _paise(v.digital_value), _paise(v.matched_value), _paise(v.unmatched_value))


@pytest.mark.parametrize("store", [None, "AAA", "CCC"])
@pytest.mark.parametrize("window", [(date(2026, 7, 1), date(2026, 7, 3)), (date(2026, 7, 2), date(2026, 7, 2))])
def test_verdict_from_saved_results_equals_the_line_query(session, monkeypatch, store, window):
    run, open_bills = _populate(session)
    _manual(session, open_bills[0])
    _manual(session, open_bills[2])
    session.commit()
    fast = q.verdict(session, run.id, window[0], window[1], store)
    monkeypatch.setattr(q, "_r1_result_rows", lambda *a, **k: None)          # force the original line-level query
    slow = q.verdict(session, run.id, window[0], window[1], store)
    assert _verdict_tuple(fast) == _verdict_tuple(slow)


def test_daily_trend_from_saved_results_equals_the_line_query(session, monkeypatch):
    run, _ = _populate(session)
    fast = q.daily_trend(session, run.id, date(2026, 7, 1), date(2026, 7, 3), None)
    monkeypatch.setattr(q, "_r1_result_rows", lambda *a, **k: None)
    slow = q.daily_trend(session, run.id, date(2026, 7, 1), date(2026, 7, 3), None)
    assert list(fast["business_date"]) == list(slow["business_date"]) and len(fast) == 3
    for col in ("bill_lines", "matched"):
        assert list(fast[col]) == list(slow[col])
    for col in ("unmatched_value", "digital_value"):
        assert [_paise(x) for x in fast[col]] == [_paise(x) for x in slow[col]]


def test_a_run_without_saved_results_falls_back_to_the_line_query(session):
    run, _ = _populate(session, with_results=False)
    assert q._r1_result_rows(session, run.id, None, None, None) is None
    v = q.verdict(session, run.id, date(2026, 7, 1), date(2026, 7, 3))
    assert v.digital_lines > 0 and v.matched > 0           # counted from the lines, not zero


@pytest.mark.parametrize("store", [None, "BBB"])
def test_fast_digital_received_equals_the_original_with_manual_matches(session, store):
    run, open_bills = _populate(session)
    _manual(session, open_bills[1])
    _manual(session, open_bills[3])
    # a reversed manual match must not count
    _manual(session, open_bills[4])
    session.query(ManualMatch).filter(ManualMatch.bill_no == open_bills[4][2]).update({"lifecycle": "reversed"})
    session.commit()
    sheet = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 3), store)[0]
    old = src._live_digital_received(session, run.id, date(2026, 7, 1), date(2026, 7, 3), store)
    merged = sheet[["date", "store", "digital_received"]].merge(old.rename(columns={"digital_received": "f_old"}), on=["date", "store"], how="left")
    merged["f_old"] = merged["f_old"].fillna(0.0)
    assert len(merged) > 0
    assert [_paise(x) for x in merged["digital_received"]] == [_paise(x) for x in merged["f_old"]]


def test_receivable_is_the_unmatched_amount_less_active_manual_matches(session):
    run, open_bills = _populate(session)
    d, st, bill_no, amount = next(b for b in open_bills if b[1] is not None)
    before = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 3), st)[0]
    row_before = before[(before["date"] == d)].iloc[0]
    _manual(session, (d, st, bill_no, amount))
    session.commit()
    after = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 3), st)[0]
    row_after = after[(after["date"] == d)].iloc[0]
    assert _paise(row_before["digital_receivable"] - row_after["digital_receivable"]) == _paise(amount)   # the match moves G with no re-run
    assert _paise(row_after["digital_received"] - row_before["digital_received"]) == _paise(amount)
