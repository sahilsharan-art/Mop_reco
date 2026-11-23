"""The side-by-side check between an ERP-sourced frame and the file-sourced frame (2026-11-23). Pure functions: DataFrames in, results out.

Both frames are the NORMALISED ones (ingest._normalise_pos_frame / _normalise_ptc_frame), so a difference here is a difference in the
data, never in how it was read. This is the proof that decides when a source may move from the Excel file to the ERP
(docs/ERP_INTEGRATION_BRIEF.md section 8); nothing in a reconciliation run depends on it.

POS is compared at bill x payment-mode level, because the ERP keeps credit-note lines separate where the Excel export carried one
(found 2026-11-23: 199 + 199 against 398) - the amount per bill and mode still agrees, and credit notes are non-settling. What
matters most is the REFERENCES: for every bill and mode, the Paytm / Axis / Bajaj reference parsed from the ERP's terminal reply must
equal the one parsed from the Excel file's, because that reference is what R1 matches on.
"""

from __future__ import annotations

import pandas as pd

GATEWAY_REFS = ("paytm_ref", "axis_ref", "bajaj_ref")


def _refset(series: pd.Series) -> frozenset:
    return frozenset(str(v) for v in series.dropna())


def _aggregate_pos(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        cols = ["bill_no", "mop", "n", "v", "bill_time", "created_by", "parse_failed"] + list(GATEWAY_REFS)
        return pd.DataFrame(columns=cols)
    g = df.groupby(["bill_no", "mop"], sort=False)
    out = g.agg(n=("amount", "size"), v=("amount", "sum")).reset_index()
    out["v"] = out["v"].astype(float).round(2)
    out["bill_time"] = g["bill_time"].agg(lambda s: min((x for x in s if x), default=None)).values if "bill_time" in df.columns else None
    out["created_by"] = g["created_by"].agg(lambda s: next((x for x in s if isinstance(x, str) and x), None)).values \
        if "created_by" in df.columns else None
    out["parse_failed"] = g["tpedc_parse_failed"].agg(lambda s: int(s.astype(bool).sum())).values if "tpedc_parse_failed" in df.columns else 0
    for ref in GATEWAY_REFS:
        out[ref] = g[ref].agg(_refset).values if ref in df.columns else [frozenset()] * len(out)
    return out


def compare_pos(erp_df: pd.DataFrame, file_df: pd.DataFrame, max_examples: int = 15) -> dict:
    """Compare two normalised POS frames (typically one business date) at bill x payment-mode level."""
    e, f = _aggregate_pos(erp_df), _aggregate_pos(file_df)
    m = e.merge(f, on=["bill_no", "mop"], how="outer", suffixes=("_erp", "_file"), indicator=True)
    only_erp = m[m["_merge"] == "left_only"]
    only_file = m[m["_merge"] == "right_only"]
    both = m[m["_merge"] == "both"].copy()

    both["lines_differ"] = both["n_erp"] != both["n_file"]
    both["amount_differs"] = (both["v_erp"] - both["v_file"]).abs() > 0.004
    ref_cols = {}
    for ref in GATEWAY_REFS:
        ref_cols[ref] = both[f"{ref}_erp"] != both[f"{ref}_file"]
    both["ref_differs"] = ref_cols["paytm_ref"] | ref_cols["axis_ref"] | ref_cols["bajaj_ref"]
    both["time_differs"] = both["bill_time_erp"].fillna("") != both["bill_time_file"].fillna("")
    both["operator_differs"] = both["created_by_erp"].fillna("") != both["created_by_file"].fillna("")

    ref_examples = []
    for _, row in both[both["ref_differs"]].head(max_examples).iterrows():
        for ref in GATEWAY_REFS:
            if row[f"{ref}_erp"] != row[f"{ref}_file"]:
                short = lambda s: ",".join(sorted(x[:14] + ("..." if len(x) > 14 else "") for x in s)) or "(none)"  # noqa: E731
                ref_examples.append({"bill_no": row["bill_no"], "mop": row["mop"], "gateway": ref.replace("_ref", ""),
                                     "file": short(row[f"{ref}_file"]), "erp": short(row[f"{ref}_erp"])})

    def pick(frame, mask, cols):
        return frame.loc[mask, cols].head(max_examples).to_dict("records")

    return {
        "bills_modes_erp": int(len(e)), "bills_modes_file": int(len(f)), "lines_erp": int(e["n"].sum()), "lines_file": int(f["n"].sum()),
        "amount_erp": round(float(e["v"].sum()), 2), "amount_file": round(float(f["v"].sum()), 2),
        "only_in_erp": int(len(only_erp)), "only_in_file": int(len(only_file)),
        "only_in_erp_amount": round(float(only_erp["v_erp"].sum()), 2), "only_in_file_amount": round(float(only_file["v_file"].sum()), 2),
        "both": int(len(both)), "lines_differ": int(both["lines_differ"].sum()), "amount_differs": int(both["amount_differs"].sum()),
        "ref_differs": int(both["ref_differs"].sum()),
        "ref_differs_by_gateway": {k.replace("_ref", ""): int(v.sum()) for k, v in ref_cols.items()},
        "time_differs": int(both["time_differs"].sum()), "operator_differs": int(both["operator_differs"].sum()),
        "parse_failed_erp": int(e["parse_failed"].sum()), "parse_failed_file": int(f["parse_failed"].sum()),
        "examples_only_erp": pick(only_erp, slice(None), ["bill_no", "mop", "n_erp", "v_erp"]),
        "examples_only_file": pick(only_file, slice(None), ["bill_no", "mop", "n_file", "v_file"]),
        "examples_amount": pick(both, both["amount_differs"], ["bill_no", "mop", "n_erp", "v_erp", "n_file", "v_file"]),
        "examples_refs": ref_examples,
    }


def pos_agrees(result: dict) -> bool:
    """True only when nothing that matters differs: same bills and modes, same amounts, same references. (Extra credit-note LINES with
    the same amount are not a difference - `lines_differ` is reported but does not fail the check.)"""
    return not (result["only_in_erp"] or result["only_in_file"] or result["amount_differs"] or result["ref_differs"])


def compare_ptc(erp_df: pd.DataFrame, file_df: pd.DataFrame, max_examples: int = 15) -> dict:
    """Compare two normalised PTC frames: vouchers by store + voucher number, their mode, head, ledger and amount, and the deposit total."""
    def keyed(df):
        if df is None or df.empty:
            return pd.DataFrame(columns=["key", "mode", "head", "ledger", "amount"])
        k = df.assign(key=df["store"].astype(str).str.strip() + "|" + df["voucher_no"].astype(str).str.strip())
        return k.groupby("key", as_index=False).agg(mode=("mode", "first"), head=("head", "first"), ledger=("ledger", "first"),
                                                     amount=("amount", "sum"), n=("amount", "size"))
    e, f = keyed(erp_df), keyed(file_df)
    m = e.merge(f, on="key", how="outer", suffixes=("_erp", "_file"), indicator=True)
    only_erp, only_file = m[m["_merge"] == "left_only"], m[m["_merge"] == "right_only"]
    both = m[m["_merge"] == "both"].copy()
    both["amount_differs"] = (both["amount_erp"] - both["amount_file"]).abs() > 0.004
    both["class_differs"] = (both["mode_erp"] != both["mode_file"]) | (both["head_erp"] != both["head_file"]) | (both["ledger_erp"] != both["ledger_file"])

    def deposits(df):
        return 0.0 if df is None or df.empty else round(float(df.loc[df["is_deposit"], "deposit_amount"].sum()), 2) if "is_deposit" in df.columns else 0.0

    return {
        "vouchers_erp": int(len(e)), "vouchers_file": int(len(f)), "amount_erp": round(float(e["amount"].sum()), 2),
        "amount_file": round(float(f["amount"].sum()), 2), "deposit_total_erp": deposits(erp_df), "deposit_total_file": deposits(file_df),
        "only_in_erp": int(len(only_erp)), "only_in_file": int(len(only_file)), "both": int(len(both)),
        "amount_differs": int(both["amount_differs"].sum()), "class_differs": int(both["class_differs"].sum()),
        "examples_only_erp": only_erp[["key", "mode_erp", "head_erp", "amount_erp"]].head(max_examples).to_dict("records"),
        "examples_only_file": only_file[["key", "mode_file", "head_file", "amount_file"]].head(max_examples).to_dict("records"),
        "examples_amount": both.loc[both["amount_differs"], ["key", "amount_erp", "amount_file"]].head(max_examples).to_dict("records"),
    }


def ptc_agrees(result: dict) -> bool:
    return not (result["only_in_erp"] or result["only_in_file"] or result["amount_differs"] or result["class_differs"])
