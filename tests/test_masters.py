"""Stage 1 tests — master data loaders.

Runs against the real files in the working folders (read-only) and a fresh,
throwaway SQLite DB per test — never the dev db at db/reco.sqlite.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco.config import load_config
from mop_reco.db import get_engine, get_session, init_db
import pandas as pd

from mop_reco.masters import (
    MasterLoadError,
    _resolve_alias,
    _upsert_master_version,
    build_unmapped_report,
    check_mid_integrity,
    derive_provisional_store_master,
    due_date,
    get_rows_asof,
    known_pickup_location_codes,
    load_master,
    pickup_codes_needing_location_registration,
    resolve_pickup_store,
    resolve_store,
    version_history,
    DUE_DATE_BASIS_NO_HOLIDAYS,
)

BUSINESS_DATE = date(2026, 7, 21)  # a date the CMS/pickup data actually covers


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "test_reco.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()



SNAPSHOT = Path(__file__).resolve().parents[1] / "snapshots" / "masters_snapshot.json"


@pytest.fixture
def db_masters(session):
    """The real masters, as the database now holds them: the one-time snapshot, every version at its ORIGINAL date.
    (These tests used to read the live Excel workbooks, whose contents drift - and one lost a column.)"""
    import json
    from mop_reco import masters_db
    if not SNAPSHOT.exists():
        pytest.skip("snapshots/masters_snapshot.json not present")
    masters_db.import_snapshot(session, json.loads(SNAPSHOT.read_text(encoding="utf-8")), "test")
    return session


def _rows_in_force(name, on):
    """Row count of the snapshot version in force on `on` (the earliest if none yet) - computed independently."""
    import json
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    versions = snap["masters"][name]["versions"]
    cutoff = datetime.combine(on, datetime.max.time())
    before = [v for v in versions if datetime.fromisoformat(v["loaded_at"]) <= cutoff]
    return len((before[-1] if before else versions[0])["rows"])


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def daily_pos_file(config):
    return config.sources["pos_bill_wise"] / "MOP SALE DATA (20.07.2026).xlsx"


@pytest.fixture
def daily_cms_file(config):
    return config.sources["axis_cms"] / "CMS 21st_July'26.xlsx"


@pytest.fixture
def daily_axis_file(config):
    return config.sources["axis_edc"] / "Axis_20th_July'26.xlsx"


@pytest.fixture
def daily_paytm_zips(config):
    root = config.sources["paytm_settlement"]
    return [p for p in config.source_status("paytm_settlement").files
            if p.parent == root and p.suffix.lower() == ".zip"]


# ---------------------------------------------------------------------------
# 1. Pickup master: loads all codes, flags only codes that fail to resolve —
#    never a code just because it doesn't look like AX#####.
# ---------------------------------------------------------------------------

def test_pickup_master_loads_all_codes(config, session, db_masters):
    # The version in force on BUSINESS_DATE, counted independently from the snapshot file.
    expected = _rows_in_force("pickup_code", BUSINESS_DATE)
    version, rows = get_rows_asof(session, "pickup_code", BUSINESS_DATE)
    assert expected > 0 and len(rows) == expected == version.row_count


def test_pickup_master_flags_only_unresolvable_codes(config, session):
    load_master(config, session, "pickup_code")

    # A real, non-AX-format code from the master (BRD) must resolve fine —
    # looking unusual is not a reason to flag it.
    result = resolve_store(session, "pickup_code", "BRD", BUSINESS_DATE)
    assert result.status == "resolved"
    assert result.store is not None

    # A real AX-format code must also resolve fine.
    result_ax = resolve_store(session, "pickup_code", "AX29035", BUSINESS_DATE)
    assert result_ax.status == "resolved"
    assert result_ax.store == "AGT"

    # A code that genuinely isn't in the master — this is the only thing that
    # should be flagged.
    result_bad = resolve_store(session, "pickup_code", "NOT-A-REAL-CODE", BUSINESS_DATE)
    assert result_bad.status == "unmapped"


def test_pickup_master_all_real_cms_transactions_resolve(config, session, daily_cms_file):
    """Cross-check every distinct pickup code seen in the real CMS received
    file against the master — all 185 must resolve, none unmapped."""
    import openpyxl

    load_master(config, session, "pickup_code")

    file_path = daily_cms_file
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    header = list(next(it))
    idx = header.index("PICKUPPOINTCODE")
    codes = sorted({row[idx] for row in it if row[idx]})

    assert len(codes) == 185
    unmapped = [c for c in codes if resolve_store(session, "pickup_code", c, BUSINESS_DATE).status != "resolved"]
    assert unmapped == []


# ---------------------------------------------------------------------------
# 1b. Pickup code + location (2026-09-12, defect A) — APM/MMR share AX31918.
# Every other code must resolve exactly as before; only this one code's
# resolution should ever consult location.
# ---------------------------------------------------------------------------

def test_pickup_location_master_loads_the_registered_code(config, session, db_masters):
    version, rows = get_rows_asof(session, "pickup_location", BUSINESS_DATE)
    assert version.row_count == 2 == len(rows)
    assert {r["pickup_code"] for r in rows} == {"AX31918"}


def test_known_pickup_location_codes_is_just_the_one_registered_code(config, session, db_masters):
    assert known_pickup_location_codes(session, BUSINESS_DATE) == {"AX31918"}


def test_resolve_pickup_store_is_unaffected_for_every_other_code(config, session, db_masters):
    """Nothing changes for the ~209 codes that don't need location — same
    result as plain resolve_store, whatever the (irrelevant) location."""

    plain = resolve_store(session, "pickup_code", "AX29035", BUSINESS_DATE)
    via_pickup = resolve_pickup_store(session, "AX29035", "ANYTHING-OR-NOTHING", BUSINESS_DATE)
    assert via_pickup.status == "resolved" == plain.status
    assert via_pickup.store == plain.store == "AGT"

    via_pickup_no_location = resolve_pickup_store(session, "AX29035", None, BUSINESS_DATE)
    assert via_pickup_no_location.store == "AGT"


def test_resolve_pickup_store_uses_location_for_the_shared_code(config, session, db_masters):

    apm = resolve_pickup_store(session, "AX31918", "ARRAH", BUSINESS_DATE)
    assert apm.status == "resolved"
    assert apm.store == "APM"

    mmr = resolve_pickup_store(session, "AX31918", "MADHB", BUSINESS_DATE)
    assert mmr.status == "resolved"
    assert mmr.store == "MMR"

    # Exact-after-normalising, the same as the store-name match elsewhere —
    # not fuzzy, just case/whitespace-insensitive.
    lower = resolve_pickup_store(session, "AX31918", " arrah ", BUSINESS_DATE)
    assert lower.store == "APM"


def test_resolve_pickup_store_blank_location_on_shared_code_is_unresolved(config, session, db_masters):
    """Rule: never fall back to picking one when location is blank."""

    result = resolve_pickup_store(session, "AX31918", None, BUSINESS_DATE)
    assert result.status == "location_unresolved"
    assert result.store is None
    assert result.candidates == ("APM", "MMR")


def test_resolve_pickup_store_unrecognised_location_on_shared_code_is_unresolved(config, session, db_masters):

    result = resolve_pickup_store(session, "AX31918", "SOMEWHERE-ELSE", BUSINESS_DATE)
    assert result.status == "location_unresolved"
    assert result.store is None


def _cms_rows(rows):
    return pd.DataFrame(rows, columns=["PICKUPPOINTCODE", "LOCAT"])


def test_pickup_codes_needing_location_registration_flags_an_unregistered_code(config, session, db_masters):
    cms = _cms_rows([
        ("AX99999", "TOWNONE"), ("AX99999", "TOWNTWO"),   # new, unregistered
        ("AX31918", "ARRAH"), ("AX31918", "MADHB"),        # already registered
        ("AX29035", "SOMEPLACE"), ("AX29035", "SOMEPLACE"),  # single location
    ])
    flagged = pickup_codes_needing_location_registration(cms, session, BUSINESS_DATE)
    assert flagged == ["AX99999"]


def test_pickup_codes_needing_location_registration_is_empty_when_nothing_new(config, session, db_masters):
    cms = _cms_rows([("AX31918", "ARRAH"), ("AX31918", "MADHB"),
                     ("AX29035", "SOMEPLACE"), ("AX29035", "SOMEPLACE")])
    assert pickup_codes_needing_location_registration(cms, session, BUSINESS_DATE) == []


def test_pickup_codes_needing_location_registration_on_empty_input():
    assert pickup_codes_needing_location_registration(pd.DataFrame(), None, BUSINESS_DATE) == []


# ---------------------------------------------------------------------------
# 2. Both MID masters load; integrity check passes now that GLA is fixed.
# ---------------------------------------------------------------------------

def test_mid_masters_load(config, session):
    v_axis, _ = load_master(config, session, "mid_axis")
    v_paytm, _ = load_master(config, session, "mid_paytm")
    assert v_axis.row_count > 0
    assert v_paytm.row_count > 0


def test_mid_integrity_passes_after_gla_fix(config, session, db_masters):

    violations = check_mid_integrity(session, BUSINESS_DATE)
    assert violations == [], f"unexpected MID integrity violations: {violations}"

    # The specific MID that used to be ambiguous now resolves cleanly to GLA.
    result = resolve_store(session, "axis_mid", "037215021610005", BUSINESS_DATE)
    assert result.status == "resolved"
    assert result.store == "GLA"


def test_mid_integrity_detects_a_synthetic_conflict(session):
    """The check itself must actually catch a conflict, not just pass by
    default — prove it on synthetic data rather than trusting an always-clean
    result."""
    rows = [{"mid": "999", "store": "AAA"}, {"mid": "999", "store": "BBB"}]
    _upsert_master_version(session, "mid_paytm", rows, "synthetic", None, loaded_at=datetime(2026, 1, 1))
    _upsert_master_version(session, "mid_axis", [], "synthetic", None, loaded_at=datetime(2026, 1, 1))

    violations = check_mid_integrity(session, date(2026, 1, 2))
    assert len(violations) == 1
    assert violations[0].mid == "999"
    assert violations[0].stores == ("AAA", "BBB")


# ---------------------------------------------------------------------------
# 3. Loading the same, unchanged master twice creates one version, not two.
# ---------------------------------------------------------------------------

def test_reload_unchanged_file_creates_one_version_not_two(config, session):
    _, created_first = load_master(config, session, "pickup_code")
    assert created_first is True

    _, created_second = load_master(config, session, "pickup_code")
    assert created_second is False

    history = version_history(session, "pickup_code")
    assert len(history) == 1


# ---------------------------------------------------------------------------
# 4. A lookup for a past business date uses the version that applied then,
#    not the newest — tested on synthetic, timestamped versions so it's
#    deterministic and never touches a real file.
# ---------------------------------------------------------------------------

def test_asof_lookup_uses_the_version_that_applied_then(session):
    v1_time = datetime(2026, 8, 1, 9, 0)
    v2_time = datetime(2026, 8, 24, 9, 0)

    v1, created1 = _upsert_master_version(
        session, "test_master", [{"k": "A"}], "synthetic", None, loaded_at=v1_time
    )
    assert created1 is True

    v2, created2 = _upsert_master_version(
        session, "test_master", [{"k": "B"}], "synthetic", None, loaded_at=v2_time
    )
    assert created2 is True
    assert v2.id != v1.id

    # Between the two loads: must get v1's content, not v2's.
    version_mid, rows_mid = get_rows_asof(session, "test_master", date(2026, 8, 10))
    assert version_mid.id == v1.id
    assert rows_mid == [{"k": "A"}]

    # On/after the second load: must get v2's content.
    version_late, rows_late = get_rows_asof(session, "test_master", date(2026, 8, 24))
    assert version_late.id == v2.id
    assert rows_late == [{"k": "B"}]

    # v1 must show as closed off, not silently overwritten.
    assert v1.effective_to == v2_time
    assert v1.is_current is False
    assert v2.is_current is True


def test_asof_lookup_before_any_version_falls_back_to_earliest(session):
    v1, _ = _upsert_master_version(
        session, "test_master_2", [{"k": "only"}], "synthetic", None,
        loaded_at=datetime(2026, 8, 1),
    )
    version, rows = get_rows_asof(session, "test_master_2", date(2020, 1, 1))
    assert version.id == v1.id
    assert rows == [{"k": "only"}]


# ---------------------------------------------------------------------------
# Alias map: an unrecognised/missing required column raises, naming the file
# and the field — never guessed by position.
# ---------------------------------------------------------------------------

def test_missing_alias_raises_naming_file_and_field():
    with pytest.raises(MasterLoadError, match="fake_file.*missing_field"):
        _resolve_alias(["A", "B"], "missing_field", ["NOT_PRESENT"], "fake_file.xlsx")


# ---------------------------------------------------------------------------
# Unmapped report — real cross-file check, matching the 2026-08-24 inventory:
# 1 Axis MID and 9 Paytm MIDs don't resolve against their masters.
# ---------------------------------------------------------------------------

def test_unmapped_report_matches_known_inventory_gaps(config, session, daily_axis_file, daily_paytm_zips, db_masters):
    import csv
    import zipfile

    import openpyxl


    axis_file = daily_axis_file
    wb = openpyxl.load_workbook(axis_file, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    header = list(next(it))
    mid_idx = header.index("MID")
    amt_idx = header.index("NET_AMT")
    axis_records = [(row[mid_idx], row[amt_idx]) for row in it]

    axis_unmapped = build_unmapped_report(session, "axis_mid", axis_records, BUSINESS_DATE)
    # 037135034530134 was missing from the masters in force in July (the 2026-08-24 inventory) and a LATER master
    # version added it (store DMR). CLAUDE.md 6.25: an identifier absent from the as-at version resolves through the
    # earliest later version that holds it - said so, never guessed - so it is no longer unmapped.
    late = resolve_store(session, "axis_mid", "037135034530134", BUSINESS_DATE)
    assert late.status == "resolved" and late.store == "DMR" and late.via_later_version
    assert [u.identifier for u in axis_unmapped] == []

    paytm_records = []
    for zpath in daily_paytm_zips:
        with zipfile.ZipFile(zpath) as zf:
            for name in [n for n in zf.namelist() if n.endswith(".csv")]:
                with zf.open(name) as fh:
                    reader = csv.DictReader(line.decode("utf-8", errors="replace") for line in fh)
                    for row in reader:
                        paytm_records.append((row["original_mid"].strip("'"), row["amount"].strip("'")))

    paytm_unmapped = build_unmapped_report(session, "paytm_mid", paytm_records, BUSINESS_DATE)
    # The 9 Paytm MIDs that were unmapped in the 2026-08-24 inventory are all in a later master version now, so each
    # resolves THROUGH it (6.25) rather than being reported unmapped: none is lost, and the route is recorded.
    assert paytm_unmapped == []
    via_later = {m for m, _a in paytm_records
                 if resolve_store(session, "paytm_mid", m, BUSINESS_DATE).via_later_version}
    assert len(via_later) == 9


# ---------------------------------------------------------------------------
# Working-day helpers
# ---------------------------------------------------------------------------

def test_due_date_skips_weekend_and_labels_basis():
    friday = date(2026, 1, 1)
    while friday.weekday() != 4:
        friday += timedelta(days=1)

    result = due_date(friday, 1)
    assert result.date.weekday() == 0  # Monday
    assert result.basis == DUE_DATE_BASIS_NO_HOLIDAYS


# ---------------------------------------------------------------------------
# Provisional store list (no store master exists)
# ---------------------------------------------------------------------------

def test_provisional_store_master_is_derived_and_labelled(config, daily_pos_file):
    provisional, note = derive_provisional_store_master(config, file_path=daily_pos_file)
    assert len(provisional) == 188
    assert all(p.cms_cycle_days == 1 and p.cms_cycle_source == "cycle_assumed" for p in provisional)
    assert "NOT authoritative" in note


def test_resolve_store_pos_source_uses_provisional_list(config, session, daily_pos_file):
    provisional, _ = derive_provisional_store_master(config, file_path=daily_pos_file)
    codes = {p.store for p in provisional}

    result = resolve_store(session, "pos", next(iter(codes)), BUSINESS_DATE, pos_store_codes=codes)
    assert result.status == "resolved"

    result_bad = resolve_store(session, "pos", "NOT-A-STORE", BUSINESS_DATE, pos_store_codes=codes)
    assert result_bad.status == "unmapped"


def test_an_identifier_added_by_a_later_master_still_resolves_an_earlier_date_but_a_correction_does_not(session):
    """Effective dating is by load date, so a September MID only in an October file used to stay unmapped. Absence
    falls back to the earliest later version; a MID present in both versions keeps the as-at mapping (6.12)."""
    _upsert_master_version(session, "mid_paytm", [{"mid": "OLD", "store": "AAA"}], "s", None,
                           loaded_at=datetime(2026, 9, 1))
    _upsert_master_version(session, "mid_paytm", [{"mid": "OLD", "store": "BBB"}, {"mid": "NEW", "store": "CCC"}],
                           "s", None, loaded_at=datetime(2026, 10, 9))
    late = resolve_store(session, "paytm_mid", "NEW", date(2026, 9, 15))
    assert (late.status, late.store) == ("resolved", "CCC") and late.via_later_version
    corrected = resolve_store(session, "paytm_mid", "OLD", date(2026, 9, 15))
    assert corrected.store == "AAA" and corrected.via_later_version is None     # the correction is not back-applied
    assert resolve_store(session, "paytm_mid", "NOPE", date(2026, 9, 15)).status == "unmapped"
