"""Bill match (item 3a) and Transactions, the gateway side (item 3b) —
2026-09-12.

Bill match must count a manually-matched bill as CLOSED even though
R1BillLine.matched stays False for it (the overlay never writes to the
engine's own rows, CLAUDE.md 6.9b) — that is the one non-obvious rule this
file exists to pin down. Transactions (gateway side) must read only the dates
asked for from warehouse Parquet and correctly report mapped/unmapped.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import gateway_txns as gt
from mop_reco import queries as q
from mop_reco.db import (
    ManualMatch,
    R1BillLine,
    Run,
    TransactionRefund,
    get_engine,
    get_session,
    init_db,
)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "bm.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def run(session):
    r = Run(started_at=datetime(2026, 7, 20, 9, 0), finished_at=datetime(2026, 7, 20, 9, 5),
            business_dates=json.dumps(["2026-07-20", "2026-07-20"]), status="completed",
            triggered_by="test")
    session.add(r)
    session.commit()
    return r


def _bill(session, run_id, bill_no, matched, amount=100.0, gateway=None, txn_ref=None,
         business_date="2026-07-20", store="ABD"):
    session.add(R1BillLine(
        run_id=run_id, business_date=business_date, store=store, bill_no=bill_no,
        line_seq=1, mop="PAYTM EDC", mop_class="digital", amount=amount,
        matched=matched, match_tier="Tier 1" if matched else None,
        matched_gateway=gateway, matched_txn_ref=txn_ref,
        status="Tier 1" if matched else "Not found in gateway",
        paytm_ref=txn_ref if not matched else None,
    ))
    session.commit()


def _manual_match(session, bill_no, gateway, ref, amount=100.0,
                  business_date="2026-07-20", store="ABD"):
    session.add(ManualMatch(
        bill_key=f"{business_date}|{store}|{bill_no}|PAYTM EDC",
        business_date=business_date, store=store, bill_no=bill_no, mop="PAYTM EDC",
        amount=amount, gateway=gateway, transaction_ref=ref,
        status="matched_manual", validation_case="a", reason_code="other_see_note",
        operator="tester", created_at=datetime(2026, 7, 20, 10, 0), lifecycle="active",
    ))
    session.commit()


# ---------------------------------------------------------------------------
# Bill match — Open/Closed, overlay-aware
# ---------------------------------------------------------------------------

def test_a_system_matched_bill_is_closed(session, run):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="T1")
    summary = q.bill_match_summary(session, run.id)
    assert summary["closed"]["count"] == 1
    assert summary["open"]["count"] == 0


def test_a_manually_matched_bill_is_closed_even_though_r1_still_says_unmatched(session, run):
    """The rule that matters: manual_match.record() never writes to
    R1BillLine — the overlay is applied on read, not on the engine's own
    table (CLAUDE.md 6.9b). A bill match page that only looked at
    R1BillLine.matched would wrongly show this bill as still open."""
    _bill(session, run.id, "B2", matched=False)
    _manual_match(session, "B2", "paytm", "T2")

    summary = q.bill_match_summary(session, run.id)
    assert summary["closed"]["count"] == 1
    assert summary["open"]["count"] == 0

    open_df, open_total = q.bill_match_page(session, run.id, show="open")
    assert open_total == 0

    closed_df, closed_total = q.bill_match_page(session, run.id, show="closed")
    assert closed_total == 1
    row = closed_df.iloc[0]
    assert row["bill_no"] == "B2"
    assert row["closed_by"] == "manual"
    assert row["gateway"] == "paytm"
    assert row["matched_txn"] == "T2"


def test_an_unmatched_bill_with_no_overlay_is_open(session, run):
    _bill(session, run.id, "B3", matched=False)
    summary = q.bill_match_summary(session, run.id)
    assert summary["open"]["count"] == 1
    assert summary["closed"]["count"] == 0

    df, total = q.bill_match_page(session, run.id, show="open")
    assert total == 1
    assert df.iloc[0]["bill_no"] == "B3"
    assert df.iloc[0]["closed_by"] is None


def test_open_plus_closed_equals_total_exactly(session, run):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="T1", amount=500.0)
    _bill(session, run.id, "B2", matched=False, amount=300.0)
    _manual_match(session, "B2", "paytm", "T2", amount=300.0)
    _bill(session, run.id, "B3", matched=False, amount=200.0)

    summary = q.bill_match_summary(session, run.id)
    assert summary["open"]["count"] + summary["closed"]["count"] == summary["total"]["count"]
    assert abs(summary["open"]["value"] + summary["closed"]["value"]
              - summary["total"]["value"]) < 0.005
    assert summary["total"]["count"] == 3
    assert summary["open"]["count"] == 1       # B3 only
    assert summary["closed"]["count"] == 2     # B1 (system) + B2 (manual)


def test_show_all_returns_every_bill(session, run):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="T1")
    _bill(session, run.id, "B2", matched=False)
    df, total = q.bill_match_page(session, run.id, show="all")
    assert total == 2


def test_bill_match_export_matches_the_page_population(session, run):
    for i in range(5):
        _bill(session, run.id, f"B{i}", matched=False, amount=10.0)
    exported = q.bill_match_export(session, run.id, show="open")
    assert len(exported) == 5


# ---------------------------------------------------------------------------
# Transactions — the gateway side
# ---------------------------------------------------------------------------

def test_gateway_pool_for_range_reads_only_the_requested_dates(tmp_path):
    root = tmp_path
    (root / "paytm_settlement").mkdir()
    for day, ref in (("2026-07-19", "P-19"), ("2026-07-20", "P-20"),
                    ("2026-07-21", "P-21")):
        pd.DataFrame([{
            "transaction_id": ref, "transaction_time": "10:00:00", "amount": 100.0,
            "status": "SUCCESS", "transaction_type": "ACQUIRING",
            "payment_mode": "UPI", "original_mid": "MID1", "store": "ABD",
        }]).to_parquet(root / "paytm_settlement" / f"{day}.parquet")

    pool = gt.gateway_pool_for_range("2026-07-20", "2026-07-21", warehouse=root)
    assert set(pool["txn_ref"]) == {"P-20", "P-21"}
    assert set(pool["business_date"]) == {"2026-07-20", "2026-07-21"}
    assert set(pool["mid"]) == {"MID1"}


def test_gateway_pool_for_range_spans_all_three_gateways(tmp_path):
    root = tmp_path
    (root / "paytm_settlement").mkdir()
    (root / "axis_edc").mkdir()
    (root / "bajaj").mkdir()
    pd.DataFrame([{"transaction_id": "P1", "amount": 100.0, "status": "SUCCESS",
                  "transaction_type": "ACQUIRING", "store": "ABD", "original_mid": "M1"}]
                ).to_parquet(root / "paytm_settlement" / "2026-07-20.parquet")
    pd.DataFrame([{"RRN": "A1", "GROSS_AMT": 200.0, "store": "ABD", "MID": "M2",
                  "TRAN_TIME": "11:00:00"}]
                ).to_parquet(root / "axis_edc" / "2026-07-20.parquet")
    pd.DataFrame([{"Transaction Id": "B1", "Transaction Amount": 300.0,
                  "Transaction Status": "Success", "store": "ABD", "MID": "M3"}]
                ).to_parquet(root / "bajaj" / "2026-07-20.parquet")

    pool = gt.gateway_pool_for_range("2026-07-20", "2026-07-20", warehouse=root)
    assert set(pool["gateway"]) == {"paytm", "axis", "bajaj"}
    # Axis carries no status/type column of its own — defaulted, same
    # assumption r1_bill_txn.build_gateway_pool makes for the matching pool.
    axis_row = pool[pool["gateway"] == "axis"].iloc[0]
    assert axis_row["status"] == "SUCCESS"
    assert axis_row["txn_type"] == "ACQUIRING"


def test_with_bill_mapping_marks_system_and_manual_matches(session, run, tmp_path):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="P1")
    _bill(session, run.id, "B2", matched=False)
    _manual_match(session, "B2", "paytm", "P2")

    pool = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P2", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P3", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
    ])

    out = gt.with_bill_mapping(session, pool)
    by_ref = out.set_index("txn_ref")
    assert by_ref.loc["P1", "mapped"] == True  # noqa: E712
    assert by_ref.loc["P1", "match_source"] == "system"
    assert by_ref.loc["P1", "matched_bill_no"] == "B1"

    assert by_ref.loc["P2", "mapped"] == True  # noqa: E712
    assert by_ref.loc["P2", "match_source"] == "manual"
    assert by_ref.loc["P2", "matched_bill_no"] == "B2"

    assert by_ref.loc["P3", "mapped"] == False  # noqa: E712
    assert pd.isna(by_ref.loc["P3", "match_source"])


def test_mapped_plus_unmapped_equals_total(session, run):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="P1")
    pool = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P2", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 50.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
    ])
    out = gt.with_bill_mapping(session, pool)
    assert int(out["mapped"].sum()) + int((~out["mapped"]).sum()) == len(out)
    assert int(out["mapped"].sum()) == 1


def test_an_empty_pool_maps_cleanly(session, run):
    out = gt.with_bill_mapping(session, gt.gateway_pool_for_range(
        "2026-07-01", "2026-07-01", warehouse=Path("does_not_exist")))
    assert out.empty
    assert list(out.columns[-3:]) == ["matched_bill_no", "match_source", "mapped"]


# ---------------------------------------------------------------------------
# Customer refunds — a THIRD way a transaction closes (Request B, part 3).
# A refund is never a bill match: `with_refunds` must never call a refunded
# transaction "mapped", and a transaction that is genuinely mapped to a bill
# must never be reported as refunded even if a refund row exists for a
# different transaction with the same store.
# ---------------------------------------------------------------------------

def _refund(session, gateway, ref, status="pending", zoho_no="Z1"):
    session.add(TransactionRefund(
        gateway=gateway, transaction_ref=ref, business_date="2026-07-20",
        store="ABD", amount=100.0, zoho_no=zoho_no, customer_name="Test Customer",
        refund_date="2026-07-20", refund_status=status,
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 21, 9, 0),
    ))
    session.commit()


def test_with_refunds_marks_a_refunded_transaction_and_never_mapped(session, run):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="P1")
    _refund(session, "paytm", "P2")

    pool = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P2", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P3", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
    ])
    mapped = gt.with_bill_mapping(session, pool)
    out = gt.with_refunds(session, mapped)
    by_ref = out.set_index("txn_ref")

    assert by_ref.loc["P1", "resolution"] == "mapped"
    assert pd.isna(by_ref.loc["P1", "zoho_no"])

    assert by_ref.loc["P2", "resolution"] == "refunded"
    # `mapped` (bill-side) is untouched by a refund — a refund is never a
    # bill match, and P2 was never matched to any bill here.
    assert by_ref.loc["P2", "mapped"] == False  # noqa: E712
    assert by_ref.loc["P2", "zoho_no"] == "Z1"
    assert by_ref.loc["P2", "refund_status"] == "pending"

    assert by_ref.loc["P3", "resolution"] == "unmapped"


def test_mapped_plus_refunded_plus_unmapped_equals_total(session, run):
    _bill(session, run.id, "B1", matched=True, gateway="paytm", txn_ref="P1")
    _refund(session, "paytm", "P2")
    pool = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P2", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 50.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
        {"gateway": "paytm", "txn_ref": "P3", "business_date": "2026-07-20",
         "txn_time": None, "mid": "M1", "store": "ABD", "amount": 25.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"},
    ])
    out = gt.with_refunds(session, gt.with_bill_mapping(session, pool))
    counts = out["resolution"].value_counts()
    assert counts.get("mapped", 0) + counts.get("refunded", 0) + counts.get("unmapped", 0) == len(out)
    assert counts["mapped"] == 1
    assert counts["refunded"] == 1
    assert counts["unmapped"] == 1


def test_with_refunds_on_an_empty_pool_adds_columns_cleanly(session, run):
    out = gt.with_refunds(session, gt.with_bill_mapping(session, pd.DataFrame(
        columns=["gateway", "txn_ref", "business_date", "store", "amount"])))
    assert out.empty
    for column in ("zoho_no", "customer_name", "refund_date", "refund_status", "resolution"):
        assert column in out.columns
