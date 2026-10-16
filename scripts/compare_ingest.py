"""Compare two saved ingest frames (scripts/perf_ingest.py --save) cell for cell.

    python scripts/compare_ingest.py BASE_DIR NEW_DIR [source ...]

NA-aware: a null equals a null, nothing else equals a null. Exits non-zero on any
difference, printing the first few differing cells of each column.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def compare(a: pd.DataFrame, b: pd.DataFrame, label: str) -> int:
    bad_total = 0
    if len(a) != len(b):
        print(f"  {label}: ROW COUNT differs {len(a):,} vs {len(b):,}")
        return 1
    if list(a.columns) != list(b.columns):
        print(f"  {label}: COLUMNS differ\n    base {list(a.columns)}\n    new  {list(b.columns)}")
        bad_total += 1
    for c in [c for c in a.columns if c in b.columns]:
        x, y = a[c].reset_index(drop=True), b[c].reset_index(drop=True)
        xn, yn = x.isna().to_numpy(), y.isna().to_numpy()
        both = ~xn & ~yn
        eq = np.ones(len(x), dtype=bool)
        eq[xn != yn] = False
        xo, yo = x.astype(object).to_numpy(), y.astype(object).to_numpy()
        eq[both] = xo[both] == yo[both]
        bad = int((~eq).sum())
        if bad:
            idx = np.flatnonzero(~eq)[:3]
            print(f"  {label}.{c}: {bad:,} differing cell(s); first: "
                  + "; ".join(f"row {i}: {xo[i]!r} vs {yo[i]!r}" for i in idx))
        bad_total += bad
    return bad_total


def main() -> int:
    base, new = Path(sys.argv[1]), Path(sys.argv[2])
    names = sys.argv[3:] or sorted(p.stem for p in base.glob("*.parquet"))
    rc = 0
    for n in names:
        a, b = pd.read_parquet(base / f"{n}.parquet"), pd.read_parquet(new / f"{n}.parquet")
        bad = compare(a, b, n)
        print(f"{n}: {len(a):,} rows x {len(a.columns)} cols - "
              f"{'IDENTICAL' if bad == 0 else f'{bad:,} DIFFERENCE(S)'}")
        rc |= int(bad != 0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
