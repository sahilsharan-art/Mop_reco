from pathlib import Path
p = Path('app/mop_reco/store_sheet.py')
s = p.read_text(encoding='utf-8')

anchor = "def run_controls("
add = '''def summarise_by_store(sheet: pd.DataFrame) -> pd.DataFrame:
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
        row["float_amount"] = row["opening_balance"] + row["cash_deposit"]
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


''' + anchor
assert anchor in s, "run_controls anchor for insertion"
s = s.replace(anchor, add, 1)
p.write_text(s, encoding='utf-8')
print("summarise_by_store and grand_total_row added")
