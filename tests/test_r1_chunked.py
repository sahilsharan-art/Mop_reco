"""The one-date-at-a-time R1 engine must reproduce the whole-frame engine.

Inputs are crowded on purpose: the same (reference, amount) key repeats within
a day and ACROSS days, a bill can be dated AFTER its transaction (so Tier 1 of
a later date consumes what an earlier date's Tier 2 would have wanted),
transactions sit on dates with no bills, references are null, amounts are NaN,
and a REFUND links to an original. Every output of `run_r1` is compared.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import r1_bill_txn as whole  # noqa: E402
from mop_reco import r1_chunked as chunked  # noqa: E402

MOPS = ["PAYTM EDC", "PaytmDQR", "Bajajpay UPI", "EDC OTHER", "EDC OTHER AXIS"]
DAYS = 6


def _maybe(rng, value, p):
    return None if rng.random() < p else value


def _unique_ids(rng, refs, n):
    pick = rng.sample(refs, min(len(refs), n // 2))
    ids = pick + [f"U{i:04d}" for i in range(n - len(pick))]
    rng.shuffle(ids)
    return ids   # never null: a Paytm row always has its transaction_id


def make_world(seed: int):
    rng = random.Random(seed)
    refs = [f"R{i:03d}" for i in range(rng.choice([8, 20, 60]))]
    amounts = [100.0, 250.5, 99.99, 1132.0, 0.1 + 0.2, 2.675]
    stores = ["AAA", "BBB", "CCC"]
    d0 = date(2026, 6, 28)          # spans a month boundary: June 28 .. July 3
    dates = [d0 + timedelta(days=i) for i in range(DAYS)]
    pool_dates = dates + [d0 + timedelta(days=DAYS + 2)]     # a date with transactions but no bills

    rows = []
    for i in range(140):
        mop = rng.choice(MOPS)
        other = mop in whole.EDC_OTHER_MOPS
        rows.append(dict(
            business_date=rng.choice(dates), store=rng.choice(stores), bill_no=f"B{i:04d}",
            line_seq=1.0, mop=mop, mop_class="digital_noref" if other else "digital",
            in_sale_universe=True, amount=rng.choice(amounts),
            paytm_ref=None if other else _maybe(rng, rng.choice(refs), .5),
            axis_ref=None if other else _maybe(rng, rng.choice(refs), .7),
            bajaj_ref=None if other else _maybe(rng, rng.choice(refs), .8),
            bill_time="12:00:00", created_by="X"))
    bills = pd.DataFrame(rows).sort_values("business_date", kind="stable").reset_index(drop=True)
    # Non-digital and out-of-universe lines must be ignored by both engines.
    extra = bills.head(5).copy()
    extra["mop_class"] = "cash"
    extra["bill_no"] = [f"C{i}" for i in range(5)]
    bills = pd.concat([bills, extra], ignore_index=True).sort_values(
        "business_date", kind="stable").reset_index(drop=True)

    def txn_common(n):
        return dict(
            business_date=[rng.choice(pool_dates) for _ in range(n)],
            store=[rng.choice(stores + [None]) if rng.random() < .05 else rng.choice(stores)
                   for _ in range(n)],
            transaction_time=[None] * n)

    n = 130
    paytm = pd.DataFrame(dict(
        # Paytm ingest dedupes on transaction_id (CLAUDE.md 5): ids are unique.
        # (Axis/Bajaj below DO repeat references, to exercise the cross-day
        # consumed-count logic on a pool with duplicate keys.)
        transaction_id=_unique_ids(rng, refs, n),
        amount=[rng.choice(amounts + [float("nan")]) if rng.random() < .03 else rng.choice(amounts)
                for _ in range(n)],
        status=[rng.choice(["SUCCESS", "SUCCESS", "success", "FAILURE"]) for _ in range(n)],
        transaction_type="ACQUIRING",
        payment_mode=[rng.choice(["UPI", "DEBIT_CARD", None]) for _ in range(n)],
        reference_transaction_id=None, **txn_common(n)))
    # a REFUND whose original is present, one whose original is missing
    originals = paytm[(paytm["status"].str.upper() == "SUCCESS") & paytm["transaction_id"].notna()]
    orig = originals.iloc[0]
    refund = {c: None for c in paytm.columns}
    refund.update(transaction_id="REFUND-1", amount=orig["amount"], status="SUCCESS",
                  transaction_type="REFUND", reference_transaction_id=orig["transaction_id"],
                  business_date=pool_dates[2], store=None, payment_mode=None)
    lost = dict(refund, transaction_id="REFUND-2", reference_transaction_id="NO-SUCH-ORIGINAL")
    paytm = pd.concat([paytm, pd.DataFrame([refund, lost])], ignore_index=True)

    m = 40
    axis = pd.DataFrame(dict(
        RRN=[_maybe(rng, rng.choice(refs), .03) for _ in range(m)],
        GROSS_AMT=[rng.choice(amounts) for _ in range(m)],
        CARD_TYPE=[rng.choice(["VISA", "RUPAY"]) for _ in range(m)], **txn_common(m)))
    k = 20
    bajaj = pd.DataFrame({
        "Transaction Id": [_maybe(rng, rng.choice(refs), .03) for _ in range(k)],
        "Transaction Amount": [rng.choice(amounts) for _ in range(k)],
        "Transaction Status": [rng.choice(["Success", "SUCCESS", "Refunded"]) for _ in range(k)],
        "Card Scheme": ["UPI"] * k, **txn_common(k)})
    for df in (paytm, axis, bajaj):
        df.sort_values("business_date", kind="stable", inplace=True)
        df.reset_index(drop=True, inplace=True)
    return bills, paytm, axis, bajaj


def write_warehouse(root: Path, bills, paytm, axis, bajaj):
    def by_date(df, name):
        out = {}
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        for day, g in df.groupby("business_date"):
            p = d / f"{day}.parquet"
            g.to_parquet(p, index=False)
            out[str(day)] = str(p)
        return out
    return (by_date(bills, "pos"),
            {"paytm": by_date(paytm, "paytm"), "axis": by_date(axis, "axis"),
             "bajaj": by_date(bajaj, "bajaj")})


def run_chunked(root, bills, paytm, axis, bajaj, chunk_rows=0):
    bill_files, pool_files = write_warehouse(root, bills, paytm, axis, bajaj)
    frames = {}
    res = chunked.run_r1_chunked(bill_files, pool_files, on_day=lambda d, f: frames.update({d: f}),
                                 work_dir=root / "work", chunk_rows=chunk_rows)
    out = pd.concat(frames.values(), ignore_index=True) if frames else pd.DataFrame()
    return res, out


def _norm(v):
    return None if v is None or (not isinstance(v, str) and pd.isna(v)) else v


# chunk_rows: 0 = whole calendar months (two chunks here: June | July), 1 = every
# date its own chunk (the strictest cross-chunk test), 40 = mixed sizes.
@pytest.mark.parametrize("chunk_rows", [0, 1, 40])
@pytest.mark.parametrize("seed", range(30))
def test_chunked_engine_equals_whole_frame_engine(tmp_path, seed, chunk_rows):
    bills, paytm, axis, bajaj = make_world(seed)
    old = whole.run_r1(bills.copy(), paytm.copy(), axis.copy(), bajaj.copy())
    res, new = run_chunked(tmp_path, bills, paytm, axis, bajaj, chunk_rows)

    cols = ["matched", "match_tier", "matched_gateway", "matched_txn_ref", "matched_payment_mode",
            "date_diff_days", "match_note", "refs_tried", "refs_resolved", "status",
            "ambiguous_candidates"]
    a = old.bill_lines.set_index("bill_no")
    b = new.set_index("bill_no")
    assert set(a.index) == set(b.index)
    for c in cols:
        for bill in a.index:
            assert _norm(a.at[bill, c]) == _norm(b.at[bill, c]), (seed, c, bill)

    def txn_rows(df):
        return sorted((str(r.gateway), str(_norm(r.txn_ref)), str(r.business_date),
                       "nan" if pd.isna(r.amount) else repr(float(r.amount)), str(_norm(r.store)))
                      for r in df.itertuples())
    assert txn_rows(old.unmapped_txns) == txn_rows(res.unmapped_txns), seed

    assert sorted(old.linkage_exceptions["transaction_id"]) == \
        sorted(res.linkage_exceptions["transaction_id"])
    for key in ("digital_bill_lines", "tier1_matched", "tier2_matched", "ambiguous",
                "open_exceptions", "matched_total", "unmapped_txns", "unmapped_txns_by_gateway",
                "linkage_exceptions", "gateway_pool_rows", "matching_pool_rows"):
        assert old.summary[key] == res.summary[key], (seed, key)
    assert old.summary["matched_value"] == pytest.approx(res.summary["matched_value"])
    assert old.summary["unmatched_value"] == pytest.approx(res.summary["unmatched_value"])


def test_the_synthetic_world_exercises_the_hard_cases(tmp_path):
    seen = dict(tier1=0, tier2=0, ambiguous=0, backwards=0, unmapped_no_bill_day=0, unmatched=0)
    for seed in range(30):
        bills, paytm, axis, bajaj = make_world(seed)
        old = whole.run_r1(bills.copy(), paytm.copy(), axis.copy(), bajaj.copy())
        s = old.summary
        seen["tier1"] += s["tier1_matched"]
        seen["tier2"] += s["tier2_matched"]
        seen["ambiguous"] += s["ambiguous"]
        seen["unmatched"] += s["open_exceptions"]
        seen["backwards"] += int((old.bill_lines["date_diff_days"] < 0).sum())
        last = str(date(2026, 6, 28) + timedelta(days=DAYS + 2))
        seen["unmapped_no_bill_day"] += int((old.unmapped_txns["business_date"].astype(str) == last).sum())
    assert all(v > 0 for v in seen.values()), seen


def test_pool_is_looked_up_from_the_index_not_loaded(tmp_path):
    """The engine never materialises the whole pool: it is built into an on-disk
    index one file at a time, and a lookup returns only the rows asked for."""
    bills, paytm, axis, bajaj = make_world(5)
    bill_files, pool_files = write_warehouse(tmp_path, bills, paytm, axis, bajaj)
    store = chunked._PoolStore(pool_files, tmp_path / "idx.sqlite")
    store.build()
    hits = store.lookup("paytm", ["R001"])
    assert len(hits) < len(paytm)
    assert set(hits["txn_ref"].dropna().unique()) <= {"R001"}
    assert store.matching_pool_rows() > 0
    store.db.close()


def test_chunks_are_months_unless_memory_says_otherwise():
    dates = ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03", "2026-08-01"]
    w = {d: 100 for d in dates}.__getitem__
    assert chunked.plan_chunks(dates, w) == [dates[:2], dates[2:5], dates[5:]]          # calendar months
    assert chunked.plan_chunks(dates, w, max_rows=250) == [dates[:2], dates[2:4], dates[4:5], dates[5:]]
    assert chunked.plan_chunks(dates, w, max_rows=1) == [[d] for d in dates]            # a date is never split
    assert chunked.plan_chunks([], w) == []


def test_chunk_size_is_the_measured_default_and_shrinks_on_a_small_machine(monkeypatch):
    class VM:
        def __init__(self, avail): self.available = avail
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM(32 * 1024 ** 3))
    assert chunked._adaptive_chunk_rows() == chunked.DEFAULT_CHUNK_ROWS          # plenty of RAM
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM(100 * 1024 ** 2))
    small = chunked._adaptive_chunk_rows()
    assert 10_000 <= small < chunked.DEFAULT_CHUNK_ROWS                           # little RAM
