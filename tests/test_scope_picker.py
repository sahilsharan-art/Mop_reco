"""The Region | Cluster | Store picker at the top of the sidebar (2026-11-28): its choices, and the sidebar font fix."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import dashboard_common as dc
from mop_reco import store_scope as ss


class FakeState(dict):
    """Stands in for st.session_state: the callbacks only read, write and .get."""


@pytest.fixture
def state(monkeypatch):
    fake = FakeState({dc.K_REGION: ss.ALL_REGIONS, dc.K_CLUSTER: ss.ALL_CLUSTERS, dc.K_STORE: "All stores", dc.K_LEVEL: "region"})
    monkeypatch.setattr(dc.st, "session_state", fake)
    return fake


def test_choosing_a_region_resets_the_cluster_and_store_and_gives_the_store_list_a_fresh_widget(state):
    state[dc.K_CLUSTER], state[dc.K_STORE] = "BR1", "ABD"
    dc._scope_pick_region("R2")
    assert state[dc.K_REGION] == "R2" and state[dc.K_CLUSTER] == ss.ALL_CLUSTERS and state[dc.K_STORE] == "All stores"
    assert state[dc.K_SCOPE_GEN] == 1                    # a new key for the Store list: Streamlit never reuses a stale selection


def test_choosing_a_cluster_resets_only_the_store(state):
    state[dc.K_REGION], state[dc.K_STORE] = "R1", "ABD"
    dc._scope_pick_cluster("BR2")
    assert state[dc.K_REGION] == "R1" and state[dc.K_CLUSTER] == "BR2" and state[dc.K_STORE] == "All stores"


def test_the_level_buttons_only_change_what_is_shown(state):
    state[dc.K_REGION], state[dc.K_CLUSTER] = "R1", "BR1"
    dc._scope_set_level("store")
    assert state[dc.K_LEVEL] == "store" and state[dc.K_REGION] == "R1" and state[dc.K_CLUSTER] == "BR1"


def test_the_picker_sits_above_the_line_and_the_old_selectboxes_are_gone():
    source = (APP / "dashboard_common.py").read_text(encoding="utf-8")
    body = source[source.index("def sidebar("):]
    assert body.index("_scope_picker(hierarchy, loaded)") < body.index("st.divider()")          # Region | Cluster | Store, then the line
    assert 'st.selectbox("Region"' not in source and 'st.selectbox("Cluster"' not in source
    assert '("region", "Region"), ("cluster", "Cluster"), ("store", "Store")' in source          # the three buttons, in that order


def test_sidebar_button_text_is_sized_on_the_paragraph_inside_the_button():
    """The 12px on the <button> never reached the words (an inner <p> is 16px); the size must be set on the <p> itself."""
    css = (APP / "assets" / "theme.css").read_text(encoding="utf-8")
    assert 'section[data-testid="stSidebar"] .stButton button p' in css
    rule = css[css.index('section[data-testid="stSidebar"] .stButton button p'):]
    assert "font-size: 12px" in rule[:400] and "white-space: nowrap" in rule[:400]
    assert ".st-key-rcs .stButton button" in css and ".mop-rcs-crumb" in css
