"""The Upload page's store master panel, end to end: upload -> preview -> save as a new master version -> history line.

The browser cannot pick a file, so Streamlit's own AppTest drives the panel with `st.file_uploader` replaced by one that hands back the
workbook. The database is a throw-away SQLite file in 'masters in the database' mode."""

from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import masters_db as mdb
from mop_reco import uploads as up
from mop_reco.db import UploadedFile, get_engine, get_session, init_db

HEADER = ["SR.No", "SITES NAMES", "Store Code", "Cluster Name", "Region Type"]
ROWS = [[1, "CITYKART STORES PVT. LTD. (ARRAH-APM)", "APM", "Patna", "North"],
        [2, "CITYKART STORES PVT. LTD. (BALLIA-BIA)", "BIA", "Ballia", "North"],
        [3, "CITYKART STORES PVT. LTD. (KOLKATA-KPH)", "KPH", "Kolkata", "East"]]


def _panel(db_path, xlsx_path):
    import sys
    import streamlit as st
    sys.path.insert(0, r"D:\MOP_Reco\app")
    from mop_reco.db import get_engine, get_session
    import store_master_panel

    class Fake:
        name = "stores.xlsx"

        def __init__(self):
            self._b = open(xlsx_path, "rb").read()
            self.size = len(self._b)

        def getvalue(self):
            return self._b

    st.file_uploader = lambda *a, **k: Fake()
    store_master_panel.render(get_session(get_engine(db_path)), key="smu")


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.sqlite"
    session = get_session(init_db(get_engine(db)))
    mdb.set_setting(session, mdb.SETTING_MASTERS_SOURCE, mdb.SOURCE_DATABASE)
    mdb.save_version(session, "store", [{"store_name": "CITYKART STORES PVT. LTD. (ARRAH-APM)", "store": "APM"},
                                        {"store_name": "CITYKART STORES PVT. LTD. (OLD-OLD)", "store": "OLD"}],
                     "t", "seed", source_kind="screen_edit", enforce=False)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    xlsx = tmp_path / "stores.xlsx"
    wb.save(xlsx)
    yield session, str(db), str(xlsx)
    session.close()


def _app(env):
    session, db, xlsx = env
    at = AppTest.from_function(_panel, args=(db, xlsx), default_timeout=60)
    at.session_state["operator"] = "Aditi Agarwal"
    return at.run()


def test_the_preview_shows_the_hierarchy_and_names_what_would_be_removed(env):
    at = _app(env)
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "3 stores" in text and "2 regions" in text and "2 clusters" not in text and "3 clusters" in text
    assert any("OLD" in e.value and "would be removed" in e.value for e in at.error)       # a store the file leaves out is named
    save = next(b for b in at.button if b.label == "Save as the new store master")
    assert save.disabled                                                                    # until the removal is ticked


def test_saving_stores_a_new_version_with_cluster_and_region_and_a_history_line(env):
    session, _, _ = env
    at = _app(env)
    next(c for c in at.checkbox if "will no longer be in the store master" in c.label).check().run()
    next(b for b in at.button if b.label == "Save as the new store master").click().run()
    assert not at.exception, [e.value for e in at.exception]
    session.expire_all()
    rows = {r["store"]: r for r in mdb.current_rows(session, "store")}
    assert set(rows) == {"APM", "BIA", "KPH"} and rows["BIA"]["cluster"] == "Ballia" and rows["KPH"]["region"] == "East"
    versions = mdb.history(session, "store")
    assert versions[0]["source_kind"] == "upload" and versions[0]["created_by"] == "Aditi Agarwal" and len(versions) == 2   # old one kept
    line = session.query(UploadedFile).filter(UploadedFile.source_key == up.STORE_MASTER_SOURCE_KEY).one()
    assert line.status == "recorded" and line.stored_path == "" and "3 stores" in line.note and line.uploaded_by == "Aditi Agarwal"
    assert any("Store master saved as version" in s.value for s in at.success)


def test_outside_database_mode_the_panel_says_it_would_not_be_used(env, tmp_path):
    session, db, xlsx = env
    session.execute(mdb.AppSetting.__table__.delete())
    session.commit()
    at = AppTest.from_function(_panel, args=(db, xlsx), default_timeout=60).run()
    assert not at.exception and any("would not be used" in i.value for i in at.info)


def test_a_file_without_site_names_is_applied_onto_the_stored_master(env, tmp_path):
    """The user's real file: STORE CODE, CLUSTER NAME, REGION_TYPE only. Names are kept, nothing is removed, no tick is needed."""
    session, db, _ = env
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["STORE CODE", "CLUSTER NAME", "REGION_TYPE"])
    ws.append(["APM", "Patna", "North"])
    ws.append(["NEW", "Ballia", "North"])                       # not in the stored master: reported, not added
    path = tmp_path / "STORE MASTER.xlsx"
    wb.save(path)
    at = AppTest.from_function(_panel, args=(db, str(path)), default_timeout=60)
    at.session_state["operator"] = "Aditi Agarwal"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("no site-name column" in i.value for i in at.info)
    assert any("NEW" in w.value and "NOT in the store master" in w.value for w in at.warning)
    assert not any("would be removed" in e.value for e in at.error)
    save = next(b for b in at.button if b.label == "Save as the new store master")
    assert not save.disabled                                    # nothing is removed, so nothing to confirm
    save.click().run()
    assert not at.exception, [e.value for e in at.exception]
    session.expire_all()
    rows = {r["store"]: r for r in mdb.current_rows(session, "store")}
    assert set(rows) == {"APM", "OLD"}                          # no store added or removed
    assert rows["APM"]["store_name"] == "CITYKART STORES PVT. LTD. (ARRAH-APM)" and rows["APM"]["cluster"] == "Patna"
    assert "cluster" not in rows["OLD"]                         # not in the file: untouched, shows as Unassigned
