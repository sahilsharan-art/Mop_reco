"""Audit Phase 2: independent re-performance of the daily store sheet (columns A..N) against a stored run. READ-ONLY.

    deploy\\run_in_app_env.ps1 scripts\\audit_live_sheet_check.py --run 2 --from 2026-04-01 --to 2026-11-14
    ... --baseline        read POS / PTC from warehouse/_file_baseline (the file-derived partitions kept before an ERP-sourced run)

What it does, using EXACT integer paise (never floating-point sums) so it cannot share a rounding error with the application:
  1. stored precision      : how many stored money values carry more than 2 decimals (rounding artefacts)
  2. row identities        : C = D+E, K = I-J, M = H+E-I+L, N = H-I on every stored store-day
  3. carry-forward         : H(d+1) = M(d) per store; gaps in a store's dates
  4. date totals           : every date_total_agg row equals the sum of its store rows
  5. independent recompute : D, E from the POS partitions; I, L from the PTC partitions; J from the stored CMS slips - compared with the stored sheet
  6. the Summary matrix    : its digital / cash / total sale agree with the stored sheet (+ the Gift Voucher and Credit Note lines)
It prints counts, the largest absolute difference and examples. Exit code 0 only if every check holds. Dates are arguments (CLAUDE.md rule 8).
"""
import argparse
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(os.environ.get("MOP_ROOT") or Path(__file__).resolve().parents[1])
for _c in (Path(__file__).resolve().parents[1] / "app", ROOT / "app"):
    if _c.is_dir():
        sys.path.insert(0, str(_c))

from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run", type=int, required=True)
ap.add_argument("--from", dest="lo", required=True)
ap.add_argument("--to", dest="hi", required=True)
ap.add_argument("--baseline", action="store_true")
args = ap.parse_args()
LO, HI = date.fromisoformat(args.lo), date.fromisoformat(args.hi)
WH = ROOT / "warehouse"
POS_DIR = (WH / "_file_baseline" / "pos_bill_wise") if args.baseline else (WH / "pos_bill_wise")
PTC_DIR = (WH / "_file_baseline" / "ptc") if args.baseline else (WH / "ptc")
engine = init_db(get_engine())
MONEY = ["total_sale", "digital_sale", "cash_sale", "opening_balance", "cash_deposit", "cash_received", "cash_receivable",
         "cash_expense_income", "closing_balance", "float_amount"]
ok_all = []


def paise(x) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value())


def report(name, ok, detail=""):
    ok_all.append(ok)
    print(f"{'OK  ' if ok else 'FAIL'} {name}{(' - ' + detail) if detail else ''}")


print(f"Audit re-performance of run {args.run}, {LO} to {HI}; POS/PTC from {POS_DIR.parent.name}/{POS_DIR.name}\n")
sda = pd.read_sql(text("select * from store_day_agg where run_id = :r and business_date >= :a and business_date <= :b"), engine,
                  params={"r": args.run, "a": LO.isoformat(), "b": HI.isoformat()})
print(f"stored store-days: {len(sda):,} across {sda.store.nunique()} stores and {sda.business_date.nunique()} dates")
P = pd.DataFrame({c: [paise(v) for v in sda[c]] for c in MONEY})       # exact integer paise of every stored value
P["store"], P["d"] = sda["store"].values, sda["business_date"].values

# 1. precision
extra = {c: int(sum(1 for v in sda[c] if Decimal(str(v)).as_tuple().exponent < -2)) for c in MONEY}
report("1 stored money values carry at most 2 decimals", not any(extra.values()), f"values with >2 decimals per column: {{k: v for k, v in extra.items() if v}} -> {({k: v for k, v in extra.items() if v})}")

# 2. identities
viol = {
    "C = D + E": (P.total_sale != P.digital_sale + P.cash_sale),
    "K = I - J": (P.cash_receivable != P.cash_deposit - P.cash_received),
    "M = H + E - I + L": (P.closing_balance != P.opening_balance + P.cash_sale - P.cash_deposit + P.cash_expense_income),
    "N = H - I": (P.float_amount != P.opening_balance - P.cash_deposit),
}
for name, mask in viol.items():
    n = int(mask.sum())
    report(f"2 identity {name} on every store-day", n == 0, f"{n:,} violations" + (f"; first {P.loc[mask, ['store', 'd']].head(3).values.tolist()}" if n else ""))

# 3. carry-forward
P = P.sort_values(["store", "d"]).reset_index(drop=True)
P["dd"] = pd.to_datetime(P["d"])
nxt = P.groupby("store").shift(-1)
consecutive = (nxt["dd"] - P["dd"]) == pd.Timedelta(days=1)
broken = consecutive & (nxt["opening_balance"] != P["closing_balance"])
gaps = P.groupby("store")["dd"].apply(lambda s: int(((s.diff().dropna()) > pd.Timedelta(days=1)).sum())).sum()
report("3 carry-forward H(d+1) = M(d) for every consecutive store-day", int(broken.sum()) == 0,
       f"{int(broken.sum()):,} breaks of {int(consecutive.sum()):,} links" + (f"; first {P.loc[broken, ['store', 'd']].head(3).values.tolist()}" if broken.any() else ""))
print(f"     (information) store date gaps (a store missing a day inside its own span): {int(gaps):,}")

# 4. date totals
dta = pd.read_sql(text("select * from date_total_agg where run_id = :r and business_date >= :a and business_date <= :b"), engine,
                  params={"r": args.run, "a": LO.isoformat(), "b": HI.isoformat()})
sums = P.groupby("d")[MONEY].sum()
bad_dates = 0
worst = 0
for _, r in dta.iterrows():
    for c in MONEY:
        diff = abs(paise(r[c]) - int(sums.loc[r["business_date"], c])) if r["business_date"] in sums.index else 10 ** 12
        worst = max(worst, diff)
        bad_dates += diff != 0
report("4 every date_total_agg row = the sum of that date's store rows (exact paise)", bad_dates == 0,
       f"{len(dta):,} dates, {int(bad_dates)} differing cells, largest difference {worst / 100:,.2f}")

# 5. independent recompute from the source partitions
pos_parts, ptc_parts, nonint = [], [], 0
d = LO
while d <= HI:
    f = POS_DIR / f"{d.isoformat()}.parquet"
    if f.is_file():
        t = pd.read_parquet(f, columns=["store", "mop_class", "in_sale_universe", "amount"])
        t = t[t["in_sale_universe"]]
        a100 = t["amount"].astype(float) * 100
        nonint += int((np.abs(a100 - np.rint(a100)) > 1e-4).sum())
        t = t.assign(p=np.rint(a100).astype("int64"), d=d.isoformat())
        pos_parts.append(t[["store", "d", "mop_class", "p"]])
    g = PTC_DIR / f"{d.isoformat()}.parquet"
    if g.is_file():
        u = pd.read_parquet(g, columns=["store", "amount", "is_deposit", "deposit_amount"])
        a100 = u["amount"].astype(float) * 100
        nonint += int((np.abs(a100 - np.rint(a100)) > 1e-4).sum())
        ptc_dep = np.rint(u["deposit_amount"].astype(float) * 100).astype("int64")
        u = u.assign(p=np.rint(a100).astype("int64"), dep=ptc_dep, d=d.isoformat())
        ptc_parts.append(u[["store", "d", "is_deposit", "p", "dep"]])
    d += timedelta(days=1)
pos = pd.concat(pos_parts, ignore_index=True)
ptc = pd.concat(ptc_parts, ignore_index=True)
print(f"     source rows read: POS {len(pos):,} sale-universe lines, PTC {len(ptc):,} vouchers; amounts not on a whole paisa: {nonint:,}")
Dg = pos[pos["mop_class"].isin(["digital", "digital_noref"])].groupby(["store", "d"])["p"].sum().rename("D")
Eg = pos[pos["mop_class"] == "cash"].groupby(["store", "d"])["p"].sum().rename("E")
Ig = ptc.groupby(["store", "d"])["dep"].sum().rename("I")
Lg = ptc[~ptc["is_deposit"]].groupby(["store", "d"])["p"].sum().rename("L")
cms = pd.read_sql(text("select row_type, dep_date, store, amount, adj, recovered from cms_slips where run_id = :r and dep_date >= :a and dep_date <= :b"),
                  engine, params={"r": args.run, "a": LO.isoformat(), "b": HI.isoformat()})
cms["p"] = [paise(a) if t == "COL" else (paise(j or 0) if t == "ADJ.CR." else (-paise(rv or 0) if t == "ADJ.DB." else 0))
            for t, a, j, rv in zip(cms.row_type, cms.amount, cms.adj, cms.recovered)]
Jg = cms[cms["store"] != "UNRESOLVED"].groupby(["store", "dep_date"])["p"].sum().rename("J")
Jg.index.names = ["store", "d"]
R = pd.concat([Dg, Eg, Ig, Lg, Jg], axis=1).fillna(0).astype("int64")
S = P.set_index(["store", "d"])
for label, rc, sc in (("D digital sale", "D", "digital_sale"), ("E cash sale", "E", "cash_sale"), ("I cash deposit", "I", "cash_deposit"),
                      ("L cash expense/income", "L", "cash_expense_income"), ("J cash received", "J", "cash_received")):
    j = S[[sc]].join(R[[rc]], how="outer").fillna(0).astype("int64")
    diff = (j[sc] - j[rc]).abs()
    n = int((diff > 0).sum())
    report(f"5 {label}: stored = independent recompute, every store-day", n == 0,
           f"{len(j):,} store-days compared, {n:,} differ, largest {diff.max() / 100:,.2f}" + (f"; first {diff[diff > 0].head(3).index.tolist()}" if n else ""))

# 6. the Summary matrix
session = get_session(engine)
from mop_reco import store_sheet_source as src  # noqa: E402
tot = lambda c, df=P: int(df[c].sum())  # noqa: E731
for label, a, b, store in (("whole range", LO, HI, None), ("one month", date(2026, 7, 1), date(2026, 7, 31), None),
                           ("one store", LO, HI, P["store"].mode().iloc[0])):
    sub = P if store is None else P[P["store"] == store]
    sub = sub[(sub["d"] >= a.isoformat()) & (sub["d"] <= b.isoformat())]
    m = src.mop_particulars_matrix(session, args.run, a, b, store)
    pairs = [("total digital sale = sum D", paise(round(m.total_digital.sale, 2)), tot("digital_sale", sub)),
             ("total cash sale = sum E", paise(round(m.total_cash.sale, 2)), tot("cash_sale", sub)),
             ("UPI + Card = total digital", paise(round(m.upi.sale + m.card.sale, 2)), paise(round(m.total_digital.sale, 2))),
             ("total sale = D + E + gift voucher + credit notes",
              paise(round(m.total_sale.sale, 2)), tot("digital_sale", sub) + tot("cash_sale", sub)
              + paise(round(m.gift_voucher_redeemed + m.credit_note_issued + m.credit_note_received, 2)))]
    for name, got, want in pairs:
        report(f"6 matrix ({label}): {name}", got == want, f"matrix {got / 100:,.2f} vs {want / 100:,.2f}" if got != want else "")

print("\nVERDICT:", "EVERY CHECK HOLDS." if all(ok_all) else f"{ok_all.count(False)} CHECK(S) FAILED - see above.")
sys.exit(0 if all(ok_all) else 1)
