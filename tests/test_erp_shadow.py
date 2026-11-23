"""The side-by-side comparison (mop_reco/erp_shadow.py): it must pass identical data, tolerate the known harmless difference, and catch
every real one (a missing bill, a different amount, a different payment reference)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import erp_shadow as sh


def pos(rows):
    cols = ["bill_no", "mop", "amount", "bill_time", "created_by", "tpedc_parse_failed", "paytm_ref", "axis_ref", "bajaj_ref"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


BASE = [
    ("B1", "PAYTM EDC", 100.0, "10:00:00", "T1", False, "P1", None, None),
    ("B2", "EDC OTHER AXIS", 200.0, "10:05:00", "T1", False, None, "A2", None),
    ("B3", "Bajajpay UPI", 300.0, "10:10:00", "T2", False, None, None, "J3"),
    ("B4", "Cash", 400.0, "10:15:00", "T2", False, None, None, None),
    ("B5", "Credit Note Received", 398.0, "10:20:00", "T3", False, None, None, None),
]


def test_identical_frames_agree():
    r = sh.compare_pos(pos(BASE), pos(BASE))
    assert sh.pos_agrees(r) and r["both"] == 5 and r["ref_differs"] == 0 and r["time_differs"] == 0 and r["operator_differs"] == 0
    assert r["amount_erp"] == r["amount_file"] == 1398.0


def test_the_credit_note_split_is_reported_but_is_not_a_difference():
    erp = pos(BASE[:4] + [("B5", "Credit Note Received", 199.0, "10:20:00", "T3", False, None, None, None),
                          ("B5", "Credit Note Received", 199.0, "10:20:00", "T3", False, None, None, None)])
    r = sh.compare_pos(erp, pos(BASE))
    assert r["lines_differ"] == 1 and r["lines_erp"] == 6 and r["lines_file"] == 5
    assert r["amount_differs"] == 0 and sh.pos_agrees(r)


def test_a_missing_bill_on_either_side_fails():
    r = sh.compare_pos(pos(BASE[:4]), pos(BASE))
    assert r["only_in_file"] == 1 and r["only_in_file_amount"] == 398.0 and not sh.pos_agrees(r)
    assert r["examples_only_file"][0]["bill_no"] == "B5"
    r = sh.compare_pos(pos(BASE), pos(BASE[:3]))
    assert r["only_in_erp"] == 2 and not sh.pos_agrees(r)


def test_a_different_amount_fails():
    erp = pos([BASE[0][:2] + (101.0,) + BASE[0][3:]] + BASE[1:])
    r = sh.compare_pos(erp, pos(BASE))
    assert r["amount_differs"] == 1 and not sh.pos_agrees(r)
    assert r["examples_amount"][0]["v_erp"] == 101.0 and r["examples_amount"][0]["v_file"] == 100.0


def test_a_different_payment_reference_fails_and_names_the_gateway():
    erp = pos([BASE[0][:6] + ("PX9",) + BASE[0][7:]] + BASE[1:])
    r = sh.compare_pos(erp, pos(BASE))
    assert r["ref_differs"] == 1 and r["ref_differs_by_gateway"] == {"paytm": 1, "axis": 0, "bajaj": 0} and not sh.pos_agrees(r)
    ex = r["examples_refs"][0]
    assert ex["gateway"] == "paytm" and ex["file"] == "P1" and ex["erp"] == "PX9"


def test_a_reference_present_on_one_side_only_fails():
    erp = pos([BASE[0][:6] + (None,) + BASE[0][7:]] + BASE[1:])
    r = sh.compare_pos(erp, pos(BASE))
    assert r["ref_differs"] == 1 and r["examples_refs"][0]["erp"] == "(none)"


def test_time_operator_and_parse_failures_are_counted():
    erp = pos([("B1", "PAYTM EDC", 100.0, "10:00:01", "T9", True, "P1", None, None)] + BASE[1:])
    r = sh.compare_pos(erp, pos(BASE))
    assert r["time_differs"] == 1 and r["operator_differs"] == 1 and r["parse_failed_erp"] == 1 and r["parse_failed_file"] == 0


def test_empty_sides_do_not_crash():
    r = sh.compare_pos(pos([]), pos(BASE))
    assert r["only_in_file"] == 5 and r["lines_erp"] == 0 and not sh.pos_agrees(r)
    assert sh.compare_pos(pos([]), pos([]))["both"] == 0


def ptc(rows):
    cols = ["store", "voucher_no", "mode", "head", "ledger", "amount", "is_deposit", "deposit_amount"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


PTC = [("ABD", "V1", "PAYMENT", "Cash/CMS Deposit", "CMS-RECD.", -87937.0, True, 87937.0),
       ("ABD", "V2", "PAYMENT", "Tea", "EXP", -320.0, False, 0.0),
       ("AGT", "V3", "RECEIVE", "Income", "INC", 150.0, False, 0.0)]


def test_ptc_identical_agrees_and_reports_the_deposit_total():
    r = sh.compare_ptc(ptc(PTC), ptc(PTC))
    assert sh.ptc_agrees(r) and r["vouchers_erp"] == 3 and r["deposit_total_erp"] == r["deposit_total_file"] == 87937.0


def test_ptc_missing_voucher_amount_and_classification_differences_fail():
    r = sh.compare_ptc(ptc(PTC[:2]), ptc(PTC))
    assert r["only_in_file"] == 1 and r["examples_only_file"][0]["key"] == "AGT|V3" and not sh.ptc_agrees(r)
    changed = [PTC[0][:5] + (-87000.0,) + PTC[0][6:]] + PTC[1:]
    r = sh.compare_ptc(ptc(changed), ptc(PTC))
    assert r["amount_differs"] == 1 and r["examples_amount"][0]["key"] == "ABD|V1" and not sh.ptc_agrees(r)
    reclassified = [PTC[0][:3] + ("Other head",) + PTC[0][4:]] + PTC[1:]
    assert sh.compare_ptc(ptc(reclassified), ptc(PTC))["class_differs"] == 1
