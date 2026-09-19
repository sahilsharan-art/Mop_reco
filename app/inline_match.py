"""Inline manual matching — paste a transaction id into the Matched txn cell.

Kept out of `mop_reco/` because it imports Streamlit (§8). Every rule it
enforces comes from `mop_reco.manual_match`; this file only renders the
validation and collects the confirmation.

The gateway is never asked for. `validate_by_id` searches Paytm, Axis and
Bajaj for the pasted id and reports which one holds it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashboard_common import require_operator
from mop_reco import manual_match as mm
from mop_reco.config import load_config
from mop_reco.formatting import money_table

BLANK = ("", "nan", "None", "—", None)

# Where a pending paste lives between reruns. A case (b) or (c) paste needs a
# reason or a decision, which takes another rerun, and the edit must survive
# it — otherwise the operator types the id twice.
K_PENDING = "_inline_pending"


def reason_codes() -> list:
    return list(load_config().raw.get("manual_match", {}).get("reason_codes", []))


def _blank(value) -> bool:
    return value is None or str(value).strip() in ("", "nan", "None", "—")


def editable_table(df: pd.DataFrame) -> pd.DataFrame:
    """The display table. Only "Matched txn" is editable."""
    return pd.DataFrame({
        "Date": df["date"],
        "Store": df["store"],
        "Bill no": df["bill_no"],
        "MOP": df["mop"],
        "Amount (Rs)": df["amount"].map(money_table),
        "Status": df["status"],
        "Gateway": df["gateway"].fillna("—"),
        "Matched txn": df["matched_txn"].fillna(""),
    })


def find_edit(original: pd.DataFrame, edited: pd.DataFrame):
    """The one changed "Matched txn" cell, as (row_index, pasted_id).

    Returns None when nothing changed. Only that column is editable, so
    anything else changing would be a Streamlit bug rather than an operator
    action.
    """
    if edited is None or len(edited) != len(original):
        return None
    before = original["Matched txn"].fillna("").astype(str)
    after = edited["Matched txn"].fillna("").astype(str)
    changed = [i for i in range(len(before))
               if before.iloc[i].strip() != after.iloc[i].strip()]
    if not changed:
        return None
    index = changed[0]
    return index, after.iloc[index].strip()


def handle_paste(session, df: pd.DataFrame, index: int, pasted: str, key: str):
    """Validate a pasted id inline and, where clean, record it immediately.

    Case (a) is accepted on the paste alone — the operator asked for "paste
    and press enter, nothing else", and a clean, unused, exact-amount,
    same-store SUCCESS transaction is exactly the case where nothing else is
    needed. Cases (b) and (c) always stop for a decision.
    """
    row = df.iloc[index]
    if _blank(pasted):
        return None

    bill = {
        "business_date": row["date"],
        "store": row["store"],
        "bill_no": row["bill_no"],
        "mop": row["mop"],
        "amount": float(row["amount"]),
    }

    # A bill the engine already matched is not a manual-match target.
    if not _blank(row["matched_txn"]):
        st.warning(
            f"**{row['bill_no']}** is already matched by the engine to "
            f"{row['matched_txn']}. Manual matching is for unmatched bills — "
            "nothing was changed."
        )
        return None

    existing = (session.query(mm.ManualMatch)
                .filter(mm.ManualMatch.bill_key == mm.bill_key_of(bill),
                        mm.ManualMatch.lifecycle == mm.ACTIVE).first())
    if existing:
        st.warning(
            f"**{row['bill_no']}** already has an active manual match "
            f"(#{existing.id}, {existing.status}, by {existing.operator}). "
            "Reverse it on the **Manual matches** page before matching again — "
            "nothing was changed."
        )
        return None

    validation = mm.validate_by_id(session, bill, pasted)

    with st.container(border=True):
        st.markdown(f"**{row['bill_no']}** · {row['date']} · {row['store']} · "
                    f"{row['mop']} · Rs {money_table(float(row['amount']))}")
        st.markdown(f"Pasted: `{pasted}`")

        if validation.case == mm.CASE_CLEAN:
            return _clean_case(session, bill, pasted, validation, key)
        if validation.case == mm.CASE_DISCREPANT:
            return _discrepant_case(session, bill, pasted, validation, key)
        return _not_found_case(session, bill, pasted, validation, key)


def _gateway_line(validation) -> str:
    return (f"Found in **{validation.gateway.upper()}**"
            if validation.gateway else "Not found in any gateway")


def _reason_picker(key: str, default_index: int = 0):
    codes = reason_codes()
    if not codes:
        return st.text_input("Reason code", key=f"{key}_rc")
    return st.selectbox("Reason code", codes, index=default_index, key=f"{key}_rc")


def _clean_case(session, bill, pasted, validation, key):
    """Green. Accepted on the paste alone."""
    txn = validation.txn or {}
    st.success(
        f"✓ {_gateway_line(validation)} — Rs "
        f"{money_table(float(txn.get('amount') or 0))}, store "
        f"{txn.get('store') or '—'}, {txn.get('status') or '—'}. Amount and "
        "store agree and nothing else is using it."
    )
    # A same-window date lag is still accepted on the paste alone (2026-09-12)
    # — but never silently. The transaction sitting in a different day's file
    # than the bill is shown here, not hidden inside the audit record.
    if validation.date_lag_note:
        st.info(f"📅 {validation.date_lag_note}")
    operator = require_operator("recording this match", key=f"{key}_op")
    if not operator:
        return None
    try:
        entry = mm.record(session, bill, validation.gateway, pasted,
                          reason_codes()[0] if reason_codes() else "inline_paste",
                          operator, validation)
    except (ValueError, LookupError) as exc:
        st.error(f"Refused — {exc}")
        return None
    st.success(
        f"Matched manually to **{validation.gateway.upper()}** `{pasted}`. "
        f"The exception is closed with reason *matched manually*, the "
        f"transaction is marked consumed, and manual match #{entry.id} is on "
        "the **Manual matches** register. The row leaves the unmatched view on "
        "the next refresh."
    )
    return entry


def _discrepant_case(session, bill, pasted, validation, key):
    """Amber. Names the problem and the other bill; needs a written reason."""
    st.warning(f"⚠ {_gateway_line(validation)}, but this cannot be accepted "
               "as-is:")
    for problem in validation.problems:
        st.markdown(f"- {problem}")
    if validation.date_lag_note:
        st.info(f"📅 {validation.date_lag_note}")
    if validation.txn:
        st.dataframe(pd.DataFrame([validation.txn]), hide_index=True,
                     width="stretch")

    st.markdown("**To record it anyway, say why the discrepancy is acceptable.**")
    c1, c2 = st.columns([2, 1])
    reason = c1.text_input("Why is this correct despite the problem above? (required)",
                           key=f"{key}_why",
                           placeholder="e.g. Paytm confirmed by email, ticket 12345")
    with c2:
        reason_code = _reason_picker(key)
    confirm = st.checkbox("I have read the problems above and accept "
                          "responsibility for this override.", key=f"{key}_ok")

    if not st.button("Override and record", key=f"{key}_go"):
        return None
    if not str(reason).strip():
        st.error("The reason is mandatory. Nothing was recorded.")
        return None
    if not confirm:
        st.error("Tick the confirmation box. Nothing was recorded.")
        return None
    operator = require_operator("overriding this", key=f"{key}_op")
    if not operator:
        return None
    try:
        entry = mm.record(session, bill, validation.gateway, pasted, reason_code,
                          operator, validation, override_reason=reason.strip())
    except (ValueError, LookupError) as exc:
        st.error(f"Refused — {exc}")
        return None
    st.success(f"Recorded with an override as manual match #{entry.id}. The "
               "override and your reason are on the register.")
    return entry


def _not_found_case(session, bill, pasted, validation, key):
    """Red. Can only ever be saved as asserted, never as matched."""
    st.error("✗ " + " ".join(validation.problems))
    st.markdown(
        "This can only be saved as **asserted, unverified** — a distinct "
        "status. The bill **stays an open item** and is not counted in any "
        "match rate, because no transaction has been found to support it."
    )
    c1, c2 = st.columns([2, 1])
    note = c1.text_input("Note (what is this based on?)", key=f"{key}_note")
    with c2:
        reason_code = _reason_picker(key)

    if not st.button("Save as asserted, unverified", key=f"{key}_assert"):
        return None
    operator = require_operator("asserting this", key=f"{key}_op")
    if not operator:
        return None
    try:
        entry = mm.record(session, bill, None, pasted, reason_code, operator,
                          validation, note=note)
    except (ValueError, LookupError) as exc:
        st.error(f"Refused — {exc}")
        return None
    st.warning(f"Saved as **asserted, unverified** (#{entry.id}). It is NOT a "
               "match and the bill remains an open item.")
    return entry
