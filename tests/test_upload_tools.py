"""The Upload screen's engine: what is uploadable, per-date statistics from a file, the month grid, the comparison with the folders."""
import io
import json
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from mop_reco import upload_tools as ut  # noqa: E402
from mop_reco import uploads as up  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import UploadedFile, UploadFileStat, get_engine, get_session, init_db  # noqa: E402


@pytest.fixture()
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    yield s
    s.close()


def _paytm_zip(path: Path, rows: list):
    csv = "transaction_id,transaction_date,amount,status\n" + "\n".join(f"'{i}','{d}','{a}','SUCCESS'" for i, d, a in rows) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("part-00000.csv", csv)


def _upload(session, source, month, name="f.zip", rows=3, start=None, end=None, **kw):
    r = UploadedFile(uploaded_at=kw.pop("at", datetime(2026, 11, 20, 10, 0)), source_key=source, report_name=source, month=month,
                     original_file_name=name, stored_path=kw.pop("path", "x"), size_bytes=1, row_count=rows, date_range_start=start,
                     date_range_end=end, uploaded_by="Test User", status="stored", **kw)
    session.add(r)
    session.commit()
    return r


def test_paytm_statistics_group_rows_and_money_by_the_date_in_the_file(tmp_path):
    z = tmp_path / "p.zip"
    _paytm_zip(z, [("a", "01-11-2026 10:00:00", "100.50"), ("b", "01-11-2026 11:00:00", "200"), ("c", "02-11-2026 09:00:00", "50")])
    got = {d: (n, a) for d, n, a in ut.compute_stats("paytm_settlement", z, load_config())}
    assert got == {"2026-11-01": (2, 300.5), "2026-11-02": (1, 50.0)}


def test_an_iso_date_is_not_misread_as_day_first(tmp_path):
    z = tmp_path / "p.zip"
    _paytm_zip(z, [("a", "2026-11-01 10:00:00", "10")])
    assert ut.compute_stats("paytm_settlement", z, load_config()) == [("", 1, 10.0)]       # 11 January would be a silent wrong answer


def test_a_file_with_no_readable_date_is_counted_not_hidden(tmp_path):
    z = tmp_path / "p.zip"
    _paytm_zip(z, [("a", "not a date", "10")])
    assert ut.compute_stats("paytm_settlement", z, load_config()) == [("", 1, 10.0)]


def test_record_stats_writes_once_and_a_bad_file_is_recorded_not_raised(session, tmp_path):
    z = tmp_path / "p.zip"
    _paytm_zip(z, [("a", "01-11-2026 10:00:00", "10")])
    row = _upload(session, "paytm_settlement", "2026-11", path=str(z))
    ut.record_stats(session, row, load_config())
    ut.record_stats(session, row, load_config())                          # again: replaced, not doubled
    assert session.query(UploadFileStat).filter_by(upload_id=row.id).count() == 1 and row.stats_at is not None
    bad = _upload(session, "paytm_settlement", "2026-11", path=str(tmp_path / "missing.zip"))
    ut.record_stats(session, bad, load_config())
    assert bad.stats_at is None and bad.stats_error and ut.pending_stats(session) == []


def test_grid_shows_each_month_with_dates_present_against_days_so_far(session):
    cfg = load_config()
    r = _upload(session, "paytm_settlement", "2026-11", start="2026-11-01", end="2026-11-14", stats_at=datetime(2026, 11, 20))
    for d in range(1, 15):
        session.add(UploadFileStat(upload_id=r.id, source_key="paytm_settlement", business_date=f"2026-11-{d:02d}", rows=10, amount=1))
    session.commit()
    grid = ut.coverage_grid(session, cfg, today=date(2026, 11, 26), specs=[up.SOURCE_BY_KEY["paytm_settlement"], up.SOURCE_BY_KEY["axis_edc"]])
    assert grid["months"][0] == "2026-04" and grid["months"][-1] == "2026-11"
    paytm = next(x for x in grid["rows"] if x["source_key"] == "paytm_settlement")["months"]
    assert paytm["2026-10"]["state"] == "none"
    nov = paytm["2026-11"]
    assert nov["state"] == "partial" and nov["dates_present"] == 14 and nov["days_so_far"] == 26 and nov["to"] == "2026-11-14"
    assert next(x for x in grid["rows"] if x["source_key"] == "axis_edc")["months"]["2026-11"]["state"] == "none"


def test_a_withdrawn_upload_does_not_count_in_the_grid(session):
    r = _upload(session, "bajaj", "2026-11")
    r.status = "deleted"
    session.commit()
    grid = ut.coverage_grid(session, load_config(), today=date(2026, 11, 26), specs=[up.SOURCE_BY_KEY["bajaj"]])
    assert grid["rows"][0]["months"]["2026-11"]["state"] == "none"


def test_comparison_says_equal_differs_and_one_sided_to_the_paisa(session, tmp_path):
    base = {"created_at": "2026-11-26T10:00:00", "sources": {"paytm_settlement": {"per_date": {
        "2026-11-01": {"rows": 2, "amount": 300.5}, "2026-11-02": {"rows": 1, "amount": 50.0}, "2026-11-03": {"rows": 4, "amount": 9.0}}}}}
    path = tmp_path / "BASELINE.json"
    path.write_text(json.dumps(base))
    r = _upload(session, "paytm_settlement", "2026-11")
    for d, n, a in (("2026-11-01", 2, 300.5), ("2026-11-02", 1, 50.01), ("2026-11-09", 5, 7.0)):
        session.add(UploadFileStat(upload_id=r.id, source_key="paytm_settlement", business_date=d, rows=n, amount=a))
    session.commit()
    out = ut.compare_with_folders(session, path)["sources"][0]
    assert (out["dates_equal"], out["dates_differ"], out["dates_folder_only"], out["dates_upload_only"]) == (1, 1, 1, 1)
    differs = next(x for x in out["details"] if x["kind"] == "differs")
    assert differs["amount_difference"] == 0.01                          # a single paisa is a difference (nil tolerance)
    assert out["folder_amount"] == 359.5 and out["upload_amount"] == 357.51


def test_comparison_without_a_baseline_says_so(session, tmp_path):
    out = ut.compare_with_folders(session, tmp_path / "nope.json")
    assert out["available"] is False and "baseline" in out["message"]


def test_uploadable_specs_leave_out_the_erp_sources_and_the_database_masters(session, monkeypatch):
    monkeypatch.setenv("MOP_SOURCE_POS", "erp")
    monkeypatch.setenv("MOP_SOURCE_PTC", "erp")
    keys = {s.key for s in ut.uploadable_specs(session)}
    assert "pos_bill_wise" not in keys and "ptc" not in keys
    assert {"paytm_settlement", "axis_edc", "bajaj", "axis_cms"} <= keys
    monkeypatch.setenv("MOP_SOURCE_POS", "file")
    assert "pos_bill_wise" in {s.key for s in ut.uploadable_specs(session)}


def test_dry_run_reads_without_writing_the_warehouse_and_a_normal_run_still_writes(tmp_path, monkeypatch):
    from mop_reco import ingest
    monkeypatch.setattr(ingest, "WAREHOUSE_ROOT", tmp_path)
    df = pd.DataFrame({"business_date": [date(2026, 11, 1), date(2026, 11, 1), None], "x": [1, 2, 3]})
    with ingest.dry_run():
        written, without = ingest._write_parquet_by_date(df, "bajaj", "business_date")
    assert written == {} and without == 1 and not (tmp_path / "bajaj").exists()
    written, _ = ingest._write_parquet_by_date(df, "bajaj", "business_date")
    assert list(written) == ["2026-11-01"] and (tmp_path / "bajaj" / "2026-11-01.parquet").exists()


def test_the_exact_comparison_uses_the_runs_readers_and_states_equal_and_different(session, tmp_path, monkeypatch):
    from mop_reco import ingest
    base = {"created_at": "2026-11-26T10:00:00", "sources": {
        "paytm_settlement": {"money_column": "amount", "per_date": {"2026-11-01": {"rows": 2, "amount": 300.5}, "2026-11-02": {"rows": 1, "amount": 50.0}}},
        "bajaj": {"money_column": "Transaction Amount", "per_date": {"2026-11-01": {"rows": 1, "amount": 10.0}}}}}
    bp, out = tmp_path / "BASELINE.json", tmp_path / "cmp.json"
    bp.write_text(json.dumps(base))
    frames = {"ingest_paytm": pd.DataFrame({"business_date": [date(2026, 11, 1)] * 2 + [date(2026, 11, 2)], "amount": ["100.50", "200", "51"]}),
              "ingest_bajaj": pd.DataFrame({"business_date": [date(2026, 11, 1)], "Transaction Amount": [10.0]})}
    seen = {}
    for name, frame in frames.items():
        def fake(config, session, _f=frame, _n=name):
            seen[_n] = ingest._DRY_RUN.get()                                  # the comparison runs inside the dry-run switch
            return _f, None
        monkeypatch.setattr(ingest, name, fake)
    doc = ut.run_exact_comparison(load_config(), session, bp, out)
    assert doc["state"] == "done" and seen == {"ingest_paytm": True, "ingest_bajaj": True}
    paytm = next(s for s in doc["sources"] if s["source_key"] == "paytm_settlement")
    assert (paytm["dates_equal"], paytm["dates_differ"]) == (1, 1) and paytm["details"][0]["amount_difference"] == 1.0
    assert next(s for s in doc["sources"] if s["source_key"] == "bajaj")["dates_equal"] == 1
    assert ut.read_comparison(out)["state"] == "done"


def test_a_failing_comparison_is_recorded_as_failed_not_lost(session, tmp_path, monkeypatch):
    from mop_reco import ingest
    bp, out = tmp_path / "BASELINE.json", tmp_path / "cmp.json"
    bp.write_text(json.dumps({"sources": {"bajaj": {"money_column": "x", "per_date": {}}}}))
    monkeypatch.setattr(ingest, "ingest_bajaj", lambda c, s: (_ for _ in ()).throw(RuntimeError("file unreadable")))
    doc = ut.run_exact_comparison(load_config(), session, bp, out)
    assert doc["state"] == "failed" and "file unreadable" in doc["error"]


def test_the_paytm_upload_check_requires_every_column_the_run_reads():
    from mop_reco import ingest
    assert set(ingest.PAYTM_REQUIRED_COLUMNS) <= set(up.SOURCE_BY_KEY["paytm_settlement"].required_columns)
