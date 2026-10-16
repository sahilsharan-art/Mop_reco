"""Pairing open exception bills with the unmapped transactions that probably paid them.

2026-10-14: candidates found per bill. 2026-10-15: made a GLOBAL ASSIGNMENT after the first version offered
one transaction to five bills (705 transactions suggested against more than one bill). The tests below pin the
rules: a transaction goes to at most one bill; contests are decided by time, then MOP family, else nobody; an
ambiguous cluster is flagged and left blank; Txn 2 is only a genuine split; and the whole set is asserted.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import exception_pairing as ep
from mop_reco import manual_match as mm
from mop_reco.db import Base, Exception_, ManualMatch, R1BillLine, Run, TransactionRefund, get_session


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), status="completed", business_dates="[]"))
    s.commit()
    return s


def txn_exc(session, gateway, tid, store, amount, day, time, status="open"):
    session.add(Exception_(exception_key=f"{gateway}|{tid}", reconciliation="R1", type="unmapped_gateway_transaction",
                           business_date=day, store=store, amount=amount, status=status, transaction_time=time,
                           detail=json.dumps({"gateway": gateway, "transaction_id": tid}),
                           first_seen_run_id=1, last_seen_run_id=1))
    session.commit()


def bills(*rows):
    """(key, store, amount, date, time[, mop])"""
    out = []
    for r in rows:
        k, st, a, d, t = r[:5]
        out.append({"bill_key": k, "store": st, "amount": a, "date": d, "time": t,
                    "mop": r[5] if len(r) > 5 else "PAYTM EDC"})
    return pd.DataFrame(out)


def pool_of(*rows):
    """(txn_id, store, amount, date, time[, gateway])"""
    return pd.DataFrame([{"gateway": r[5] if len(r) > 5 else "paytm", "txn_id": r[0], "store": r[1], "amount": r[2],
                          "date": r[3], "time": r[4], "exception_key": r[0]} for r in rows])


def ids(a):
    return {k: [c.txn_id for c in v] for k, v in a.candidates.items()}


# --- one transaction, at most one bill ---------------------------------------------------------

def test_a_transaction_is_offered_to_the_bill_nearest_in_time_only():
    a = ep.assign(bills(("far", "ABD", 500.0, "2026-08-03", "10:00:00"), ("near", "ABD", 500.0, "2026-08-03", "12:01:00")),
                  pool_of(("T1", "ABD", 500.0, "2026-08-03", "12:05:00")))
    assert ids(a) == {"near": ["T1"]}          # the far bill gets nothing - not a second copy
    assert "far" not in a.ambiguous            # a lost contest is a "none", not an ambiguous cluster


def test_five_bills_one_transaction_gives_it_to_one_bill_at_most():
    bs = bills(*[(f"b{i}", "ABD", 100.0, "2026-08-03", f"1{i}:00:00") for i in range(5)])
    a = ep.assign(bs, pool_of(("T1", "ABD", 100.0, "2026-08-03", "13:05:00")))
    assert sum(len(v) for v in a.candidates.values()) == 1 and list(ids(a).values()) == [["T1"]]


def test_a_tie_on_time_is_broken_by_the_mop_family():
    a = ep.assign(bills(("paytm_bill", "ABD", 100.0, "2026-08-03", "12:00:00", "PAYTM EDC"),
                        ("axis_bill", "ABD", 100.0, "2026-08-03", "12:00:00", "EDC OTHER AXIS")),
                  pool_of(("T1", "ABD", 100.0, "2026-08-03", "12:05:00", "axis")))
    assert ids(a) == {"axis_bill": ["T1"]}


def test_a_full_tie_gives_nobody_the_transaction_and_flags_the_cluster():
    a = ep.assign(bills(("b1", "ABD", 100.0, "2026-08-03", "12:00:00"), ("b2", "ABD", 100.0, "2026-08-03", "12:00:00")),
                  pool_of(("T1", "ABD", 100.0, "2026-08-03", "12:05:00"), ("T2", "ABD", 100.0, "2026-08-03", "12:05:00")))
    assert a.candidates == {}
    assert a.flag("b1") == a.flag("b2") == "ambiguous - 2 bills, 2 transactions"


def test_the_result_does_not_depend_on_row_order():
    rows = [("b1", "ABD", 100.0, "2026-08-03", "12:00:00"), ("b2", "ABD", 100.0, "2026-08-03", "12:00:00"),
            ("b3", "ABD", 100.0, "2026-08-03", "12:30:00")]
    txns = [("T1", "ABD", 100.0, "2026-08-03", "12:05:00"), ("T2", "ABD", 100.0, "2026-08-03", "12:31:00")]
    a1 = ep.assign(bills(*rows), pool_of(*txns))
    a2 = ep.assign(bills(*reversed(rows)), pool_of(*reversed(txns)))
    assert ids(a1) == ids(a2) and a1.ambiguous.keys() == a2.ambiguous.keys()


def test_exact_amount_and_store_are_required_and_the_day_window_holds():
    pool = pool_of(("T_other_store", "AGT", 500.0, "2026-08-03", "12:01:00"),
                   ("T_other_amount", "ABD", 500.5, "2026-08-03", "12:01:00"),
                   ("T_far", "ABD", 500.0, "2026-08-06", "12:00:00"))
    assert ep.assign(bills(("b1", "ABD", 500.0, "2026-08-03", "12:00:00")), pool).candidates == {}


def test_untimed_transactions_tie_on_the_day_not_on_a_made_up_time():
    # Axis has no time of day: two bills at different clock times are equally far from it, so the contest
    # is a tie (no MOP help) and nobody is given the transaction.
    a = ep.assign(bills(("b1", "ABD", 100.0, "2026-08-03", "10:00:00"), ("b2", "ABD", 100.0, "2026-08-03", "20:00:00")),
                  pool_of(("A1", "ABD", 100.0, "2026-08-03", "", "axis")))
    assert a.candidates == {} and a.flag("b1").startswith("ambiguous - 2 bills, 1 transactions")


def test_tier_is_minutes_only_when_both_sides_have_a_time():
    a = ep.assign(bills(("b1", "ABD", 100.0, "2026-08-03", "12:00:00")),
                  pool_of(("T1", "ABD", 100.0, "2026-08-03", "12:05:00")))
    assert a.candidates["b1"][0].tier == 1
    a = ep.assign(bills(("b1", "ABD", 100.0, "2026-08-03", "12:00:00")),
                  pool_of(("T1", "ABD", 100.0, "2026-08-03", "12:40:00")))
    assert a.candidates["b1"][0].tier == 2


# --- Txn 2 only for a genuine split ------------------------------------------------------------

def test_txn_2_is_a_split_whose_amounts_sum_to_the_bill():
    a = ep.assign(bills(("b1", "ABD", 1000.0, "2026-08-03", "12:00:00")),
                  pool_of(("S1", "ABD", 400.0, "2026-08-03", "12:01:00"), ("S2", "ABD", 600.0, "2026-08-03", "12:02:00")))
    assert a.kind["b1"] == "split" and ids(a) == {"b1": ["S2", "S1"]}
    assert sum(c.amount for c in a.candidates["b1"]) == 1000.0


def test_two_equal_amount_transactions_are_never_a_split_of_one_bill():
    # Amount 1 already equals the bill: the second is a duplicate payment, not Txn 2. One bill, two
    # equal transactions that cannot be separated is an ambiguous cluster - blank, flagged.
    a = ep.assign(bills(("b1", "ABD", 500.0, "2026-08-03", "12:00:00")),
                  pool_of(("T1", "ABD", 500.0, "2026-08-03", "12:01:00"), ("T2", "ABD", 500.0, "2026-08-03", "12:30:00")))
    assert ids(a) == {"b1": ["T1"]}          # nearest wins; T2 is NOT offered as a second


def test_a_pair_that_does_not_sum_exactly_is_not_offered():
    a = ep.assign(bills(("b1", "ABD", 1000.0, "2026-08-03", "12:00:00")),
                  pool_of(("S1", "ABD", 400.0, "2026-08-03", "12:01:00"), ("S2", "ABD", 599.0, "2026-08-03", "12:02:00")))
    assert a.candidates == {}


def test_a_transaction_used_exactly_is_not_also_used_in_a_split():
    a = ep.assign(bills(("exact", "ABD", 400.0, "2026-08-03", "12:00:00"), ("split", "ABD", 1000.0, "2026-08-03", "12:00:00")),
                  pool_of(("S1", "ABD", 400.0, "2026-08-03", "12:01:00"), ("S2", "ABD", 600.0, "2026-08-03", "12:02:00")))
    assert ids(a) == {"exact": ["S1"]}


def test_two_bills_cannot_share_a_split_pair():
    a = ep.assign(bills(("b1", "ABD", 1000.0, "2026-08-03", "12:00:00"), ("b2", "ABD", 1000.0, "2026-08-03", "12:00:00")),
                  pool_of(("S1", "ABD", 400.0, "2026-08-03", "12:01:00"), ("S2", "ABD", 600.0, "2026-08-03", "12:02:00")))
    assert a.candidates == {}


# --- the assertion, over a larger random population ---------------------------------------------

def test_no_transaction_appears_twice_over_a_whole_population():
    rnd = random.Random(7)
    stores, amounts = ["A", "B", "C"], [100.0, 200.0, 300.0, 500.0]
    bs, ps = [], []
    for i in range(300):
        bs.append((f"b{i}", rnd.choice(stores), rnd.choice(amounts), f"2026-08-0{rnd.randint(1, 4)}",
                   f"{rnd.randint(8, 21):02d}:{rnd.randint(0, 59):02d}:00"))
    for i in range(240):
        ps.append((f"T{i}", rnd.choice(stores), rnd.choice(amounts), f"2026-08-0{rnd.randint(1, 4)}",
                   f"{rnd.randint(8, 21):02d}:{rnd.randint(0, 59):02d}:00"))
    a = ep.assign(bills(*bs), pool_of(*ps))
    flat = [c.txn_id for v in a.candidates.values() for c in v]
    assert len(flat) == len(set(flat))
    assert not ({t for c in a.clusters.values() for t in c["txns"]} & set(flat))


def test_the_build_fails_if_a_transaction_is_offered_twice():
    bad = ep.Assignment(candidates={"b1": [ep.Candidate("paytm", "T1", 1.0, "d", "", 1, 0)],
                                    "b2": [ep.Candidate("paytm", "T1", 1.0, "d", "", 1, 0)]})
    with pytest.raises(ep.DuplicateSuggestion):
        ep.assert_unique(bad)
    twice = ep.Assignment(candidates={"b1": [ep.Candidate("paytm", "T1", 1.0, "d", "", 1, 0),
                                             ep.Candidate("paytm", "T1", 1.0, "d", "", 1, 0)]})
    with pytest.raises(ep.DuplicateSuggestion):
        ep.assert_unique(twice)


def test_count_summary_reports_counts_and_values():
    bs = bills(("1", "A", 10.0, "2026-08-03", ""), ("2", "B", 20.0, "2026-08-03", ""), ("3", "C", 30.0, "2026-08-03", ""),
               ("4", "D", 40.0, "2026-08-03", ""))
    a = ep.assign(bs, pool_of(("t1", "A", 10.0, "2026-08-03", ""), ("t2", "C", 30.0, "2026-08-03", "")))
    r = ep.count_summary(bs, a)
    assert (r["exact"], r["exact_value"], r["none"], r["none_value"], r["suggested_ids"]) == (2, 40.0, 2, 60.0, 2)


def test_the_pool_leaves_out_matched_refunded_and_closed(session):
    for t in ("T_manual", "T_refund", "T_closed", "T_free"):
        txn_exc(session, "paytm", t, "ABD", 500.0, "2026-08-03", "12:00:00",
                status="closed" if t == "T_closed" else "open")
    session.add(ManualMatch(bill_key="k", business_date="2026-08-03", store="ABD", bill_no="B", mop="M", amount=500,
                            gateway="paytm", transaction_ref="T_manual", status="matched_manual", validation_case="a",
                            reason_code="x", operator="o", created_at=datetime(2026, 9, 2), lifecycle="active"))
    session.add(TransactionRefund(gateway="paytm", transaction_ref="T_refund", business_date="2026-08-03", store="ABD",
                                  amount=500, zoho_no="Z1", customer_name="c", refund_date="2026-08-05",
                                  refund_status="pending", uploaded_by="o", uploaded_at=datetime(2026, 9, 2)))
    session.commit()
    assert ep.unmapped_pool(session)["txn_id"].tolist() == ["T_free"]


# --- the one-click match: same validation as every manual match -----------------------------

def _bill(**kw):
    return {"business_date": "2026-08-03", "store": "ABD", "bill_no": "B1", "mop": "PAYTM EDC", "amount": 500.0, **kw}


def _row(tid="T1", **kw):
    return pd.DataFrame([{"gateway": "paytm", "txn_ref": tid, "business_date": "2026-08-03", "store": "ABD",
                          "amount": 500.0, "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI", **kw}])


def _bill_line(session, no):
    session.add(R1BillLine(run_id=1, business_date="2026-08-03", store="ABD", bill_no=no, line_seq=1, mop="PAYTM EDC",
                           mop_class="digital", amount=500.0, matched=False, status="x"))
    session.commit()


def test_match_on_a_candidate_row_is_accepted_and_cascades(session):
    _bill_line(session, "B1")
    v = mm.validate_by_id(session, _bill(), "T1", gateway_pool=_row())
    assert v.ok and v.gateway == "paytm"
    mm.record(session, _bill(), v.gateway, "T1", "manual_match_by_bill", "Aditi", v)
    assert session.query(ManualMatch).filter_by(bill_no="B1", lifecycle="active").count() == 1


def test_a_transaction_already_held_by_another_bill_is_refused_naming_it(session):
    _bill_line(session, "B1")
    _bill_line(session, "B2")
    v1 = mm.validate_by_id(session, _bill(), "T1", gateway_pool=_row())
    mm.record(session, _bill(), v1.gateway, "T1", "manual_match_by_bill", "Aditi", v1)
    v2 = mm.validate_by_id(session, _bill(bill_no="B2"), "T1", gateway_pool=_row())
    assert not v2.ok and "B1" in " ".join(v2.problems)


def test_a_taken_suggestion_is_no_longer_in_the_fresh_pool(session):
    """The page re-reads the unmapped pool at match time; once another bill has the transaction it is
    gone from that pool, which is what makes the page refuse a stale suggestion."""
    txn_exc(session, "paytm", "T1", "ABD", 500.0, "2026-08-03", "12:00:00")
    assert "T1" in set(ep.unmapped_pool(session)["txn_id"])
    _bill_line(session, "B1")
    v = mm.validate_by_id(session, _bill(), "T1", gateway_pool=_row())
    mm.record(session, _bill(), v.gateway, "T1", "manual_match_by_bill", "Aditi", v)
    assert "T1" not in set(ep.unmapped_pool(session)["txn_id"])


def test_a_different_amount_is_refused():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    v = mm.validate_by_id(get_session(e), _bill(), "T1", gateway_pool=_row(amount=499.0))
    assert not v.ok and any("Amount differs" in p for p in v.problems)


def test_after_matching_one_the_duplicate_payment_is_flagged_open(session):
    txn_exc(session, "paytm", "T1", "ABD", 500.0, "2026-08-03", "12:00:00")
    txn_exc(session, "paytm", "T2", "ABD", 500.0, "2026-08-03", "12:30:00")
    _bill_line(session, "B1")
    v = mm.validate_by_id(session, _bill(), "T1", gateway_pool=_row("T1"))
    mm.record(session, _bill(), v.gateway, "T1", "manual_match_by_bill", "Aditi", v)
    second = ep.second_transaction_still_open(session, ep.unmapped_pool(session))
    assert second["second_txn_id"].tolist() == ["T2"] and second["bill_no"].tolist() == ["B1"]
    assert ep.unmapped_pool(session)["txn_id"].tolist() == ["T2"]
