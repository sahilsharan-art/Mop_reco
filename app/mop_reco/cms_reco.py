"""NOTE (2026-10-16): the CMS Reco PAGE now uses cms_blocks.py (one block per deposit date).
Only build_ledger() remains here, for store_tasks.cms_candidates.

CMS Reco — the cash-with-agency reconciliation, tracked as a RUNNING
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
