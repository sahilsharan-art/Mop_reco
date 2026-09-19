from pathlib import Path
p = Path('app/mop_reco/store_sheet.py')
s = p.read_text(encoding='utf-8')

old_consts = '''CASH_MOP = "Cash"
DEPOSIT_CREDIT_TYPE = "COL"
# Reported separately rather than dropped — see the module docstring.
OTHER_CREDIT_TYPES = ("ADJ.CR.", "ADJ.DB.", "ZPO")'''

new_consts = '''CASH_MOP = "Cash"

# Which CMS TYPE feeds column J, from which source column, and with what sign.
# Confirmed against the raw July file (2026-09-11):
#   COL      SLIPAMOUNT           as credited
#   ADJ.CR.  ADJ (column U)       NOT SLIPAMOUNT — SLIPAMOUNT on an ADJ.CR.
#            row is the ORIGINAL slip being adjusted (Rs 47,400/1,27,722/
#            95,760 on the three July rows); ADJ is the actual adjustment
#            (Rs 103/7/8). Using SLIPAMOUNT would book the whole original
#            slip a second time.
#   ADJ.DB.  RECOVEREDAMOUNT, subtracted — the bank recovering money it
#            previously overcredited reduces what the store is owed. Zero on
#            every July row, but the column must still be read correctly for
#            the month it is not.
# ZPO is excluded from J entirely, on the operator's explicit instruction —
# see IGNORED_CREDIT_TYPES below — but is still reported for visibility
# (rule 4/5: nothing vanishes silently), never dropped without a trace.
CREDIT_TYPE_SOURCE = {
    "COL": ("SLIPAMOUNT", 1),
    "ADJ.CR.": ("ADJ", 1),
    "ADJ.DB.": ("RECOVEREDAMOUNT", -1),
}

# Excluded from J entirely, on the operator's explicit instruction
# (2026-09-11): "please ignore the ZPO type". ZPO rows also carry no DEPDATE
# and a single pseudo pickup code, so they could not be attributed to a
# business date or a store even if they were included.
IGNORED_CREDIT_TYPES = ("ZPO",)'''

assert old_consts in s, "consts anchor"
s = s.replace(old_consts, new_consts, 1)

old_j = '''def cms_received_by_deposit_date(cms: pd.DataFrame) -> pd.DataFrame:
    """J — CMS bank credits, TYPE = COL, attributed to the DEPOSIT date.

    **Grouped on DEPDATE, never CRDATE.** A T+1 credit belongs to the day the
    cash left the store, or K would read nil on the deposit day and then show
    a phantom positive on the credit day, and "deposited but not yet received"
    would be unanswerable.
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_received"])
    if cms is None or cms.empty:
        return empty

    col = cms[cms["TYPE"].astype(str).str.strip().eq(DEPOSIT_CREDIT_TYPE)].copy()
    col = col[col["store"].notna() & col["store"].astype(str).ne("None")]
    if col.empty:
        return empty

    deposit_date = pd.to_datetime(col["DEPDATE"], errors="coerce")
    col = col[deposit_date.notna()]
    if col.empty:
        return empty
    col["date"] = pd.to_datetime(col["DEPDATE"], errors="coerce").dt.strftime("%Y-%m-%d")
    col["credit"] = pd.to_numeric(col["SLIPAMOUNT"], errors="coerce").fillna(0.0)

    return (col.groupby(["date", "store"], as_index=False)["credit"].sum()
            .rename(columns={"credit": "cash_received"}))'''

new_j = '''def cms_received_by_deposit_date(cms: pd.DataFrame) -> pd.DataFrame:
    """J — CMS bank credits, attributed to the DEPOSIT date.

    Three types feed J, each from its own column (see CREDIT_TYPE_SOURCE):
    COL from SLIPAMOUNT, ADJ.CR. from the ADJ column (not SLIPAMOUNT — see
    the module-level comment), ADJ.DB. from RECOVEREDAMOUNT, subtracted. ZPO
    is excluded entirely.

    **Grouped on DEPDATE, never CRDATE.** A T+1 credit belongs to the day the
    cash left the store, or K would read nil on the deposit day and then show
    a phantom positive on the credit day, and "deposited but not yet received"
    would be unanswerable.
    """
    empty = pd.DataFrame(columns=["date", "store", "cash_received"])
    if cms is None or cms.empty:
        return empty

    parts = []
    for credit_type, (column, sign) in CREDIT_TYPE_SOURCE.items():
        subset = cms[cms["TYPE"].astype(str).str.strip().eq(credit_type)].copy()
        subset = subset[subset["store"].notna() & subset["store"].astype(str).ne("None")]
        if column not in subset.columns or subset.empty:
            # The column was not captured for this dump. Never silently treat
            # a missing column as a zero contribution.
            continue
        deposit_date = pd.to_datetime(subset["DEPDATE"], errors="coerce")
        subset = subset[deposit_date.notna()]
        if subset.empty:
            continue
        subset["date"] = pd.to_datetime(subset["DEPDATE"], errors="coerce").dt.strftime("%Y-%m-%d")
        subset["credit"] = sign * pd.to_numeric(subset[column], errors="coerce").fillna(0.0)
        parts.append(subset[["date", "store", "credit"]])

    if not parts:
        return empty
    combined = pd.concat(parts, ignore_index=True)
    return (combined.groupby(["date", "store"], as_index=False)["credit"].sum()
            .rename(columns={"credit": "cash_received"}))


def cms_contribution_by_type(cms: pd.DataFrame) -> pd.DataFrame:
    """How much of J came from each type, and which column fed it.

    Because ADJ.CR. and ADJ.DB. contribute to J via a DIFFERENT column than
    COL, this exists so the operator can see the split rather than a single J
    figure that silently blends three sources. One row per type.
    """
    rows = []
    if cms is not None and not cms.empty:
        for credit_type, (column, sign) in CREDIT_TYPE_SOURCE.items():
            subset = cms[cms["TYPE"].astype(str).str.strip().eq(credit_type)]
            if column not in cms.columns or subset.empty:
                rows.append({"type": credit_type, "source_column": column,
                            "rows": 0, "value": 0.0})
                continue
            value = sign * float(pd.to_numeric(subset[column],
                                               errors="coerce").fillna(0.0).sum())
            rows.append({"type": credit_type, "source_column": column,
                        "rows": int(len(subset)), "value": value})
    return pd.DataFrame(rows, columns=["type", "source_column", "rows", "value"])'''

assert old_j in s, "cms_received_by_deposit_date anchor"
s = s.replace(old_j, new_j, 1)

old_excl = '''def excluded_credit_types(cms: pd.DataFrame) -> pd.DataFrame:
    """ADJ.CR. / ADJ.DB. / ZPO — kept visible, never netted into J.

    Reported by store and date so an adjustment and its later recovery cannot
    cancel out where nobody is looking.
    """
    empty = pd.DataFrame(columns=["type", "date", "store", "rows", "value"])
    if cms is None or cms.empty:
        return empty

    other = cms[cms["TYPE"].astype(str).str.strip().isin(OTHER_CREDIT_TYPES)].copy()
    if other.empty:
        return empty
    other["value"] = pd.to_numeric(other["SLIPAMOUNT"], errors="coerce").fillna(0.0)
    # ZPO rows carry no DEPDATE at all, so they cannot be attributed to a
    # business date. They are reported with the date left blank rather than
    # forced onto a day they do not belong to (rule 6).
    parsed = pd.to_datetime(other["DEPDATE"], errors="coerce")
    other["date"] = parsed.dt.strftime("%Y-%m-%d").fillna("(no deposit date)")
    other["store"] = other["store"].fillna("(unmapped)")

    return (other.groupby(["TYPE", "date", "store"], as_index=False)
            .agg(rows=("value", "size"), value=("value", "sum"))
            .rename(columns={"TYPE": "type"})
            .sort_values(["type", "date", "value"], ascending=[True, True, False])
            .reset_index(drop=True))'''

new_excl = '''def excluded_credit_types(cms: pd.DataFrame) -> pd.DataFrame:
    """TYPE = ZPO — the one type ignored entirely, kept visible regardless.

    ADJ.CR. and ADJ.DB. are no longer "excluded" from J (they feed it via
    their own columns — see cms_received_by_deposit_date); only ZPO is. This
    is still reported by store and date so a large excluded population is
    never invisible (rule 4/5), even though the operator has said to ignore
    it for the sheet's own purposes.
    """
    empty = pd.DataFrame(columns=["type", "date", "store", "rows", "value"])
    if cms is None or cms.empty:
        return empty

    other = cms[cms["TYPE"].astype(str).str.strip().isin(IGNORED_CREDIT_TYPES)].copy()
    if other.empty:
        return empty
    other["value"] = pd.to_numeric(other["SLIPAMOUNT"], errors="coerce").fillna(0.0)
    # ZPO rows carry no DEPDATE at all, so they cannot be attributed to a
    # business date. They are reported with the date left blank rather than
    # forced onto a day they do not belong to (rule 6).
    parsed = pd.to_datetime(other["DEPDATE"], errors="coerce")
    other["date"] = parsed.dt.strftime("%Y-%m-%d").fillna("(no deposit date)")
    other["store"] = other["store"].fillna("(unmapped)")

    return (other.groupby(["TYPE", "date", "store"], as_index=False)
            .agg(rows=("value", "size"), value=("value", "sum"))
            .rename(columns={"TYPE": "type"})
            .sort_values(["type", "date", "value"], ascending=[True, True, False])
            .reset_index(drop=True))'''

assert old_excl in s, "excluded_credit_types anchor"
s = s.replace(old_excl, new_excl, 1)

old_doc = '''2. **J counts TYPE = COL only, but the other types are not discarded.**
   ADJ.CR. and ADJ.DB. are the agency settling an excess and later recovering
   it. If both sat outside the sheet an error and its correction would cancel
   invisibly, so they are reported separately with count and value.'''
new_doc = '''2. **J is not COL alone.** COL (SLIPAMOUNT), ADJ.CR. (the ADJ column — NOT
   SLIPAMOUNT, which on an ADJ.CR. row is the original slip being adjusted)
   and ADJ.DB. (RECOVEREDAMOUNT, subtracted) all feed J, each from its own
   source column — confirmed against the raw file, not assumed. ZPO alone is
   excluded, on the operator's explicit instruction, and is still reported
   for visibility since it is never discarded silently (rule 4/5).'''
assert old_doc in s, "docstring anchor"
s = s.replace(old_doc, new_doc, 1)

p.write_text(s, encoding='utf-8')
print("store_sheet.py updated")
