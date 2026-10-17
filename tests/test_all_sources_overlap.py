"""One test over ALL sources: two files that cover overlapping dates must load as their UNION, never as the newest
file alone. The shared reader is `ingest.read_source_files`; any reader that deliberately takes only the newest file
must be named in `ingest.NEWEST_ONLY_SOURCES` with its reason (and the guard below fails if one is not)."""
import inspect
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import ingest  # noqa: E402

# source -> (natural key column, composite-key builder or None)
SOURCES = {
    "ptc": ("_ptc_key", lambda c: c.__setitem__("_ptc_key", c["store"] + "|" + c["voucher_no"].astype(str))),
    "axis_edc": ("RRN", None),
    "bajaj": ("Transaction Id", None),
    "axis_cms": ("_cms_key", lambda c: c.__setitem__("_cms_key", c["pk"].astype(str))),
}


class _Cfg:
    def __init__(self, files):
        self._files = files

    def source_status(self, source):
        return SimpleNamespace(files=self._files, configured_path="test")


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_two_overlapping_files_load_as_the_union_not_the_newest(tmp_path, source):
    key, make_key = SOURCES[source]
    older, newer = tmp_path / "older.dat", tmp_path / "newer.dat"
    older.write_text("x"); newer.write_text("x")
    import os
    os.utime(older, (1_700_000_000, 1_700_000_000)); os.utime(newer, (1_700_100_000, 1_700_100_000))

    def frame(ids, amount):
        d = pd.DataFrame({"store": "AAA", "voucher_no": ids, "RRN": ids, "Transaction Id": ids, "pk": ids,
                          "amount": amount})
        return d

    def read_one(path, mtime):
        # older covers 1..6, newer covers 4..9: 4,5,6 overlap and the newer file re-states them
        return frame(list(range(1, 7)), 1.0) if path.name == "older.dat" else frame(list(range(4, 10)), 2.0)

    df, files, report = ingest.read_source_files(_Cfg([older, newer]), source, None, read_one, key, make_key=make_key)
    assert len(files) == 2
    assert len(df) == 9, "the load must be the union of both files, not the newest file alone"
    by_id = df.set_index(df["Transaction Id"] if "Transaction Id" in df else df.index)
    assert sorted(df["pk"]) == list(range(1, 10))
    assert set(df.loc[df["pk"] <= 3, "amount"]) == {1.0}                 # only the older file has these
    assert set(df.loc[df["pk"] >= 4, "amount"]) == {2.0}                 # the overlap goes to the newer file
    assert sum(r["rows_dropped"] for r in report) == 3                    # and the three superseded rows are reported


def test_every_ingest_reads_every_file_unless_it_is_registered_as_newest_only():
    text = inspect.getsource(ingest)
    users = set(re.findall(r'io_\.latest_file\(config, "(\w+)"\)', text))
    assert users <= set(ingest.NEWEST_ONLY_SOURCES), (
        f"{sorted(users - set(ingest.NEWEST_ONLY_SOURCES))} read only the newest file without being listed in "
        "NEWEST_ONLY_SOURCES (with a reason)")
    for source in ingest.NEWEST_ONLY_SOURCES:
        assert ingest.NEWEST_ONLY_SOURCES[source].strip()
    for fn, source in (("ingest_ptc", "ptc"), ("ingest_axis_edc", "axis_edc"), ("ingest_bajaj", "bajaj"),
                       ("ingest_axis_cms", "axis_cms")):
        assert "read_source_files(" in inspect.getsource(getattr(ingest, fn)), f"{fn} bypasses the shared reader"
    # POS and Paytm keep their own loops (date-scoped cache; zip parts) but still read every file
    assert "_discover_files_for_source(" in inspect.getsource(ingest.ingest_pos_bill_wise)
    assert "zip_files" in inspect.getsource(ingest.ingest_paytm)
