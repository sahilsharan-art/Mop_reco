"""Digital bill lines that cannot be judged yet because their only possible gateway has not delivered (CLAUDE.md 6.28).

Pure: DataFrames in, DataFrames out. A line is PARKED when all of these hold -
  * the engine left it unmatched (and it is not an ambiguous EDC OTHER, which has candidates to choose from);
  * its business date is later than the last date the awaited gateway has ANY row for (data-driven, never a typed date);
  * it can only settle through that gateway: it carries that gateway's reference (for Axis the card-conditional
    retrievalReferenceNo - a card payment on a Paytm EDC also carries a Paytm acquirementId, but the Paytm file never
    lists it: 12,602 October lines showed exactly this), or its MOP is listed in config `awaiting_source.<gateway>.only_mops`.
Nothing else is parked. A bill that carries a Bajaj reference, or no Axis indication at all, stays a genuine open bill.
"""

from __future__ import annotations

import pandas as pd

AXIS = "axis"
AWAITING_LABEL = {"axis": "Awaiting Axis - not an exception"}


def only_mops(config_raw: dict, gateway: str) -> list:
    return list((((config_raw or {}).get("awaiting_source") or {}).get(gateway) or {}).get("only_mops") or [])


def parked_mask(day: pd.DataFrame, gateway_last_date, config_raw: dict, gateway: str = AXIS) -> pd.Series:
    """True for the lines of `day` that must be parked. `gateway_last_date` None means the gateway has no data at all."""
    if day.empty:
        return pd.Series(False, index=day.index)
    unmatched = ~day["matched"].astype(bool)
    tier = day["match_tier"].fillna("").astype(str) if "match_tier" in day.columns else pd.Series("", index=day.index)
    not_ambiguous = tier != "Ambiguous"
    dates = day["business_date"].astype(str)
    after = dates > str(gateway_last_date) if gateway_last_date is not None else pd.Series(True, index=day.index)
    own = {"axis": "axis_ref"}[gateway]
    others = [c for c in ("bajaj_ref",) if c in day.columns]
    has_own = day[own].notna() if own in day.columns else pd.Series(False, index=day.index)
    has_other = pd.Series(False, index=day.index)
    for c in others:
        has_other |= day[c].notna()
    by_ref = has_own & ~has_other
    by_mop = day["mop"].isin(only_mops(config_raw, gateway))
    return unmatched & not_ambiguous & after & (by_ref | by_mop)


def split_parked(day: pd.DataFrame, gateway_last_date, config_raw: dict, gateway: str = AXIS):
    """(kept, parked)."""
    mask = parked_mask(day, gateway_last_date, config_raw, gateway)
    return day.loc[~mask], day.loc[mask]
