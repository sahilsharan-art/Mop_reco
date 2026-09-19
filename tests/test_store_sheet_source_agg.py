"""store_sheet_source's precomputed aggregates (2026-09-20) — the
performance rework: `persist_aggregates()` writes store_day_agg /
date_total_agg once, at run time; `load_fast()` reads them with the filter
pushed into SQL, joining in F (digital_received) live so a manual match
still cascades immediately (CLAUDE.md 6.9b-ii item 1) with no re-run.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_sheet as ss
from mop_reco import store_sheet_source as src
from mop_reco.db import (
    DateTotalAgg,
    ManualMatch,
    R1BillLine,
    Run,
    StoreDayAgg,
    get_engine,
    get_session,
    init_db,
)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "agg.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def run(session):
    r = Run(started_at=datetime(2026, 9, 20, 9, 0),
           business_dates="[\"2026-07-01\", \"2026-07-02\"]",
           status="completed", triggered_by="test")
    session.add(r)
    session.commit()
    return r


def add_bill_line(session, run_id, bd, store, bill_no, amount, matched):
    session.add(R1BillLine(
        run_id=run_id, business_date=bd, store=store, bill_no=bill_no,
        line_seq=1, mop="PAYTM EDC", mop_class="digital", amount=amount,
        matched=matched, status="matched" if matched else "open",
    ))


def add_manual_match(session, bd, store, bill_no, gateway="paytm", ref="TXN1"):
    session.add(ManualMatch(
        bill_key=f"{bd}|{store}|{bill_no}|PAYTM EDC", business_date=bd, store=store,
        bill_no=bill_no, mop="PAYTM EDC", amount=100.0, gateway=gateway,
        transaction_ref=ref, status="matched_manual", validation_case="a",
        reason_code="clean", operator="tester", created_at=datetime(2026, 9, 20, 9, 5),
        lifecycle="active",
    ))


# ---------------------------------------------------------------------------
# _live_digital_received — the one column computed live, never precomputed
# ---------------------------------------------------------------------------

def test_live_digital_received_sums_engine_matched_bills(session, run):
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B1", 600.0, True)
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B2", 400.0, False)
    session.commit()

    out = src._live_digital_received(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    row = out[(out["date"] == "2026-07-01") & (out["store"] == "ABD")].iloc[0]
    assert row["digital_received"] == 600.0


def test_live_digital_received_folds_in_an_active_manual_match(session, run):
    """A manual match on an engine-UNMATCHED bill must count toward F
    immediately — this is the cascade CLAUDE.md 6.9b-ii item 1 requires,
    now read through the two-query SQL aggregate rather than a pandas
    groupby over every bill line."""
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B1", 600.0, True)
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B2", 400.0, False)
    add_manual_match(session, "2026-07-01", "ABD", "B2")
    session.commit()

    out = src._live_digital_received(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    row = out[(out["date"] == "2026-07-01") & (out["store"] == "ABD")].iloc[0]
    assert row["digital_received"] == 1_000.0  # both bills now count


def test_live_digital_received_ignores_a_reversed_manual_match(session, run):
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B1", 400.0, False)
    add_manual_match(session, "2026-07-01", "ABD", "B1")
    session.query(ManualMatch).update({"lifecycle": "reversed"})
    session.commit()

    out = src._live_digital_received(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert out.empty or out.iloc[0]["digital_received"] == 0.0


def test_live_digital_received_respects_the_store_filter(session, run):
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B1", 600.0, True)
    add_bill_line(session, run.id, "2026-07-01", "XYZ", "B2", 900.0, True)
    session.commit()

    out = src._live_digital_received(session, run.id, date(2026, 7, 1), date(2026, 7, 1),
                                     store="XYZ")
    assert set(out["store"]) == {"XYZ"}
    assert float(out["digital_received"].sum()) == 900.0


def test_live_digital_received_on_no_bills_is_empty_not_a_crash(session, run):
    out = src._live_digital_received(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert out.empty
    assert list(out.columns) == ["date", "store", "digital_received"]


# ---------------------------------------------------------------------------
# persist_aggregates — writes store_day_agg / date_total_agg from build()
# ---------------------------------------------------------------------------

def _fake_bundle():
    """A tiny, fully-known two-store, one-date sheet — avoids exercising
    build()'s own raw-file reads (opening balance / PTC / CMS), which is
    what tests/test_data_quality_rows.py and store_sheet's own tests
    already cover directly against the pure engine."""
    sheet = pd.DataFrame([
        {"date": "2026-07-01", "store": "ABD", "total_sale": 1000.0,
         "digital_sale": 600.0, "cash_sale": 400.0, "digital_received": 600.0,
         "digital_receivable": 0.0, "opening_balance": 100.0, "cash_deposit": 90.0,
         "cash_received": 90.0, "cash_receivable": 0.0, "cash_expense_income": -5.0,
         "closing_balance": 405.0, "float_amount": 10.0},
        {"date": "2026-07-01", "store": "XYZ", "total_sale": 500.0,
         "digital_sale": 200.0, "cash_sale": 300.0, "digital_received": 200.0,
         "digital_receivable": 0.0, "opening_balance": 50.0, "cash_deposit": 200.0,
         "cash_received": 50.0, "cash_receivable": 150.0, "cash_expense_income": 0.0,
         "closing_balance": 150.0, "float_amount": -150.0},
    ])[ss.FIELDS]
    summary = ss.summarise_by_date(sheet)
    bundle = src.SheetBundle(sheet=sheet, summary=summary)
    return bundle


def test_persist_aggregates_writes_one_store_day_agg_row_per_sheet_row(
        session, run, monkeypatch):
    monkeypatch.setattr(src, "build", lambda *a, **k: _fake_bundle())
    report = src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert report["store_days"] == 2
    assert report["dates"] == 1

    rows = session.query(StoreDayAgg).filter_by(run_id=run.id).all()
    assert {r.store for r in rows} == {"ABD", "XYZ"}
    abd = next(r for r in rows if r.store == "ABD")
    assert float(abd.cash_receivable) == 0.0
    xyz = next(r for r in rows if r.store == "XYZ")
    assert float(xyz.cash_receivable) == 150.0
    # F/G are deliberately not columns on this table at all.
    assert not hasattr(StoreDayAgg, "digital_received")

    dt = session.query(DateTotalAgg).filter_by(run_id=run.id).one()
    assert float(dt.total_sale) == 1500.0


def test_persist_aggregates_flags_defect_b_rows(session, run, monkeypatch):
    """A negative cash_receivable (K < 0, a credit exceeding its deposit —
    Defect B) must be flagged so the Data quality header can be answered
    from this table alone, with no per-view CMS read."""
    bundle = _fake_bundle()
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "cash_receivable"] = -50.0
    monkeypatch.setattr(src, "build", lambda *a, **k: bundle)
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))

    rows = {r.store: r.defect_b for r in
           session.query(StoreDayAgg).filter_by(run_id=run.id).all()}
    assert rows["XYZ"] is True
    assert rows["ABD"] is False


def test_persist_aggregates_replaces_not_appends_on_a_second_call(
        session, run, monkeypatch):
    monkeypatch.setattr(src, "build", lambda *a, **k: _fake_bundle())
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert session.query(StoreDayAgg).filter_by(run_id=run.id).count() == 2
    assert session.query(DateTotalAgg).filter_by(run_id=run.id).count() == 1


def test_persist_aggregates_on_an_empty_sheet_writes_nothing_not_a_crash(
        session, run, monkeypatch):
    monkeypatch.setattr(src, "build", lambda *a, **k: src.SheetBundle(
        sheet=pd.DataFrame(columns=ss.FIELDS), summary=pd.DataFrame(columns=ss.FIELDS)))
    report = src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert report["store_days"] == 0
    assert report["dates"] == 0
    assert "static" in report


# ---------------------------------------------------------------------------
# load_fast — the indexed read Summary.py's page load now calls
# ---------------------------------------------------------------------------

def test_load_fast_matches_build_column_by_column(session, run, monkeypatch):
    """The number that matters most: the fast path must agree with the slow
    one to the rupee, not just run faster."""
    bundle = _fake_bundle()
    monkeypatch.setattr(src, "build", lambda *a, **k: bundle)
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    add_bill_line(session, run.id, "2026-07-01", "ABD", "B1", 600.0, True)
    add_bill_line(session, run.id, "2026-07-01", "XYZ", "B2", 200.0, True)
    session.commit()

    sheet, summary = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert list(sheet.columns) == ss.FIELDS
    for column in ss.FIELDS:
        if column in ("date", "store"):
            continue
        assert float(sheet[column].sum()) == pytest.approx(
            float(bundle.sheet[column].sum())), column
    total_row = summary[summary["date"] == "TOTAL"].iloc[0]
    assert float(total_row["total_sale"]) == pytest.approx(1500.0)


def test_load_fast_cascades_a_manual_match_with_no_re_persist(session, run, monkeypatch):
    """The whole point of keeping F/G live: recording a manual match must
    move digital_receivable (G) on the very next read, without calling
    persist_aggregates() again."""
    bundle = _fake_bundle()
    # Make XYZ's bill genuinely unmatched so there is room for G to move.
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "digital_received"] = 0.0
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "digital_receivable"] = 200.0
    monkeypatch.setattr(src, "build", lambda *a, **k: bundle)
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    add_bill_line(session, run.id, "2026-07-01", "XYZ", "B2", 200.0, False)
    session.commit()

    before, _ = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 1), store="XYZ")
    assert float(before.iloc[0]["digital_receivable"]) == 200.0

    add_manual_match(session, "2026-07-01", "XYZ", "B2")
    session.commit()

    after, _ = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 1), store="XYZ")
    assert float(after.iloc[0]["digital_receivable"]) == 0.0
    assert float(after.iloc[0]["digital_received"]) == 200.0
    # Nothing else on the row moved — only F/G are ever live.
    assert float(after.iloc[0]["cash_receivable"]) == float(before.iloc[0]["cash_receivable"])


def test_load_fast_pushes_the_store_filter_into_sql(session, run, monkeypatch):
    monkeypatch.setattr(src, "build", lambda *a, **k: _fake_bundle())
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))

    sheet, _ = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 1), store="ABD")
    assert set(sheet["store"]) == {"ABD"}
    assert len(sheet) == 1


def test_load_fast_falls_back_to_build_when_nothing_is_precomputed(session, run, monkeypatch):
    """A run from before store_day_agg existed — never crash, never show a
    blank page; use the slow path exactly as Summary.py always did."""
    called = {}

    def fake_build(sess, run_id, lo, hi, cms=None):
        called["hit"] = True
        return _fake_bundle()

    monkeypatch.setattr(src, "build", fake_build)
    sheet, summary = src.load_fast(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert called.get("hit") is True
    assert len(sheet) == 2


# ---------------------------------------------------------------------------
# defect_b_summary — the cheap header count for a collapsed Data quality
# ---------------------------------------------------------------------------

def test_defect_b_summary_counts_only_flagged_rows(session, run, monkeypatch):
    bundle = _fake_bundle()
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "cash_receivable"] = -50.0
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "cash_received"] = 250.0
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "cash_deposit"] = 200.0
    monkeypatch.setattr(src, "build", lambda *a, **k: bundle)
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))

    n, v = src.defect_b_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert n == 1
    assert v == pytest.approx(50.0)  # cash_received - cash_deposit


def test_defect_b_summary_is_zero_not_a_crash_when_nothing_is_flagged(session, run, monkeypatch):
    monkeypatch.setattr(src, "build", lambda *a, **k: _fake_bundle())
    src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    n, v = src.defect_b_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert (n, v) == (0, 0.0)


def test_defect_b_summary_falls_back_when_nothing_is_precomputed(session, run, monkeypatch):
    bundle = _fake_bundle()
    bundle.sheet.loc[bundle.sheet["store"] == "XYZ", "cash_receivable"] = -50.0
    monkeypatch.setattr(src, "build", lambda *a, **k: bundle)
    # Deliberately never call persist_aggregates() — store_day_agg is empty.
    n, v = src.defect_b_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert n == 1


# ---------------------------------------------------------------------------
# static_bundle_json / load_static — controls, notes and the CMS type
# breakdowns, precomputed once and read back with no computation at all
# ---------------------------------------------------------------------------

def test_static_bundle_json_round_trips_through_run_notes(session, run, monkeypatch):
    import json as json_mod

    bundle = _fake_bundle()
    bundle.checks = ss.run_controls(bundle.sheet, bundle.summary, None,
                                    dashboard_digital=float(bundle.sheet["digital_sale"].sum()))
    bundle.notes = ["a note about the opening balance"]
    bundle.excluded = pd.DataFrame([{"type": "ZPO", "date": "(no deposit date)",
                                     "store": "(unmapped)", "rows": 2, "value": 500.0}])
    monkeypatch.setattr(src, "build", lambda *a, **k: bundle)
    report = src.persist_aggregates(session, run.id, date(2026, 7, 1), date(2026, 7, 1))

    run.notes = json_mod.dumps({"store_day_agg": report})
    session.commit()

    static = src.load_static(run)
    assert static is not None
    assert static["notes"] == ["a note about the opening balance"]
    assert len(static["controls"]) == len(bundle.checks.controls)
    assert static["excluded"].iloc[0]["type"] == "ZPO"
    assert float(static["excluded"].iloc[0]["value"]) == 500.0


def test_load_static_is_none_for_a_run_that_predates_it(session, run):
    run.notes = None
    session.commit()
    assert src.load_static(run) is None
