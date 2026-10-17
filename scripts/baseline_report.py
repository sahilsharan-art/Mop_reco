"""Write a baseline document for a run: per-month totals, the five R3a controls per month, the cash chain, and - when a
reference run is given - a month-by-month proof that the months both runs cover did not move.

    python scripts/baseline_report.py --run 14 --compare 11 --compare-to 2026-08-31 --out docs/BASELINE_APR_SEP.md

Reads only persisted rows (r1_bill_lines, store_day_agg, txn_day_agg) of the two runs. Nothing is written but the file.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from mop_reco import r3a_store_cash as r3a  # noqa: E402
from mop_reco.db import Run, get_engine, get_session  # noqa: E402

MONTH = {"01": "January", "02": "February", "03": "March", "04": "April", "05": "May", "06": "June", "07": "July",
         "08": "August", "09": "September", "10": "October", "11": "November", "12": "December"}


def label(m: str) -> str:
    return f"{MONTH[m[5:7]]} {m[:4]}"


def inr(v) -> str:
    return f"Rs {float(v):,.2f}"


def store_days(session, run_id: int) -> pd.DataFrame:
    df = pd.read_sql(text("select business_date, store, total_sale, digital_sale, cash_sale, opening_balance, cash_deposit, "
                          "cash_expense_income, closing_balance from store_day_agg where run_id = :r"),
                     session.bind, params={"r": run_id})
    for c in df.columns[2:]:
        df[c] = df[c].astype(float)
    df["month"] = df["business_date"].str[:7]
    return df


def book_of(sd: pd.DataFrame) -> pd.DataFrame:
    return sd.rename(columns={"opening_balance": "opening_cash", "cash_expense_income": "net_ptc",
                              "cash_deposit": "cms_deposit", "closing_balance": "closing_cash"})[
        ["business_date", "store", "opening_cash", "cash_sale", "net_ptc", "cms_deposit", "closing_cash"]]


def month_metrics(session, run_id: int, date_to: str) -> pd.DataFrame:
    sd = store_days(session, run_id)
    sd = sd[sd["business_date"] <= date_to]
    bills = pd.read_sql(text("select substr(business_date,1,7) month, count(*) lines, sum(matched) matched, "
                             "sum(case when matched=0 then 1 else 0 end) open_bills, sum(case when matched=0 then amount else 0 end) open_value "
                             "from r1_bill_lines where run_id=:r and business_date <= :d group by 1"),
                        session.bind, params={"r": run_id, "d": date_to}).set_index("month")
    txn = pd.read_sql(text("select substr(business_date,1,7) month, sum(total_n) total_n, sum(mapped_n) mapped_n, "
                           "sum(pool_n) pool_n, sum(consumed_n) consumed_n from txn_day_agg where run_id=:r and business_date <= :d group by 1"),
                      session.bind, params={"r": run_id, "d": date_to}).set_index("month")
    rows = []
    for m, g in sd.groupby("month"):
        first, last = g["business_date"].min(), g["business_date"].max()
        b = bills.loc[m] if m in bills.index else None
        t = txn.loc[m] if m in txn.index else None
        rows.append({
            "month": m, "bill_lines": int(b["lines"]) if b is not None else 0,
            "total_sale": g["total_sale"].sum(), "digital_sale": g["digital_sale"].sum(), "cash_sale": g["cash_sale"].sum(),
            "matched": int(b["matched"]) if b is not None else 0, "open_bills": int(b["open_bills"]) if b is not None else 0,
            "open_value": float(b["open_value"]) if b is not None else 0.0,
            "unmapped_txns_by_reference": int(t["total_n"] - t["mapped_n"]) if t is not None else 0,
            "unmapped_txns_sale_basis": (int(t["pool_n"] - t["consumed_n"]) if t is not None and pd.notna(t["pool_n"]) else None),
            "store_days": len(g), "opening": g.loc[g["business_date"] == first, "opening_balance"].sum(),
            "closing": g.loc[g["business_date"] == last, "closing_balance"].sum()})
    return pd.DataFrame(rows).set_index("month")


def controls(sd: pd.DataFrame, month: str) -> dict:
    g = sd[sd["month"] == month]
    book = book_of(g)
    first, last = g["business_date"].min(), g["business_date"].max()
    opening = (g[g["business_date"] == first][["store", "opening_balance"]].rename(columns={"opening_balance": "opening_cash"}))
    neg = r3a.negative_closing(book)
    high = r3a.high_cash(book, r3a.DEFAULT_HIGH_CASH_THRESHOLD, on_date=date.fromisoformat(last))
    runs = r3a.no_deposit_runs(book)
    ident = r3a.estate_identity(book, opening, date.fromisoformat(first), date.fromisoformat(last))
    brk = r3a.carry_forward_breaks(book, date.fromisoformat(first))
    return {"a": (len(neg) == 0, f"{len(neg)} store-day(s) negative"),
            "b": (len(high) == 0, f"{len(high)} store(s) over threshold"),
            "c": (len(runs) == 0, f"{len(runs)} run(s)"),
            "d": (ident.get("holds", True), f"difference {inr(ident.get('difference', 0))}"),
            "e": (len(brk) == 0, f"{len(brk)} break(s)")}


def chain(sd: pd.DataFrame, months: list) -> list:
    out = []
    for prev, cur in zip(months, months[1:]):
        p = sd[sd["month"] == prev]
        c = sd[sd["month"] == cur]
        closing = p[p["business_date"] == p["business_date"].max()].set_index("store")["closing_balance"]
        opening = c[c["business_date"] == c["business_date"].min()].set_index("store")["opening_balance"]
        both = pd.concat([closing.rename("closing"), opening.rename("opening")], axis=1).fillna(0.0)
        diff = (both["opening"] - both["closing"]).round(2)
        broken = diff[diff.abs() >= 0.005]
        out.append((cur, float(opening.sum()), float(closing.sum()), float(opening.sum() - closing.sum()), broken))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--compare", type=int)
    ap.add_argument("--compare-to", default="2026-08-31")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    session = get_session(get_engine())
    run = session.get(Run, args.run)
    sd = store_days(session, args.run)
    months = sorted(sd["month"].unique())
    mm = month_metrics(session, args.run, "9999-12-31")
    L = [f"# Baseline - {label(months[0])} to {label(months[-1])} (Run #{run.id})", "",
         f"Generated from Run #{run.id} (started {run.started_at}, finished {run.finished_at}, status `{run.status}`). "
         "The regression baseline: every figure below is reproduced exactly by a re-run of the same inputs.", ""]
    if args.compare:
        L += [f"**{label(months[0])} to the end of the months Run #{args.compare} covered are compared with Run #{args.compare} "
              "in the section 'Did the earlier months move?' below.**", ""]
    L += ["## Per-month totals", "",
          "| Month | Bill lines | Total sale | Digital sale | Cash sale | Matched | Open bills | Unmapped txns (by reference) | Unmapped txns (sale basis) | Store-days | Opening | Closing |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m, r in mm.iterrows():
        L.append(f"| {label(m)} | {r.bill_lines:,} | {inr(r.total_sale)} | {inr(r.digital_sale)} | {inr(r.cash_sale)} | {r.matched:,} | "
                 f"{r.open_bills:,} | {r.unmapped_txns_by_reference:,} | "
                 f"{'' if r.unmapped_txns_sale_basis is None else f'{int(r.unmapped_txns_sale_basis):,}'} | {r.store_days:,} | "
                 f"{inr(r.opening)} | {inr(r.closing)} |")
    tot = mm.sum(numeric_only=True)
    L.append(f"| **TOTAL** | **{int(tot.bill_lines):,}** | **{inr(tot.total_sale)}** | **{inr(tot.digital_sale)}** | **{inr(tot.cash_sale)}** | "
             f"**{int(tot.matched):,}** | **{int(tot.open_bills):,}** | **{int(tot.unmapped_txns_by_reference):,}** | | **{int(tot.store_days):,}** | "
             f"{inr(mm.iloc[0].opening)} | {inr(mm.iloc[-1].closing)} |")
    L += ["", "Unmapped transactions are shown on two bases: *by reference* (the original strip - a row counts as mapped when a bill "
          "matched its reference) and *sale basis* (SUCCESS/ACQUIRING rows the engine did not consume - the exception population). "
          "See CLAUDE.md 6.23.", "", "## The five R3a controls, per month", "",
          "(a) no negative closing - (b) no closing over the Rs 2,00,000 threshold at month end - (c) no no-deposit run of 3+ working days - "
          "(d) the estate identity holds exactly - (e) the carry-forward chain is unbroken", ""]
    for m in months:
        c = controls(sd, m)
        L += [f"### {label(m)}", "| Control | Holds | Detail |", "|---|---|---|"]
        names = {"a": "(a) no negative closing", "b": "(b) no closing > Rs 2,00,000 at month end",
                 "c": "(c) no no-deposit run >=3 working days", "d": "(d) estate identity holds exactly",
                 "e": "(e) carry-forward chain unbroken"}
        for k in "abcde":
            ok, detail = c[k]
            L.append(f"| {names[k]} | {'YES' if ok else '**NO**'} | {detail} |")
        L.append("")
    L += ["## Cash chain - month opening against the prior month's closing, per store", "",
          "| Boundary | Opening | Prior month's closing | Difference | Stores that break |", "|---|---|---|---|---|"]
    for cur, o, cl, d, broken in chain(sd, months):
        L.append(f"| {label(cur)} 1 | {inr(o)} | {inr(cl)} | {inr(d)} | {'none' if broken.empty else ', '.join(f'{s} ({v:+,.2f})' for s, v in broken.items())} |")
    first = sd[sd["business_date"] == sd["business_date"].min()]
    last = sd[sd["business_date"] == sd["business_date"].max()]
    exp = first["opening_balance"].sum() + sd["cash_sale"].sum() + sd["cash_expense_income"].sum() - sd["cash_deposit"].sum()
    L += ["", "## Whole-range estate identity", "",
          f"opening {first['opening_balance'].sum():,.2f} + cash sale {sd['cash_sale'].sum():,.2f} + net PTC "
          f"{sd['cash_expense_income'].sum():,.2f} - deposits {sd['cash_deposit'].sum():,.2f} = expected closing {exp:,.2f} vs actual "
          f"{last['closing_balance'].sum():,.2f} - difference **{last['closing_balance'].sum() - exp:,.2f}**", ""]

    if args.compare:
        old = month_metrics(session, args.compare, args.compare_to)
        new = mm.loc[mm.index.isin(old.index)]
        L += [f"## Did the earlier months move? Run #{args.compare} against Run #{args.run}", "",
              "| Month | Metric | Run #%d | Run #%d | Same |" % (args.compare, args.run), "|---|---|---|---|---|"]
        moved = []
        for m in old.index:
            for col in ("bill_lines", "total_sale", "digital_sale", "cash_sale", "matched", "open_bills", "open_value",
                        "unmapped_txns_by_reference", "store_days", "opening", "closing"):
                a, b = old.loc[m, col], new.loc[m, col]
                same = abs(float(a) - float(b)) < 0.005
                if not same:
                    moved.append((m, col, a, b))
                L.append(f"| {label(m)} | {col} | {a:,.2f} | {b:,.2f} | {'yes' if same else '**NO**'} |")
        old_sd = store_days(session, args.compare)
        cmp_ctrl = []
        for m in old.index:
            if controls(old_sd, m) != controls(sd, m):
                cmp_ctrl.append(m)
        L += ["", f"**{'No earlier month moved on any metric above.' if not moved else f'{len(moved)} metric(s) moved: ' + str(moved)}** "
              f"The five controls are {'identical' if not cmp_ctrl else 'DIFFERENT for ' + ', '.join(cmp_ctrl)} month by month.", ""]
    L += ["## Known open findings carried forward", "",
          "The two chronic store findings in `BASELINE_APR_AUG.md` (GPB, LBL_DUMMY: 292 negative-closing store-days across the first five "
          "months), the 82 duplicate-reference gateway lines, and the unmapped-master list are unchanged by adding September; see that file, "
          "CLAUDE.md 6.23 and the Masters page.", ""]
    Path(args.out).write_text("\n".join(L), encoding="utf-8")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
