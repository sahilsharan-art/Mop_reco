"""The ERP as a run source (mop_reco/erp_ingest.py + the dispatch in ingest.py). The ERP is a fake; the warehouse is a temp folder."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mop_reco import erp, erp_ingest, erp_source, ingest
from mop_reco.config import load_config
from test_erp_source import BILLS, PTC_COLS, POS_FULL, FakeErp, _pos_row, _ptc_row

CONFIG = load_config()
TODAY = date(2026, 11, 24)


class Erp(FakeErp):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.closed = False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("select min(business_date), max(business_date) from"):
            dates = [r[2] for r in self.ptc_rows] if self.ptc_rows else []
            return type("C", (), {"fetchall": lambda _s: [(min(dates), max(dates))] if dates else [(None, None)]})()
        return super().execute(sql, params)

    def close(self):
        self.closed = True

    def data_queries(self):
        return [p for s, p in self.executed if "from gold_mop.pos_bill_lines where business_date" in " ".join(s.split()).lower()]


def bills_on(day: datetime):
    rows = []
    for b in BILLS:
        r = list(_pos_row(b, POS_FULL))
        r[POS_FULL.index("business_date")] = day
        rows.append(tuple(r))
    return rows


@pytest.fixture
def wh(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WAREHOUSE_ROOT", tmp_path)
    return tmp_path


def use(monkeypatch, fake):
    monkeypatch.setattr(erp, "connect", lambda *a, **k: fake)
    return fake


# --- the mode setting ---------------------------------------------------------------------------------------------

def test_the_default_is_the_files_and_a_bad_value_is_refused(monkeypatch):
    monkeypatch.delenv("MOP_SOURCE_POS", raising=False)
    monkeypatch.delenv("MOP_SOURCE_PTC", raising=False)
    assert ingest.source_mode("pos_bill_wise") == "file" and ingest.source_mode("ptc") == "file"
    monkeypatch.setenv("MOP_SOURCE_POS", " ERP ")
    assert ingest.source_mode("pos_bill_wise") == "erp"
    monkeypatch.setenv("MOP_SOURCE_PTC", "excel")
    with pytest.raises(ingest.IngestError, match="'file' or 'erp'"):
        ingest.source_mode("ptc")


def test_erp_mode_routes_the_pos_reader_to_the_erp(monkeypatch):
    monkeypatch.setenv("MOP_SOURCE_POS", "erp")
    sentinel = (pd.DataFrame(), "report")
    monkeypatch.setattr(erp_ingest, "ingest_pos_from_erp", lambda config, session, d1, d2, on_progress=None: sentinel)
    assert ingest.ingest_pos_bill_wise(CONFIG, None, date_from="2026-07-20", date_to="2026-07-20") is sentinel


# --- POS ----------------------------------------------------------------------------------------------------------

def test_a_date_range_is_required(wh, monkeypatch):
    use(monkeypatch, Erp(POS_FULL, []))
    with pytest.raises(ingest.IngestError, match="date range"):
        erp_ingest.ingest_pos_from_erp(CONFIG, None, None, None)
    with pytest.raises(ingest.IngestError, match="before its start"):
        erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-07-19")


def test_pos_is_pulled_a_day_at_a_time_cached_and_reported(wh, monkeypatch):
    fake = use(monkeypatch, Erp(POS_FULL, bills_on(datetime(2026, 7, 20))))
    df, report = erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-19", "2026-07-21", today=TODAY)
    assert len(fake.data_queries()) == 3                                            # one half-open query per day
    assert fake.closed and len(df) == len(BILLS)
    assert report.rows_read == len(df) and report.business_dates == ["2026-07-20"]
    assert set(report.warehouse_paths) == {"2026-07-20"} and Path(report.warehouse_paths["2026-07-20"]).is_file()
    assert report.files[0].path == erp_ingest.POS_SOURCE_LABEL
    assert any("ERP" in n for n in report.notes)
    manifest = json.loads((wh / "pos_bill_wise" / "_erp_manifest.json").read_text())
    assert set(manifest["days"]) == {"2026-07-19", "2026-07-20", "2026-07-21"} and manifest["days"]["2026-07-20"]["rows"] == len(BILLS)
    assert manifest["days"]["2026-07-19"]["rows"] == 0                              # an empty day is remembered, not re-asked forever


def test_older_days_come_from_the_cache_and_recent_days_are_pulled_again(wh, monkeypatch):
    old, recent = datetime(2026, 7, 20), datetime(2026, 11, 20)
    fake = use(monkeypatch, Erp(POS_FULL, bills_on(old) + bills_on(recent)))
    erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-07-20", today=TODAY)
    erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-11-20", "2026-11-20", today=TODAY)
    before = len(fake.data_queries())
    df, report = erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-11-20", today=TODAY)
    new = fake.data_queries()[before:]
    pulled = {p[0].date() for p in new}
    # 2026-07-20 is older than the 65-day window and cached: not asked again. Every date from 2026-09-20 on is inside the window.
    assert date(2026, 7, 20) not in pulled and date(2026, 11, 20) in pulled and date(2026, 9, 20) in pulled
    assert len(df) == 2 * len(BILLS) and len(report.business_dates) == 2


def test_a_day_the_erp_no_longer_holds_loses_its_stale_partition(wh, monkeypatch):
    (wh / "pos_bill_wise").mkdir()
    stale = wh / "pos_bill_wise" / "2026-11-21.parquet"
    pd.DataFrame({"bill_no": ["X"]}).to_parquet(stale)
    use(monkeypatch, Erp(POS_FULL, bills_on(datetime(2026, 11, 22))))
    df, report = erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-11-21", "2026-11-22", today=TODAY)
    assert not stale.exists() and "2026-11-21" not in report.warehouse_paths and report.business_dates == ["2026-11-22"]


def test_the_file_derived_partitions_are_copied_aside_once(wh, monkeypatch):
    part = wh / "pos_bill_wise"
    part.mkdir()
    pd.DataFrame({"bill_no": ["FILE"]}).to_parquet(part / "2026-07-01.parquet")
    use(monkeypatch, Erp(POS_FULL, bills_on(datetime(2026, 7, 20))))
    erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-07-20", today=TODAY)
    saved = wh / "_file_baseline" / "pos_bill_wise" / "2026-07-01.parquet"
    assert saved.is_file() and pd.read_parquet(saved)["bill_no"].tolist() == ["FILE"]
    (saved).write_bytes(b"changed")                                                 # a second run must not copy over the baseline again
    erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-07-20", today=TODAY)
    assert saved.read_bytes() == b"changed"


def test_no_rows_at_all_is_an_error_not_an_empty_success(wh, monkeypatch):
    use(monkeypatch, Erp(POS_FULL, []))
    with pytest.raises(ingest.IngestError, match="no POS bill lines"):
        erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-07-21", today=TODAY)


def test_an_unready_view_stops_the_run_with_its_reason(wh, monkeypatch):
    use(monkeypatch, Erp([c for c in POS_FULL if c != "edc_response"], []))
    with pytest.raises(erp_source.ErpSchemaError, match="edc_response"):
        erp_ingest.ingest_pos_from_erp(CONFIG, None, "2026-07-20", "2026-07-20", today=TODAY)


# --- petty cash ---------------------------------------------------------------------------------------------------

def test_ptc_whole_view_partitions_deposits_and_stale_partitions(wh, monkeypatch):
    d1, d2 = datetime(2026, 7, 20), datetime(2026, 7, 22)
    rows = [_ptc_row("ABD", d1, "V1", "PAYMENT", "Cash/CMS Deposit", "CMS-RECD.", -87937.0),
            _ptc_row("ABD", d1, "V2", "PAYMENT", "Tea", "EXP", -320.0),
            _ptc_row("AGT", d2, "V3", "RECEIVE", "Income", "INC", 150.0)]
    part = wh / "ptc"
    part.mkdir()
    for name in ("2026-03-02", "2026-07-21"):                                        # before the ERP range (kept) / inside the range, not in the ERP (removed)
        pd.DataFrame({"store": ["Z"]}).to_parquet(part / f"{name}.parquet")
    use(monkeypatch, Erp(POS_FULL, [], PTC_COLS, rows))
    df, report = erp_ingest.ingest_ptc_from_erp(CONFIG, None)
    assert len(df) == 3 and report.rows_read == 3 and report.business_dates == ["2026-07-20", "2026-07-22"]
    assert report.breakdown["deposit_vouchers"] == 1 and report.breakdown["deposit_total"] == 87937.0
    assert (part / "2026-07-20.parquet").is_file() and (part / "2026-03-02.parquet").is_file() and not (part / "2026-07-21.parquet").exists()
    assert report.files[0].path == erp_ingest.PTC_SOURCE_LABEL and any("ERP" in n for n in report.notes)
    assert (wh / "_file_baseline" / "ptc" / "2026-07-21.parquet").is_file()


def test_ptc_with_no_vouchers_is_an_error(wh, monkeypatch):
    use(monkeypatch, Erp(POS_FULL, [], PTC_COLS, []))
    with pytest.raises(ingest.IngestError, match="no petty-cash"):
        erp_ingest.ingest_ptc_from_erp(CONFIG, None)


def test_the_days_to_pull_rule():
    part = Path("nowhere")
    days = [date(2026, 7, 20), date(2026, 9, 25), date(2026, 11, 20)]
    manifest = {"days": {"2026-07-20": {"rows": 5}, "2026-09-25": {"rows": 5}}}
    # window starts 2026-09-20: 07-20 cached (partition missing -> pulled again because rows > 0), 09-25 and 11-20 inside / never pulled
    assert erp_ingest.days_to_pull(days, manifest, TODAY, 65, part) == days
    manifest["days"]["2026-07-20"]["rows"] = 0                                       # an empty cached day needs no partition
    assert erp_ingest.days_to_pull(days, manifest, TODAY, 65, part) == days[1:]
