"""Per-date persistence must write exactly what the per-row code wrote.

`runs._bill_line_record` (one row -> one dict) stays as the specification; the
column-wise `_bill_line_records` that the run now uses is asserted equal to it
row for row, value AND python type. Likewise the digital-mode aggregate is
asserted equal to the original per-line loop.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import mop_matrix as mm
from mop_reco import runs
from mop_reco import store_sheet_source as sss
from mop_reco.db import (DigitalModeAgg, R1BillLine, ReconciliationResult, Run, get_engine,
                         get_session, init_db)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    sess = get_session(engine)
    for rid in (7, 8, 9, 11):      # real Run rows: the result tables have foreign keys to runs
        sess.add(Run(id=rid, started_at=datetime(2026, 10, 13), business_dates="[]", status="completed"))
    sess.commit()
    yield sess
    sess.close()


def day_frame(n=60, seed=0):
    rng = np.random.default_rng(seed)
    mops = ["PAYTM EDC", "PaytmDQR", "Bajajpay Card", "Bajajpay UPI", "EDC OTHER"]
    gw = rng.choice(["paytm", "axis", "bajaj", None], n)
    matched = gw != None  # noqa: E711
    modes = np.where(matched & (gw == "paytm"), rng.choice(["UPI", "DEBIT_CARD", "ZZ"], n), None)
    df = pd.DataFrame({
        "business_date": [date(2026, 7, 3)] * n,
        "store": rng.choice(["AAA", "BBB", None], n),
        "bill_no": [f"B{i}" for i in range(n)],
        "line_seq": rng.choice([1.0, 2.0], n),
        "mop": rng.choice(mops, n),
        "mop_class": "digital",
        "amount": rng.choice([100.0, 250.5, 0.1 + 0.2], n),
        "paytm_ref": pd.array(rng.choice(["R1", "R2", None], n), dtype="string"),
        "axis_ref": pd.array(rng.choice(["A1", None], n), dtype="string"),
        "bajaj_ref": [None] * n,
        "matched": matched,
        "match_tier": np.where(matched, "Tier 1", None).astype(object),
        "matched_gateway": gw.astype(object),
        "matched_txn_ref": np.where(matched, "T", None).astype(object),
        "matched_payment_mode": pd.array(modes, dtype="string"),
        "status": np.where(matched, "Tier 1", "Not found in gateway").astype(object),
        "date_diff_days": np.where(matched, rng.choice([0, 0, 1, -2], n), 0),
        "match_note": np.where(rng.random(n) < .1, "note", None).astype(object),
        "bill_time": rng.choice(["12:00:00", None], n),
        "created_by": rng.choice(["CASH1", None], n),
    })
    return df


def test_column_wise_records_equal_the_per_row_specification():
    day = day_frame()
    spec = [runs._bill_line_record(7, row) for row in day.itertuples(index=False)]
    got = runs._bill_line_records(7, day)
    assert len(spec) == len(got)
    for i, (a, b) in enumerate(zip(spec, got)):
        assert a.keys() == b.keys()
        for k in a:
            assert a[k] == b[k] and type(a[k]) is type(b[k]), (i, k, a[k], b[k])


def test_persist_day_writes_the_same_rows_as_the_spec(session):
    """Stored rows, not python values: the column is Numeric, so the honest
    comparison is database-to-database. Run 7 goes through the new path, run 8
    through the per-row specification."""
    day = day_frame(seed=3)
    n = runs._persist_day(session, 7, date(2026, 7, 3), day, datetime(2026, 10, 13))
    session.commit()
    assert n == len(day)
    session.bulk_insert_mappings(
        R1BillLine, [runs._bill_line_record(8, row) for row in day.itertuples(index=False)])
    session.commit()

    cols = [c.name for c in R1BillLine.__table__.columns if c.name not in ("id", "run_id")]
    def stored(run_id):
        rows = session.query(R1BillLine).filter_by(run_id=run_id).order_by(R1BillLine.id).all()
        return [tuple(getattr(r, c) for c in cols) for r in rows]
    assert stored(7) == stored(8)
    assert len(stored(7)) == len(day)
    results = session.query(ReconciliationResult).filter_by(run_id=7).all()
    assert {r.store for r in results} == {str(s) for s in day["store"].unique()}


def _old_digital_mode_loop(bill_lines):
    modes, other_count, other_value = [], 0, 0.0
    for row in bill_lines.itertuples(index=False):
        if bool(row.matched):
            mode = mm.classify_digital_mode(
                getattr(row, "matched_gateway", None),
                None if pd.isna(row.matched_payment_mode) else row.matched_payment_mode, row.mop)
            if mode == "other":
                other_count += 1
                other_value += float(row.amount)
                mode = "unclassified"
        else:
            mode = "unclassified"
        modes.append(mode)
    df = bill_lines[["business_date", "store", "amount"]].copy()
    df["mode"] = modes
    g = df.groupby(["business_date", "store", "mode"], as_index=False)["amount"].sum()
    return ({(str(r.business_date), str(r.store), r.mode): float(r.amount)
             for r in g.itertuples(index=False)}, other_count, other_value)


def test_digital_mode_aggregate_equals_the_per_line_loop(session):
    day = day_frame(n=200, seed=5)
    expected, oc, ov = _old_digital_mode_loop(day)
    sss.digital_mode_begin(session, 9)
    rows, other_count, other_value = sss.digital_mode_add_day(session, 9, day)
    session.commit()
    got = {(r.business_date, r.store, r.mode): float(r.sale)
           for r in session.query(DigitalModeAgg).filter_by(run_id=9)}
    assert got == expected
    assert (rows, other_count) == (len(expected), oc)
    assert other_value == pytest.approx(ov)
    assert oc > 0          # the fixture contains an unrecognised payment mode ("ZZ")


def test_whole_frame_wrapper_still_works(session):
    day = day_frame(n=50, seed=9)
    report = sss.persist_digital_mode_agg(session, 11, day)
    expected, oc, _ = _old_digital_mode_loop(day)
    assert report["rows"] == len(expected) and report["other_matched_count"] == oc


def _old_store_payloads(day):
    """The per-group pandas loop `_store_payloads` replaced - kept here as the spec."""
    out = []
    for store, g in day.groupby("store", dropna=False):
        out.append((store, {
            "digital_bill_lines": int(len(g)),
            "matched": int(g["matched"].sum()),
            "unmatched": int((~g["matched"]).sum()),
            "tier1": int((g["match_tier"] == "Tier 1").sum()),
            "tier2": int((g["match_tier"] == "Matched (EDC OTHER)").sum()),
            "ambiguous": int((g["match_tier"] == "Ambiguous").sum()),
            "digital_value": float(g["amount"].sum()),
            "matched_value": float(g.loc[g["matched"], "amount"].sum()),
            "unmatched_value": float(g.loc[~g["matched"], "amount"].sum()),
        }))
    return out


@pytest.mark.parametrize("seed", range(5))
def test_store_payloads_are_bit_identical_to_the_per_group_loop(seed):
    import json as _json
    rng = np.random.default_rng(seed)
    n = 4000
    day = pd.DataFrame({
        "store": rng.choice([f"S{i:02d}" for i in range(40)] + [None], n),
        # awkward floats on purpose: summation order and algorithm show up in the last bit
        "amount": rng.random(n) * 10_000 + rng.choice([0.1, 0.2, 0.3, 1 / 3], n),
        "matched": rng.random(n) < 0.9,
        "match_tier": pd.Series(rng.choice(["Tier 1", "Matched (EDC OTHER)", "Ambiguous", None], n),
                                dtype=object),
    })
    old = _old_store_payloads(day)
    new = runs._store_payloads(day)
    assert [str(s) for s, _ in old] == [str(s) for s, _ in new]            # same keys, same order
    for (_, a), (_, b) in zip(old, new):
        assert _json.dumps(a) == _json.dumps(b)                            # byte-identical stored text
