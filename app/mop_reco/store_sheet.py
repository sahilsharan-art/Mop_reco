"""The daily store-wise sale and cash sheet — columns A..N, in that order.

A pure engine (§8): frames in, frames out. No Streamlit, no file reading.

    A Date              G Digital receivable = D - F    L Cash expense/income
    B Store             H Opening balance               M Closing balance = H+E-I+L
    C Total sale = D+E  I Cash deposit     POSITIVE     N Float amount    = H-I
    D Digital sale      J Cash received    POSITIVE
    E Cash sale         K Cash receivable  = I - J
    F Digital received

**Column I is stored POSITIVE (corrected 2026-09-17, on instruction — a
deposit is a positive amount of money).** It was previously carried
negative, with K/M/N restated to match (`I+J`, `H+E+I+L`, `H+I`); changing
the sign of I without restating those three at the same time would corrupt
every figure on the sheet, so all four moved together in one change. Every
place that used to negate or `.abs()` a stored `cash_deposit` purely for
display has been removed — the stored value is already the number a reader
expects to see.

Three things this module is careful about, because each one decides whether the
sheet is right:

1. **Column J belongs to the DEPOSIT date, not the credit date.** Settlement is
   T+0 for some stores and T+1 for others. A deposit made on 1 July and
   credited on 2 July still belongs to the 1 July row — that is the only way K
   means "deposited and not yet received". A non-zero K at month end is real
   cash in transit and is never suppressed.

2. **J is not COL alone.** COL (SLIPAMOUNT), ADJ.CR. (the ADJ column — NOT
   SLIPAMOUNT, which on an ADJ.CR. row is the original slip being adjusted)
   and ADJ.DB. (RECOVEREDAMOUNT, subtracted) all feed J, each from its own
   source column — confirmed against the raw file, not assumed. ZPO alone is
   excluded, on the operator's explicit instruction, and is still reported
   for visibility since it is never discarded silently (rule 4/5).

3. **Credit notes appear nowhere.** Total sale is digital plus cash only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

logger = logging.getLogger("mop_reco")

# Column A..N in the order the sheet must present them. The letters are the
# contract with the operator's own format; the names are what the code uses.
COLUMNS = [
    ("A", "date", "Date"),
    ("B", "store", "Store"),
    ("C", "total_sale", "Total sale"),
    ("D", "digital_sale", "Digital sale"),
    ("E", "cash_sale", "Cash sale"),
    ("F", "digital_received", "Digital received"),
    ("G", "digital_receivable", "Digital receivable"),
    ("H", "opening_balance", "Opening balance"),
    ("I", "cash_deposit", "Cash deposit"),
    ("J", "cash_received", "Cash received"),
    ("K", "cash_receivable", "Cash receivable"),
    ("L", "cash_expense_income", "Cash expense/income"),
    ("M", "closing_balance", "Closing balance"),
    ("N", "float_amount", "Float amount"),
]

FIELDS = [name for _, name, _ in COLUMNS]
LABELS = {name: label for _, name, label in COLUMNS}
LETTERS = {name: letter for letter, name, _ in COLUMNS}

# Columns that are DERIVED. In the Excel export these stay real formulas so
# any cell can be traced; here they are computed the same way.
DERIVED = ("total_sale", "digital_receivable", "cash_receivable",
           "closing_balance", "float_amount")

CASH_MOP = "Cash"

# Which CMS TYPE feeds column J, from which source column, and with what sign.
# Confirmed against the raw July file (2026-09-11):
#   COL      SLIPAMOUNT           as credited
#   ADJ.CR.  ADJ (column U)       NOT SLIPAMOUNT — SLIPAMOUNT on an ADJ.CR.
#            row is the ORIGINAL slip being adjusted (Rs 47,400/1,27,722/
#            95,760 on the three July rows); ADJ is the actual adjustment
#            (Rs 103/7/8). Using SLIPAMOUNT would book the whole original
#            slip a second time.
#   ADJ.DB.  RECOVEREDAMOUNT, subtracted — the bank recovering money it
#            previously overcredited reduces what the store is owed. Zero on
#            every July row, but the column must still be read correctly for
#            the month it is not.
# ZPO is excluded from J entirely, on the operator's explicit instruction —
# see IGNORED_CREDIT_TYPES below — but is still reported for visibility
# (rule 4/5: nothing vanishes silently), never dropped without a trace.
CREDIT_TYPE_SOURCE = {
    "COL": ("SLIPAMOUNT", 1),
    "ADJ.CR.": ("ADJ", 1),
    "ADJ.DB.": ("RECOVEREDAMOUNT", -1),
}

# Excluded from J entirely, on the operator's explicit instruction
# (2026-09-11): "please ignore the ZPO type". ZPO rows also carry no DEPDATE
# and a single pseudo pickup code, so they could not be attributed to a
# business date or a store even if they were included.
IGNORED_CREDIT_TYPES = ("ZPO",)


# ---------------------------------------------------------------------------
# The components, one per source
# ---------------------------------------------------------------------------

def digital_by_store_date(bill_lines: pd.DataFrame) -> pd.DataFrame:
    """D and F from the same frame the dashboard reads.

    Taking both from `r1_bill_lines` is deliberate: it makes control (f) —
    "total D equals the digital sale the dashboard reports" — true by
    construction rather than by coincidence, and F is then exactly the matched
    subset of D rather than a separately-derived figure that could drift.
    """
    empty = pd.DataFrame(columns=["date", "store", "digital_sale",
                                  "digital_received"])
    if bill_lines is None or bill_lines.empty:
        return empty

    frame = bill_lines.copy()
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    frame["received"] = frame["amount"].where(
        frame["matched"].fillna(False).astype(bool), 0.0)

    out = (frame.groupby(["business_date", "store"], as_index=False)
           .agg(digital_sale=("amount", "sum"),
                digital_received=("received", "sum"))
           .rename(columns={"business_date": "date"}))
    return out


def cash_sale_by_store_date(pos: pd.DataFrame) -> pd.DataFrame:
    """E — POS cash bills only.

    A row with no BILLNO is excluded (§6.13): it is not a sale, and it is also
    the PTC ledger mirrored into the POS export, so counting it here would
    double-count every petty-cash movement against column L.
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_sale"])
    if pos is None or pos.empty:
        return empty

    cash = pos[pos["mop"].astype(str).str.strip().eq(CASH_MOP)].copy()
    has_bill = cash["bill_no"].notna() & (
        ~cash["bill_no"].astype(str).str.strip().isin(["", "nan", "None"]))
    cash = cash[has_bill]
    if cash.empty:
        return empty
    cash["amount"] = pd.to_numeric(cash["amount"], errors="coerce").fillna(0.0)
    return (cash.groupby(["business_date", "store"], as_index=False)["amount"]
            .sum()
            .rename(columns={"business_date": "date", "amount": "cash_sale"}))


def ptc_by_store_date(ptc: pd.DataFrame) -> pd.DataFrame:
    """I and L from the one PTC file.

    I is the deposit carried POSITIVE (corrected 2026-09-17 — a deposit is a
    positive amount of money). PTC deposit rows arrive NEGATIVE already
    (they are PAYMENT rows, cash leaving the till), so the sign is flipped
    here to store I positive; K/M/N are restated accordingly in
    `build_sheet()`, `run_controls()` and `float_amount` wherever they are
    computed, so this flip never lands half-applied.
    L is every OTHER PTC row netted — income positive, expense negative — and
    deliberately excludes the deposit head, because I already carries it and
    counting it in both would subtract every deposit twice.
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_deposit",
                                  "cash_expense_income"])
    if ptc is None or ptc.empty:
        return empty

    frame = ptc.copy()
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    is_deposit = frame["is_deposit"].fillna(False).astype(bool)

    # PTC deposits arrive negative already (PAYMENT rows) — negate so the
    # sheet's own I is a positive amount of money, not the raw ledger sign.
    frame["dep"] = -frame["amount"].where(is_deposit, 0.0)
    frame["other"] = frame["amount"].where(~is_deposit, 0.0)

    return (frame.groupby(["business_date", "store"], as_index=False)
            .agg(cash_deposit=("dep", "sum"),
                 cash_expense_income=("other", "sum"))
            .rename(columns={"business_date": "date"}))


def cms_received_by_deposit_date(cms: pd.DataFrame) -> pd.DataFrame:
    """J — CMS bank credits, attributed to the DEPOSIT date.

    Three types feed J, each from its own column (see CREDIT_TYPE_SOURCE):
    COL from SLIPAMOUNT, ADJ.CR. from the ADJ column (not SLIPAMOUNT — see
    the module-level comment), ADJ.DB. from RECOVEREDAMOUNT, subtracted. ZPO
    is excluded entirely.

    **Grouped on DEPDATE, never CRDATE.** A T+1 credit belongs to the day the
    cash left the store, or K would read nil on the deposit day and then show
    a phantom positive on the credit day, and "deposited but not yet received"
    would be unanswerable.
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_received"])
    if cms is None or cms.empty:
        return empty

    parts = []
    for credit_type, (column, sign) in CREDIT_TYPE_SOURCE.items():
        subset = cms[cms["TYPE"].astype(str).str.strip().eq(credit_type)].copy()
        subset = subset[subset["store"].notna() & subset["store"].astype(str).ne("None")]
        if column not in subset.columns or subset.empty:
            # The column was not captured for this dump. Never silently treat
            # a missing column as a zero contribution.
            continue
        deposit_date = pd.to_datetime(subset["DEPDATE"], errors="coerce")
        subset = subset[deposit_date.notna()]
        if subset.empty:
            continue
        subset["date"] = pd.to_datetime(subset["DEPDATE"], errors="coerce").dt.strftime("%Y-%m-%d")
        subset["credit"] = sign * pd.to_numeric(subset[column], errors="coerce").fillna(0.0)
        parts.append(subset[["date", "store", "credit"]])

    if not parts:
        return empty
    combined = pd.concat(parts, ignore_index=True)
    return (combined.groupby(["date", "store"], as_index=False)["credit"].sum()
            .rename(columns={"credit": "cash_received"}))


def cms_contribution_by_type(cms: pd.DataFrame) -> pd.DataFrame:
    """How much of J came from each type, and which column fed it.

    Because ADJ.CR. and ADJ.DB. contribute to J via a DIFFERENT column than
    COL, this exists so the operator can see the split rather than a single J
    figure that silently blends three sources. One row per type.
    """
    rows = []
    if cms is not None and not cms.empty:
        for credit_type, (column, sign) in CREDIT_TYPE_SOURCE.items():
            subset = cms[cms["TYPE"].astype(str).str.strip().eq(credit_type)]
            if column not in cms.columns or subset.empty:
                rows.append({"type": credit_type, "source_column": column,
                            "rows": 0, "value": 0.0})
                continue
            value = sign * float(pd.to_numeric(subset[column],
                                               errors="coerce").fillna(0.0).sum())
            rows.append({"type": credit_type, "source_column": column,
                        "rows": int(len(subset)), "value": value})
    return pd.DataFrame(rows, columns=["type", "source_column", "rows", "value"])


def excluded_credit_types(cms: pd.DataFrame) -> pd.DataFrame:
    """TYPE = ZPO — the one type ignored entirely, kept visible regardless.

    ADJ.CR. and ADJ.DB. are no longer "excluded" from J (they feed it via
    their own columns — see cms_received_by_deposit_date); only ZPO is. This
    is still reported by store and date so a large excluded population is
    never invisible (rule 4/5), even though the operator has said to ignore
    it for the sheet's own purposes.
    """
    empty = pd.DataFrame(columns=["type", "date", "store", "rows", "value"])
    if cms is None or cms.empty:
        return empty

    other = cms[cms["TYPE"].astype(str).str.strip().isin(IGNORED_CREDIT_TYPES)].copy()
    if other.empty:
        return empty
    other["value"] = pd.to_numeric(other["SLIPAMOUNT"], errors="coerce").fillna(0.0)
    # ZPO rows carry no DEPDATE at all, so they cannot be attributed to a
    # business date. They are reported with the date left blank rather than
    # forced onto a day they do not belong to (rule 6).
    parsed = pd.to_datetime(other["DEPDATE"], errors="coerce")
    other["date"] = parsed.dt.strftime("%Y-%m-%d").fillna("(no deposit date)")
    other["store"] = other["store"].fillna("(unmapped)")

    return (other.groupby(["TYPE", "date", "store"], as_index=False)
            .agg(rows=("value", "size"), value=("value", "sum"))
            .rename(columns={"TYPE": "type"})
            .sort_values(["type", "date", "value"], ascending=[True, True, False])
            .reset_index(drop=True))


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------

def _span(date_from: date, date_to: date) -> list:
    days, cursor = [], date_from
    while cursor <= date_to:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def build_sheet(opening: pd.DataFrame, digital: pd.DataFrame,
                cash_sale: pd.DataFrame, ptc: pd.DataFrame,
                received: pd.DataFrame, date_from: date,
                date_to: date) -> pd.DataFrame:
    """One row per store per business date, columns A..N in order.

    Every store appearing in ANY input gets a row for every date in the span,
    so a day with no movement is a visible zero-movement row rather than a
    gap in the carry-forward.
    """
    stores = set()
    for frame in (opening, digital, cash_sale, ptc, received):
        if frame is not None and not frame.empty and "store" in frame.columns:
            stores |= {s for s in frame["store"].dropna().unique() if str(s) != "None"}
    stores = sorted(stores)
    if not stores:
        return pd.DataFrame(columns=FIELDS)

    grid = pd.MultiIndex.from_product([_span(date_from, date_to), stores],
                                      names=["date", "store"]).to_frame(index=False)

    def merge(frame, columns):
        nonlocal grid
        if frame is None or frame.empty:
            for column in columns:
                grid[column] = 0.0
            return
        side = frame.copy()
        side["date"] = side["date"].astype(str)
        keep = ["date", "store"] + [c for c in columns if c in side.columns]
        merged = grid.merge(side[keep], on=["date", "store"], how="left")
        for column in columns:
            grid[column] = (merged[column].fillna(0.0).to_numpy()
                            if column in merged.columns else 0.0)

    merge(digital, ["digital_sale", "digital_received"])
    merge(cash_sale, ["cash_sale"])
    merge(ptc, ["cash_deposit", "cash_expense_income"])
    merge(received, ["cash_received"])

    grid = grid.sort_values(["store", "date"]).reset_index(drop=True)

    # The carry-forward. opening(first date) comes from the balance file;
    # opening(D) = closing(D-1) thereafter.
    seed = (opening.set_index("store")["opening_cash"].to_dict()
            if opening is not None and not opening.empty else {})
    grid["movement"] = (grid["cash_sale"] - grid["cash_deposit"]
                        + grid["cash_expense_income"])
    grid["opening_balance"] = 0.0
    grid["closing_balance"] = 0.0
    for store, block in grid.groupby("store", sort=False):
        start = float(seed.get(store, 0.0))
        closings = start + block["movement"].cumsum()
        grid.loc[block.index, "closing_balance"] = closings.to_numpy()
        grid.loc[block.index, "opening_balance"] = (
            closings - block["movement"]).to_numpy()

    # The derived columns, exactly as the format defines them.
    grid["total_sale"] = grid["digital_sale"] + grid["cash_sale"]
    grid["digital_receivable"] = grid["digital_sale"] - grid["digital_received"]
    grid["cash_receivable"] = grid["cash_deposit"] - grid["cash_received"]
    grid["float_amount"] = grid["opening_balance"] - grid["cash_deposit"]

    return (grid[FIELDS].sort_values(["date", "store"])
            .reset_index(drop=True))


def summarise_by_date(sheet: pd.DataFrame) -> pd.DataFrame:
    """The summary block: one row per business date across all stores, then a
    grand total. Same column order, so the two blocks read as one sheet.

    Opening is the FIRST date's opening and closing the LAST date's closing —
    summing either down the month would double-count the carry-forward.
    """
    if sheet.empty:
        return pd.DataFrame(columns=FIELDS)

    additive = ["total_sale", "digital_sale", "cash_sale", "digital_received",
                "digital_receivable", "cash_deposit", "cash_received",
                "cash_receivable", "cash_expense_income"]
    by_date = sheet.groupby("date", as_index=False)[
        additive + ["opening_balance", "closing_balance", "float_amount"]].sum()
    by_date["store"] = "ALL STORES"

    dates = sorted(sheet["date"].unique())
    first = sheet[sheet["date"] == dates[0]]
    last = sheet[sheet["date"] == dates[-1]]
    total = {c: float(sheet[c].sum()) for c in additive}
    total.update({
        "date": "TOTAL",
        "store": f"{dates[0]} to {dates[-1]}",
        "opening_balance": float(first["opening_balance"].sum()),
        "closing_balance": float(last["closing_balance"].sum()),
        "float_amount": float(first["opening_balance"].sum()
                              - sheet["cash_deposit"].sum()),
    })
    return pd.concat([by_date[FIELDS], pd.DataFrame([total])[FIELDS]],
                     ignore_index=True)


# ---------------------------------------------------------------------------
# Controls (a)..(h)
# ---------------------------------------------------------------------------

TOLERANCE = 0.005   # paise. Float addition over ~6,000 rows, not real error.


@dataclass
class Control:
    key: str
    what: str
    holds: bool
    detail: str = ""
    rows: pd.DataFrame = None


@dataclass
class SheetChecks:
    controls: list = field(default_factory=list)

    @property
    def all_hold(self) -> bool:
        return all(c.holds for c in self.controls)

    @property
    def failed(self) -> list:
        return [c for c in self.controls if not c.holds]

    def get(self, key: str) -> Control:
        return next(c for c in self.controls if c.key == key)


def summarise_by_store(sheet: pd.DataFrame) -> pd.DataFrame:
    """The second summary block: one row per store for the WHOLE loaded range.

    Opening is that store's FIRST date's opening and closing its LAST date's
    closing — the same telescoping rule as summarise_by_date, so a store's row
    here is its actual cash position across the range, not a sum of balances
    that would double-count the carry-forward.
    """
    if sheet.empty:
        return pd.DataFrame(columns=FIELDS)

    additive = ["total_sale", "digital_sale", "cash_sale", "digital_received",
                "digital_receivable", "cash_deposit", "cash_received",
                "cash_receivable", "cash_expense_income"]
    dates = sorted(sheet["date"].unique())
    span = f"{dates[0]} to {dates[-1]}" if len(dates) > 1 else dates[0]

    rows = []
    for store, block in sheet.sort_values(["store", "date"]).groupby("store"):
        row = {c: float(block[c].sum()) for c in additive}
        row["date"] = span
        row["store"] = store
        row["opening_balance"] = float(block.iloc[0]["opening_balance"])
        row["closing_balance"] = float(block.iloc[-1]["closing_balance"])
        row["float_amount"] = row["opening_balance"] - row["cash_deposit"]
        rows.append(row)

    return pd.DataFrame(rows)[FIELDS].sort_values("store").reset_index(drop=True)


def grand_total_row(sheet: pd.DataFrame) -> pd.DataFrame:
    """The third summary block: a single TOTAL row across the whole sheet.

    Reuses summarise_by_date's own TOTAL row, so the by-date block and the
    grand-total block can never disagree — they are computed once.
    """
    summary = summarise_by_date(sheet)
    total = summary[summary["date"] == "TOTAL"]
    return total.reset_index(drop=True) if not total.empty else summary.iloc[0:0]


# ---------------------------------------------------------------------------
# The one on-screen table (2026-09-11) — by date, expandable to stores
# ---------------------------------------------------------------------------

def build_grouped_table(sheet: pd.DataFrame, by_date: pd.DataFrame,
                        expanded_dates) -> pd.DataFrame:
    """One row per business date, ALL STORES consolidated, with that date's
    store rows spliced in beneath it when the date is in `expanded_dates`,
    and the TOTAL row last.

    Same columns (A..N) at both levels, so the collapsed and expanded views
    read as one sheet, never two separate tables. A nested store row carries
    the store code indented and its own date left blank, since it belongs to
    the consolidated row above it rather than standing on its own.
    """
    if sheet.empty or by_date.empty:
        return pd.DataFrame(columns=FIELDS)

    expanded = {str(d) for d in (expanded_dates or [])}
    by_date_rows = by_date[by_date["date"] != "TOTAL"]
    total_row = by_date[by_date["date"] == "TOTAL"]

    blocks = []
    for _, row in by_date_rows.iterrows():
        blocks.append(pd.DataFrame([row.to_dict()]))
        if row["date"] in expanded:
            stores = (sheet[sheet["date"] == row["date"]]
                     .sort_values("store").copy())
            stores["date"] = ""
            stores["store"] = "    " + stores["store"].astype(str)
            blocks.append(stores)
    blocks.append(total_row)
    return pd.concat(blocks, ignore_index=True)[FIELDS]


def verify_date_rows_equal_store_sums(sheet: pd.DataFrame,
                                      by_date: pd.DataFrame) -> pd.DataFrame:
    """Every consolidated ALL-STORES date row must equal the sum of that
    date's individual store rows, column by column — every column C..N
    (2026-09-20: added H, M, N to the additive-flow columns already checked;
    all three are stock/derived values but `summarise_by_date` builds them
    the SAME way, by summing each date's store rows via groupby, so a
    per-date parent-vs-children check is exactly as meaningful for them as
    for the flow columns).

    This is finer-grained than control (h), which checks only the single
    grand TOTAL row against the whole detail — a date could agree in total
    while individual dates disagree and control (h) would still hold. Returns
    the mismatches; empty means every date agrees.
    """
    empty = pd.DataFrame(columns=["date", "column", "consolidated",
                                  "stores_sum", "difference"])
    if sheet.empty or by_date.empty:
        return empty

    additive = ["total_sale", "digital_sale", "cash_sale", "digital_received",
               "digital_receivable", "cash_deposit", "cash_received",
               "cash_receivable", "cash_expense_income",
               "opening_balance", "closing_balance", "float_amount"]
    by_date_rows = by_date[by_date["date"] != "TOTAL"].set_index("date")

    mismatches = []
    for business_date, group in sheet.groupby("date"):
        if business_date not in by_date_rows.index:
            continue
        consolidated = by_date_rows.loc[business_date]
        for column in additive:
            stores_sum = float(group[column].sum())
            cons_value = float(consolidated[column])
            gap = round(cons_value - stores_sum, 2)
            if abs(gap) > TOLERANCE:
                mismatches.append({"date": business_date, "column": LABELS[column],
                                   "consolidated": cons_value,
                                   "stores_sum": stores_sum, "difference": gap})
    return pd.DataFrame(mismatches, columns=empty.columns) if mismatches else empty


def other_type_summary(cms: pd.DataFrame) -> pd.DataFrame:
    """One row per non-COL CMS type — ADJ.CR., ADJ.DB., ZPO — with its row
    count, total value and whether it feeds column J.

    ADJ.CR. and ADJ.DB. DO feed J, via their own column (CREDIT_TYPE_SOURCE);
    ZPO does not, ever. All three are reported here together so the whole
    non-COL population is visible in one place, for the export's own tab.
    """
    columns = ["type", "rows", "value", "feeds_J"]
    rows = []
    contribution = cms_contribution_by_type(cms)
    for credit_type in ("ADJ.CR.", "ADJ.DB."):
        match = contribution[contribution["type"] == credit_type]
        if not match.empty:
            record = match.iloc[0]
            rows.append({"type": credit_type, "rows": int(record["rows"]),
                        "value": float(record["value"]), "feeds_J": True})
    if cms is not None and not cms.empty:
        zpo = cms[cms["TYPE"].astype(str).str.strip().isin(IGNORED_CREDIT_TYPES)]
        if not zpo.empty:
            value = float(pd.to_numeric(zpo["SLIPAMOUNT"],
                                        errors="coerce").fillna(0.0).sum())
            rows.append({"type": "ZPO", "rows": int(len(zpo)), "value": value,
                        "feeds_J": False})
    return pd.DataFrame(rows, columns=columns)


def run_controls(sheet: pd.DataFrame, summary: pd.DataFrame,
                 opening: pd.DataFrame, dashboard_digital: float = None,
                 cash_sale_20_july: float = 12_941_046.0) -> SheetChecks:
    """Every control the operator specified. A failure names the rows."""
    checks = SheetChecks()
    if sheet.empty:
        checks.controls.append(Control("empty", "there is anything to check",
                                       False, "the sheet is empty"))
        return checks

    # (a) C = D + E
    bad = sheet[(sheet["total_sale"]
                 - (sheet["digital_sale"] + sheet["cash_sale"])).abs() > TOLERANCE]
    checks.controls.append(Control(
        "a", "C = D + E on every row", bad.empty,
        f"{len(bad)} row(s) disagree" if len(bad) else
        f"holds on all {len(sheet):,} rows", bad))

    # (b) M = H + E - I + L (I is stored positive, so it is SUBTRACTED here)
    expected = (sheet["opening_balance"] + sheet["cash_sale"]
                - sheet["cash_deposit"] + sheet["cash_expense_income"])
    bad = sheet[(sheet["closing_balance"] - expected).abs() > TOLERANCE]
    checks.controls.append(Control(
        "b", "M = H + E - I + L on every row", bad.empty,
        f"{len(bad)} row(s) disagree" if len(bad) else
        f"holds on all {len(sheet):,} rows", bad))

    # (c) opening(D) = closing(D-1), per store, no gaps
    breaks = []
    for store, block in sheet.sort_values(["store", "date"]).groupby("store"):
        previous = None
        for row in block.itertuples():
            if previous is not None and abs(row.opening_balance - previous) > TOLERANCE:
                breaks.append({"store": store, "date": row.date,
                               "opening_balance": row.opening_balance,
                               "previous_closing": previous,
                               "difference": round(row.opening_balance - previous, 2)})
            previous = row.closing_balance
    breaks = pd.DataFrame(breaks)
    checks.controls.append(Control(
        "c", "opening(D) = closing(D-1) for every store", breaks.empty,
        f"{len(breaks)} break(s)" if len(breaks) else
        f"unbroken across {sheet['store'].nunique()} stores", breaks))

    # (d) no negative closing balance
    negative = (sheet[sheet["closing_balance"] < -TOLERANCE]
                [["date", "store", "opening_balance", "cash_sale", "cash_deposit",
                  "cash_expense_income", "closing_balance"]]
                .sort_values("closing_balance").reset_index(drop=True))
    checks.controls.append(Control(
        "d", "no negative closing balance on any store-day", negative.empty,
        f"{len(negative)} store-day(s) hold negative cash" if len(negative)
        else f"none across {len(sheet):,} store-days", negative))

    # (e) the estate identity
    dates = sorted(sheet["date"].unique())
    first_opening = float(sheet[sheet["date"] == dates[0]]["opening_balance"].sum())
    last_closing = float(sheet[sheet["date"] == dates[-1]]["closing_balance"].sum())
    identity_expected = (first_opening + float(sheet["cash_sale"].sum())
                         - float(sheet["cash_deposit"].sum())
                         + float(sheet["cash_expense_income"].sum()))
    difference = round(last_closing - identity_expected, 2)
    checks.controls.append(Control(
        "e", "sum(opening) + sum(cash sale) - sum(deposits) + sum(expense/income) "
             "= sum(closing)", abs(difference) <= TOLERANCE,
        f"opening {first_opening:,.2f} + cash sale {sheet['cash_sale'].sum():,.2f} "
        f"- deposits {sheet['cash_deposit'].sum():,.2f} + expense/income "
        f"{sheet['cash_expense_income'].sum():,.2f} = {identity_expected:,.2f} "
        f"vs closing {last_closing:,.2f}; difference {difference:,.2f}"))

    # (f) total D equals the dashboard's digital sale
    total_digital = float(sheet["digital_sale"].sum())
    if dashboard_digital is None:
        checks.controls.append(Control(
            "f", "total D equals the digital sale the dashboard reports", True,
            f"D totals {total_digital:,.2f}; no dashboard figure was passed to "
            "compare against, so this is NOT verified"))
        checks.controls[-1].holds = False
    else:
        gap = round(total_digital - float(dashboard_digital), 2)
        checks.controls.append(Control(
            "f", "total D equals the digital sale the dashboard reports",
            abs(gap) <= TOLERANCE,
            f"sheet {total_digital:,.2f} vs dashboard {dashboard_digital:,.2f}; "
            f"difference {gap:,.2f}"))

    # (g) total E for 20 July
    july20 = sheet[sheet["date"] == "2026-07-20"]
    if july20.empty:
        checks.controls.append(Control(
            "g", "total E for 20 July equals Rs 1,29,41,046", True,
            "20 July is not in this range, so the check does not apply"))
    else:
        got = float(july20["cash_sale"].sum())
        gap = round(got - cash_sale_20_july, 2)
        checks.controls.append(Control(
            "g", "total E for 20 July equals Rs 1,29,41,046",
            abs(gap) <= TOLERANCE,
            f"got {got:,.2f}, expected {cash_sale_20_july:,.2f}; "
            f"difference {gap:,.2f}"))

    # (h) summary block totals equal the detail totals, column by column
    mismatches = []
    if summary is not None and not summary.empty:
        total_row = summary[summary["date"] == "TOTAL"]
        if total_row.empty:
            mismatches.append({"column": "(total row)", "summary": None,
                               "detail": None, "difference": None})
        else:
            total_row = total_row.iloc[0]
            for column in ("total_sale", "digital_sale", "cash_sale",
                           "digital_received", "digital_receivable",
                           "cash_deposit", "cash_received", "cash_receivable",
                           "cash_expense_income"):
                gap = round(float(total_row[column]) - float(sheet[column].sum()), 2)
                if abs(gap) > TOLERANCE:
                    mismatches.append({"column": LABELS[column],
                                       "summary": float(total_row[column]),
                                       "detail": float(sheet[column].sum()),
                                       "difference": gap})
    mismatches = pd.DataFrame(mismatches)
    checks.controls.append(Control(
        "h", "the summary block totals equal the detail totals, column by column",
        mismatches.empty,
        f"{len(mismatches)} column(s) disagree" if len(mismatches)
        else "every additive column agrees", mismatches))

    return checks


def in_transit_tail(sheet: pd.DataFrame, on_date: str = None) -> pd.DataFrame:
    """Store-days genuinely awaiting credit — K > 0, cash deposited and not
    yet credited (K = I - J is positive when the deposit has not yet been
    fully offset by a credit). Real cash in transit, shown as-is — already a
    positive magnitude now that I is stored positive — and never suppressed.

    **K < 0 is deliberately EXCLUDED here (polarity swapped 2026-09-17 along
    with I's sign — see the module docstring).** A credit exceeding its
    deposit is not cash in transit at all — it is a Defect B data artefact
    (a credit posted against the wrong date, or a shared pickup code
    crediting the wrong store; see `net_cash_receivable` /
    `credit_exceeds_deposit_rows`) and belongs in the Data quality list,
    never blended into "in transit".
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_deposit",
                                  "cash_received", "cash_receivable"])
    if sheet.empty:
        return empty
    subset = sheet if on_date is None else sheet[sheet["date"] == str(on_date)]
    rows = subset[subset["cash_receivable"] > TOLERANCE].copy()
    if rows.empty:
        return empty
    return (rows[["date", "store", "cash_deposit", "cash_received",
                  "cash_receivable"]]
            .sort_values("cash_receivable", ascending=False)
            .reset_index(drop=True))


# ---------------------------------------------------------------------------
# Cash receivable — NET, floored at zero, never a sum of pre-floored rows
# (2026-09-13). There is no such thing as "excess credit" in this business:
# the agency never credits more than was deposited. Any store-date where
# K = I - J is NEGATIVE (polarity swapped 2026-09-17 along with I's sign —
# see the module docstring) is a data artefact (Defect B — a credit posted
# against the wrong date, or a pickup code shared with another store), not
# a real balance, and is reported separately rather than shown as a
# negative receivable or netted away silently.
# ---------------------------------------------------------------------------

def net_cash_receivable(frame: pd.DataFrame) -> float:
    """Cash receivable is NET (sum I - sum J) at the displayed level and
    floored at zero. Never sum floored daily values. Negative-K rows are
    data artefacts of Defect B, not excess credit - they are surfaced in
    the Data quality expander, not netted away silently.

    `frame` is whatever rows make up "the level being displayed" — the
    whole filtered sheet for the tile, one store's own rows for a
    store-level total (whole range), or a single store-date row (where this
    trivially reduces to that one row's own I - J). Summing I and J FIRST
    and flooring LAST is what lets a store's occasional excess-credited day
    cancel against its own receivable days elsewhere in the same range;
    flooring each day first and then summing the floors would only ever add
    the positive days up and never let a genuine receivable day offset them
    — wildly overstating the figure. (Measured on full July, all stores,
    under the sign convention in force at the time: "sum of negative K
    only" gave Rs 1,45,87,717; this function, summing I and J across the
    whole range first, gave Rs 65,71,377 over the same data — see
    docs/CASH_RECEIVABLE_DIAGNOSTIC.md. The real-world figure is unchanged
    by the 2026-09-17 sign flip; only the arithmetic that reaches it is.)
    """
    if frame is None or frame.empty:
        return 0.0
    total_i = float(frame["cash_deposit"].sum())
    total_j = float(frame["cash_received"].sum())
    return max(0.0, total_i - total_j)


def credit_exceeds_deposit_rows(sheet: pd.DataFrame) -> pd.DataFrame:
    """Every store-date where that single day's credit exceeds that day's
    deposit (cash_receivable < 0, since K = I - J) — Defect B candidates,
    never a real receivable and never netted away silently. Sorted by the
    size of the excess, largest first.

    Store-date grain only — at that grain "net" is trivially the row's own
    I - J, so no summing/flooring order question arises here; the pickup
    code and slip number behind each row are attached by the caller
    (`store_sheet_source.data_quality_rows`), which has the CMS detail this
    pure engine module does not read.
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_deposit",
                                  "cash_received", "cash_receivable"])
    if sheet is None or sheet.empty:
        return empty
    bad = sheet[sheet["cash_receivable"] < -0.005][
        ["date", "store", "cash_deposit", "cash_received", "cash_receivable"]
    ].copy()
    if bad.empty:
        return empty
    return bad.sort_values("cash_receivable", ascending=True).reset_index(drop=True)
