"""Region -> Cluster -> Store: the filter value, the hierarchy, and a real query with a set of stores."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q
from mop_reco import store_scope as ss
from mop_reco.db import Exception_, Run, get_engine, get_session, init_db

ROWS = [
    {"store_name": "A", "store": "ABD", "cluster": "Patna", "region": "North"},
    {"store_name": "B", "store": "AGT", "cluster": "Patna", "region": "North"},
    {"store_name": "C", "store": "BIA", "cluster": "Ballia", "region": "North"},
    {"store_name": "D", "store": "KPH", "cluster": "Kolkata", "region": "East"},
    {"store_name": "E", "store": "XYZ", "cluster": "", "region": ""},
]


def test_normalize():
    assert ss.normalize(None) is None and ss.normalize("") is None and ss.normalize("All stores") is None
    assert ss.normalize("ABD") == "ABD"
    assert ss.normalize(["B", "A", "B"]) == ("A", "B")
    assert ss.normalize(["A"]) == "A"                       # one store stays one store
    with pytest.raises(ValueError):
        ss.normalize([])                                    # an empty selection is never "every store"


def test_mask_contains_and_label():
    s = pd.Series(["A", "B", "C"])
    assert ss.mask(s, None).tolist() == [True, True, True]
    assert ss.mask(s, "B").tolist() == [False, True, False]
    assert ss.mask(s, ("A", "C")).tolist() == [True, False, True]
    assert ss.contains(None, "Z") and ss.contains("A", "A") and not ss.contains("A", "B") and ss.contains(("A", "B"), "B")
    assert ss.label(None) == "all stores" and ss.label("ABD") == "ABD only"
    assert ss.label(("A", "B"), cluster="Patna") == "cluster Patna, 2 stores"
    assert ss.label(("A", "B", "C"), region="North") == "region North, 3 stores"


def test_hierarchy_cascades_and_keeps_unassigned():
    h = ss.Hierarchy.from_rows(ROWS)
    loaded = ["ABD", "AGT", "BIA", "KPH", "XYZ", "NOT_IN_MASTER"]
    assert h.regions(loaded) == ["East", "North", ss.UNASSIGNED]            # Unassigned is last, never dropped
    assert h.clusters("North", loaded) == ["Ballia", "Patna"]
    assert h.stores("North", "Patna", loaded) == ["ABD", "AGT"]
    assert h.stores(ss.UNASSIGNED, None, loaded) == ["NOT_IN_MASTER", "XYZ"]  # a loaded store the master lacks is still reachable
    assert h.stores(None, None, ["ABD"]) == ["ABD"]                          # only loaded stores are offered
    assert h.has_any and not ss.Hierarchy.from_rows([{"store": "A"}]).has_any


def test_resolve():
    h = ss.Hierarchy.from_rows(ROWS)
    loaded = ["ABD", "AGT", "BIA", "KPH"]
    assert ss.resolve(h, loaded) is None
    assert ss.resolve(h, loaded, region="North") == ("ABD", "AGT", "BIA")
    assert ss.resolve(h, loaded, region="North", cluster="Patna") == ("ABD", "AGT")
    assert ss.resolve(h, loaded, region="East", cluster="Kolkata") == "KPH"
    assert ss.resolve(h, loaded, region="North", cluster="Patna", store="BIA") == "BIA"      # a named store wins
    with pytest.raises(ValueError):
        ss.resolve(h, ["KPH"], region="North")                                  # a region with no loaded store: said, never "all"


def test_a_cluster_name_under_two_regions_is_reported():
    rows = ROWS + [{"store_name": "F", "store": "ZZZ", "cluster": "Patna", "region": "South"}]
    assert ss.Hierarchy.from_rows(rows).clusters_in_several_regions() == {"Patna": ["North", "South"]}


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    run = Run(started_at=datetime(2026, 9, 1), finished_at=datetime(2026, 9, 1), business_dates=json.dumps(["2026-07-01"]),
              status="completed", triggered_by="t", notes="{}")
    s.add(run)
    s.flush()
    for i, (store, amount) in enumerate([("ABD", 100), ("AGT", 200), ("BIA", 400), ("KPH", 800)]):
        s.add(Exception_(exception_key=f"k{i}", reconciliation="R1", type="bill_not_matched", business_date="2026-07-01", store=store,
                         amount=amount, detail="{}", status="open", first_seen_run_id=run.id, last_seen_run_id=run.id))
    s.commit()
    yield s
    s.close()


def test_a_set_of_stores_filters_a_real_query(session):
    types = ("bill_not_matched",)

    def count(store):
        return q.exception_counts(session, "2026-07-01", "2026-07-01", store, types, ())[:2]

    assert count(None) == (4, 1500.0)
    assert count("KPH") == (1, 800.0)                        # one store: exactly as before
    assert count(("ABD", "AGT")) == (2, 300.0)               # a cluster
    assert count(("ABD", "AGT", "BIA")) == (3, 700.0)        # a region
