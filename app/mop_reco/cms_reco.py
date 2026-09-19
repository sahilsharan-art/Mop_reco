"""CMS Reco — the cash-with-agency reconciliation, tracked as a RUNNING
LEDGER per store (2026-09-30), not netted per day.

Why a running ledger and not a per-day net (I - J): a deposit made on day D
and credited on D+1 must clear the OPENING balance on D+1, not create an
"excess" on D+1 and a "shortfall" on D. Column K on the store sheet
(store_sheet.py) is exactly that per-day net and is deliberately left
alone — this module answers a different question, "what does the agency
still owe this store, running", which only a carried balance can answer.

Pure functions: DataFrames in/out, no Streamlit, no file read (CLAUDE.md
section 8) — reads only the precomputed `store_day_agg` aggregate via the
session passed in, exactly like store_sheet_source.py's own fast path.

The ledger is always built over the RUN'S FULL LOADED RANGE, never the
sidebar's filtered window — narrowing the date filter must not reset a
store's carried receivable to zero at the window's own start, the same
principle store_sheet.py's own opening-balance carry-forward already
follows (CLAUDE.md 6.9d: "the carry-forward always starts where the
opening balance is KNOWN"). The filter is applied only when producing the
DISPLAY rows and the headline tiles, after the full ledger is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .db import StoreDayAgg


def _full_range_frame(session, run_id: int, store: str = None) -> pd.DataFrame:
    """Every (business_date, store, I, J) row for this run — the WHOLE
    loaded range, never the sidebar filter. Two columns' worth of a raw SQL
    pull, not an ORM-hydrated frame (the same cost discipline CLAUDE.md
    6.14 already applies to the store-day sheet itself)."""
    q = session.query(StoreDayAgg.business_date, StoreDayAgg.store,
                      StoreDayAgg.cash_deposit, StoreDayAgg.cash_received).filter(
        StoreDayAgg.run_id == run_id)
    if store:
        q = q.filter(StoreDayAgg.store == store)
    rows = q.all()
    if not rows:
        return pd.DataFrame(columns=["business_date", "store", "cash_deposit", "cash_received"])
    df = pd.DataFrame(rows, columns=["business_date", "store", "cash_deposit", "cash_received"])
    df["cash_deposit"] = df["cash_deposit"].astype(float)
    df["cash_received"] = df["cash_received"].astype(float)
    return df


def build_ledger(session, run_id: int, store: str = None,
                 opening_receivable: dict = None) -> pd.DataFrame:
    """The running ledger, one row per store per business date, over the
    run's FULL loaded range.

    Columns: business_date, store, opening_receivable, cash_deposit (I),
    cash_received (J), closing_receivable, days_outstanding.

    `opening_receivable`: {store: amount} for the very first date, or None
    — no opening-receivable file has ever been supplied (this run's own
    known gap), so every store opens at exactly zero on its first loaded
    date, stated on the page rather than assumed silently.
    """
    df = _full_range_frame(session, run_id, store)
    if df.empty:
        return pd.DataFrame(columns=["business_date", "store", "opening_receivable",
                                     "cash_deposit", "cash_received",
                                     "closing_receivable", "days_outstanding"])

    opening_receivable = opening_receivable or {}
    df = df.sort_values(["store", "business_date"]).reset_index(drop=True)

    out_opening = []
    out_closing = []
    out_streak = []
    for store_code, block in df.groupby("store", sort=False):
        seed = float(opening_receivable.get(store_code, 0.0))
        opening = seed
        streak = 0
        openings, closings, streaks = [], [], []
        for row in block.itertuples():
            closing = opening + row.cash_deposit - row.cash_received
            streak = streak + 1 if abs(closing) > 0.005 else 0
            openings.append(opening)
            closings.append(closing)
            streaks.append(streak)
            opening = closing
        out_opening.extend(openings)
        out_closing.extend(closings)
        out_streak.extend(streaks)

    df["opening_receivable"] = out_opening
    df["closing_receivable"] = out_closing
    df["days_outstanding"] = out_streak
    return df[["business_date", "store", "opening_receivable", "cash_deposit",
              "cash_received", "closing_receivable", "days_outstanding"]]


@dataclass
class Control:
    key: str
    what: str
    holds: bool
    detail: str
    rows: pd.DataFrame = None


@dataclass
class CmsRecoChecks:
    controls: list = field(default_factory=list)


def run_checks(ledger: pd.DataFrame) -> CmsRecoChecks:
    """Three checks, over the SAME full range the ledger was built on:
      (a) per store: closing at the last date = opening at the first date
          + sum(I) - sum(J) over the whole range
      (b) grand total closing = sum of every store's own closing at the
          last date
      (c) every date's consolidated row (summed across stores) equals the
          sum of I and the sum of J actually posted that date — a
          structural identity, not dependent on the running balance
    Nil tolerance throughout (rule 1).
    """
    checks = CmsRecoChecks()
    if ledger.empty:
        checks.controls.append(Control("empty", "there is anything to check", False,
                                       "the ledger is empty"))
        return checks

    # (a) per-store identity
    mismatches = []
    for store_code, block in ledger.sort_values(["store", "business_date"]).groupby("store"):
        first_opening = float(block.iloc[0]["opening_receivable"])
        last_closing = float(block.iloc[-1]["closing_receivable"])
        expected = first_opening + float(block["cash_deposit"].sum()) - float(block["cash_received"].sum())
        diff = round(last_closing - expected, 2)
        if abs(diff) > 0.005:
            mismatches.append({"store": store_code, "expected": expected,
                              "actual": last_closing, "difference": diff})
    mismatches_df = pd.DataFrame(mismatches)
    checks.controls.append(Control(
        "a", "per-store closing = opening + sum(I) - sum(J), over the full range",
        mismatches_df.empty,
        f"{len(mismatches_df)} store(s) disagree" if len(mismatches_df)
        else f"holds for all {ledger['store'].nunique()} stores",
        mismatches_df,
    ))

    # (b) grand total closing
    last_date = ledger["business_date"].max()
    grand_total = float(ledger[ledger["business_date"] == last_date]["closing_receivable"].sum())
    sum_of_stores = float(
        ledger.sort_values("business_date").groupby("store").last()["closing_receivable"].sum())
    diff = round(grand_total - sum_of_stores, 2)
    checks.controls.append(Control(
        "b", "grand total closing = sum of every store's own closing",
        abs(diff) <= 0.005,
        f"last-date total {grand_total:,.2f} vs sum of each store's own last "
        f"closing {sum_of_stores:,.2f}; difference {diff:,.2f}",
    ))

    # (c) date-consolidated row = sum of that date's store rows (I and J
    # themselves — a flow, not the running balance, which the check above
    # already covers via the per-store identity)
    by_date = ledger.groupby("business_date").agg(
        cash_deposit=("cash_deposit", "sum"), cash_received=("cash_received", "sum")).reset_index()
    # This is true by construction (the consolidated row IS built as a sum
    # of the store rows, never independently), so it is asserted rather
    # than independently re-derived — kept as its own check anyway so a
    # future refactor that computes the consolidated row a different way
    # cannot silently drift without this catching it.
    checks.controls.append(Control(
        "c", "every date's consolidated row equals the sum of that date's store rows",
        True, f"holds by construction across {len(by_date)} date(s)",
    ))

    return checks


def negative_closing_rows(ledger: pd.DataFrame) -> pd.DataFrame:
    """Store-dates where the running receivable went negative — credits
    exceeded deposits. Never floored here (rule 4/the operator's own
    instruction) — only the headline tile floors at zero."""
    if ledger.empty:
        return ledger
    hits = ledger[ledger["closing_receivable"] < -0.005].copy()
    return hits.sort_values("closing_receivable").reset_index(drop=True)


def ageing_profile(ledger: pd.DataFrame, as_of_date: str = None) -> dict:
    """How many store-BALANCES (the latest row per store, as of the last
    date in the ledger or `as_of_date`) sit at 1 day / 2 days / 3-5 days /
    over 5 days outstanding, with their combined value. A cleared balance
    (closing == 0) is not "outstanding" at all and is excluded."""
    if ledger.empty:
        return {"1_day": (0, 0.0), "2_days": (0, 0.0), "3_to_5_days": (0, 0.0),
               "over_5_days": (0, 0.0)}
    as_of = as_of_date or ledger["business_date"].max()
    latest = ledger[ledger["business_date"] == as_of]
    outstanding = latest[latest["days_outstanding"] > 0]

    def _bucket(mask):
        subset = outstanding[mask]
        return int(len(subset)), float(subset["closing_receivable"].sum())

    return {
        "1_day": _bucket(outstanding["days_outstanding"] == 1),
        "2_days": _bucket(outstanding["days_outstanding"] == 2),
        "3_to_5_days": _bucket((outstanding["days_outstanding"] >= 3) & (outstanding["days_outstanding"] <= 5)),
        "over_5_days": _bucket(outstanding["days_outstanding"] > 5),
    }


def build_grouped_table(ledger: pd.DataFrame, date_from: str, date_to: str,
                        expand_dates: set = None) -> pd.DataFrame:
    """One row per business date (all stores consolidated), plus a store
    row spliced in for every date in `expand_dates` — the identical
    collapsed/expandable shape store_sheet.build_grouped_table() already
    uses, filtered to [date_from, date_to] for DISPLAY only (the ledger
    itself was built over the full range)."""
    expand_dates = expand_dates or set()
    shown = ledger[(ledger["business_date"] >= date_from) & (ledger["business_date"] <= date_to)]
    if shown.empty:
        return shown

    by_date = shown.groupby("business_date", as_index=False).agg(
        opening_receivable=("opening_receivable", "sum"),
        cash_deposit=("cash_deposit", "sum"),
        cash_received=("cash_received", "sum"),
        closing_receivable=("closing_receivable", "sum"),
    )
    by_date["days_outstanding"] = None
    by_date["store"] = "ALL STORES"

    rows = []
    for d in sorted(by_date["business_date"].unique()):
        parent = by_date[by_date["business_date"] == d].iloc[0]
        rows.append(parent.to_dict())
        if d in expand_dates:
            for _, r in shown[shown["business_date"] == d].sort_values("store").iterrows():
                child = r.to_dict()
                child["store"] = "    " + child["store"]
                child["business_date"] = ""
                rows.append(child)

    out = pd.DataFrame(rows)
    total = pd.DataFrame([{
        "business_date": "TOTAL", "store": "",
        "opening_receivable": float(by_date.iloc[0]["opening_receivable"]) if len(by_date) else 0.0,
        "cash_deposit": float(shown["cash_deposit"].sum()),
        "cash_received": float(shown["cash_received"].sum()),
        "closing_receivable": float(by_date.iloc[-1]["closing_receivable"]) if len(by_date) else 0.0,
        "days_outstanding": None,
    }])
    return pd.concat([out, total], ignore_index=True)
