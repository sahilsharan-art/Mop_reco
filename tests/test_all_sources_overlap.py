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


def test_paytm_a_corrected_settlement_file_supersedes_the_earlier_one_by_file_age_and_reports_it(tmp_path, monkeypatch):
    """Paytm reads every zip; where one transaction is in two files the MOST RECENTLY MODIFIED file wins (not whichever
    sorts first by path), the superseded rows are counted by key, and an id repeated inside one file is reported too."""
    import csv
    import io as _io
    import os
    import zipfile

    from mop_reco.config import load_config
    from mop_reco.db import get_engine, get_session, init_db

    cols = ingest.PAYTM_REQUIRED_COLUMNS

    def write_zip(name, rows, mtime):
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("part-00000.csv", buf.getvalue())
        os.utime(path, (mtime, mtime))
        return path

    def row(txn, amount, settled):
        return {"transaction_id": f"'{txn}'", "transaction_date": "'05-07-2026 10:00:00'", "status": "'SUCCESS'",
                "transaction_type": "'ACQUIRING'", "original_mid": "'M1'", "amount": f"'{amount}'", "commission": "'1'",
                "gst": "'0'", "payout_id": "'P'", "utr_no": "'U'", "settled_amount": f"'{settled}'",
                "settled_date": "'06-07-2026'", "payment_mode": "'UPI'"}

    # the OLDER file sorts LAST by name, so a "first by path" rule would keep the stale row
    newer = write_zip("a_corrected.zip", [row("T1", 100, 97), row("T3", 30, 29), row("T4", 10, 9), row("T4", 10, 9)], 1_700_200_000)
    older = write_zip("z_original.zip", [row("T1", 100, 98), row("T2", 50, 49)], 1_700_000_000)
    monkeypatch.setattr(ingest, "load_master", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "_resolve_store_column", lambda s, src, ids, amt, dates: ([None] * len(ids), 0, 0, {}))
    monkeypatch.setattr(ingest, "WAREHOUSE_ROOT", tmp_path / "wh")
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    df, report = ingest.ingest_paytm(load_config(), get_session(engine), file_paths=[older, newer])
    assert sorted(df["transaction_id"]) == ["T1", "T2", "T3", "T4"]
    assert float(df.loc[df["transaction_id"] == "T1", "settled_amount"].iloc[0]) == 97.0        # the newer file's restatement
    d = report.breakdown["duplicates"]
    assert (d["superseded_by_a_more_recent_file"], d["transaction_ids_superseded"], d["duplicates_inside_one_file"]) == (1, 1, 1)
    assert report.rows_before_dedupe == 6 and report.rows_after_dedupe == 4
    assert report.superseded[0]["transaction_id"] == "T1" and "a_corrected.zip" in report.superseded[0]["winning_file(s)"][0]
