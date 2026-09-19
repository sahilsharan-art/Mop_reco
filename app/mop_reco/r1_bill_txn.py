"""R1 — POS bill line ↔ gateway transaction. CLAUDE.md sections 6.2 / 6.3.

Pure functions: DataFrames in, DataFrames out. No Streamlit, no database, no
file reading. The dashboard is a viewer over this; every number a view shows
is produced here and asserted by a test.

Governing rule (§6.3): a bill needs ONE successful gateway match, not a match
for every reference it carries. An unresolved reference is not, by itself, an
exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

logger = logging.getLogger("mop_reco")

DIGITAL_CLASSES = ("digital", "digital_noref")

# Only these MOPs go through the Tier-2 store+date+amount procedure. Never
# applied to a MOP that should carry a reference (§6.3).
EDC_OTHER_MOPS = ("EDC OTHER", "EDC OTHER AXIS")

NON_ACQUIRING_TYPES = ("REFUND", "CHARGEBACK", "CHARGEBACK_REVERSAL", "REVERSAL")


@dataclass
class R1Result:
    bill_lines: pd.DataFrame       # every digital bill line, with its match outcome
    unmapped_txns: pd.DataFrame     # successful gateway rows no bill consumed
    linkage_exceptions: pd.DataFrame  # non-ACQUIRING rows whose original is missing
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gateway pool — three sources, one shape
# ---------------------------------------------------------------------------

def build_gateway_pool(paytm_df: pd.DataFrame = None,
                       axis_df: pd.DataFrame = None,
                       bajaj_df: pd.DataFrame = None) -> pd.DataFrame:
    """Normalise all three gateways into one pool with a common shape.

    Columns out: gateway, txn_ref, business_date, amount, status, txn_type,
    payment_mode, store, txn_time, source_row_id.
    """
    frames = []

    if paytm_df is not None and len(paytm_df):
        p = pd.DataFrame({
            "gateway": "paytm",
            "txn_ref": paytm_df["transaction_id"].astype("string"),
            "business_date": paytm_df["business_date"],
            "amount": paytm_df["amount"].astype(float),
            "status": paytm_df["status"].astype("string"),
            "txn_type": paytm_df["transaction_type"].astype("string"),
            "payment_mode": paytm_df["payment_mode"].astype("string"),
            "store": paytm_df["store"] if "store" in paytm_df.columns else None,
            "txn_time": paytm_df["transaction_time"] if "transaction_time" in paytm_df.columns else None,
        })
        frames.append(p)

    if axis_df is not None and len(axis_df):
        a = pd.DataFrame({
            "gateway": "axis",
            "txn_ref": axis_df["RRN"].astype("string"),
            "business_date": axis_df["business_date"],
            "amount": axis_df["GROSS_AMT"].astype(float),
            # The Axis EDC report is settled card transactions only — every row
            # is by definition a successful acquiring transaction; the file
            # carries no status/type column of its own.
            "status": "SUCCESS",
            "txn_type": "ACQUIRING",
            "payment_mode": axis_df["CARD_TYPE"].astype("string") if "CARD_TYPE" in axis_df.columns else None,
            "store": axis_df["store"] if "store" in axis_df.columns else None,
            # NOT axis_df["TRAN_TIME"] (2026-09-29) — that raw field is a
            # constant '000000' placeholder on every row of every month's
            # file (confirmed against the real April/July/August exports),
            # never a captured time. `transaction_time` is ingest_axis_edc's
            # own, genuinely-None column for exactly this reason.
            "txn_time": axis_df["transaction_time"] if "transaction_time" in axis_df.columns else None,
        })
        frames.append(a)

    if bajaj_df is not None and len(bajaj_df):
        b = pd.DataFrame({
            "gateway": "bajaj",
            # The join key is "Transaction Id" — NOT the file's own RRN column,
            # which is the literal string 'NA' on every row (§5).
            "txn_ref": bajaj_df["Transaction Id"].astype("string"),
            "business_date": bajaj_df["business_date"],
            "amount": bajaj_df["Transaction Amount"].astype(float),
            "status": bajaj_df["Transaction Status"].astype("string"),
            "txn_type": "ACQUIRING",
            "payment_mode": bajaj_df["Card Scheme"].astype("string") if "Card Scheme" in bajaj_df.columns else None,
            "store": bajaj_df["store"] if "store" in bajaj_df.columns else None,
            # A real, per-row time embedded in "Transaction Date"
            # ('01-08-2026 22:32:38') — confirmed varying on the real July
            # file, unlike Axis's placeholder. ingest_bajaj's own
            # transaction_time column (2026-09-29) carries it.
            "txn_time": bajaj_df["transaction_time"] if "transaction_time" in bajaj_df.columns else None,
        })
        frames.append(b)

    if not frames:
        return pd.DataFrame(columns=[
            "gateway", "txn_ref", "business_date", "amount", "status",
            "txn_type", "payment_mode", "store", "txn_time",
        ])

    pool = pd.concat(frames, ignore_index=True)
    pool["txn_ref"] = pool["txn_ref"].str.strip()
    return pool


def successful_acquiring(pool: pd.DataFrame) -> pd.DataFrame:
    """The R1 matching pool: SUCCESS + ACQUIRING only (§6.11).

    Status is compared case-insensitively — the Bajaj file mixes 'Success' and
    'SUCCESS' (532/37), and a case-sensitive filter would silently drop 532
    perfectly good transactions.
    """
    if pool.empty:
        return pool
    status_ok = pool["status"].str.upper().eq("SUCCESS")
    type_ok = pool["txn_type"].str.upper().eq("ACQUIRING")
    return pool.loc[status_ok & type_ok].copy()


# ---------------------------------------------------------------------------
# Non-ACQUIRING rows link to their ORIGINAL, never to a bill (§6.3)
# ---------------------------------------------------------------------------

def link_non_acquiring(paytm_df: pd.DataFrame):
    """Returns (linked, exceptions).

    Every REFUND / CHARGEBACK / CHARGEBACK_REVERSAL / REVERSAL row carries
    reference_transaction_id pointing at the original sale. If the original is
    in the loaded range we link it and inherit the original's payment_mode and
    store — which is why the 13 chargebacks with a blank payment_mode need no
    special routing branch. If it is absent, that is an exception naming the
    missing original and its date.

    This runs BEFORE any date-range filter: two July chargeback-reversals carry
    a transaction_date of 21-May and 28-May, and filtering first would drop
    them before they could be flagged.
    """
    empty_cols = ["gateway", "transaction_id", "transaction_type", "business_date",
                  "amount", "reference_transaction_id", "original_found",
                  "original_business_date", "payment_mode", "store"]
    if paytm_df is None or paytm_df.empty:
        return pd.DataFrame(columns=empty_cols), pd.DataFrame(columns=empty_cols)

    if "reference_transaction_id" not in paytm_df.columns:
        # The column is not in the loader's projection — say so rather than
        # silently reporting zero non-ACQUIRING rows.
        raise KeyError(
            "paytm frame has no 'reference_transaction_id' column — non-ACQUIRING "
            "rows cannot be linked to their originals (CLAUDE.md 6.3)"
        )

    is_odd = paytm_df["transaction_type"].str.upper().isin(NON_ACQUIRING_TYPES)
    odd = paytm_df.loc[is_odd].copy()
    if odd.empty:
        return pd.DataFrame(columns=empty_cols), pd.DataFrame(columns=empty_cols)

    originals = paytm_df.set_index("transaction_id")
    ref = odd["reference_transaction_id"].astype("string").str.strip()

    odd["original_found"] = ref.isin(originals.index)
    odd["original_business_date"] = ref.map(originals["business_date"])
    # Inherit the original's mode and store where we found it.
    inherited_mode = ref.map(originals["payment_mode"])
    odd["payment_mode"] = odd["payment_mode"].where(
        odd["payment_mode"].astype("string").str.len() > 0, inherited_mode
    )
    if "store" in originals.columns:
        odd["store"] = odd["store"].where(odd["store"].notna(), ref.map(originals["store"]))

    odd["gateway"] = "paytm"
    linked = odd.loc[odd["original_found"]].copy()
    exceptions = odd.loc[~odd["original_found"]].copy()

    logger.info(
        f"R1 non-ACQUIRING linkage: {len(odd)} row(s) — {len(linked)} linked to an "
        f"original in range, {len(exceptions)} exception(s)"
    )
    return linked, exceptions


# ---------------------------------------------------------------------------
# Tier 1 — reference match
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run_r1(bill_lines: pd.DataFrame,
           paytm_df: pd.DataFrame = None,
           axis_df: pd.DataFrame = None,
           bajaj_df: pd.DataFrame = None,
           on_progress=None) -> R1Result:
    """Full R1 for whatever date range the frames cover.

    `bill_lines` is the ingested POS bill-wise frame. Only rows in the sale
    universe (§6.13) and of a digital class take part.
    """
    bills = bill_lines
    if "in_sale_universe" in bills.columns:
        bills = bills.loc[bills["in_sale_universe"]]
    digital = bills.loc[bills["mop_class"].isin(DIGITAL_CLASSES)].copy().reset_index(drop=True)

    linked, linkage_exceptions = link_non_acquiring(paytm_df)

    pool = build_gateway_pool(paytm_df, axis_df, bajaj_df)
    matching_pool = successful_acquiring(pool)

    matched_bills, consumed = match_tier1(digital, matching_pool, on_progress=on_progress)
    matched_bills, consumed = match_tier2_edc_other(matched_bills, matching_pool, consumed)

    unmapped = matching_pool.loc[~matching_pool.index.isin(consumed)].copy()

    matched_bills["status"] = _bill_status_vectorized(matched_bills)

    summary = {
        "digital_bill_lines": int(len(matched_bills)),
        "tier1_matched": int((matched_bills["match_tier"] == "Tier 1").sum()),
        "tier2_matched": int((matched_bills["match_tier"] == "Matched (EDC OTHER)").sum()),
        "ambiguous": int((matched_bills["match_tier"] == "Ambiguous").sum()),
        "open_exceptions": int((~matched_bills["matched"] & (matched_bills["match_tier"] != "Ambiguous")).sum()),
        "matched_total": int(matched_bills["matched"].sum()),
        "unmapped_txns": int(len(unmapped)),
        "unmapped_txns_by_gateway": unmapped["gateway"].value_counts().to_dict() if len(unmapped) else {},
        "linkage_exceptions": int(len(linkage_exceptions)),
        "gateway_pool_rows": int(len(pool)),
        "matching_pool_rows": int(len(matching_pool)),
        "matched_value": float(matched_bills.loc[matched_bills["matched"], "amount"].sum()),
        "unmatched_value": float(matched_bills.loc[~matched_bills["matched"], "amount"].sum()),
    }
    logger.info(f"R1 summary: {summary}")

    return R1Result(
        bill_lines=matched_bills,
        unmapped_txns=unmapped,
        linkage_exceptions=linkage_exceptions,
        summary=summary,
    )


def _bill_status(row) -> str:
    # match_tier is pd.NA for an unmatched bill; comparing NA with == yields NA,
    # and bool(NA) raises. Normalise to a plain string first.
    tier = row["match_tier"]
    tier = "" if pd.isna(tier) else str(tier)

    if bool(row["matched"]):
        return tier
    if tier == "Ambiguous":
        return "Ambiguous"
    if row["mop"] in EDC_OTHER_MOPS:
        return "No reference, no match"
    if int(row.get("refs_tried", 0) or 0) == 0:
        return "No reference at POS"
    return "Not found in gateway"


def _bill_status_vectorized(bills: pd.DataFrame) -> pd.Series:
    """Same rule as `_bill_status`, row for row, but never calls
    `DataFrame.apply(axis=1)` — found 2026-09-24 on a real 5-month run:
    `.apply(axis=1)` on ~3.7M rows tries to interleave every column (mixed
    Arrow-string and numeric dtypes) into one giant object-dtype 2D array
    first, which is what actually asked for the 2.97 GB allocation that
    failed with `ArrowMemoryError`, not anything about the row count alone.
    This has nothing to do with the `.at[]` matching loop CLAUDE.md already
    flags as the real throughput bottleneck — `_bill_status` runs once,
    AFTER matching is fully decided, over four already-scalar columns, with
    no merge/join semantics of its own. Safe to vectorize on its own,
    independently of that larger, deliberately-deferred rewrite.

    Priority, highest first (identical to the row-wise version): matched >
    Ambiguous > EDC_OTHER_MOPS with no reference > no reference tried at
    all > not found in the gateway. Built by applying `.mask()` in the
    OPPOSITE order — lowest priority first — since each later `.mask()`
    call overwrites only where ITS OWN condition is true, so applying them
    lowest-to-highest makes the highest-priority condition win exactly
    where the original if/elif chain would have returned first.
    """
    tier = bills["match_tier"].fillna("").astype(str)
    matched = bills["matched"].astype(bool)
    refs_tried = pd.to_numeric(bills.get("refs_tried", 0), errors="coerce").fillna(0).astype(int)

    status = pd.Series("Not found in gateway", index=bills.index, dtype=object)
    status = status.mask(refs_tried == 0, "No reference at POS")
    status = status.mask(bills["mop"].isin(EDC_OTHER_MOPS), "No reference, no match")
    status = status.mask(tier == "Ambiguous", "Ambiguous")
    status = status.mask(matched, tier)
    return status
