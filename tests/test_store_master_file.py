"""The store master workbook with Cluster and Region: read by name, checked, compared; and the master rules around it."""

from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import masters_db as mdb
from mop_reco import store_master_file as smf
from mop_reco.db import get_engine, get_session, init_db


def _book(tmp_path, header, rows, name="stores.xlsx", lead=0):
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(lead):
        ws.append(["Store master as at 27 Nov"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    p = tmp_path / name
    wb.save(p)
    return p


GOOD_HEADER = ["SR.No", "SITES NAMES", "Store Code", "Cluster Name", "Region Type"]
GOOD_ROWS = [[1, "CITYKART STORES PVT. LTD. (ARRAH-APM)", "APM", "Patna", "North"],
             [2, "CITYKART STORES PVT. LTD. (BALLIA-BIA)", "BIA", "Ballia", "North"],
             [3, "CITYKART STORES PVT. LTD. (KOLKATA-KPH)", "KPH", "Kolkata", "East"]]


def test_parse_finds_columns_by_name_even_under_a_title_row(tmp_path):
    parsed = smf.parse(_book(tmp_path, GOOD_HEADER, GOOD_ROWS, lead=2))
    assert parsed.ok and parsed.header_row == 3
    assert parsed.rows[0] == {"store_name": "CITYKART STORES PVT. LTD. (ARRAH-APM)", "store": "APM", "cluster": "Patna", "region": "North"}
    assert parsed.columns_found["cluster"] == "Cluster Name" and parsed.columns_found["region"] == "Region Type"
    assert len(parsed.sha256) == 64


def test_a_missing_column_is_named_and_nothing_is_guessed(tmp_path):
    parsed = smf.parse(_book(tmp_path, ["SITES NAMES", "Store Code", "Cluster Name"], [["x", "APM", "Patna"]]))
    assert not parsed.ok and any("Region" in e for e in parsed.errors)
    no_region = smf.parse(_book(tmp_path, ["Store Code", "Cluster"], [["APM", "Patna"]], name="b.xlsx"))
    assert not no_region.ok and any("Region" in e for e in no_region.errors)


def test_a_file_with_only_code_cluster_and_region_is_a_merge_file(tmp_path):
    """The user's real header: STORE CODE, CLUSTER NAME, REGION_TYPE - no site name."""
    parsed = smf.parse(_book(tmp_path, ["STORE CODE", "CLUSTER NAME", "REGION_TYPE"],
                             [["APM", "Patna", "North"], ["KPH", "Kolkata", "East"], ["ZZZ", "Nowhere", "West"]], name="m.xlsx"))
    assert parsed.ok and parsed.mode == "merge"
    assert parsed.rows[0] == {"store": "APM", "cluster": "Patna", "region": "North"}          # no store_name key at all
    current = [{"store_name": "CITYKART (ARRAH-APM)", "store": "APM"}, {"store_name": "CITYKART (KOLKATA-KPH)", "store": "KPH"},
               {"store_name": "CITYKART (OTHER-OTH)", "store": "OTH"}]
    m = smf.merge_into_current(parsed.rows, current)
    assert [r["store_name"] for r in m["merged"]] == ["CITYKART (ARRAH-APM)", "CITYKART (KOLKATA-KPH)", "CITYKART (OTHER-OTH)"]   # names kept
    assert m["merged"][0]["cluster"] == "Patna" and m["merged"][1]["region"] == "East"
    assert m["merged"][2]["region"] == "Closed" and m["merged"][2]["cluster"] == "Closed"     # the file is the ACTIVE list: a store it lacks is Closed
    assert m["not_in_master"] == ["ZZZ"] and m["closed"] == ["OTH"] and m["applied"] == 2     # reported by code, never added or dropped
    info = smf.summary(m["merged"])
    assert info["regions"] == ["East", "North"] and info["closed"] == ["OTH"] and info["no_region"] == []   # Closed is not a business region
    assert smf.compare(m["merged"], current)["removed"] == [] and smf.compare(m["merged"], current)["added"] == []


def test_blank_cluster_or_region_is_kept_blank(tmp_path):
    parsed = smf.parse(_book(tmp_path, GOOD_HEADER, GOOD_ROWS + [[4, "CITYKART STORES PVT. LTD. (NEW-NEW)", "NEW", None, None]]))
    assert parsed.ok and parsed.rows[-1]["cluster"] == "" and parsed.rows[-1]["region"] == ""
    info = smf.summary(parsed.rows)
    assert info["no_region"] == ["NEW"] and info["no_cluster"] == ["NEW"] and info["regions"] == ["East", "North"] and info["clusters"] == 3


def test_a_code_with_two_different_regions_is_refused(tmp_path):
    rows = GOOD_ROWS + [[4, "CITYKART STORES PVT. LTD. (ARRAH-APM)", "APM", "Patna", "South"]]
    parsed = smf.parse(_book(tmp_path, GOOD_HEADER, rows))
    assert not parsed.ok and any("APM" in e and "different cluster / region" in e for e in parsed.errors)


def test_an_excel_error_value_is_refused(tmp_path):
    parsed = smf.parse(_book(tmp_path, GOOD_HEADER, GOOD_ROWS + [[4, "x (Y-YYY)", "YYY", "#REF!", "North"]]))
    assert not parsed.ok and any("Excel error" in e for e in parsed.errors)


def test_csv_is_read_too(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("SITES NAMES,Store Code,Cluster,Region\nCITYKART (A-APM),APM,Patna,North\n", encoding="utf-8")
    parsed = smf.parse(p)
    assert parsed.ok and parsed.rows[0]["region"] == "North"


def test_compare_names_what_is_added_removed_and_moved():
    current = [{"store_name": "A", "store": "APM", "cluster": None, "region": None},
               {"store_name": "B", "store": "OLD", "cluster": "", "region": ""},
               {"store_name": "C", "store": "KPH", "cluster": "Kolkata", "region": "East"}]
    new = [{"store_name": "A", "store": "APM", "cluster": "Patna", "region": "North"},
           {"store_name": "C2", "store": "KPH", "cluster": "Kolkata", "region": "East"},
           {"store_name": "N", "store": "NEW", "cluster": "Ballia", "region": "North"}]
    d = smf.compare(new, current)
    assert d["added"] == ["NEW"] and d["removed"] == ["OLD"]
    assert [m["store"] for m in d["cluster_region_changed"]] == ["APM"]
    assert [m["store"] for m in d["renamed"]] == ["KPH"] and d["unchanged"] == 0


@pytest.fixture
def session(tmp_path):
    s = get_session(init_db(get_engine(tmp_path / "t.sqlite")))
    yield s
    s.close()


def test_master_rules_allow_blank_cluster_and_region_but_say_so():
    rows = [{"store_name": "A", "store": "APM", "cluster": "", "region": ""},
            {"store_name": "B", "store": "BIA", "cluster": "Ballia", "region": "North"}]
    v = mdb.validate("store", rows, [])
    assert v.ok and any("no region" in w for w in v.warnings) and any("no cluster" in w for w in v.warnings)
    blank_name = mdb.validate("store", [{"store_name": "", "store": "APM", "cluster": "x", "region": "y"}], [])
    assert not blank_name.ok                                # site name and code are still required


def test_the_store_master_saves_cluster_and_region_as_a_new_version(session):
    old = [{"store_name": "CITYKART (A-APM)", "store": "APM"}]                       # a version from before clusters existed
    mdb.save_version(session, "store", old, "t", "first", source_kind="screen_edit", enforce=False)
    new = [{"store_name": "CITYKART (A-APM)", "store": "APM", "cluster": "Patna", "region": "North"}]
    version, created, diff = mdb.save_version(session, "store", new, "Aditi", "uploaded stores.xlsx", source_kind="upload")
    assert created and mdb.current_rows(session, "store") == new
    assert [h["source_kind"] for h in mdb.history(session, "store")][0] == "upload"
    assert len(mdb.history(session, "store")) == 2                                    # the old version is kept
