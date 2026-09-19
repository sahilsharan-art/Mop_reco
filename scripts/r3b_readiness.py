"""R3b readiness — REPORT ONLY. Builds no matching, no UI.

For the CMS bank-credit file: how many rows carry a pickup code that resolves
to a store, how many do not, and the total credited for the period. Then the
comparison against the PTC Cash/CMS Deposit total — that difference is the cash
in transit plus anything genuinely missing, and it is the figure R3b explains.

    python scripts/r3b_readiness.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import ingest
from mop_reco.config import load_config
from mop_reco.db import get_session, init_db

WAREHOUSE = ROOT / "warehouse"
pd.set_option("display.width", 200)

# §6.7: ZPO is excluded from credits. It is also a near-mirror of COL — 106
# rows totalling almost exactly the COL total — so including it would roughly
# double the credited value.
CREDIT_TYPES = ("COL", "ADJ.CR.")
NEGATIVE_CREDIT_TYPE = "ADJ.DB."


def rule(title):
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)


def read_source(source: str) -> pd.DataFrame:
    files = [p for p in sorted((WAREHOUSE / source).glob("*.parquet"))
             if not p.stem.startswith("_")]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)


def main():
    config = load_config()
    session = get_session(init_db())

    cms, cms_report = ingest.ingest_axis_cms(config, session)
    ptc = read_source("ptc")
    if ptc.empty:
        raise SystemExit("No PTC data in the warehouse — run the ingest first.")

    period_from = min(ptc["business_date"].astype(str))
    period_to = max(ptc["business_date"].astype(str))

    rule(f"CMS BANK CREDIT FILE — {period_from} to {period_to}")
    print(f"rows read              : {len(cms):,}")
    print(f"TYPE split             : {cms['TYPE'].value_counts().to_dict()}")

    excluded = cms[~cms["TYPE"].isin(CREDIT_TYPES)]
    print(f"excluded (not a credit): {len(excluded):,} rows, "
          f"Rs {excluded['SLIPAMOUNT'].astype(float).sum():,.2f}")
    print("  ZPO is excluded by §6.7 and is a near-mirror of COL — including it")
    print("  would roughly double the credited value.")

    credits = cms[cms["TYPE"].isin(CREDIT_TYPES)].copy()
    print(f"credit rows (COL + ADJ.CR.): {len(credits):,}")

    rule("PICKUP CODE RESOLUTION")
    resolved = credits["store"].notna() & credits["store"].astype(str).ne("None")
    print(f"pickup code resolves to a store : {int(resolved.sum()):,} of {len(credits):,}")
    print(f"pickup code does NOT resolve    : {int((~resolved).sum()):,}")
    if int((~resolved).sum()):
        print("\nunresolved codes (reported by name, never guessed):")
        print(credits.loc[~resolved, ["PICKUPPOINTCODE", "LOCATIONNAME", "SLIPAMOUNT"]]
              .drop_duplicates().head(25).to_string(index=False))
    print(f"distinct pickup codes seen      : {credits['PICKUPPOINTCODE'].nunique()}")
    print(f"distinct stores behind them     : {credits['store'].nunique()}")

    rule("CREDITED VALUE vs DEPOSITED VALUE")
    credits["dep_date"] = pd.to_datetime(credits["DEPDATE"], errors="coerce")
    in_period = credits[
        (credits["dep_date"] >= period_from) & (credits["dep_date"] <= period_to)]
    outside = len(credits) - len(in_period)

    credited = float(in_period["credit_amount"].astype(float).sum())
    deposited = float(ptc["deposit_amount"].sum())
    vouchers = int(ptc["is_deposit"].sum())
    difference = deposited - credited

    print(f"credit rows with a deposit date in period : {len(in_period):,}")
    print(f"credit rows dated outside it / undated    : {outside:,}")
    print("  (deposits made in June and credited in July — they belong to June's")
    print("   receivable, not this period's)")
    print()
    print(f"PTC Cash/CMS Deposit  : {vouchers:>6,} vouchers   Rs {deposited:>18,.2f}")
    print(f"CMS credited          : {len(in_period):>6,} rows       Rs {credited:>18,.2f}")
    print(f"DIFFERENCE            : {'':>6}              Rs {difference:>18,.2f}")
    print(f"                        ({difference / deposited * 100:.3f}% of deposits)")
    print()
    print("That difference is what R3b will explain: cash in transit at the period")
    print("end (a late-July deposit credits in early August, which is not in this")
    print("file) plus anything genuinely missing. It is NOT yet an exception —")
    print("splitting in-transit from missing needs the pickup-level match, and")
    print("that is R3b.")

    rule("WHAT R3b STILL NEEDS")
    print("- a bank statement, to prove the CMS credit actually reached the bank")
    print("  (config sources.bank_statement is still null)")
    print("- the per-store CMS cycle (T+0 or T+1) from the deposit master, to")
    print("  decide whether a credit is on time; unknown stores default to T+1")
    print("  and are labelled 'cycle assumed'")
    print("- a holiday calendar, for the working-day due date (§6.5)")
    print()
    print("Credit lag already measurable from this file:")
    print(f"  {cms_report.breakdown.get('credit_lag_days')}")

    session.close()


if __name__ == "__main__":
    main()
