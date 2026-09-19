# -*- coding: utf-8 -*-
from pathlib import Path
p = Path('tests/test_store_sheet.py')
s = p.read_text(encoding='utf-8')

# --- 1. extend the cms() builder with ADJ and RECOVEREDAMOUNT --------------
old_cms = '''def cms(rows):
    return pd.DataFrame(rows, columns=["TYPE", "store", "DEPDATE", "CRDATE",
                                       "SLIPAMOUNT"])'''
new_cms = '''def cms(rows, adj=None, recovered=None):
    """`adj` and `recovered` are optional parallel lists for the ADJ and
    RECOVEREDAMOUNT columns, so most tests (which only care about COL /
    SLIPAMOUNT) do not have to pad every row with zeros."""
    frame = pd.DataFrame(rows, columns=["TYPE", "store", "DEPDATE", "CRDATE",
                                        "SLIPAMOUNT"])
    frame["ADJ"] = adj if adj is not None else 0.0
    frame["RECOVEREDAMOUNT"] = recovered if recovered is not None else 0.0
    return frame'''
assert old_cms in s, "cms() builder anchor"
s = s.replace(old_cms, new_cms, 1)

# --- 2. rewrite the RULE 2 test block ---------------------------------------
old_block_start = s.index('# ---------------------------------------------------------------------------\n# RULE 2 — COL only in J, the rest reported not discarded')
old_block_end = s.index('# ---------------------------------------------------------------------------\n# The carry-forward')
old_block = s[old_block_start:old_block_end]

new_block = '''# ---------------------------------------------------------------------------
# RULE 2 — J is built from three types, each via its own column; ZPO ignored
# ---------------------------------------------------------------------------

def test_COL_uses_SLIPAMOUNT():
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0)])
    received = ss.cms_received_by_deposit_date(rows)
    assert len(received) == 1
    assert received.iloc[0]["cash_received"] == 1_000.0


def test_ADJ_CR_uses_the_ADJ_column_not_SLIPAMOUNT():
    """The one rule that matters most here: SLIPAMOUNT on an ADJ.CR. row is
    the ORIGINAL slip being adjusted, not the size of the adjustment. Using it
    would book the whole original slip a second time. Confirmed against the
    raw July file: the three ADJ.CR. rows carry ADJ 103/7/8 against SLIPAMOUNT
    47,400/1,27,722/95,760 — wildly different figures."""
    rows = cms([("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 47_400.0)],
              adj=[103.0])
    received = ss.cms_received_by_deposit_date(rows)
    assert len(received) == 1
    assert received.iloc[0]["cash_received"] == 103.0   # ADJ, not SLIPAMOUNT


def test_ADJ_DB_subtracts_RECOVEREDAMOUNT():
    """The bank recovering an earlier overcredit reduces what the store is
    owed, so it is SUBTRACTED from J."""
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.DB.", "ABD", "2026-07-01", "2026-07-01", 0.0)],
              recovered=[0.0, 250.0])
    received = ss.cms_received_by_deposit_date(rows)
    assert received.iloc[0]["cash_received"] == 750.0   # 1,000 - 250


def test_ZPO_never_reaches_J_regardless_of_SLIPAMOUNT():
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ZPO", "ABD", "2026-07-01", "2026-07-01", 9_999.0)])
    received = ss.cms_received_by_deposit_date(rows)
    assert len(received) == 1
    assert received.iloc[0]["cash_received"] == 1_000.0


def test_all_three_types_combine_on_the_same_store_date():
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 500.0),
               ("ADJ.DB.", "ABD", "2026-07-01", "2026-07-01", 0.0)],
              adj=[0.0, 50.0, 0.0], recovered=[0.0, 0.0, 20.0])
    received = ss.cms_received_by_deposit_date(rows)
    # 1,000 (COL/SLIPAMOUNT) + 50 (ADJ.CR/ADJ) - 20 (ADJ.DB/RECOVEREDAMOUNT)
    assert received.iloc[0]["cash_received"] == 1_030.0


def test_cms_contribution_by_type_reports_the_source_column():
    """So the operator can see the split rather than one blended J figure."""
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 47_400.0)],
              adj=[0.0, 103.0])
    report = ss.cms_contribution_by_type(rows)
    by_type = report.set_index("type")
    assert by_type.loc["COL", "source_column"] == "SLIPAMOUNT"
    assert by_type.loc["COL", "value"] == 1_000.0
    assert by_type.loc["ADJ.CR.", "source_column"] == "ADJ"
    assert by_type.loc["ADJ.CR.", "value"] == 103.0    # not 47,400
    assert by_type.loc["ADJ.DB.", "value"] == 0.0


def test_only_ZPO_is_reported_as_excluded_now():
    """ADJ.CR. and ADJ.DB. feed J via their own columns and are no longer
    'excluded' — only ZPO is."""
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 500.0),
               ("ADJ.DB.", "ABD", "2026-07-05", "2026-07-05", 0.0),
               ("ZPO", "ABD", None, None, 9_999.0)])
    report = ss.excluded_credit_types(rows)
    assert set(report["type"]) == {"ZPO"}
    assert "COL" not in set(report["type"])
    assert "ADJ.CR." not in set(report["type"])
    assert "ADJ.DB." not in set(report["type"])


def test_a_ZPO_row_with_no_deposit_date_is_not_forced_onto_a_date():
    """Rule 6: a figure is never attributed to a day it does not belong to."""
    report = ss.excluded_credit_types(cms([("ZPO", None, None, None, 500.0)]))
    assert report.iloc[0]["date"] == "(no deposit date)"
    assert report.iloc[0]["store"] == "(unmapped)"


'''

s = s[:old_block_start] + new_block + s[old_block_end:]
p.write_text(s, encoding='utf-8')
print("RULE 2 tests rewritten")
