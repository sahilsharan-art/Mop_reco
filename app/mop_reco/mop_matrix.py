"""The Summary particulars matrix (2026-09-18) — Sale/Collected/Receivable
by payment bucket (UPI, Card, Total digital, Total cash) plus Gift Voucher
Redeemed / Credit Note Issued / Credit Note Received as their own lines,
replacing the seven-tile ribbon.

Pure functions only (§8) — no Streamlit, no session/query here. The bucket
a MOP belongs to is config-driven (`config.yaml`'s `mop_classification.*.
bucket`), never hardcoded, so a new MOP or a reclassification is a config
change, not a code change — and an unmapped MOP is a loud error here, never
a silent drop (rule 5): every MOP found in the sale universe must resolve to
exactly one of the buckets below, with no seventh "OTHER" left over,
because config.yaml's own bucket assignment (docs/MOP_CLASSIFICATION.md)
recorded a ruling for every MOP found in the data before this shipped.

**Card vs UPI is NOT one of these buckets (corrected 2026-09-19, see
docs/MOP_PAYMENT_MODE.md).** The first version of this matrix split UPI vs
Card from the POS MOP name via a `card`/`upi` bucket here — wrong, because
"PAYTM EDC" is one shared terminal that accepts both card and UPI (Axis
TIDs are mapped onto Paytm machines for card acceptance), so the MOP name
cannot tell you which one a given bill used. Every digital MOP except the
two Bajaj ones now carries `bucket: digital` (one lump sum, no card/upi
split at this layer at all); `classify_digital_mode()` below does the real
split, from the matched TRANSACTION's own identity, in
`store_sheet_source.py`'s `_digital_mode_sales()`/`persist_digital_mode_
agg()` — never from this module's own bucket field.
"""

from __future__ import annotations

import pandas as pd

BUCKETS = ("digital", "cash", "gift_voucher", "credit_note_issued",
          "credit_note_received")

# **PROVISIONAL DEFAULT, not a fact about the payment mode (2026-09-21,
# operator's own explicit business rule).** A digital bill line with no
# matched transaction has no known payment_mode — until it is matched, this
# is the ONE place that decides what to assume: it counts as UPI. The
# instant a manual match lands, `store_sheet_source._digital_mode_sales()`
# reclassifies it to whatever the transaction's own `payment_mode` actually
# says (card or upi), with no re-run. This constant exists so nobody
# mistakes the assumption for a discovered fact, and so the one place that
# applies it is never duplicated — `_digital_mode_sales()` is the only
# reader.
PROVISIONAL_DIGITAL_MODE = "upi"


def classify_digital_mode(gateway, payment_mode, mop) -> str:
    """card | upi | other, from the matched GATEWAY TRANSACTION's own
    identity — never from the POS MOP name. See the module docstring and
    docs/MOP_PAYMENT_MODE.md for the full data behind each rule.

    - **axis** -> always "card". The Axis EDC report is settled card
      transactions only, by design (CLAUDE.md §5) — confirmed on the data:
      every `CARD_TYPE` value seen (VC/MC/RC/VD/RD/MD) is a card scheme,
      never UPI.
    - **bajaj** -> from the bill's own MOP. The one narrow, deliberate
      exception to "never MOP": Bajaj's own settlement field (`Card
      Scheme`) is "NA" on 536 of 573 rows and is known to read UPI/
      SALE-UPI even on card MOPs (CLAUDE.md §6.2) — it cannot be trusted.
      But "Bajajpay Card" and "Bajajpay UPI" are two distinct, separately
      -posted POS MOPs, not one shared terminal like Paytm EDC — there is
      no ambiguity for the MOP to paper over here, so reading it directly
      is a fact, not an inference. An unexpected Bajaj MOP (should never
      happen — Bajaj only ever posts these two) is reported as "other",
      never guessed either way.
    - **paytm** -> from the transaction's own `payment_mode`: "UPI" ->
      upi; "DEBIT_CARD"/"CREDIT_CARD" -> card (confirmed the only three
      values present in the July data, 628,195 / 13,572 / 385 rows). "VC",
      "MC", "MD", "RC" -> card too, in case a future Paytm export ever
      carries the same scheme codes Axis does. Anything else is "other" —
      reported, never assumed to be either.
    - any other gateway, or no gateway at all (an unmatched bill line) ->
      "other". The caller folds "other" into "Unclassified" and reports
      the count/value separately if it is ever non-trivial (CLAUDE.md
      rule 5 — an unrecognised value is named, not silently absorbed).
    """
    gateway = (gateway or "").strip().lower()
    if gateway == "axis":
        return "card"
    if gateway == "bajaj":
        if mop == "Bajajpay Card":
            return "card"
        if mop == "Bajajpay UPI":
            return "upi"
        return "other"
    if gateway == "paytm":
        pm = (payment_mode or "").strip().upper()
        if pm == "UPI":
            return "upi"
        if pm in ("DEBIT_CARD", "CREDIT_CARD", "VC", "MC", "MD", "RC"):
            return "card"
        return "other"
    return "other"


def load_mop_buckets(config) -> dict:
    """{mop_string: bucket} from config.yaml's `mop_classification`.

    Raises if any MOP in the config has no `bucket` at all — the config
    file is the one place this mapping is allowed to be edited, but every
    entry must actually carry one; a MOP present in `mop_classification`
    with no `bucket` key would otherwise silently vanish from the matrix
    entirely; rule 5 forbids that as much as guessing one.
    """
    buckets = {}
    for mop, entry in config.mop_classification.items():
        bucket = entry.get("bucket")
        if not bucket:
            raise ValueError(
                f"config.yaml: mop_classification['{mop}'] has no 'bucket' — "
                "every MOP must resolve to one of the particulars-matrix "
                f"buckets ({', '.join(BUCKETS)}) or be explicitly ruled "
                "unclassifiable (docs/MOP_CLASSIFICATION.md); nothing is "
                "silently left out of the matrix."
            )
        if bucket not in BUCKETS:
            raise ValueError(
                f"config.yaml: mop_classification['{mop}'].bucket = "
                f"{bucket!r} is not one of {BUCKETS}"
            )
        buckets[mop] = bucket
    return buckets


def bucket_sales_by_store_date(pos: pd.DataFrame, mop_buckets: dict) -> pd.DataFrame:
    """`pos` (business_date, store, mop, amount, ...) grouped into
    (business_date, store, bucket) -> sale. Every row's `mop` MUST resolve
    via `mop_buckets` — an unmapped MOP raises rather than being dropped or
    guessed into an existing bucket (rule 5), naming the MOP, its row count
    and its total so the gap is actionable, not a stack trace.
    """
    if pos.empty:
        return pd.DataFrame(columns=["business_date", "store", "bucket", "sale"])

    unmapped = pos.loc[~pos["mop"].isin(mop_buckets), "mop"]
    if not unmapped.empty:
        counts = unmapped.value_counts()
        detail = "; ".join(f"{mop!r} ({n} row(s))" for mop, n in counts.items())
        raise ValueError(
            f"mop_matrix: {len(unmapped)} row(s) carry a MOP not in "
            f"config.yaml's mop_classification, so they cannot be placed in "
            f"any particulars-matrix bucket: {detail}. Add it to "
            "config.yaml (with a ruled bucket, not a guessed one) before "
            "this run's aggregate can be built."
        )

    out = pos.copy()
    out["bucket"] = out["mop"].map(mop_buckets)
    grouped = (out.groupby(["business_date", "store", "bucket"], as_index=False)
              ["amount"].sum())
    return grouped.rename(columns={"amount": "sale"})
