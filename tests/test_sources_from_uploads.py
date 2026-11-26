"""Breaking the link with the numbered folders: SOURCES_FROM=uploads (MOP_SOURCES_FROM). Synthetic data only."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pandas as pd  # noqa: E402
from mop_reco import run_coverage as rc  # noqa: E402
from mop_reco.config import UPLOAD_FED_SOURCES, UPLOADS_FOLDER_NAME, load_config  # noqa: E402


def test_default_keeps_the_numbered_folders(monkeypatch):
    monkeypatch.delenv("MOP_SOURCES_FROM", raising=False)
    cfg = load_config()
    assert cfg.uploads_only is False
    assert cfg.sources["paytm_settlement"].name == "Paytm"


def test_uploads_mode_points_every_file_source_at_its_own_uploaded_folder_and_leaves_masters_alone(monkeypatch):
    monkeypatch.setenv("MOP_SOURCES_FROM", "uploads")
    cfg = load_config()
    assert cfg.uploads_only is True
    for name in UPLOAD_FED_SOURCES:
        if name not in cfg.sources:
            continue
        folder = cfg.sources[name]
        assert folder.parent.name == UPLOADS_FOLDER_NAME, name
    assert cfg.sources["paytm_settlement"] != cfg.sources["axis_edc"]           # one folder per source: "newest file" never crosses sources
    assert cfg.sources["pos_store_wise"] == cfg.sources["pos_bill_wise"]
    assert cfg.sources["mid_master"].name == "6.MID WISE STORE CODE MASTER"      # masters are not file-fed
    monkeypatch.delenv("MOP_SOURCES_FROM")
    assert load_config().sources["paytm_settlement"].name == "Paytm"


def test_an_empty_uploads_folder_holds_no_dates_even_when_the_warehouse_has_old_partitions(monkeypatch, tmp_path):
    """The folder-based runs left Paytm partitions in the warehouse. Once uploads are the only way in, those dates are history,
    not evidence that a file was supplied: coverage must say nothing has arrived."""
    wh = tmp_path / "warehouse" / "paytm_settlement"
    wh.mkdir(parents=True)
    pd.DataFrame({"x": [1]}).to_parquet(wh / "2026-11-10.parquet")
    monkeypatch.delenv("MOP_SOURCES_FROM", raising=False)
    assert rc.source_dates(load_config(), "paytm_settlement", root=tmp_path / "warehouse") == {date(2026, 11, 10)}
    monkeypatch.setenv("MOP_SOURCES_FROM", "uploads")
    assert rc.source_dates(load_config(), "paytm_settlement", root=tmp_path / "warehouse") == set()
    # the ERP-fed sources keep the warehouse's own dates
    (tmp_path / "warehouse" / "ptc").mkdir()
    pd.DataFrame({"x": [1]}).to_parquet(tmp_path / "warehouse" / "ptc" / "2026-11-10.parquet")
    assert date(2026, 11, 10) in rc.source_dates(load_config(), "ptc", root=tmp_path / "warehouse")
