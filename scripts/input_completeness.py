"""Per-date input completeness for the whole loaded range.

For every business date and every source: was a file found, how many rows were
read, what was the control total — and is any date sharply out of line with its
neighbours? A date where the Paytm file is absent or short makes every bill that
day unmatched, and that must not be invisible.

Read-only. Run from the project root:  python scripts/input_completeness.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.formatting import money_table

WAREHOUSE = ROOT / "warehouse"

# source -> (parquet folder, the column that carries value)
SOURCES = {
    "pos_bill_wise": ("pos_bill_wise", "amount"),
    "paytm_settlement": ("paytm_settlement", "amount"),
    "axis_edc": ("axis_edc", "GROSS_AMT"),
    "bajaj": ("bajaj", "Transaction Amount"),
}

# A day is flagged when its row count falls below this share of the median of
# the surrounding days. 0.6 catches a genuinely short file without firing on
# ordinary weekday/weekend variation.
SHORT_RATIO = 0.6

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)


def read_source(folder: str, value_col: str) -> pd.DataFrame:
    rows = []
    for path in sorted((WAREHOUSE / folder).glob("*.parquet")):
        if path.stem.startswith("_"):
            continue
        df = pd.read_parquet(path)
        value = float(df[value_col].astype(float).sum()) if value_col in df.columns else 0.0
        rows.append({"business_date": path.stem, "rows": len(df), "value": value})
    return pd.DataFrame(rows)


per_source = {name: read_source(folder, col) for name, (folder, col) in SOURCES.items()}

all_dates = sorted({d for df in per_source.values() for d in df["business_date"]})
pos_dates = set(per_source["pos_bill_wise"]["business_date"])

print("=" * 100)
print("PER-DATE INPUT COMPLETENESS")
print("=" * 100)

records = []
for d in all_dates:
    row = {"business_date": d}
    for name, df in per_source.items():
        hit = df[df["business_date"] == d]
        row[f"{name}_rows"] = int(hit.iloc[0]["rows"]) if not hit.empty else 0
        row[f"{name}_value"] = float(hit.iloc[0]["value"]) if not hit.empty else 0.0
    records.append(row)

table = pd.DataFrame(records)

# Flag: source absent on a date where POS has bills, or sharply short.
flags = []
for name in SOURCES:
    col = f"{name}_rows"
    series = table.set_index("business_date")[col]
    # Median of the other days, so one bad day doesn't hide inside its own median.
    for d in table["business_date"]:
        value = series[d]
        others = series.drop(index=d)
        others = others[others > 0]
        median = others.median() if len(others) else 0

        if value == 0 and d in pos_dates and name != "pos_bill_wise":
            flags.append({"business_date": d, "source": name, "rows": 0,
                          "median_other_days": int(median),
                          "flag": "SOURCE MISSING for a date that has POS bills"})
        elif value and median and value < median * SHORT_RATIO:
            flags.append({"business_date": d, "source": name, "rows": int(value),
                          "median_other_days": int(median),
                          "flag": f"SHORT — {value / median * 100:.0f}% of the median day"})

display = table.copy()
for name in SOURCES:
    display[f"{name}_value"] = display[f"{name}_value"].map(money_table)
print(display.to_string(index=False))

print("\n" + "=" * 100)
print("FLAGGED DATES")
print("=" * 100)
if flags:
    print(pd.DataFrame(flags).sort_values(["source", "business_date"]).to_string(index=False))
else:
    print("No date has a missing source, and no source is sharply short on any date.")

print("\n" + "=" * 100)
print("COVERAGE SUMMARY")
print("=" * 100)
for name in SOURCES:
    col = f"{name}_rows"
    present = int((table[col] > 0).sum())
    print(f"  {name:<20} present on {present:>2} of {len(all_dates)} dates · "
          f"{int(table[col].sum()):>9,} rows total")

out = ROOT / "logs" / "input_completeness.csv"
table.to_csv(out, index=False)
print(f"\nwritten to {out}")
