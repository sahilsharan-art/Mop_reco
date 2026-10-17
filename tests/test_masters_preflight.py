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


def test_one_bad_master_does_not_stop_the_others(monkeypatch):
    """The Masters page must still render every other master when one has lost a column."""
    from mop_reco import masters

    def fake_preflight(config, name, stored_rows=None):
        check = masters.MasterCheck(master=name, file="x.xlsx", modified="2026-10-08 12:55:44",
                                    columns_found=["PICKCODE", "CODE", "PICKUP CODE"])
        if name == "pickup_location":
            check.missing = ["location"]
            check.problems = ["lost required column"]
        return check

    monkeypatch.setattr(masters, "preflight_master", fake_preflight)
    monkeypatch.setattr(masters, "load_master", lambda config, session, name: (f"v-{name}", False))
    loaded, failed = masters.load_all_masters_safe(None, None)
    assert set(failed) == {"pickup_location"}
    assert failed["pickup_location"].startswith("Pickup location master cannot be loaded - LOCATION column missing")
    assert "pickup_location" not in loaded and len(loaded) == len(masters.MASTER_SPECS) - 1


# --- an operator may authorise ONE run to use the stored version of a broken master (2026-10-17) -------------

def _stored_pickup_location(tmp_path, patched):
    """Load the good files once so a stored version exists, then break the file on disk."""
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    session = get_session(engine)
    masters.load_all_masters(load_config(), session)
    patched["pickup_location_master"] = make_xlsx(tmp_path / "bad.xlsx", {"Sheet1": [
        ("PICKCODE", "CODE", "PICKUP CODE"), ("AX29035", "AGT", "AX29035")]})
    return session


def test_stored_version_is_used_only_when_authorised_and_is_recorded(patched, tmp_path):
    session = _stored_pickup_location(tmp_path, patched)
    with pytest.raises(masters.MasterPreflightError):                      # not authorised: still blocks
        masters.preflight_masters(load_config(), session)
    checks = masters.preflight_masters(load_config(), session,
                                       use_stored={"pickup_location": "file lost LOCATION; operator said so"})
    check = next(c for c in checks if c.master == "pickup_location")
    assert check.ok and check.problems == []
    f = check.stored_in_force
    assert f["reason"] == "file lost LOCATION; operator said so" and f["header_seen"] == ["PICKCODE", "CODE", "PICKUP CODE"]
    assert f["file_modified"] and f["stored_rows"] == 2 and "bad.xlsx" in f["file"]
    assert any("USES THE STORED VERSION" in w for w in check.warnings)
    summary = masters.preflight_summary(checks)
    assert summary["pickup_location"]["stored_in_force"]["stored_version"] == f["stored_version"]
    assert summary["pickup_code"]["stored_in_force"] is None               # only the named master


def test_a_master_with_no_stored_version_cannot_be_overridden(patched, tmp_path):
    patched["pickup_location_master"] = make_xlsx(tmp_path / "bad.xlsx", {"Sheet1": [("PICKCODE", "CODE")]})
    engine = get_engine(tmp_path / "empty.sqlite")
    init_db(engine)
    with pytest.raises(masters.MasterPreflightError):
        masters.preflight_masters(load_config(), get_session(engine), use_stored={"pickup_location": "x"})


def test_load_master_falls_back_to_the_stored_version_only_while_in_force(patched, tmp_path):
    session = _stored_pickup_location(tmp_path, patched)
    with pytest.raises(masters.MasterLoadError):
        masters.load_master(load_config(), session, "pickup_location")
    masters.set_stored_masters_in_force({"pickup_location": "x"})
    try:
        version, created = masters.load_master(load_config(), session, "pickup_location")
        assert created is False and version.row_count == 2 and version.is_current
    finally:
        masters.clear_stored_masters_in_force()
    with pytest.raises(masters.MasterLoadError):                          # one run only: blocking again
        masters.load_master(load_config(), session, "pickup_location")


def test_master_gaps_are_listed_with_whether_the_current_master_now_holds_them(patched, tmp_path):
    import pandas as pd

    from mop_reco import master_gaps as mg
    from mop_reco.db import CmsSlip
    engine = get_engine(tmp_path / "g.sqlite")
    init_db(engine)
    session = get_session(engine)
    session.add(Run(id=1, started_at=datetime(2026, 10, 1), business_dates="[]", status="completed"))
    session.commit()
    masters.load_all_masters(load_config(), session)           # P1 -> AAA, M1 -> AAA, AX1 -> AAA are now in the masters
    pfiles = {}
    for day, mid in (("2026-09-02", "P1"), ("2026-09-05", "PX"), ("2026-10-01", "PX")):
        d = tmp_path / f"p{day}.parquet"
        pd.DataFrame({"original_mid": [mid], "store": [None], "amount": [100.0]}).to_parquet(d)
        pfiles[day] = str(d)
    session.add_all([
        CmsSlip(run_id=1, row_type="COL", dep_date="2026-09-03", cr_date="2026-09-03", store="UNRESOLVED",
                pickup_code="AX1", location="CBRAL", slip_no="1", amount=500),
        CmsSlip(run_id=1, row_type="COL", dep_date="2026-09-04", cr_date="2026-09-04", store="UNRESOLVED",
                pickup_code="ZZZ", location="CBRAL", slip_no="2", amount=700),
        CmsSlip(run_id=1, row_type="COL", dep_date="2026-09-04", cr_date="2026-09-04", store="BNK",
                pickup_code="BNK1", location=None, slip_no="3", amount=50)])
    session.commit()
    report = mg.persist(session, 1, "2026-09-01", "2026-09-30", pfiles, {})
    assert report["rows"] == 5
    df = mg.frame(session, 1).set_index(["source", "code"])
    p1 = df.loc[("Paytm MID", "P1")]
    assert p1["in_current_master"] == "yes" and p1["rows"] == 1 and p1["first_seen"] == "2026-09-02"
    px = df.loc[("Paytm MID", "PX")]
    assert px["in_current_master"] == "no" and px["rows"] == 1       # the 1 Oct row is outside the range
    assert df.loc[("CMS pickup code", "AX1")]["in_current_master"] == "yes"
    assert df.loc[("CMS pickup code", "ZZZ")]["in_current_master"] == "no"
    assert df.loc[("CMS - BNK pseudo-store", "BNK1")]["rows"] == 1
