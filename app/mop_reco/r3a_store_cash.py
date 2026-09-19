"""R3a — store cash book. A pure engine: DataFrames in, DataFrames out.

    closing(D) = opening(D) + cash_sale(D) + net_ptc(D) - cms_deposit(D)
    opening(1 Jul) = the opening balance file
    opening(D)     = closing(D-1)

Two things this module does NOT do, deliberately:

  - It does not compare the computed closing against a declared physical cash
    balance, because no such figure exists for any date after 1 July. R3a is a
    COMPUTED cash book with no independent confirmation, and `verdict()` says
    so in words. When a physical balance arrives for a later date, that becomes
    the two-sided check.
  - `net_ptc` EXCLUDES the deposit vouchers. The deposit is subtracted by its
    own term, so including those rows in net_ptc as well would count every
    deposit twice — Rs 48.99 Cr on full July. See `net_ptc_by_store_date`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

logger = logging.getLogger("mop_reco")

# Cash accumulating at a store instead of being deposited. Configurable, and
# read from config.yaml rather than hardcoded at the call site.
DEFAULT_HIGH_CASH_THRESHOLD = 200_000.0

# A store with cash sales but no deposit voucher for this many consecutive
# business days is flagged.
DEFAULT_NO_DEPOSIT_DAYS = 3

CASH_MOP = "Cash"


# ---------------------------------------------------------------------------
# The three daily components
# ---------------------------------------------------------------------------

def cash_sale_by_store_date(pos: pd.DataFrame) -> pd.DataFrame:
    """Cash sale per store-day.

    Restricted to the sale universe (§6.13): a POS row with no BILLNO is not a
    bill, it is a cash in/out movement. Those rows are also the PTC ledger
    mirrored into the POS export — see `overlap_with_ptc` — so counting them
    here while also loading PTC would double-count every petty-cash movement.
    """
    if pos.empty:
        return pd.DataFrame(columns=["business_date", "store", "cash_sale"])

    cash = pos[pos["mop"].astype(str).str.strip().eq(CASH_MOP)].copy()
    has_bill = cash["bill_no"].notna() & (
        ~cash["bill_no"].astype(str).str.strip().isin(["", "nan", "None"]))
    cash = cash[has_bill]

    return (cash.groupby(["business_date", "store"], as_index=False)["amount"]
            .sum().rename(columns={"amount": "cash_sale"}))


def net_ptc_by_store_date(ptc: pd.DataFrame) -> pd.DataFrame:
    """Net PTC per store-day — ONE netted column (§6.6), no income/expense
    split in the output.

    Deposit vouchers are excluded here on purpose: `cms_deposit_by_store_date`
    carries them, and the reconciliation subtracts that term separately. A
    deposit counted in both terms would be subtracted twice.
    """
    if ptc.empty:
        return pd.DataFrame(columns=["business_date", "store", "net_ptc"])

    movements = ptc[~ptc["is_deposit"].fillna(False).astype(bool)]
    return (movements.groupby(["business_date", "store"], as_index=False)["amount"]
            .sum().rename(columns={"amount": "net_ptc"}))


def cms_deposit_by_store_date(ptc: pd.DataFrame) -> pd.DataFrame:
    """Cash handed to the CMS agency, per store-day, as a positive figure.

    Identified by either co-identifier (PTC Head 'Cash/CMS Deposit' or Ledger
    'CMS-RECD.') at ingest time; the sign flip to positive happens there too.
    """
    if ptc.empty:
        return pd.DataFrame(columns=["business_date", "store", "cms_deposit"])

    deposits = ptc[ptc["is_deposit"].fillna(False).astype(bool)]
    return (deposits.groupby(["business_date", "store"], as_index=False)["deposit_amount"]
            .sum().rename(columns={"deposit_amount": "cms_deposit"}))


def ptc_income_expense_split(ptc: pd.DataFrame) -> dict:
    """For information only (§6.6 keeps the OUTPUT netted). Deposits excluded,
    matching net_ptc."""
    if ptc.empty:
        return {"income": 0.0, "expense": 0.0, "net": 0.0}
    movements = ptc[~ptc["is_deposit"].fillna(False).astype(bool)]
    income = float(movements.loc[movements["amount"] > 0, "amount"].sum())
    expense = float(movements.loc[movements["amount"] < 0, "amount"].sum())
    return {"income": income, "expense": expense, "net": income + expense}


# ---------------------------------------------------------------------------
# The double-count test the reconciliation depends on
# ---------------------------------------------------------------------------

@dataclass
class OverlapResult:
    pos_rows: int = 0
    pos_total: float = 0.0
    ptc_rows: int = 0
    ptc_total: float = 0.0
    stores_compared: int = 0
    stores_agreeing: int = 0
    movements_matched: int = 0
    overlaps: bool = False
    verdict: str = ""


def overlap_with_ptc(pos: pd.DataFrame, ptc: pd.DataFrame) -> OverlapResult:
    """Do the POS no-BILLNO rows and the PTC ledger record the SAME movements?

    We exclude POS rows with no bill number because they are cash in/out, an
    expense movement — but PTC records expense movements too. If both were
    counted the movement would be counted twice, so this must be checked
    before any R3a figure is trusted.

    Measured on the 20-Jul daily export (the only supplied file whose no-BILLNO
    population is non-empty): 628 rows on each side, identical totals,
    identical per-store net across all 185 stores, and every one of the 628
    matching a PTC row on (store, exact amount). They are the SAME movements —
    the POS export mirrors the PTC ledger. The §6.13 exclusion is therefore not
    only "these are not sales", it is also what prevents a double count.
    """
    result = OverlapResult()
    if pos.empty:
        result.verdict = "no POS rows supplied"
        return result

    no_bill = pos[pos["bill_no"].isna() | (
        pos["bill_no"].astype(str).str.strip().isin(["", "nan", "None"]))]
    result.pos_rows = len(no_bill)
    result.pos_total = float(no_bill["amount"].sum())

    if not len(no_bill):
        result.verdict = (
            "no POS rows without a bill number in this data, so the two "
            "populations cannot overlap here"
        )
        return result

    dates = set(no_bill["business_date"].unique())
    same_dates = ptc[ptc["business_date"].isin(dates)]
    result.ptc_rows = len(same_dates)
    result.ptc_total = float(same_dates["amount"].sum())

    pos_net = no_bill.groupby("store")["amount"].sum()
    ptc_net = same_dates.groupby("store")["amount"].sum()
    compared = pd.concat([pos_net.rename("pos"), ptc_net.rename("ptc")],
                         axis=1).fillna(0.0)
    result.stores_compared = len(compared)
    result.stores_agreeing = int((compared["pos"] - compared["ptc"]).abs().lt(0.005).sum())

    # Movement level: how many POS rows are matched by a PTC row on the same
    # store and the exact same amount.
    pos_keys = no_bill.assign(k=no_bill["amount"].round(2)).groupby(["store", "k"]).size()
    ptc_keys = same_dates.assign(k=same_dates["amount"].round(2)).groupby(["store", "k"]).size()
    paired = pd.concat([pos_keys.rename("pos"), ptc_keys.rename("ptc")], axis=1).dropna()
    result.movements_matched = int(paired.min(axis=1).sum()) if len(paired) else 0

    result.overlaps = (
        result.pos_rows == result.ptc_rows
        and abs(result.pos_total - result.ptc_total) < 0.005
        and result.stores_agreeing == result.stores_compared
    )
    result.verdict = (
        "THE SAME MOVEMENTS — identical row count, identical total and identical "
        "per-store net. Counting the POS no-bill rows as cash movement while also "
        "loading PTC would double-count every one of them."
        if result.overlaps else
        "the two populations differ; they are not simply the same ledger twice"
    )
    return result


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------

def _date_span(start: date, end: date) -> list:
    days, cursor = [], start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def build_cash_book(opening: pd.DataFrame, cash_sale: pd.DataFrame,
                    net_ptc: pd.DataFrame, cms_deposit: pd.DataFrame,
                    date_from: date, date_to: date) -> pd.DataFrame:
    """One row per store x business date, with the carry-forward applied.

    Every store that appears in ANY input gets a row for every date in the
    span, so a day with no movement is a visible zero-movement row rather than
    a gap. Rule 6: a missing input is a gap, never filled from a neighbour —
    here a store-day with no cash sale genuinely had none in the data, and the
    closing simply carries forward.
    """
    stores = set()
    for frame in (opening, cash_sale, net_ptc, cms_deposit):
        if not frame.empty:
            stores |= set(frame["store"].dropna().unique())
    stores = sorted(stores)
    if not stores:
        return pd.DataFrame(columns=[
            "business_date", "store", "opening_cash", "cash_sale", "net_ptc",
            "cms_deposit", "closing_cash"])

    days = _date_span(date_from, date_to)
    grid = pd.MultiIndex.from_product(
        [[str(d) for d in days], stores], names=["business_date", "store"]
    ).to_frame(index=False)

    def merge(frame, column):
        if frame.empty:
            grid[column] = 0.0
            return
        side = frame.copy()
        side["business_date"] = side["business_date"].astype(str)
        merged = grid.merge(side[["business_date", "store", column]],
                            on=["business_date", "store"], how="left")
        grid[column] = merged[column].fillna(0.0).to_numpy()

    for frame, column in ((cash_sale, "cash_sale"), (net_ptc, "net_ptc"),
                          (cms_deposit, "cms_deposit")):
        merge(frame, column)

    first_opening = (opening.set_index("store")["opening_cash"].to_dict()
                     if not opening.empty else {})

    grid = grid.sort_values(["store", "business_date"]).reset_index(drop=True)
    grid["movement"] = grid["cash_sale"] + grid["net_ptc"] - grid["cms_deposit"]

    # Carry-forward per store: opening(D) = closing(D-1), seeded from the
    # opening balance file on the first date in the span.
    grid["opening_cash"] = 0.0
    grid["closing_cash"] = 0.0
    for store, block in grid.groupby("store", sort=False):
        seed = float(first_opening.get(store, 0.0))
        closings = seed + block["movement"].cumsum()
        openings = closings - block["movement"]
        grid.loc[block.index, "opening_cash"] = openings.to_numpy()
        grid.loc[block.index, "closing_cash"] = closings.to_numpy()

    ordered = ["business_date", "store", "opening_cash", "cash_sale", "net_ptc",
               "cms_deposit", "movement", "closing_cash"]
    return grid[ordered].sort_values(["business_date", "store"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# The checks that are actually available (step 3). No invented comparison.
# ---------------------------------------------------------------------------

@dataclass
class R3aChecks:
    negative_closing: pd.DataFrame = None
    high_cash: pd.DataFrame = None
    no_deposit_runs: pd.DataFrame = None
    identity: dict = field(default_factory=dict)
    carry_forward_breaks: pd.DataFrame = None
    threshold: float = DEFAULT_HIGH_CASH_THRESHOLD
    agency_non_collection_dates: list = field(default_factory=list)


def negative_closing(book: pd.DataFrame) -> pd.DataFrame:
    """(a) A store cannot hold negative cash. Always a defect — a missing sale,
    an overstated deposit, or a mapping error."""
    if book.empty:
        return book
    hits = book[book["closing_cash"] < -0.005].copy()
    return (hits[["business_date", "store", "opening_cash", "cash_sale", "net_ptc",
                  "cms_deposit", "closing_cash"]]
            .sort_values("closing_cash").reset_index(drop=True))


def high_cash(book: pd.DataFrame, threshold: float = DEFAULT_HIGH_CASH_THRESHOLD,
              on_date: date = None) -> pd.DataFrame:
    """(b) Cash accumulating and not being deposited."""
    if book.empty:
        return book
    subset = book if on_date is None else book[book["business_date"] == str(on_date)]
    hits = subset[subset["closing_cash"] > threshold].copy()
    return (hits[["business_date", "store", "closing_cash", "cms_deposit"]]
            .sort_values("closing_cash", ascending=False).reset_index(drop=True))


def agency_non_collection_dates(book: pd.DataFrame) -> set:
    """Dates on which NO store in the estate had a pickup.

    On such a day the agency did not collect anywhere, so a store holding cash
    is not a store failing to deposit. Measured on July: the three Sundays
    (05, 12, 26) have zero estate-wide deposits and Mondays run at double a
    normal day — the working-day cycle of §6.5. Derived from the data rather
    than from a weekday rule, because no holiday calendar has been supplied
    (§4) and a public holiday would look identical.
    """
    if book.empty:
        return set()
    by_date = book.groupby("business_date")["cms_deposit"].sum()
    return set(by_date[by_date <= 0.005].index)


def no_deposit_runs(book: pd.DataFrame,
                    min_days: int = DEFAULT_NO_DEPOSIT_DAYS,
                    ignore_dates: set = None) -> pd.DataFrame:
    """(c) A store with cash sales but no deposit voucher for several
    consecutive days. Returns one row per run, longest first.

    A date on which the agency collected from nobody does not break a run and
    does not count towards its length — otherwise every store is flagged every
    weekend, which blames the store for the agency's cycle. Pass
    `ignore_dates=set()` to count raw calendar days instead.
    """
    if book.empty:
        return pd.DataFrame(columns=["store", "from_date", "to_date", "days",
                                     "cash_sale_in_run", "closing_at_end"])
    if ignore_dates is None:
        ignore_dates = agency_non_collection_dates(book)

    runs = []
    for store, block in book.sort_values(["store", "business_date"]).groupby("store"):
        current = []
        for row in block.itertuples():
            if row.business_date in ignore_dates:
                continue  # neither breaks the run nor lengthens it
            dry = row.cms_deposit <= 0.005 and row.cash_sale > 0.005
            if dry:
                current.append(row)
                continue
            if len(current) >= min_days:
                runs.append(_run_row(store, current))
            current = []
        if len(current) >= min_days:
            runs.append(_run_row(store, current))
    if not runs:
        return pd.DataFrame(columns=["store", "from_date", "to_date", "days",
                                     "cash_sale_in_run", "closing_at_end"])
    return (pd.DataFrame(runs).sort_values(["days", "cash_sale_in_run"],
                                           ascending=False).reset_index(drop=True))


def _run_row(store, rows) -> dict:
    return {
        "store": store,
        "from_date": rows[0].business_date,
        "to_date": rows[-1].business_date,
        "days": len(rows),
        "cash_sale_in_run": float(sum(r.cash_sale for r in rows)),
        "closing_at_end": float(rows[-1].closing_cash),
    }


def estate_identity(book: pd.DataFrame, opening: pd.DataFrame,
                    date_from: date, date_to: date) -> dict:
    """(d) sum(opening) + sum(cash sale) + sum(net PTC) - sum(deposits)
    must equal sum(closing on the last date), EXACTLY. Tolerance is 0."""
    if book.empty:
        return {"holds": True, "note": "no rows"}

    first = book[book["business_date"] == str(date_from)]
    last = book[book["business_date"] == str(date_to)]

    opening_total = float(first["opening_cash"].sum())
    supplied_opening = float(opening["opening_cash"].sum()) if not opening.empty else 0.0
    cash_sale_total = float(book["cash_sale"].sum())
    net_ptc_total = float(book["net_ptc"].sum())
    deposit_total = float(book["cms_deposit"].sum())
    closing_total = float(last["closing_cash"].sum())

    expected = opening_total + cash_sale_total + net_ptc_total - deposit_total
    difference = round(closing_total - expected, 2)

    return {
        "opening_total": opening_total,
        "opening_from_file": supplied_opening,
        "cash_sale_total": cash_sale_total,
        "net_ptc_total": net_ptc_total,
        "cms_deposit_total": deposit_total,
        "expected_closing": expected,
        "actual_closing": closing_total,
        "difference": difference,
        # Nil tolerance (rule 1). Rounded to paise because float addition over
        # 5,983 store-days accumulates representation error, not real error.
        "holds": abs(difference) < 0.005,
    }


def carry_forward_breaks(book: pd.DataFrame, date_from: date) -> pd.DataFrame:
    """(e) opening(D) must equal closing(D-1) for every store-date after the
    first. A break means the chain was rebuilt wrongly, not a data problem."""
    if book.empty:
        return pd.DataFrame(columns=["store", "business_date", "opening_cash",
                                     "previous_closing", "difference"])
    breaks = []
    for store, block in book.sort_values(["store", "business_date"]).groupby("store"):
        previous = None
        for row in block.itertuples():
            if previous is not None and abs(row.opening_cash - previous) >= 0.005:
                breaks.append({
                    "store": store, "business_date": row.business_date,
                    "opening_cash": row.opening_cash, "previous_closing": previous,
                    "difference": round(row.opening_cash - previous, 2),
                })
            previous = row.closing_cash
    if not breaks:
        return pd.DataFrame(columns=["store", "business_date", "opening_cash",
                                     "previous_closing", "difference"])
    return pd.DataFrame(breaks)


def run_checks(book: pd.DataFrame, opening: pd.DataFrame, date_from: date,
               date_to: date, threshold: float = DEFAULT_HIGH_CASH_THRESHOLD,
               min_dry_days: int = DEFAULT_NO_DEPOSIT_DAYS) -> R3aChecks:
    return R3aChecks(
        negative_closing=negative_closing(book),
        high_cash=high_cash(book, threshold, on_date=date_to),
        no_deposit_runs=no_deposit_runs(book, min_dry_days),
        agency_non_collection_dates=sorted(agency_non_collection_dates(book)),
        identity=estate_identity(book, opening, date_from, date_to),
        carry_forward_breaks=carry_forward_breaks(book, date_from),
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# What R3a can and cannot say
# ---------------------------------------------------------------------------

NO_INDEPENDENT_CONFIRMATION = (
    "R3a is a COMPUTED cash book. Opening cash is supplied for 1 July only and "
    "there is no daily declared cash balance to compare the computed closing "
    "against, so nothing here is independently confirmed. The checks below are "
    "internal-consistency checks: they can prove the book is impossible, but "
    "not that it is right. Supply a physical cash balance for a later date and "
    "that becomes the two-sided check."
)


def verdict(book: pd.DataFrame, checks: R3aChecks) -> str:
    if book.empty:
        return "Nothing loaded for this selection."
    parts = []
    if checks.identity.get("holds"):
        parts.append("the estate identity balances exactly")
    else:
        parts.append(f"THE ESTATE IDENTITY DOES NOT BALANCE — "
                     f"difference {checks.identity.get('difference'):,.2f}")
    if len(checks.carry_forward_breaks):
        parts.append(f"{len(checks.carry_forward_breaks)} carry-forward break(s)")
    else:
        parts.append("the carry-forward chain is unbroken")
    if len(checks.negative_closing):
        parts.append(f"{len(checks.negative_closing)} store-day(s) hold negative cash")
    return "; ".join(parts) + "."
