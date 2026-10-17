"""PTC arrives as one file per period. Reading only the newest silently dropped every earlier period (Run #14: April-August
deposits vanished the moment September's file landed and closing balances ballooned to Rs 318 Cr)."""
import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest  # noqa: E402

from mop_reco import ingest  # noqa: E402
from mop_reco.config import load_config  # noqa: E402

HEADER = "Source Short Name,Billdate,Billno,Ptcmode,PTC Head,Ledger,PTC Bill Amount SUM,Remarks"


def write(path: Path, rows, mtime):
    lines = ["Cube Name:test", ""] + [HEADER] + [",".join(map(str, r)) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="cp1252")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WAREHOUSE_ROOT", tmp_path / "wh")
    d = tmp_path / "ptc"
    d.mkdir()
    cfg = load_config()
    return dataclasses.replace(cfg, sources={**cfg.sources, "ptc": d}), d


def test_every_file_is_read_not_just_the_newest(config):
    cfg, d = config
    write(d / "apr_aug.csv", [("AAA", "03-08-2026", "V1", "PAYMENT", "Cash/CMS Deposit", "CMS-RECD.", -1000, ""),
                              ("AAA", "04-08-2026", "V2", "PAYMENT", "Staff Welfare", "Welfare", -50, "")], 1_000_000)
    write(d / "sep.csv", [("AAA", "03-09-2026", "V9", "PAYMENT", "Cash/CMS Deposit", "CMS-RECD.", -2000, "")], 2_000_000)
    df, rep = ingest.ingest_ptc(cfg, None)
    assert len(df) == 3 and rep.breakdown["deposit_vouchers"] == 2
    assert rep.breakdown["deposit_total"] == 3000.0                     # August's AND September's deposits
    assert sorted(rep.business_dates) == ["2026-08-03", "2026-08-04", "2026-09-03"]
    assert len(rep.files) == 2


def test_a_voucher_in_two_files_counts_once_and_is_reported(config):
    cfg, d = config
    row = ("AAA", "03-07-2026", "V1", "PAYMENT", "Cash/CMS Deposit", "CMS-RECD.", -1000, "")
    write(d / "wide.csv", [row, ("BBB", "03-07-2026", "V2", "PAYMENT", "Staff Welfare", "Welfare", -10, "")], 1_000_000)
    write(d / "july.csv", [row], 2_000_000)                             # the same voucher, a newer dump
    df, rep = ingest.ingest_ptc(cfg, None)
    assert len(df) == 2 and rep.breakdown["deposit_total"] == 1000.0     # not 2,000
    assert any("superseded_by_more_recent_file" in n for n in rep.notes)
