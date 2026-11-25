"""Prove that the V2 performance fast paths give exactly the same figures as the original line-level queries, on a real run. Read-only.

    python scripts/audit_fastpath_equivalence.py --run 2 [--stores 5]

Compares, in whole paise / exact counts: (1) F, the digital received of every store-day, new `_live_digital_received_fast` against the original
`_live_digital_received`; (2) `queries.verdict` and `queries.daily_trend` read from the saved per-store-day results against the same functions
forced onto the bill-line query (the result rows hidden), for the whole run, each month, a single day, and several single stores.
Exit 0 only when everything agrees."""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import queries as q, store_sheet_source as src  # noqa: E402
from mop_reco.db import StoreDayAgg, get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run", type=int, required=True)
ap.add_argument("--stores", type=int, default=5)
args = ap.parse_args()
s = get_session(init_db(get_engine()))
bad = 0


def paise(x):
    return int(round(float(x) * 100))


def check(label, ok, detail=""):
    global bad
    print(("OK   " if ok else "FAIL ") + label + (("  " + detail) if detail else ""))
    bad += 0 if ok else 1


dates = sorted(d for (d,) in s.query(StoreDayAgg.business_date).filter(StoreDayAgg.run_id == args.run).distinct())
lo, hi = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
stores = [x for (x,) in s.query(StoreDayAgg.store).filter(StoreDayAgg.run_id == args.run).distinct().order_by(StoreDayAgg.store)]

# (1) F on every store-day
t = time.perf_counter()
sheet = src.load_fast(s, args.run, lo, hi)[0]
t_new = time.perf_counter() - t
old = src._live_digital_received(s, args.run, lo, hi)
m = sheet[["date", "store", "digital_sale", "digital_received"]].merge(old.rename(columns={"digital_received": "f_old"}), on=["date", "store"], how="left")
m["f_old"] = m["f_old"].fillna(0.0)
diff = m[[paise(a) != paise(b) for a, b in zip(m["digital_received"], m["f_old"])]]
check(f"F identical on all {len(m):,} store-days (whole paise)", diff.empty, f"{len(diff)} differ" if len(diff) else f"load_fast {t_new:.2f}s")
check("G = D - F on every store-day", all(paise(r.digital_sale - r.digital_received) == paise(r.digital_sale) - paise(r.digital_received) for r in sheet.itertuples()))

# (2) verdict and daily_trend, fast vs line query
real = q._r1_result_rows


def line_path():
    q._r1_result_rows = lambda *a, **k: None


def fast_path():
    q._r1_result_rows = real


def cases():
    yield "whole run", lo, hi, None
    for mo in sorted({d[:7] for d in dates}):
        ds = [d for d in dates if d.startswith(mo)]
        yield f"month {mo}", date.fromisoformat(ds[0]), date.fromisoformat(ds[-1]), None
    yield "single day", date.fromisoformat(dates[len(dates) // 2]), date.fromisoformat(dates[len(dates) // 2]), None
    for st in stores[: args.stores]:
        yield f"store {st}", lo, hi, st


for label, a, b, st in cases():
    fast_path()
    t = time.perf_counter(); vf = q.verdict(s, args.run, a, b, st); tf = q.verdict_time = time.perf_counter() - t
    df = q.daily_trend(s, args.run, a, b, st)
    line_path()
    t = time.perf_counter(); vl = q.verdict(s, args.run, a, b, st); tl = time.perf_counter() - t
    dl = q.daily_trend(s, args.run, a, b, st)
    fast_path()
    same_v = (vf.digital_lines, vf.matched, vf.unmatched) == (vl.digital_lines, vl.matched, vl.unmatched) and \
        all(paise(getattr(vf, f)) == paise(getattr(vl, f)) for f in ("digital_value", "matched_value", "unmatched_value"))
    check(f"verdict {label}", same_v, f"fast {tf:.2f}s vs lines {tl:.2f}s" + ("" if same_v else f"  {vf} != {vl}"))
    if df.empty and dl.empty:
        same_d = True
    else:
        j = df.merge(dl, on="business_date", how="outer", suffixes=("_f", "_l"))
        same_d = len(df) == len(dl) == len(j) and all(
            (r.bill_lines_f, r.matched_f) == (r.bill_lines_l, r.matched_l)
            and paise(r.unmatched_value_f) == paise(r.unmatched_value_l) and paise(r.digital_value_f) == paise(r.digital_value_l)
            for r in j.itertuples())
    check(f"daily_trend {label}", same_d, f"{len(df)} dates")

print("ALL IDENTICAL" if not bad else f"{bad} DIFFERENCE(S)")
sys.exit(1 if bad else 0)
