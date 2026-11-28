"""The Region | Cluster | Store buttons at the top of the sidebar (2026-11-28): each button IS its filter. Their choices, and the sidebar font fix."""

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
    fake = FakeState({dc.K_REGION: ss.ALL_REGIONS, dc.K_CLUSTER: ss.ALL_CLUSTERS, dc.K_STORE: "All stores"})
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


def test_the_store_list_copies_its_value_into_the_plain_key_before_the_rerun(state):
    """So the Store button's label, drawn earlier in the same run, already shows the choice ("Store: BIA")."""
    state["w_flt_store_0"] = "BIA"
    dc._scope_store_changed("w_flt_store_0")
    assert state[dc.K_STORE] == "BIA"


def test_each_button_is_its_own_filter_and_sits_above_the_line():
    source = (APP / "dashboard_common.py").read_text(encoding="utf-8")
    body = source[source.index("def sidebar("):]
    assert body.index("_scope_picker(hierarchy, loaded)") < body.index("st.divider()")          # the buttons, then the line
    picker = source[source.index("def _scope_picker("):source.index("def sidebar(")]
    # three popover buttons in the order Region, Cluster, Store; a chosen value turns the button primary and shows "Region: R1"
    assert picker.index('"Region"') < picker.index('"Cluster"') < picker.index('"Store"')
    assert picker.count(".popover(") == 3 and 'type="primary" if' in picker and 'f"{name}: {chosen}"' in picker
    assert "st.selectbox(\"Region\"" not in source and "st.selectbox(\"Cluster\"" not in source   # no separate dropdowns any more


def test_sidebar_button_text_is_sized_on_the_paragraph_inside_the_button():
    """The 12px on the <button> never reached the words (an inner <p> is 16px); the size must be set on the <p> itself."""
    css = (APP / "assets" / "theme.css").read_text(encoding="utf-8")
    assert 'section[data-testid="stSidebar"] .stButton button p' in css
    rule = css[css.index('section[data-testid="stSidebar"] .stButton button p'):]
    assert "font-size: 12px" in rule[:400] and "white-space: nowrap" in rule[:400]
    # the picker's popover buttons and the popover panel (drawn outside the sidebar) are sized too
    assert 'button[data-testid="stPopoverButton"] p' in css and 'div[data-testid="stPopoverBody"] .stButton button p' in css
    assert chr(21) not in css and "\\25BE" in css            # the caret is a CSS escape, not a stray control character


def test_the_amounts_buttons_toggle_and_the_unit_is_applied_at_the_start_of_every_page(monkeypatch):
    from mop_reco import formatting
    fake = FakeState()
    monkeypatch.setattr(dc.st, "session_state", fake)
    dc._unit_toggle("crore")
    assert fake[dc.K_UNIT] == "crore"
    dc._unit_toggle("full")
    assert fake[dc.K_UNIT] == "full"
    dc._unit_toggle("full")                                  # choosing the chosen one again goes back to the standard view
    assert fake[dc.K_UNIT] == "auto"
    source = (APP / "dashboard_common.py").read_text(encoding="utf-8")
    page_body = source[source.index("def page("):source.index("def _store_hierarchy")] if "def _store_hierarchy" in source else source
    assert "formatting.set_unit(st.session_state.get(K_UNIT" in source
    body = source[source.index("def sidebar("):]
    assert body.index("_unit_picker()") < body.index("st.divider()")          # the Amounts buttons sit above the line too
    formatting.set_unit("auto")
