"""R3a — store cash, full run and report. Read-only against the raw files.

Loads the store master and opening cash, reads POS and PTC from the warehouse,
builds the cash book with carry-forward, runs the available checks, and writes
the store x date table to logs/r3a_store_cash.csv.

    python scripts/run_r3a.py [YYYY-MM-DD YYYY-MM-DD]
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import ingest, r3a_store_cash as r3a
from mop_reco.config import load_config
from mop_reco.db import get_session, init_db
from mop_reco.formatting import money_short, money_table

WAREHOUSE = ROOT / "warehouse"
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)


def rule(title):
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)


def read_source(source: str, columns=None) -> pd.DataFrame:
    files = [p for p in sorted((WAREHOUSE / source).glob("*.parquet"))
             if not p.stem.startswith("_")]
    if not files:
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(p, columns=columns) for p in files],
                      ignore_index=True)
    frame["business_date"] = frame["business_date"].astype(str)
    return frame



def _daily_pos_with_no_bill_rows(config):
    """The loose daily POS export, normalised to the columns
    overlap_with_ptc needs. Returns None if no such file is present."""
    from mop_reco import io_

    status = config.source_status("pos_bill_wise")
    candidates = [f for f in status.files if f.suffix.lower() == ".xlsx"]
    if not candidates:
        return None
    path = max(candidates, key=lambda f: f.stat().st_mtime)
    result = io_.read_any(path)
    header = [str(c).strip() for c in result.grid[0]]
    frame = pd.DataFrame(result.grid[1:], columns=header)
    dates = io_.parse_business_date_series(frame["BILLDATE"])
    return pd.DataFrame({
        "business_date": [str(d) for d in dates],
        "store": frame["STORE_NAME"],
        "bill_no": frame["BILLNO"],
        "mop": frame["MOPDESC"],
        "amount": pd.to_numeric(frame["BASEAMT"], errors="coerce"),
    })


def main(date_from: date, date_to: date):
    config = load_config()
    session = get_session(init_db())

    rule("MASTERS AND OPENING CASH")
    store_version, created = ingest.load_master(config, session, "store")
    print(f"store master version #{store_version.id} "
          f"({'new' if created else 'unchanged'}) — {store_version.row_count} rows")

    opening, opening_report = ingest.ingest_opening_cash(config, session)
    breakdown = opening_report.breakdown
    print(f"cost centres read     : {breakdown['cost_centres_read']}")
    print(f"  mapped to a store   : {breakdown['cost_centres_mapped']}")
    print(f"  NOT mapped          : {breakdown['cost_centres_unmapped']}")
    print(f"stores with an opening: {breakdown['stores']}")
    print(f"opening cash total    : Rs {opening_report.control_total:,.2f}")
    for note in opening_report.notes:
        print(f"  note: {note}")
    if breakdown["unmapped_cost_centres"]:
        print("\n  cost centres that did NOT map (reported by name, never guessed):")
        for entry in breakdown["unmapped_cost_centres"]:
            print(f"    {entry['cost_centre']}  opening {entry['opening']:,.2f}")

    pos = read_source("pos_bill_wise", ["business_date", "store", "bill_no",
                                        "mop", "amount"])
    ptc = read_source("ptc")
    print(f"\nPOS bill lines read: {len(pos):,}   PTC rows read: {len(ptc):,}")

    rule("THE DOUBLE-COUNT TEST — POS no-bill rows vs the PTC ledger")
    overlap = r3a.overlap_with_ptc(pos, ptc)
    print(f"POS rows with no bill number : {overlap.pos_rows:,} "
          f"(Rs {overlap.pos_total:,.2f})")
    print(f"PTC rows on the same dates   : {overlap.ptc_rows:,} "
          f"(Rs {overlap.ptc_total:,.2f})")
    print(f"stores whose net agrees      : {overlap.stores_agreeing} of "
          f"{overlap.stores_compared}")
    print(f"movements matched on store+amount: {overlap.movements_matched:,}")
    print(f"\nVERDICT: {overlap.verdict}")

    if overlap.pos_rows == 0:
        # The monthly export omits these rows entirely, so the test above
        # has an empty population and proves nothing. The only supplied
        # file where it is non-empty is the loose daily export — run it
        # there, or this check is a vacuous pass.
        daily = _daily_pos_with_no_bill_rows(config)
        if daily is not None:
            print()
            print("  The monthly export has no such rows, so that test")
            print("  cannot fire. Re-running it against the daily export,")
            print("  where the population is NOT empty:")
            print()
            second = r3a.overlap_with_ptc(daily, ptc)
            print(f"  POS rows with no bill number : {second.pos_rows:,} "
                  f"(Rs {second.pos_total:,.2f})")
            print(f"  PTC rows on the same dates   : {second.ptc_rows:,} "
                  f"(Rs {second.ptc_total:,.2f})")
            print(f"  stores whose net agrees      : {second.stores_agreeing} "
                  f"of {second.stores_compared}")
            print(f"  movements matched on store+amount: "
                  f"{second.movements_matched:,} of {second.pos_rows:,}")
            print()
            print(f"  VERDICT: {second.verdict}")
            if second.overlaps:
                print()
                print("  So the 6.13 exclusion is not only 'these are not")
                print("  sales' — it is also what stops every petty-cash")
                print("  movement and every deposit being counted twice.")

    rule("THE CASH BOOK")
    cash_sale = r3a.cash_sale_by_store_date(pos)
    net_ptc = r3a.net_ptc_by_store_date(ptc)
    deposits = r3a.cms_deposit_by_store_date(ptc)
    book = r3a.build_cash_book(opening, cash_sale, net_ptc, deposits,
                               date_from, date_to)
    threshold = float(config.raw.get("r3a", {}).get("high_cash_threshold",
                                                    r3a.DEFAULT_HIGH_CASH_THRESHOLD))
    checks = r3a.run_checks(book, opening, date_from, date_to, threshold=threshold)
    print(f"{len(book):,} store-days · {book['store'].nunique()} stores · "
          f"{book['business_date'].nunique()} dates")

    missing_opening = sorted(set(book["store"]) - set(opening["store"]))
    if missing_opening:
        print(f"\nstores in the data with NO opening balance ({len(missing_opening)}): "
              f"{', '.join(missing_opening)}")
        print("  seeded at zero and kept visible — never dropped (rule 5).")

    rule("STEP 4 — THE NUMBERS")
    identity = checks.identity
    split = r3a.ptc_income_expense_split(ptc)
    print(f"opening cash at {date_from}   : {breakdown['stores']} stores, "
          f"Rs {identity['opening_total']:,.2f}")
    print(f"cash sale for the period      : Rs {identity['cash_sale_total']:,.2f}")
    print(f"net PTC for the period        : Rs {identity['net_ptc_total']:,.2f}")
    print(f"  of which income (RECEIVE)   : Rs {split['income']:,.2f}")
    print(f"  of which expense (PAYMENT)  : Rs {split['expense']:,.2f}")
    print(f"  (output stays ONE netted column — the split is information only)")
    deposit_vouchers = int(ptc["is_deposit"].sum())
    print(f"Cash/CMS Deposit              : {deposit_vouchers:,} vouchers, "
          f"Rs {identity['cms_deposit_total']:,.2f}")
    print(f"closing cash at {date_to}     : Rs {identity['actual_closing']:,.2f}")

    print("\n(d) THE ESTATE IDENTITY")
    print(f"    opening      {identity['opening_total']:>18,.2f}")
    print(f"  + cash sale    {identity['cash_sale_total']:>18,.2f}")
    print(f"  + net PTC      {identity['net_ptc_total']:>18,.2f}")
    print(f"  - deposits     {identity['cms_deposit_total']:>18,.2f}")
    print(f"  = expected     {identity['expected_closing']:>18,.2f}")
    print(f"    actual       {identity['actual_closing']:>18,.2f}")
    print(f"    DIFFERENCE   {identity['difference']:>18,.2f}   "
          f"{'BALANCES' if identity['holds'] else 'DOES NOT BALANCE'}")

    print(f"\n(e) carry-forward breaks: {len(checks.carry_forward_breaks)}")
    if len(checks.carry_forward_breaks):
        print(checks.carry_forward_breaks.head(20).to_string(index=False))

    print(f"\n(a) store-days with a NEGATIVE closing: {len(checks.negative_closing)}")
    if len(checks.negative_closing):
        print("    worst ten:")
        print(checks.negative_closing.head(10).to_string(index=False))

    print(f"\n(b) stores holding over Rs {threshold:,.0f} at {date_to}: "
          f"{len(checks.high_cash)}")
    if len(checks.high_cash):
        print(checks.high_cash.to_string(index=False))

    print(f"\n(c) runs of {r3a.DEFAULT_NO_DEPOSIT_DAYS}+ collection days with cash "
          f"sales and no deposit: {len(checks.no_deposit_runs)}")
    print(f"    dates the agency collected from nobody (excluded): "
          f"{', '.join(checks.agency_non_collection_dates) or 'none'}")
    if len(checks.no_deposit_runs):
        print(checks.no_deposit_runs.head(12).to_string(index=False))

    print("\nDAILY TOTALS")
    daily = book.groupby("business_date", as_index=False).agg(
        cash_sale=("cash_sale", "sum"), net_ptc=("net_ptc", "sum"),
        cms_deposit=("cms_deposit", "sum"), closing=("closing_cash", "sum"))
    shown = daily.copy()
    for column in ("cash_sale", "net_ptc", "cms_deposit", "closing"):
        shown[column] = shown[column].map(money_table)
    print(shown.to_string(index=False))

    rule("WHAT R3a CAN AND CANNOT CHECK")
    print(r3a.NO_INDEPENDENT_CONFIRMATION)
    print(f"\n{r3a.verdict(book, checks)}")

    out = ROOT / "logs" / "r3a_store_cash.csv"
    book.to_csv(out, index=False)
    print(f"\nstore x date table written to {out}  ({len(book):,} rows)")
    session.close()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        end = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
    else:
        # Default to the loaded POS range rather than a hardcoded month (rule 8).
        pos_dates = sorted(p.stem for p in (WAREHOUSE / "pos_bill_wise").glob("*.parquet")
                           if not p.stem.startswith("_"))
        if not pos_dates:
            raise SystemExit("No POS data in the warehouse — run the ingest first.")
        start = datetime.strptime(pos_dates[0], "%Y-%m-%d").date()
        end = datetime.strptime(pos_dates[-1], "%Y-%m-%d").date()
    main(start, end)
