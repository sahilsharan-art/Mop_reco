"""The masters live in the database (2026-11-21): rules, versioning, the one-time move, and "no workbook is read".

Each test uses a throw-away SQLite database - never db/reco.sqlite.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import ingest, masters, masters_db as mdb
from mop_reco.config import load_config
from mop_reco.db import get_engine, get_session, init_db


@pytest.fixture
def session(tmp_path):
    s = get_session(init_db(get_engine(tmp_path / "t.db")))
    masters.clear_caches()
    yield s
    s.close()
    masters.clear_caches()


STORE = [{"store_name": "CITYKART STORES PVT. LTD. (A-AAA)", "store": "AAA"},
         {"store_name": "CITYKART STORES PVT. LTD. (B-BBB)", "store": "BBB"}]
BASE = {
    "store": STORE,
    "mid_axis": [{"mid": "A1", "store": "AAA"}],
    "mid_paytm": [{"mid": "P1", "store": "AAA"}, {"mid": "P2", "store": "BBB"}],
    "pickup_code": [{"pickup_code": "AX1", "store": "AAA"}],
    "pickup_location": [{"pickup_code": "AX9", "location": "ONE", "store": "AAA"},
                        {"pickup_code": "AX9", "location": "TWO", "store": "BBB"}],
    "opening_balance": [{"cost_centre": STORE[0]["store_name"], "opening": 100.5},
                        {"cost_centre": "UNKNOWN CENTRE", "opening": 0.0}],
}


def _snapshot(per_master_versions: dict) -> dict:
    masters_out = {}
    for name, versions in per_master_versions.items():
        vs = []
        for when, rows in versions:
            vs.append({"loaded_at": when, "created_by": "t", "change_note": "n", "source_kind": "excel_load",
                       "source": "x.xlsx", "source_mtime": None,
                       "meta": {"as_at": "2026-04-01", "grand_total": 100.5} if name == "opening_balance" else None,
                       "rows": rows, "fingerprint": mdb._fingerprint([mdb._clean_row(name, r) for r in rows])})
        masters_out[name] = {"versions": vs, "row_count": len(vs[-1]["rows"]), "fingerprint": vs[-1]["fingerprint"]}
    return {"created_at": "2026-11-21 10:00:00", "masters": masters_out, "report": []}


def _import_all(session, overrides=None):
    versions = {n: [("2026-04-01 00:00:00", rows)] for n, rows in BASE.items()}
    versions.update(overrides or {})
    return mdb.import_snapshot(session, _snapshot(versions), "tester")


# ---------------------------------------------------------------------------- rules

def test_blank_field_and_bad_store_code_are_errors():
    v = mdb.validate("mid_axis", [{"mid": "", "store": "A B"}])
    text = " | ".join(v.errors)
    assert "'mid' is blank" in text and "must be letters/digits/underscore" in text


def test_a_new_conflict_is_an_error_but_an_old_one_is_a_warning():
    clash = [{"mid": "M", "store": "AAA"}, {"mid": "M", "store": "BBB"}]
    assert not mdb.validate("mid_axis", clash, previous_rows=[{"mid": "M", "store": "AAA"}]).ok
    old = mdb.validate("mid_axis", clash, previous_rows=clash)
    assert old.ok and any("two different stores" in w for w in old.warnings)


def test_an_existing_bad_row_stays_a_warning_not_a_block():
    """The MID master carries a '#REF!' store today; it must not stop a run or an unrelated edit."""
    rows = [{"mid": "P1", "store": "#REF!"}, {"mid": "P2", "store": "BBB"}]
    assert mdb.validate("mid_paytm", rows, previous_rows=rows).ok
    # ...but an edit that INTRODUCES one is refused
    assert not mdb.validate("mid_paytm", rows, previous_rows=[{"mid": "P2", "store": "BBB"}]).ok


def test_unknown_store_is_a_warning_never_an_error():
    v = mdb.validate("mid_axis", [{"mid": "A1", "store": "ZZZ"}], store_codes={"AAA"})
    assert v.ok and any("not in the store master" in w for w in v.warnings)


def test_opening_must_be_a_number():
    assert not mdb.validate("opening_balance", [{"cost_centre": "X", "opening": "abc"}]).ok


# ---------------------------------------------------------------------------- versioning

def test_a_save_needs_a_name_and_a_reason(session):
    _import_all(session)
    rows = BASE["mid_axis"] + [{"mid": "A2", "store": "BBB"}]
    with pytest.raises(mdb.MasterEditError):
        mdb.save_version(session, "mid_axis", rows, "", "add A2")
    with pytest.raises(mdb.MasterEditError):
        mdb.save_version(session, "mid_axis", rows, "Asha", "  ")


def test_a_save_is_a_new_version_that_keeps_the_old_one_and_records_who_and_why(session):
    _import_all(session)
    before = mdb.current_version(session, "mid_axis").id
    rows = BASE["mid_axis"] + [{"mid": "A2", "store": "BBB"}]
    v, created, d = mdb.save_version(session, "mid_axis", rows, "Asha", "new terminal at BBB")
    assert created and v.id != before and d["summary"] == "1 added, 0 removed, 0 changed"
    assert len(mdb.current_rows(session, "mid_axis")) == 2
    assert len(mdb.version_rows(session, before)) == 1            # the old version is untouched
    top = mdb.history(session, "mid_axis")[0]
    assert (top["created_by"], top["change_note"], top["current"]) == ("Asha", "new terminal at BBB", True)
    # saving the same rows again makes no new version
    _v2, created2, _d2 = mdb.save_version(session, "mid_axis", rows, "Asha", "again")
    assert not created2


def test_a_refused_edit_leaves_the_master_untouched(session):
    _import_all(session)
    with pytest.raises(mdb.MasterEditError):
        mdb.save_version(session, "mid_axis", [{"mid": "A1", "store": "bad code!"}], "Asha", "oops")
    assert mdb.current_rows(session, "mid_axis") == BASE["mid_axis"]


def test_restore_is_a_new_version_holding_the_old_rows(session):
    _import_all(session)
    first = mdb.current_version(session, "mid_axis").id
    mdb.save_version(session, "mid_axis", [{"mid": "A9", "store": "BBB"}], "Asha", "replace")
    v, created, _ = mdb.restore_version(session, "mid_axis", first, "Ravi", "put back")
    assert created and mdb.current_rows(session, "mid_axis") == BASE["mid_axis"]
    assert len(mdb.history(session, "mid_axis")) == 3             # nothing was rewritten


def test_editing_the_opening_balance_restates_its_grand_total(session):
    _import_all(session)
    mdb.save_version(session, "opening_balance",
                     [{"cost_centre": STORE[0]["store_name"], "opening": 250.0}], "Asha", "correct the opening")
    meta = mdb.meta_of(session, "opening_balance")
    assert meta["grand_total"] == 250.0 and meta["as_at"] == "2026-04-01"


# ---------------------------------------------------------------------------- the one-time move

def test_import_is_idempotent_and_switches_the_source(session):
    assert not mdb.in_database(session)
    first = _import_all(session)
    assert mdb.in_database(session) and any("verified by reading back" in line for line in first)
    n = len(mdb.history(session, "mid_paytm"))
    second = _import_all(session)
    assert any("already present" in line for line in second)
    assert len(mdb.history(session, "mid_paytm")) == n


def test_import_refuses_a_damaged_snapshot(session):
    snap = _snapshot({n: [("2026-04-01 00:00:00", rows)] for n, rows in BASE.items()})
    snap["masters"]["mid_axis"]["versions"][0]["rows"].append({"mid": "TAMPER", "store": "AAA"})
    with pytest.raises(mdb.MasterEditError):
        mdb.import_snapshot(session, snap, "tester")


def test_every_version_keeps_its_original_date_so_a_past_date_resolves_as_it_did(session):
    """A correction made later must not rewrite how an earlier business date resolves (CLAUDE.md 6.12)."""
    old = [{"mid": "P1", "store": "AAA"}]
    new = [{"mid": "P1", "store": "BBB"}]
    _import_all(session, {"mid_paytm": [("2026-04-01 00:00:00", old), ("2026-10-01 09:00:00", new)]})
    assert masters.resolve_store(session, "paytm_mid", "P1", date(2026, 5, 15)).store == "AAA"
    assert masters.resolve_store(session, "paytm_mid", "P1", date(2026, 11, 1)).store == "BBB"
    assert [h["rows"] for h in mdb.history(session, "mid_paytm")] == [1, 1]


# ---------------------------------------------------------------------------- nothing reads Excel any more

def test_no_workbook_is_opened_once_the_masters_are_in_the_database(session, monkeypatch):
    _import_all(session)
    import openpyxl

    def boom(*a, **k):
        raise AssertionError("a master workbook was opened although the masters are in the database")

    monkeypatch.setattr(openpyxl, "load_workbook", boom)
    monkeypatch.setattr(masters, "_read_master_rows", boom)
    monkeypatch.setattr(masters, "_most_recent_file", boom)
    config = load_config()

    checks = masters.preflight_masters(config, session)
    assert len(checks) == len(masters.MASTER_SPECS) and not any(c.problems for c in checks)
    loaded, failures = masters.load_all_masters_safe(config, session)
    assert not failures and set(loaded) == set(masters.MASTER_SPECS)
    version, created = masters.load_master(config, session, "store")
    assert not created
    report = ingest.ingest_opening_cash(config, session)
    breakdown_text = repr(report)
    assert "database:opening_balance" in breakdown_text


def test_the_opening_balance_comes_from_the_database_and_names_what_it_could_not_map(session):
    _import_all(session)
    result = ingest.ingest_opening_cash(load_config(), session)
    frame = result[0] if isinstance(result, tuple) else result.data
    assert float(frame.loc[frame["store"] == "AAA", "opening_cash"].iloc[0]) == 100.5


def test_a_store_master_edit_applies_to_the_next_read_immediately(session):
    _import_all(session)
    assert masters.resolve_store(session, "axis_mid", "A1", date(2026, 11, 20)).store == "AAA"
    mdb.save_version(session, "mid_axis", [{"mid": "A1", "store": "BBB"}], "Asha", "terminal moved")
    masters.clear_caches()
    assert masters.resolve_store(session, "axis_mid", "A1", datetime.now().date()).store == "BBB"
    # the day before the edit still resolves to the old store
    assert masters.resolve_store(session, "axis_mid", "A1", date(2026, 5, 1)).store == "AAA"
