"""FROZEN reference copy of the pre-vectorization R1 matcher (per-row `.at[]` loops).

This is the oracle the vectorized matcher in `mop_reco/r1_bill_txn.py` is proven
against: same inputs in, every output column and the consumed set identical.
Do NOT edit it and do NOT import it from application code - it is slow by
design (~1,250 bills/second) and exists only so `tests/test_r1_equivalence.py`
and `scripts/r1_equivalence.py` can compare against what the engine did
when Run #11 (docs/BASELINE_APR_AUG.md) was produced.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

logger = logging.getLogger("mop_reco.reference")

EDC_OTHER_MOPS = ("EDC OTHER", "EDC OTHER AXIS")

REF_COLUMN_BY_GATEWAY = {"paytm": "paytm_ref", "axis": "axis_ref", "bajaj": "bajaj_ref"}


def _date_diff_days(txn_date, bill_date) -> int:
    txn_d = txn_date if isinstance(txn_date, date) else date.fromisoformat(str(txn_date))
    bill_d = bill_date if isinstance(bill_date, date) else date.fromisoformat(str(bill_date))
    return (txn_d - bill_d).days


def match_tier1(bills: pd.DataFrame, pool: pd.DataFrame, on_progress=None):
    """Reference match across all three gateways.

    **A gateway reference is globally unique (2026-09-13) — the search is NOT
    restricted to the bill's own business date, or any window around it.**
    Four July bills (SA62/024925, /024926, /024927, SA12/045803) were sitting
    open with their exact reference, store and amount present in the
    transaction pool, rejected only because the transaction's own date
    differed from the bill's (1 day for the first three, 7 for the fourth —
    outside even the earlier ±1-day window fix). Money received is money
    received regardless of which day the gateway happened to post it; only
    the SEARCH is now date-agnostic — the bill's own business_date is
    untouched and revenue recognition still stays there (CLAUDE.md rule 6).

    A transaction is CONSUMED by the first bill that matches it and cannot
    match again. A bill is matched as soon as ONE of its references resolves —
    the remaining references are recorded for traceability but their failure is
    not an exception (§6.3).

    Returns (bills_out, consumed_txn_index) where bills_out gains:
      matched, match_tier, matched_gateway, matched_txn_ref,
      matched_payment_mode, refs_tried, refs_resolved, date_diff_days,
      match_note. `date_diff_days` is 0 and `match_note` is <NA> when the
      transaction's date agrees with the bill's; never hidden when it does
      not. `matched_payment_mode` (2026-09-19) is the matched transaction's
      OWN payment_mode field, carried through for the particulars matrix's
      Card/UPI split (mop_matrix.classify_digital_mode) — never inferred
      from the bill's MOP.
    """
    bills = bills.copy()
    bills["matched"] = False
    bills["match_tier"] = pd.NA
    bills["matched_gateway"] = pd.NA
    bills["matched_txn_ref"] = pd.NA
    bills["matched_payment_mode"] = pd.NA
    bills["date_diff_days"] = 0
    bills["match_note"] = pd.NA
    # How many references this bill actually CARRIES — counted up front, not
    # inside the gateway loop. Counting only references whose gateway happens
    # to have rows would report "0 tried" for a bill with a real reference
    # merely because that gateway's file is absent, which is a different
    # condition entirely (no_source_report).
    bills["refs_tried"] = sum(
        bills[col].notna().astype(int)
        for col in REF_COLUMN_BY_GATEWAY.values() if col in bills.columns
    )
    bills["refs_resolved"] = 0

    if pool.empty:
        return bills, set()

    consumed: set = set()

    for gateway, ref_col in REF_COLUMN_BY_GATEWAY.items():
        if ref_col not in bills.columns:
            continue
        gw_pool = pool.loc[pool["gateway"] == gateway]
        if gw_pool.empty:
            continue

        # Candidate transactions keyed on (ref, amount) ONLY — no date in the
        # key. Amount is still compared exactly: money is never compared with
        # a tolerance here, TOLERANCE is 0. If the same ref+amount somehow
        # appears more than once in the pool (never expected — Paytm/Axis/
        # Bajaj ingest each dedupe on their own reference, §5), every
        # candidate is kept and the first unconsumed one is taken, same as
        # before; nothing here silently prefers one over another by date.
        gw_index = {}
        for idx, ref, amt in zip(gw_pool.index, gw_pool["txn_ref"], gw_pool["amount"]):
            if ref is pd.NA or ref is None:
                continue
            gw_index.setdefault((str(ref), round(float(amt), 2)), []).append(idx)

        has_ref = bills[ref_col].notna()
        candidate_positions = bills.index[has_ref & ~bills["matched"]]
        total_for_gateway = len(candidate_positions)
        if on_progress:
            on_progress(f"Tier 1 matching against {gateway}: 0 of {total_for_gateway:,} bills")

        for n, pos in enumerate(candidate_positions, 1):
            if on_progress and n % 5000 == 0:
                on_progress(f"Tier 1 matching against {gateway}: {n:,} of {total_for_gateway:,} bills")
            key = (str(bills.at[pos, ref_col]),
                   round(float(bills.at[pos, "amount"]), 2))
            candidates = gw_index.get(key)
            if not candidates:
                continue
            available = next((c for c in candidates if c not in consumed), None)
            if available is None:
                continue
            consumed.add(available)
            bills.at[pos, "matched"] = True
            bills.at[pos, "match_tier"] = "Tier 1"
            bills.at[pos, "matched_gateway"] = gateway
            bills.at[pos, "matched_txn_ref"] = str(bills.at[pos, ref_col])
            bills.at[pos, "matched_payment_mode"] = pool.at[available, "payment_mode"]
            bills.at[pos, "refs_resolved"] = bills.at[pos, "refs_resolved"] + 1

            # Never hidden: a reference match on a different date is recorded,
            # not silently accepted as if the dates always agreed.
            txn_date = pool.at[available, "business_date"]
            diff = _date_diff_days(txn_date, bills.at[pos, "business_date"])
            bills.at[pos, "date_diff_days"] = diff
            if diff != 0:
                direction = "after" if diff > 0 else "before"
                bills.at[pos, "match_note"] = (
                    f"reference matched, txn dated {abs(diff)} day(s) "
                    f"{direction} bill"
                )

    logger.info(f"R1 Tier 1: {int(bills['matched'].sum())} of {len(bills)} digital bill lines matched")
    return bills, consumed


# ---------------------------------------------------------------------------
# Tier 2 — EDC OTHER only, against the Tier-1 residual
# ---------------------------------------------------------------------------

def match_tier2_edc_other(bills: pd.DataFrame, pool: pd.DataFrame, consumed: set):
    """store + business_date + amount, against transactions Tier 1 did not
    consume. Exactly one candidate -> matched. More than one -> Ambiguous,
    ranked by |bill time − txn time| for the operator; never auto-matched on
    time. None -> left open.

    Restricting to the residual is what removes the ambiguity: on 20-Jul it
    turns 1,664 colliding store+date+amount groups into 0 ambiguous cases.
    """
    bills = bills.copy()
    if "ambiguous_candidates" not in bills.columns:
        bills["ambiguous_candidates"] = pd.NA
    if "matched_payment_mode" not in bills.columns:
        bills["matched_payment_mode"] = pd.NA

    eligible = bills["mop"].isin(EDC_OTHER_MOPS) & ~bills["matched"]
    if not eligible.any() or pool.empty:
        return bills, consumed

    residual = pool.loc[~pool.index.isin(consumed)]
    if residual.empty:
        return bills, consumed

    residual_index = {}
    for idx, store, bdate, amt in zip(residual.index, residual["store"],
                                      residual["business_date"], residual["amount"]):
        residual_index.setdefault((store, bdate, round(float(amt), 2)), []).append(idx)

    for pos in bills.index[eligible]:
        key = (bills.at[pos, "store"],
               bills.at[pos, "business_date"],
               round(float(bills.at[pos, "amount"]), 2))
        candidates = [c for c in residual_index.get(key, []) if c not in consumed]
        if not candidates:
            continue
        if len(candidates) == 1:
            chosen = candidates[0]
            consumed.add(chosen)
            bills.at[pos, "matched"] = True
            bills.at[pos, "match_tier"] = "Matched (EDC OTHER)"
            bills.at[pos, "matched_gateway"] = pool.at[chosen, "gateway"]
            bills.at[pos, "matched_txn_ref"] = pool.at[chosen, "txn_ref"]
            bills.at[pos, "matched_payment_mode"] = pool.at[chosen, "payment_mode"]
        else:
            bills.at[pos, "match_tier"] = "Ambiguous"
            bills.at[pos, "ambiguous_candidates"] = ", ".join(
                str(pool.at[c, "txn_ref"]) for c in candidates
            )

    logger.info(
        f"R1 Tier 2 (EDC OTHER): {int((bills['match_tier'] == 'Matched (EDC OTHER)').sum())} matched, "
        f"{int((bills['match_tier'] == 'Ambiguous').sum())} ambiguous"
    )
    return bills, consumed
