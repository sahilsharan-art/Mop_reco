"""Audit Phase 2: R1 against an INDEPENDENT ORACLE written from the specification (CLAUDE.md 6.1-6.3), not from the engine's code.

The oracle is plain Python: nested loops, Decimal amounts, no pandas joins, no ranking trick. It is run beside `r1_bill_txn.run_r1` on
  * hand-built scenarios whose outcome is also asserted BY HAND (so a mistake in the oracle cannot hide a mistake in the engine), and
  * a few hundred random scenarios built to collide (few stores, dates, amounts and references; duplicates; failed rows; refunds; blanks).
Any disagreement is a finding. Synthetic data only.
"""
import random
import sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import r1_bill_txn as r1  # noqa: E402

GATEWAYS = ("paytm", "axis", "bajaj")                      # applied in this order (6.3)
EDC_OTHER = {"EDC OTHER", "EDC OTHER AXIS"}                 # the only MOPs Tier 2 may touch (6.1 / 6.3)
D0 = date(2026, 7, 20)


# --------------------------------------------------------------------------------------------------- the oracle
def oracle(bills, txns):
    """bills: digital sale-universe lines in frame order. txns: every gateway row. Returns ({(bill_no, line_seq): outcome}, unmapped_pool_indexes)."""
    pool = [t for t in txns if str(t["status"]).upper() == "SUCCESS" and str(t["type"]).upper() == "ACQUIRING"]
    consumed, out = set(), {}
    for gw in GATEWAYS:                                      # Tier 1: gateway by gateway, bill by bill in frame order
        for b in bills:
            key = (b["bill_no"], b["line_seq"])
            if out.get(key, {}).get("matched"):
                continue
            ref = (b.get(gw + "_ref") or "").strip()
            if not ref:
                continue
            for i, t in enumerate(pool):
                if i in consumed or t["gateway"] != gw or (t["ref"] or "").strip() != ref or t["amount"] != b["amount"]:
                    continue
                consumed.add(i)
                out[key] = {"matched": True, "tier": "Tier 1", "gateway": gw, "ref": ref}
                break
    residual = [i for i in range(len(pool)) if i not in consumed]      # what Tier 1 left
    taken = set()
    for b in bills:                                          # Tier 2: EDC OTHER only, candidates = residual after Tier 1
        key = (b["bill_no"], b["line_seq"])
        if out.get(key, {}).get("matched") or b["mop"] not in EDC_OTHER:
            continue
        cands = [i for i in residual if pool[i]["store"] == b["store"] and pool[i]["amount"] == b["amount"] and pool[i]["date"] == b["date"]]
        if len(cands) == 1:
            if cands[0] not in taken:                         # a transaction can be consumed once
                taken.add(cands[0])
                out[key] = {"matched": True, "tier": "Matched (EDC OTHER)", "gateway": pool[cands[0]]["gateway"], "ref": pool[cands[0]]["ref"]}
        elif len(cands) >= 2:
            out[key] = {"matched": False, "tier": "Ambiguous"}
    unmapped = [i for i in range(len(pool)) if i not in consumed and i not in taken]
    return out, sorted((pool[i]["gateway"], pool[i]["ref"], pool[i]["amount"]) for i in unmapped)


# ------------------------------------------------------------------------------------------- engine plumbing
def bill(no, mop, amount, store="AAA", d=D0, seq=1, paytm=None, axis=None, bajaj=None, klass=None):
    klass = klass or ("digital_noref" if mop in EDC_OTHER else "digital")
    return {"bill_no": no, "line_seq": seq, "mop": mop, "amount": D(str(amount)), "store": store, "date": d, "paytm_ref": paytm, "axis_ref": axis,
            "bajaj_ref": bajaj, "mop_class": klass, "in_sale_universe": True}


def txn(gw, ref, amount, store="AAA", d=D0, status="SUCCESS", type_="ACQUIRING"):
    return {"gateway": gw, "ref": ref, "amount": D(str(amount)), "store": store, "date": d, "status": status, "type": type_}


def run_engine(bills, txns, extra_bills=()):
    frame = pd.DataFrame([{**b, "business_date": b["date"], "amount": float(b["amount"]), "bill_time": None} for b in list(bills) + list(extra_bills)])
    if "in_sale_universe" in frame:
        pass
    cols = {"paytm": [], "axis": [], "bajaj": []}
    for t in txns:
        base = {"business_date": t["date"], "store": t["store"], "transaction_time": None}
        if t["gateway"] == "paytm":
            cols["paytm"].append({**base, "transaction_id": t["ref"], "amount": float(t["amount"]), "status": t["status"],
                                  "transaction_type": t["type"], "payment_mode": "UPI", "reference_transaction_id": None})
        elif t["gateway"] == "axis":
            cols["axis"].append({**base, "RRN": t["ref"], "GROSS_AMT": float(t["amount"]), "CARD_TYPE": "CREDIT"})
        else:
            cols["bajaj"].append({**base, "Transaction Id": t["ref"], "Transaction Amount": float(t["amount"]), "Transaction Status": t["status"],
                                  "Card Scheme": "UPI"})
    frames = {k: (pd.DataFrame(v) if v else None) for k, v in cols.items()}
    return r1.run_r1(frame, frames["paytm"], frames["axis"], frames["bajaj"])


def engine_outcome(result):
    out = {}
    for row in result.bill_lines.itertuples():
        tier = row.match_tier if isinstance(row.match_tier, str) else None
        out[(row.bill_no, row.line_seq)] = {"matched": bool(row.matched), "tier": tier,
                                            "gateway": row.matched_gateway if row.matched else None, "ref": row.matched_txn_ref if row.matched else None}
    unmapped = sorted((u.gateway, u.txn_ref, D(str(round(u.amount, 2)))) for u in result.unmapped_txns.itertuples())
    return out, unmapped


def agree(bills, txns, extra_bills=()):
    want_out, want_unmapped = oracle(bills, txns)
    got_out, got_unmapped = engine_outcome(run_engine(bills, txns, extra_bills))
    for key in got_out:
        w = want_out.get(key, {"matched": False, "tier": None})
        g = got_out[key]
        assert (g["matched"], g["tier"]) == (w["matched"], w.get("tier")), f"{key}: engine {g} vs oracle {w}"
        if w["matched"]:
            assert (g["gateway"], g["ref"]) == (w["gateway"], w["ref"]), f"{key}: engine {g} vs oracle {w}"
    assert got_unmapped == want_unmapped, f"unmapped transactions differ: engine {got_unmapped} vs oracle {want_unmapped}"
    return got_out, got_unmapped


# ---------------------------------------------------------------------------- hand-built, hand-asserted scenarios
def test_exact_reference_and_amount_matches_and_consumes():
    out, un = agree([bill("B1", "PAYTM EDC", "100.00", paytm="P1")], [txn("paytm", "P1", "100.00")])
    assert out[("B1", 1)]["matched"] and out[("B1", 1)]["tier"] == "Tier 1" and un == []


def test_one_paisa_difference_is_not_a_match_and_the_payment_stays_unmapped():
    out, un = agree([bill("B1", "PAYTM EDC", "100.00", paytm="P1")], [txn("paytm", "P1", "100.01")])
    assert not out[("B1", 1)]["matched"] and un == [("paytm", "P1", D("100.01"))]


def test_failed_and_non_acquiring_rows_are_outside_the_matching_pool():
    out, un = agree([bill("B1", "PAYTM EDC", "100", paytm="P1"), bill("B2", "PaytmDQR", "50", paytm="P2")],
                    [txn("paytm", "P1", "100", status="FAILED"), txn("paytm", "P2", "50", type_="REFUND")])
    assert not out[("B1", 1)]["matched"] and not out[("B2", 1)]["matched"]
    assert un == [], "a FAILED or REFUND row is not an unmapped sale: it was never in the pool"


def test_status_is_compared_case_insensitively():
    out, _ = agree([bill("B1", "Bajajpay UPI", "70", bajaj="J1")], [txn("bajaj", "J1", "70", status="Success")])
    assert out[("B1", 1)]["matched"]


def test_one_transaction_cannot_back_two_bills():
    out, _ = agree([bill("B1", "PAYTM EDC", "100", paytm="P1"), bill("B2", "PAYTM EDC", "100", paytm="P1")], [txn("paytm", "P1", "100")])
    assert [out[("B1", 1)]["matched"], out[("B2", 1)]["matched"]] == [True, False], "first bill in frame order takes it, the second stays open"


def test_two_raw_rows_with_the_same_reference_serve_two_bills():
    # Axis files can carry the same RRN twice (82 such lines on Apr-Aug). Paytm ids are deduplicated at ingest, so they cannot.
    out, un = agree([bill("B1", "PAYTM EDC", "100", axis="A1"), bill("B2", "PAYTM EDC", "100", axis="A1")],
                    [txn("axis", "A1", "100"), txn("axis", "A1", "100")])
    assert out[("B1", 1)]["matched"] and out[("B2", 1)]["matched"] and un == []


def test_one_resolving_reference_is_enough_and_the_gateways_apply_in_order():
    out, _ = agree([bill("B1", "PAYTM EDC", "100", paytm="NOPE", axis="A1")], [txn("axis", "A1", "100")])
    assert out[("B1", 1)]["matched"] and out[("B1", 1)]["gateway"] == "axis"
    out, _ = agree([bill("B2", "PAYTM EDC", "100", paytm="P1", axis="A1")], [txn("paytm", "P1", "100"), txn("axis", "A1", "100")])
    assert out[("B2", 1)]["gateway"] == "paytm", "Paytm is applied first; the bill never competes in a later gateway"


def test_a_late_transaction_still_matches_and_the_date_difference_is_recorded():
    result = run_engine([bill("B1", "PAYTM EDC", "100", paytm="P1")], [txn("paytm", "P1", "100", d=D0 + timedelta(days=7))])
    row = result.bill_lines.iloc[0]
    assert bool(row["matched"]) and int(row["date_diff_days"]) == 7 and isinstance(row["match_note"], str), "never silent: a 7-day gap is recorded"
    agree([bill("B1", "PAYTM EDC", "100", paytm="P1")], [txn("paytm", "P1", "100", d=D0 + timedelta(days=7))])


def test_edc_other_one_candidate_matches_two_are_ambiguous_none_stays_open():
    out, _ = agree([bill("E1", "EDC OTHER", "500")], [txn("paytm", "T1", "500")])
    assert out[("E1", 1)]["tier"] == "Matched (EDC OTHER)"
    out, un = agree([bill("E2", "EDC OTHER", "500")], [txn("paytm", "T1", "500"), txn("paytm", "T2", "500")])
    assert out[("E2", 1)]["tier"] == "Ambiguous" and not out[("E2", 1)]["matched"] and len(un) == 2, "time never decides: nothing is consumed"
    out, _ = agree([bill("E3", "EDC OTHER", "500")], [txn("paytm", "T1", "500", d=D0 + timedelta(days=1))])
    assert not out[("E3", 1)]["matched"], "a different business date is not a candidate"
    out, _ = agree([bill("E4", "EDC OTHER", "500", store="AAA")], [txn("paytm", "T1", "500", store="BBB")])
    assert not out[("E4", 1)]["matched"], "a different store is not a candidate"


def test_tier_two_is_never_applied_to_a_mop_that_should_carry_a_reference():
    out, un = agree([bill("B1", "PAYTM EDC", "500")], [txn("paytm", "T1", "500")])
    assert not out[("B1", 1)]["matched"] and len(un) == 1


def test_a_transaction_consumed_by_tier_one_is_not_a_tier_two_candidate():
    out, _ = agree([bill("B1", "PAYTM EDC", "500", paytm="P1"), bill("E1", "EDC OTHER", "500")], [txn("paytm", "P1", "500")])
    assert out[("B1", 1)]["matched"] and not out[("E1", 1)]["matched"]


def test_two_edc_other_bills_and_one_transaction_first_bill_takes_it():
    out, _ = agree([bill("E1", "EDC OTHER", "500"), bill("E2", "EDC OTHER", "500")], [txn("paytm", "T1", "500")])
    assert [out[("E1", 1)]["matched"], out[("E2", 1)]["matched"]] == [True, False]


def test_rows_outside_the_sale_universe_or_not_digital_take_no_part():
    extra = [{**bill("C1", "Cash", "100", klass="cash"), "paytm_ref": "P1"},
             {**bill("N1", "PAYTM EDC", "100", paytm="P1"), "in_sale_universe": False}]
    result = run_engine([], [txn("paytm", "P1", "100")], extra_bills=extra)
    assert len(result.bill_lines) == 0 and len(result.unmapped_txns) == 1


def test_refund_linkage_names_the_missing_original_and_never_hides_it():
    paytm = pd.DataFrame([
        {"transaction_id": "S1", "transaction_type": "ACQUIRING", "business_date": D0, "amount": 100.0, "status": "SUCCESS", "payment_mode": "UPI",
         "store": "AAA", "reference_transaction_id": None},
        {"transaction_id": "R1", "transaction_type": "REFUND", "business_date": D0, "amount": 100.0, "status": "SUCCESS", "payment_mode": "",
         "store": None, "reference_transaction_id": "S1"},
        {"transaction_id": "R2", "transaction_type": "REFUND", "business_date": D0, "amount": 40.0, "status": "SUCCESS", "payment_mode": "",
         "store": None, "reference_transaction_id": "GONE"},
        {"transaction_id": "C1", "transaction_type": "CHARGEBACK", "business_date": D0, "amount": 10.0, "status": "SUCCESS", "payment_mode": "",
         "store": None, "reference_transaction_id": "S1"}])
    linked, exceptions = r1.link_non_acquiring(paytm)
    assert sorted(linked["transaction_id"]) == ["C1", "R1"], "an original that is present links the refund and the chargeback"
    assert list(exceptions["transaction_id"]) == ["R2"], "an original that is absent is an exception, named"


def test_every_reference_form_the_terminal_reply_can_take():
    from mop_reco import ingest
    paytm = '{"head": {}, "body": {"acquirementId": "2026072011103000028"}}'
    dqr = '{"head": {}, "body": {"txnId": "TX9"}}'
    axis = '{"head": {}, "body": {"retrievalReferenceNo": "973933049280", "issuerMaskCardNo": "XXXX1234"}}'
    axis_upi = '{"head": {}, "body": {"retrievalReferenceNo": "973933049281"}}'
    bajaj = '{"rrn": "BJ77", "tid": "T1"}'
    assert ingest._extract_pos_refs(paytm) == ("2026072011103000028", None, None, False)
    assert ingest._extract_pos_refs(dqr) == ("TX9", None, None, False)
    assert ingest._extract_pos_refs(axis) == (None, "973933049280", None, False)
    assert ingest._extract_pos_refs(axis_upi) == (None, "973933049281", None, False), "an Axis UPI row has no card number and is still matched"
    assert ingest._extract_pos_refs(bajaj) == (None, None, "BJ77", False)
    assert ingest._extract_pos_refs(None) == (None, None, None, False) and ingest._extract_pos_refs("") == (None, None, None, False)
    assert ingest._extract_pos_refs("{not json")[3] is True, "malformed JSON is reported as a parse failure, never turned into a blank reference"
    assert ingest._extract_pos_refs('["a"]') == (None, None, None, False)
    assert ingest._extract_pos_refs('{"body": {"acquirementId": "0"}}') == (None, None, None, False), "a zero reference is blank"


# ------------------------------------------------------------------------------------- random collision scenarios
def _scenario(rng):
    stores, days = ["AAA", "BBB", "CCC"], [D0 + timedelta(days=k) for k in range(3)]
    amounts = [D("100.00"), D("100.01"), D("250.50"), D("999.99")]
    refs = {"paytm": ["P1", "P2", "P3", "P4", "P5", "P6"], "axis": ["A1", "A2", "A3"], "bajaj": ["J1", "J2"]}
    paytm_free = list(refs["paytm"])
    rng.shuffle(paytm_free)
    bills, txns = [], []
    for i in range(rng.randint(6, 16)):
        mop = rng.choice(["PAYTM EDC", "PaytmDQR", "EDC OTHER", "EDC OTHER AXIS", "Bajajpay UPI", "PAYTM EDC"])
        kw = {}
        if mop not in EDC_OTHER:
            for gw in GATEWAYS:
                if rng.random() < 0.55:
                    kw[gw] = rng.choice(refs[gw])
        elif rng.random() < 0.15:
            kw["paytm"] = rng.choice(refs["paytm"])               # an EDC OTHER bill that happens to carry a reference
        bills.append(bill(f"B{i}", mop, rng.choice(amounts), store=rng.choice(stores), d=rng.choice(days), seq=rng.choice([1, 1, 2]), **kw))
    seen = set()
    bills = [b for b in bills if not ((b["bill_no"], b["line_seq"]) in seen or seen.add((b["bill_no"], b["line_seq"])))]
    for _ in range(rng.randint(6, 20)):
        gw = rng.choice(GATEWAYS)
        if gw == "paytm" and not paytm_free:
            gw = "axis"
        ref = paytm_free.pop() if gw == "paytm" else rng.choice(refs[gw])      # Paytm ids are unique after ingest
        txns.append(txn(gw, ref, rng.choice(amounts), store=rng.choice(stores), d=rng.choice(days),
                        status=rng.choice(["SUCCESS"] * 6 + ["FAILED", "success"]) if gw != "axis" else "SUCCESS",      # the Axis report has no status
                        type_=rng.choice(["ACQUIRING"] * 6 + ["REFUND"]) if gw == "paytm" else "ACQUIRING"))             # only Paytm has a type
    return bills, txns


@pytest.mark.parametrize("seed", range(300))
def test_the_engine_agrees_with_the_independent_oracle_on_random_collision_scenarios(seed):
    bills, txns = _scenario(random.Random(seed))
    agree(bills, txns)


def test_an_undeduplicated_paytm_frame_is_refused_with_a_plain_message():
    """D-003: the loader dedupes on transaction_id; an un-deduped frame must fail clearly, not with a pandas reindexing error."""
    paytm = pd.DataFrame([
        {"transaction_id": "S1", "transaction_type": "ACQUIRING", "business_date": D0, "amount": 1.0, "status": "SUCCESS",
         "payment_mode": "UPI", "store": "AAA", "reference_transaction_id": None},
        {"transaction_id": "S1", "transaction_type": "ACQUIRING", "business_date": D0, "amount": 1.0, "status": "SUCCESS",
         "payment_mode": "UPI", "store": "AAA", "reference_transaction_id": None},
        {"transaction_id": "R1", "transaction_type": "REFUND", "business_date": D0, "amount": 1.0, "status": "SUCCESS",
         "payment_mode": "", "store": None, "reference_transaction_id": "S1"}])
    with pytest.raises(ValueError, match="duplicated transaction_id"):
        r1.link_non_acquiring(paytm)
