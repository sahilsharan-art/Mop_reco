"""Time and profile the RAW-source ingest of one month per source.

Safe by construction: the warehouse root is redirected to a temporary folder (the
real warehouse is never written - the real ingest rewrites per-date Parquet and
would also pick up files dated after Run #11) and masters are snapshotted into a
scratch SQLite file. Source files are opened read-only.

    python scripts/perf_ingest.py [pos paytm axis bajaj ptc] [--profile] [--save DIR]

--save DIR writes each returned frame to DIR/<source>.parquet so two ingest
implementations can be compared cell for cell (scripts/compare_ingest.py).
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import ingest as ing  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

JULY = {
    "pos": ("pos_bill_wise", lambda c: dict(file_path=_one(c, "pos_bill_wise", "July'26", "2026-07.xlsb"))),
    "paytm": ("paytm_settlement", lambda c: dict(file_paths=_many(c, "paytm_settlement", "4.July'26"))),
    "axis": ("axis_edc", lambda c: dict(file_path=_one(c, "axis_edc", "4.July'26", ".xlsx"))),
    "bajaj": ("bajaj", lambda c: dict(file_path=_one(c, "bajaj", "July'26", ".xlsx"))),
    "ptc": ("ptc", lambda c: dict(file_path=_one(c, "ptc", "July'26", "31st_July26.csv"))),
}
FUNCS = {"pos": "ingest_pos_bill_wise", "paytm": "ingest_paytm", "axis": "ingest_axis_edc",
         "bajaj": "ingest_bajaj", "ptc": "ingest_ptc"}


def _many(config, source, folder_part):
    return [p for p in config.source_status(source).files if folder_part in str(p)]


def _one(config, source, folder_part, name_part):
    hits = [p for p in _many(config, source, folder_part) if name_part in p.name or name_part in str(p)]
    assert len(hits) == 1, (source, folder_part, name_part, hits)
    return hits[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", default=list(JULY))
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--save", type=Path)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="ingestperf_"))
    ing.WAREHOUSE_ROOT = tmp / "warehouse"              # never the real warehouse
    engine = get_engine(tmp / "scratch.sqlite")
    init_db(engine)
    session = get_session(engine)
    config = load_config()
    proc = psutil.Process()
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    for key in args.sources:
        source, kwargs_fn = JULY[key]
        kwargs = kwargs_fn(config)
        fn = getattr(ing, FUNCS[key])
        before = proc.memory_info().peak_wset
        pr = cProfile.Profile() if args.profile else None
        t = time.perf_counter()
        if pr:
            pr.enable()
        df, report = fn(config, session, **kwargs)
        if pr:
            pr.disable()
        secs = time.perf_counter() - t
        print(f"{key:6s} {secs:7.1f}s  {len(df):>10,} rows  {len(df.columns)} cols  "
              f"peak {proc.memory_info().peak_wset / 1e6:,.0f} MB  files={[Path(str(p)).name for p in (kwargs.get('file_paths') or [kwargs.get('file_path')])]}",
              flush=True)
        if pr:
            pstats.Stats(pr).sort_stats("cumulative").print_stats(14)
        if args.save:
            out = df.copy()
            for c in out.columns:                       # parquet-safe, comparable
                if out[c].dtype == object:
                    out[c] = out[c].astype("string")
            out.to_parquet(args.save / f"{key}.parquet", index=False)
        del df
    return 0


if __name__ == "__main__":
    sys.exit(main())
