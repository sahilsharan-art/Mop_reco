"""The masters' schema guard: a master that loses a column fails the RUN, up front.

Incident (2026-10-08): PICKUP LOCATION MASTER.xlsx was overwritten with a copy of
the pickup-code list and lost its LOCATION column. LOCATION splits pickup code
AX31918 between APM and MMR. Nothing failed until the test suite did, days later.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import masters, runs
from mop_reco.config import load_config
from mop_reco.db import MasterRow, MasterVersion, Run, get_engine, get_session, init_db


def make_xlsx(path: Path, sheets: dict):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def good_files(tmp_path):
    """One good file per master, keyed by the spec's `source` name."""
    return {
        "pickup_code_master": make_xlsx(tmp_path / "codes.xlsx", {"Sheet1": [
            ("PICKCODE", "CODE"), ("AX1", "AAA"), ("AX31918", "MMR")]}),
        "pickup_location_master": make_xlsx(tmp_path / "loc.xlsx", {"Sheet1": [
            ("PICKCODE", "LOCATION", "CODE"), ("AX31918", "ARRAH", "APM"), ("AX31918", "MADHB", "MMR")]}),
        "mid_master": make_xlsx(tmp_path / "mid.xlsx", {
            "Axis mid wise store": [("MID", "Store Code"), ("M1", "AAA"), ("M2", "BBB")],
            "Paytm Mid wise store code": [("MID", "Store Code"), ("P1", "AAA")]}),
        "store_master": make_xlsx(tmp_path / "store.xlsx", {"Sheet1": [
            ("SITES NAMES", "Store Code"), ("CITYKART (A-AAA)", "AAA")]}),
    }


@pytest.fixture
def patched(monkeypatch, good_files):
    files = dict(good_files)
    monkeypatch.setattr(masters, "_most_recent_file", lambda config, source: files[source])
    return files


def test_every_good_master_passes(patched):
    checks = masters.preflight_masters(load_config())
    assert [c.master for c in checks] == list(masters.MASTER_SPECS)
    assert all(c.ok for c in checks)
    assert {c.master: c.rows for c in checks} == {
        "pickup_code": 2, "pickup_location": 2, "mid_axis": 2, "mid_paytm": 1, "store": 1}


def test_a_master_that_lost_a_column_fails_and_names_it(patched, tmp_path):
    # exactly the 2026-10-08 incident: LOCATION gone, a copy of the code list instead
    patched["pickup_location_master"] = make_xlsx(tmp_path / "bad.xlsx", {"Sheet1": [
        ("PICKCODE", "CODE", "PICKUP CODE"), ("AX29035", "AGT", "AX29035")]})
    with pytest.raises(masters.MasterPreflightError) as e:
        masters.preflight_masters(load_config())
    msg = str(e.value)
    assert "pickup_location" in msg and "LOCATION" in msg and "bad.xlsx" in msg
    assert "nothing was run" in msg.lower()


def test_every_problem_is_reported_not_just_the_first(patched, tmp_path):
    patched["pickup_location_master"] = make_xlsx(tmp_path / "b1.xlsx", {"Sheet1": [("PICKCODE", "CODE")]})
    patched["mid_master"] = make_xlsx(tmp_path / "b2.xlsx", {"Axis mid wise store": [("MID", "Store Code"), ("M1", "A")]})
    with pytest.raises(masters.MasterPreflightError) as e:
        masters.preflight_masters(load_config())
    joined = " ".join(e.value.problems)
    assert "pickup_location" in joined and "mid_paytm" in joined      # missing sheet
    assert len(e.value.problems) >= 2


def test_missing_file_empty_master_and_unopenable_file(patched, tmp_path, monkeypatch):
    patched["pickup_code_master"] = make_xlsx(tmp_path / "empty.xlsx", {"Sheet1": [("PICKCODE", "CODE")]})
    (tmp_path / "junk.xlsx").write_bytes(b"not a workbook")
    patched["store_master"] = tmp_path / "junk.xlsx"
    with pytest.raises(masters.MasterPreflightError) as e:
        masters.preflight_masters(load_config())
    joined = " ".join(e.value.problems)
    assert "no data rows" in joined and "cannot be opened" in joined

    def none_found(config, source):
        raise masters.MasterLoadError(f"No usable files found for source '{source}'")
    monkeypatch.setattr(masters, "_most_recent_file", none_found)
    with pytest.raises(masters.MasterPreflightError) as e:
        masters.preflight_masters(load_config())
    assert "No usable files found" in " ".join(e.value.problems)


def test_soft_findings_are_warnings_not_failures(patched, tmp_path):
    patched["mid_master"] = make_xlsx(tmp_path / "mid2.xlsx", {
        "Axis mid wise store": [("MID", "Store Code"), ("M1", "AAA")],
        "Paytm Mid wise store code": [("MID", "Store Code"), ("P1", "#REF!"), ("P1", "BBR"), ("P2", "CCC")]})
    checks = masters.preflight_masters(load_config())          # does not raise
    paytm = next(c for c in checks if c.master == "mid_paytm")
    assert paytm.ok
    text = " ".join(paytm.warnings)
    assert "#REF!" in text and "more than one store" in text


def test_it_reports_what_changed_since_the_stored_version(patched):
    stored = [{"mid": "M1", "store": "AAA"}, {"mid": "M2", "store": "ZZZ"}, {"mid": "M9", "store": "QQQ"}]
    check = masters.preflight_master(load_config(), "mid_axis", stored)
    assert (check.stored_rows, check.added, check.removed, check.remapped) == (3, 0, 1, 1)
    assert any("differs from the stored version" in w for w in check.warnings)


def test_preflight_is_read_only(patched, tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    session = get_session(engine)
    masters.preflight_masters(load_config(), session)
    assert session.query(MasterVersion).count() == 0            # never snapshots


def test_a_run_refuses_to_start_on_a_broken_master(patched, tmp_path, monkeypatch):
    """The run fails FIRST - before a single source is read - and says why."""
    patched["pickup_location_master"] = make_xlsx(tmp_path / "bad.xlsx", {"Sheet1": [
        ("PICKCODE", "CODE", "PICKUP CODE"), ("AX29035", "AGT", "AX29035")]})
    from mop_reco import ingest as ingest_mod

    def must_not_run(*a, **k):
        raise AssertionError("a source was read although the masters pre-flight failed")
    for fn in ("ingest_pos_bill_wise", "ingest_paytm", "ingest_axis_edc", "ingest_bajaj"):
        monkeypatch.setattr(ingest_mod, fn, must_not_run)

    engine = get_engine(tmp_path / "run.sqlite")
    init_db(engine)
    session = get_session(engine)
    with pytest.raises(masters.MasterPreflightError):
        runs.run_reconciliation(load_config(), session, triggered_by="test")
    run = session.query(Run).one()
    assert run.status == "failed" and run.current_stage == "masters_preflight"
    notes = json.loads(run.notes)
    assert "LOCATION" in notes["failure_reason"] and "pickup_location" in notes["failure_reason"]
