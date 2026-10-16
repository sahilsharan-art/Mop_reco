"""CMS Reco blocks: the chain, the two credit columns, what is kept out of them, and remarks."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import cms_blocks as cb  # noqa: E402
from mop_reco.db import Base, get_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _slips(rows):
    return pd.DataFrame(rows, columns=["row_type", "dep_date", "cr_date", "store", "pickup_code", "slip_no", "amount"]).assign(
        location=None, adj=None, recovered=None)


def _dep(rows):
    return pd.DataFrame(rows, columns=["dep_date", "store", "amount"])


def test_blocks_chain_and_credit_columns():
    slips = _slips([("COL", "2026-04-01", "2026-04-01", "AAA", "P1", "1", 60.0),
                    ("COL", "2026-04-01", "2026-04-02", "AAA", "P1", "2", 30.0),
                    ("COL", "2026-04-01", "2026-04-04", "AAA", "P1", "3", 10.0)])    # T+3: neither column
    dep = _dep([("2026-04-01", "AAA", 100.0), ("2026-04-02", "AAA", 50.0)])
    m = cb.build_model(slips, dep, "2026-04-01", "2026-04-03")
    i = m.stores.index("AAA")
    assert m.cms1[0][i] == 60 and m.cms2[0][i] == 30
    assert m.closing[0][i] == 10                      # the T+3 slip is still outstanding
    assert m.opening[1][i] == 10 and m.closing[1][i] == 60
    assert m.opening[2][i] == 60
    assert all(c.ok for c in cb.block_controls(m, "2026-04-01", "2026-04-03"))
    s = cb.summary_frame(m, "2026-04-01", "2026-04-03").iloc[0]
    assert (s["opening"], s["deposit"], s["cms"], s["diff"]) == (0, 150, 90, 60)
    assert s["diff"] == m.closing[2][i]


def test_credited_later_lists_what_the_columns_miss():
    slips = _slips([("COL", "2026-04-01", "2026-04-01", "AAA", "P", "1", 5.0),
                    ("COL", "2026-04-01", "2026-05-14", "AAA", "P", "2", 7.0)])
    later = cb.credited_later(slips, "2026-04-01", "2026-04-30")
    assert len(later) == 1 and later.iloc[0]["lag_days"] == 43 and "exceptional" in later.iloc[0]["flag"]


def test_unresolved_and_bnk_stay_separate_and_in_totals():
    slips = _slips([("COL", "2026-04-01", "2026-04-01", cb.UNRESOLVED, "ZZ", "1", 9.0),
                    ("COL", "2026-04-01", "2026-04-01", "BNK", "B", "2", 4.0)])
    m = cb.build_model(slips, _dep([]), "2026-04-01", "2026-04-01")
    assert m.stores == ["BNK", cb.UNRESOLVED]
    assert cb.pseudo_store_totals(slips, "2026-04-01", "2026-04-01") == {"BNK": (1, 4.0), cb.UNRESOLVED: (1, 9.0)}


def test_opening_from_config_is_carried():
    m = cb.build_model(_slips([]), _dep([("2026-04-01", "AAA", 10.0)]), "2026-04-01", "2026-04-01", {"AAA": 5.0})
    assert m.closing[0][0] == 15


def test_remarks_survive_and_keep_history():
    engine = get_engine(":memory:") if False else None
    from sqlalchemy import create_engine
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    cb.set_remark(s, "AAA", "2026-08-01", "2026-08-31", "chasing agency", "Aditi")
    cb.set_remark(s, "AAA", "2026-08-01", "2026-08-31", "agency confirmed", "Aditi")
    assert cb.remarks_for(s, "2026-08-01", "2026-08-31")["AAA"][0] == "agency confirmed"
    assert len(cb.remark_history(s, "AAA", "2026-08-01", "2026-08-31")) == 2
    with pytest.raises(ValueError):
        cb.set_remark(s, "AAA", "2026-08-01", "2026-08-31", " ", "Aditi")
